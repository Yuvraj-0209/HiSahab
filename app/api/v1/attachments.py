"""Reading an attachment back: a short-lived signed URL (CLAUDE.md §7.3).

The bucket is private and never made public (§7.3). This is the only way a client ever sees
a receipt image -- nothing anywhere returns a raw storage URL or the object's bytes
directly.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import (
    Actor,
    get_current_user,
    get_storage,
    resolve_outlet_from_attachment,
)
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.roles import Role
from app.db.session import get_db
from app.models.attachment import Attachment
from app.models.user import OutletMembership, UserProfile
from app.services import attachments as attachment_service
from app.services.storage import StorageBackend

logger = logging.getLogger(__name__)

router = APIRouter(tags=["attachments"])


class SignedUrlResponse(BaseModel):
    url: str
    expires_in_seconds: int


def _attachment_access(
    outlet_id: UUID = Depends(resolve_outlet_from_attachment),
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Actor:
    """§7.3's role floor, deliberately **not** built from the generic `require_role`.

    Everywhere else in this codebase (shifts, nozzles, ...), a caller with no membership at
    a row's outlet gets 403 `NOT_A_MEMBER` -- which is fine there, because a shift id is not
    sensitive on its own. §7.3 sets a stricter rule for attachments specifically: *"An id
    belonging to another outlet returns 404, not 403 -- existence is not leaked across
    tenants."* A receipt is a photograph that can carry real financial and personal
    information, and a 403 already confirms "this id is real, just not yours" -- exactly
    the leak the spec forbids. So a caller with no membership at the attachment's outlet
    gets the same 404 as an attachment that does not exist at all.
    """
    membership = db.execute(
        select(OutletMembership).where(
            OutletMembership.user_id == user.id,
            OutletMembership.outlet_id == outlet_id,
        )
    ).scalar_one_or_none()
    if membership is None or not membership.is_active:
        raise AppError(
            status_code=404,
            code="ATTACHMENT_NOT_FOUND",
            detail="No attachment with that id.",
        )

    # No further role-floor check: `attendant` is already `Role`'s minimum (app/core/
    # roles.py), so every valid membership role clears it and `INSUFFICIENT_ROLE` could
    # never fire here. Writing that branch anyway would be dead code asserting a floor
    # that is unconditionally true.
    return Actor(user=user, outlet_id=outlet_id, role=Role(membership.role))


@router.get("/attachments/{attachment_id}/url", response_model=SignedUrlResponse)
def get_attachment_url(
    attachment_id: UUID,
    actor: Actor = Depends(_attachment_access),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    storage: StorageBackend = Depends(get_storage),
) -> SignedUrlResponse:
    """§7.3's permission rule, in two layers matching §8's two axes.

    `_attachment_access` has already resolved the outlet from the attachment itself and
    enforced the role floor before this line runs -- an unknown id, and an id from another
    outlet, both 404 inside that dependency, never reaching here.
    `attachment_service.may_read` decides only what remains: the ownership axis for an
    attendant specifically. A manager or admin who cleared the role floor is always
    allowed, any attachment at their outlet.
    """
    # Not None: _attachment_access already 404'd inside resolve_outlet_from_attachment.
    attachment = db.get(Attachment, attachment_id)

    if not attachment_service.may_read(
        db, user_id=actor.user.id, role=actor.role, attachment=attachment
    ):
        raise AppError(
            status_code=403,
            code="NOT_YOUR_ATTACHMENT",
            detail="You may not read this attachment.",
        )

    url = storage.signed_url(
        bucket=attachment.bucket,
        path=attachment.storage_path,
        ttl_seconds=settings.SIGNED_URL_TTL_SECONDS,
    )

    logger.info(
        "attachment signed url issued",
        extra={"attachment_id": str(attachment_id), "user_id": str(actor.user.id)},
    )
    return SignedUrlResponse(url=url, expires_in_seconds=settings.SIGNED_URL_TTL_SECONDS)
