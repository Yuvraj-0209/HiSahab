"""Shift templates -- the shifts an outlet usually runs (CLAUDE.md §5.1, §4.7).

Admin-only writes ("Manage users, nozzles, customers" in §8, of which this is the same
kind of thing), attendant-floor reads because the client pre-fills a new shift's times
from them.

**Why this table exists at all.** §4.7: days are typed in after the fact, so without
defaults somebody retypes 06:00 and 22:00 every single morning -- and that is the field
that decides which day's fuel rate a whole shift is valued at (§6.3). A rushed retype is
exactly where a wrong hour would come from.

**Why it is read only at creation.** The template supplies a default that is then
materialised onto the shift row. It is never consulted again. Editing a template must not
revalue a shift that already happened, and the only way to guarantee that is for the shift
to own its own times.
"""

from __future__ import annotations

import logging
from datetime import time
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Actor, get_default_outlet_id, require_role
from app.core.errors import AppError
from app.core.roles import Role
from app.db.session import get_db
from app.models.shift import OutletShiftTemplate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["shifts"])

# Same bounded exception to §9's cursor rule as /nozzles and /fuel-types: an outlet has a
# handful of shift patterns, not a growing history.
_MAX_ROWS = 100


def resolve_outlet_from_template(
    template_id: UUID, db: Session = Depends(get_db)
) -> UUID:
    """The outlet that owns this template, for `require_role` to authorise against (§8)."""
    outlet_id = db.execute(
        select(OutletShiftTemplate.outlet_id).where(
            OutletShiftTemplate.id == template_id
        )
    ).scalar_one_or_none()
    if outlet_id is None:
        raise AppError(
            status_code=404,
            code="SHIFT_TEMPLATE_NOT_FOUND",
            detail="No shift template with that id.",
        )
    return outlet_id


class ShiftTemplateResponse(BaseModel):
    id: UUID
    outlet_id: UUID
    sequence: int
    label: str
    starts_at_local: time
    ends_at_local: time
    # True when the template crosses midnight, e.g. a 22:00 -> 06:00 night shift. Echoed so
    # a client rendering "Shift 3: 22:00-06:00" can show the +1 day without inferring it.
    crosses_midnight: bool
    is_active: bool


class ShiftTemplateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1, le=24)
    label: str = Field(min_length=1, max_length=100)
    # Local wall-clock times, not instants -- see §5.1. The outlet's timezone comes from
    # TZ_DISPLAY (§13.11), so these are never accompanied by an offset.
    starts_at_local: time
    ends_at_local: time


class ShiftTemplateUpdate(BaseModel):
    """What may change after creation.

    `sequence` is absent on purpose, and `extra="forbid"` makes sending it a 422 rather
    than a silent no-op. The sequence is the template's identity -- shifts are matched to a
    template by it -- so renumbering one would quietly repoint future shifts at a different
    pattern. Retire the row and add a new one instead.
    """

    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=100)
    starts_at_local: time | None = None
    ends_at_local: time | None = None
    is_active: bool | None = None


def _to_response(template: OutletShiftTemplate) -> ShiftTemplateResponse:
    return ShiftTemplateResponse(
        id=template.id,
        outlet_id=template.outlet_id,
        sequence=template.sequence,
        label=template.label,
        starts_at_local=template.starts_at_local,
        ends_at_local=template.ends_at_local,
        crosses_midnight=template.ends_at_local <= template.starts_at_local,
        is_active=template.is_active,
    )


@router.get("/shift-templates", response_model=list[ShiftTemplateResponse])
def list_shift_templates(
    include_inactive: bool = Query(default=False),
    actor: Actor = Depends(require_role(Role.attendant)),
    db: Session = Depends(get_db),
) -> list[ShiftTemplateResponse]:
    """Templates at the caller's outlet. Attendant floor -- they open the shifts."""
    statement = (
        select(OutletShiftTemplate)
        .where(OutletShiftTemplate.outlet_id == actor.outlet_id)
        .order_by(OutletShiftTemplate.sequence)
    )
    if not include_inactive:
        statement = statement.where(OutletShiftTemplate.is_active.is_(True))

    return [
        _to_response(template)
        for template in db.execute(statement.limit(_MAX_ROWS)).scalars().all()
    ]


@router.post("/shift-templates", response_model=ShiftTemplateResponse, status_code=201)
def create_shift_template(
    payload: ShiftTemplateCreate,
    actor: Actor = Depends(require_role(Role.admin, get_default_outlet_id)),
    db: Session = Depends(get_db),
) -> ShiftTemplateResponse:
    """Add a shift pattern. Admin only (§8).

    A 24-hour outlet posts three of these; this outlet was seeded with one by migration
    0004. Note that nothing forces the templates to tile the day or to be contiguous -- a
    station that shuts overnight has a deliberate gap, and refusing that would refuse the
    outlet this software was written for.
    """
    if payload.starts_at_local == payload.ends_at_local:
        raise AppError(
            status_code=422,
            code="SHIFT_TEMPLATE_ZERO_LENGTH",
            detail="A shift template must start and end at different times.",
        )

    clash = db.execute(
        select(OutletShiftTemplate).where(
            OutletShiftTemplate.outlet_id == actor.outlet_id,
            OutletShiftTemplate.sequence == payload.sequence,
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise AppError(
            status_code=409,
            code="SHIFT_TEMPLATE_SEQUENCE_EXISTS",
            detail=f"A template for shift {payload.sequence} already exists.",
        )

    template = OutletShiftTemplate(
        outlet_id=actor.outlet_id,
        sequence=payload.sequence,
        label=payload.label.strip(),
        starts_at_local=payload.starts_at_local,
        ends_at_local=payload.ends_at_local,
        created_by=actor.user.id,
    )
    db.add(template)
    db.commit()
    db.refresh(template)

    logger.info(
        "shift template created",
        extra={"template_id": str(template.id), "sequence": template.sequence},
    )
    return _to_response(template)


@router.patch(
    "/shift-templates/{template_id}", response_model=ShiftTemplateResponse
)
def update_shift_template(
    template_id: UUID,
    payload: ShiftTemplateUpdate,
    actor: Actor = Depends(require_role(Role.admin, resolve_outlet_from_template)),
    db: Session = Depends(get_db),
) -> ShiftTemplateResponse:
    """Adjust or retire a shift pattern. Admin only (§8).

    No DELETE (§3 rule 6). Changing the times here affects only shifts opened *after* the
    change: existing shifts carry their own `started_at` / `ended_at`, so nothing that
    already traded is revalued.
    """
    template = db.get(OutletShiftTemplate, template_id)

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise AppError(
            status_code=422,
            code="NO_FIELDS_TO_UPDATE",
            detail="Provide at least one field to change.",
        )

    for field, value in changes.items():
        if value is None:
            continue
        setattr(template, field, value.strip() if isinstance(value, str) else value)

    if template.starts_at_local == template.ends_at_local:
        raise AppError(
            status_code=422,
            code="SHIFT_TEMPLATE_ZERO_LENGTH",
            detail="A shift template must start and end at different times.",
        )

    db.commit()
    db.refresh(template)

    logger.info(
        "shift template updated",
        extra={"template_id": str(template.id), "fields": sorted(changes)},
    )
    return _to_response(template)
