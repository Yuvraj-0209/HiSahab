"""Receipt uploads (CLAUDE.md §7.1, §7.2).

**§13.4's known approximation:** the file passes through this API server on its way to
Supabase Storage, rather than the client uploading directly with a presigned URL. Simpler,
synchronous, and easier to reason about for a developer learning backend basics -- the
right trade-off for V1's traffic (one attendant, one photo, a handful of times a day), the
wrong one at real scale. Documented here rather than left as an unstated oversight.

## Why this route has its own shift-access dependency instead of reusing `require_shift_access`

§7.2 (D1 in the Phase 8 plan) puts `shift_id` in the multipart **form body**, not the URL
path -- there is no `business_date` to build a storage path from until an expense or credit
sale exists, and neither exists yet at upload time (§7.2's own reasoning). Every other
shift-scoped route in this codebase reads `shift_id` from the path, and
`require_shift_access` is built around that: its inner `resolve_outlet_from_shift`
dependency resolves `shift_id` the way FastAPI resolves any bare, unmarked parameter --
against the URL, not the request body.

FastAPI has no way to tell that dependency "read this one from a form field instead," and
writing a second version that does would fork half of `app/api/deps.py`'s auth logic behind
a flag no other route needs. `_shift_access_from_upload_form` below spells the same three
checks out directly -- role floor, ownership, and the writable-shift guard -- for this one
route. Verbose, not clever, and easy to audit against `require_shift_access` by eye; §14
prefers exactly that trade.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Actor, ShiftAccess, get_current_user, get_storage
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.roles import Role
from app.core.shifts import ShiftStatus
from app.core.uploads import validate
from app.db.session import get_db
from app.models.shift import Shift
from app.models.user import OutletMembership, UserProfile
from app.services import attachments as attachment_service
from app.services.storage import StorageBackend

logger = logging.getLogger(__name__)

router = APIRouter(tags=["attachments"])


class UploadResponse(BaseModel):
    attachment_id: UUID


async def _shift_access_from_upload_form(
    shift_id: UUID = Form(...),
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ShiftAccess:
    """`require_shift_access(Role.attendant, writable=True)`, spelled out for a `shift_id`
    that arrives in form data. See the module docstring for why this cannot simply call
    that dependency instead. Kept behaviourally identical on purpose -- role floor,
    ownership restricted to attendants, `SHIFT_LOCKED` / `SHIFT_NOT_OPEN` on a non-open
    shift -- so an upload obeys exactly the same rules a collection or expense does.
    """
    shift = db.get(Shift, shift_id)
    if shift is None:
        raise AppError(
            status_code=404, code="SHIFT_NOT_FOUND", detail="No shift with that id."
        )

    membership = db.execute(
        select(OutletMembership).where(
            OutletMembership.user_id == user.id,
            OutletMembership.outlet_id == shift.outlet_id,
        )
    ).scalar_one_or_none()
    if membership is None:
        raise AppError(
            status_code=403,
            code="NOT_A_MEMBER",
            detail="You do not have access to this outlet.",
        )
    if not membership.is_active:
        raise AppError(
            status_code=403,
            code="MEMBERSHIP_INACTIVE",
            detail="Your access to this outlet has been revoked.",
        )

    # No role-floor check beyond membership: `attendant` is already `Role`'s minimum
    # (app/core/roles.py), so every valid membership role clears it -- `INSUFFICIENT_ROLE`
    # could never fire here, and CLAUDE.md §14 says not to write dead code.
    role = Role(membership.role)
    if role is Role.attendant and shift.attendant_id != user.id:
        raise AppError(
            status_code=403,
            code="NOT_YOUR_SHIFT",
            detail="This shift belongs to another attendant.",
        )

    status = ShiftStatus(shift.status)
    if status is ShiftStatus.locked:
        raise AppError(
            status_code=409,
            code="SHIFT_LOCKED",
            detail=(
                "This shift has been locked and can no longer be changed. Corrections "
                "must be recorded as reversal entries."
            ),
        )
    if status is ShiftStatus.closed:
        raise AppError(
            status_code=409,
            code="SHIFT_NOT_OPEN",
            detail="This shift is closed. An admin must reopen it before it can be changed.",
        )

    return ShiftAccess(
        actor=Actor(user=user, outlet_id=shift.outlet_id, role=role), shift=shift
    )


async def _read_bounded(file: UploadFile, *, max_bytes: int) -> bytes:
    """Reads at most `max_bytes + 1` bytes, in chunks, regardless of what the client's
    `Content-Length` header claims (M5 in the Phase 8 plan) -- a client can lie about it, so
    the read itself is what is bounded, not a header. The "+1" lets `core.uploads.validate`
    report a real observed size for `FILE_TOO_LARGE` rather than silently truncating an
    oversized file and validating the truncated (and now corrupt) bytes as if they were the
    whole thing.

    §13.4's proxy pattern already commits this project to holding the whole file in memory
    once per request; a real deployment behind a reverse proxy should still cap the request
    body size there too, which is outside what this application layer can enforce.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            break
    return b"".join(chunks)


@router.post("/uploads/receipt", response_model=UploadResponse, status_code=201)
async def upload_receipt(
    file: UploadFile = File(...),
    access: ShiftAccess = Depends(_shift_access_from_upload_form),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    storage: StorageBackend = Depends(get_storage),
) -> UploadResponse:
    """§7.2's sequence, steps 1-5. No `Idempotency-Key` -- see §6.10's closing note: an
    attachment is not a money record, and a retried upload creates a second row the client
    simply does not use, reclaimed by `app/jobs/cleanup_attachments.py` within 24 hours.
    """
    shift = access.shift

    data = await _read_bounded(file, max_bytes=settings.MAX_UPLOAD_BYTES)
    # Validates BEFORE touching storage (§7.2 step 2) -- `validate` raises AppError for
    # every refusal case and nothing below this line runs if it does.
    validated = validate(data, max_bytes=settings.MAX_UPLOAD_BYTES)

    row = attachment_service.create(
        db,
        storage=storage,
        bucket=settings.SUPABASE_STORAGE_BUCKET,
        outlet_id=shift.outlet_id,
        # The shift's own business_date, not today -- §7.2's whole reason for taking
        # shift_id instead of a client-supplied date. See the module this shift came
        # through: business_date is immutable once the shift is opened (§6.1).
        business_date=shift.business_date,
        uploaded_by=access.actor.user.id,
        # Display label only (§7.2) -- never appears in storage_path, which
        # attachment_service.create builds independently of the client-supplied name.
        original_filename=file.filename or "upload",
        validated=validated,
        data=data,
    )

    logger.info(
        "receipt uploaded",
        extra={
            "attachment_id": str(row.id),
            "shift_id": str(shift.id),
            "size_bytes": validated.size_bytes,
            "mime_type": validated.mime_type,
        },
    )
    return UploadResponse(attachment_id=row.id)
