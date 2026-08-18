"""Nozzles -- registering the meters that sales are computed from (CLAUDE.md §4.3, §5.1).

Admin-only writes ("Manage users, nozzles, customers" in §8), attendant-floor reads.

This module is also where the **row-scoped outlet resolver** first appears. app/api/deps.py
predicted it would arrive with `shifts` in Phase 4; PATCH needs it a phase earlier, because
the outlet a nozzle belongs to has to come from the nozzle, not from config. In V1 the two
always agree -- there is one outlet -- but wiring the check to ask the row means that the
day a second outlet exists this fails loudly instead of authorising against the wrong one.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, condecimal
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Actor, get_default_outlet_id, require_role
from app.core.errors import AppError
from app.core.roles import Role
from app.core.units import UnitOfMeasure
from app.db.session import get_db
from app.models.fuel import FuelType
from app.models.nozzle import Nozzle

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reference data"])

# See the note in fuel_types.py: a deliberate, bounded exception to §9's cursor-pagination
# rule. A station has a handful of nozzles, not a growing history.
_MAX_ROWS = 500


def resolve_outlet_from_nozzle(
    nozzle_id: UUID, db: Session = Depends(get_db)
) -> UUID:
    """The outlet that owns this nozzle, for `require_role` to authorise against.

    §8: the question is always "does this user hold role R **at the outlet that owns this
    row**". Resolving the outlet from the row rather than from config is the whole point --
    it is what stops a V2 caller from being authorised against outlet A while editing a
    nozzle belonging to outlet B.

    404 lives here rather than in the handler because this dependency runs first; without
    it, editing a nonexistent nozzle would report a permission failure instead of a
    missing row.
    """
    outlet_id = db.execute(
        select(Nozzle.outlet_id).where(Nozzle.id == nozzle_id)
    ).scalar_one_or_none()
    if outlet_id is None:
        raise AppError(
            status_code=404,
            code="NOZZLE_NOT_FOUND",
            detail="No nozzle with that id.",
        )
    return outlet_id


class NozzleResponse(BaseModel):
    id: UUID
    outlet_id: UUID
    label: str
    dispenser_label: str
    fuel_type_id: UUID
    fuel_type_code: str
    # Echoed from the fuel type so a client rendering readings never has to guess whether
    # a totalizer counts litres or kilograms (§4.5).
    unit_of_measure: UnitOfMeasure
    totalizer_max_value: Decimal
    meter_installed_at: datetime
    is_active: bool


class NozzleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=100)
    dispenser_label: str = Field(min_length=1, max_length=100)
    fuel_type_id: UUID
    # §4.3: the rollover ceiling for this specific meter, e.g. 999999.99. Required, because
    # §6.2's rollover branch cannot be computed without it -- and a nozzle that cannot be
    # read correctly is worse than one that does not exist yet.
    totalizer_max_value: condecimal(max_digits=12, decimal_places=2, gt=0)
    meter_installed_at: datetime


class NozzleUpdate(BaseModel):
    """What may change after creation.

    `fuel_type_id` and `totalizer_max_value` are absent on purpose, and `extra="forbid"`
    makes sending either a 422 rather than a silent no-op.

    Both feed §6.2 and §6.3, which recompute historical sales on read. Repointing a nozzle
    at a different fuel would revalue every past shift at the wrong rate; changing the
    ceiling would change what a past rollover meant. **A rewired or re-metered nozzle is a
    new row**: deactivate this one, create its replacement, and history stays true.
    """

    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=100)
    dispenser_label: str | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool | None = None


def _to_response(nozzle: Nozzle, fuel_type: FuelType) -> NozzleResponse:
    return NozzleResponse(
        id=nozzle.id,
        outlet_id=nozzle.outlet_id,
        label=nozzle.label,
        dispenser_label=nozzle.dispenser_label,
        fuel_type_id=nozzle.fuel_type_id,
        fuel_type_code=fuel_type.code,
        unit_of_measure=UnitOfMeasure(fuel_type.unit_of_measure),
        totalizer_max_value=nozzle.totalizer_max_value,
        meter_installed_at=nozzle.meter_installed_at,
        is_active=nozzle.is_active,
    )


@router.get("/nozzles", response_model=list[NozzleResponse])
def list_nozzles(
    include_inactive: bool = Query(default=False),
    actor: Actor = Depends(require_role(Role.attendant)),
    db: Session = Depends(get_db),
) -> list[NozzleResponse]:
    """Nozzles at the caller's outlet. Attendant floor -- they enter the readings."""
    statement = (
        select(Nozzle, FuelType)
        .join(FuelType, FuelType.id == Nozzle.fuel_type_id)
        .where(Nozzle.outlet_id == actor.outlet_id)
        .order_by(Nozzle.dispenser_label, Nozzle.label)
    )
    if not include_inactive:
        statement = statement.where(Nozzle.is_active.is_(True))

    return [
        _to_response(nozzle, fuel_type)
        for nozzle, fuel_type in db.execute(statement.limit(_MAX_ROWS)).all()
    ]


@router.post("/nozzles", response_model=NozzleResponse, status_code=201)
def create_nozzle(
    payload: NozzleCreate,
    actor: Actor = Depends(require_role(Role.admin, get_default_outlet_id)),
    db: Session = Depends(get_db),
) -> NozzleResponse:
    """Register a meter. Admin only (§8).

    Nothing is seeded by migration 0003: real labels and rollover ceilings are read off the
    physical dispensers, and a plausible guess sitting in this table would look exactly
    like data an attendant could enter readings against.
    """
    fuel_type = db.get(FuelType, payload.fuel_type_id)
    if fuel_type is None or not fuel_type.is_active:
        raise AppError(
            status_code=409,
            code="FUEL_TYPE_NOT_FOUND",
            detail="That fuel type does not exist or is no longer active.",
        )

    label = payload.label.strip()
    existing = db.execute(
        select(Nozzle).where(
            Nozzle.outlet_id == actor.outlet_id, Nozzle.label == label
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise AppError(
            status_code=409,
            code="NOZZLE_LABEL_EXISTS",
            detail=f"A nozzle labelled {label} already exists at this outlet.",
        )

    nozzle = Nozzle(
        outlet_id=actor.outlet_id,
        label=label,
        dispenser_label=payload.dispenser_label.strip(),
        fuel_type_id=payload.fuel_type_id,
        totalizer_max_value=payload.totalizer_max_value,
        meter_installed_at=payload.meter_installed_at,
        created_by=actor.user.id,
    )
    db.add(nozzle)
    db.commit()
    db.refresh(nozzle)

    logger.info(
        "nozzle created",
        extra={
            "nozzle_id": str(nozzle.id),
            "label": nozzle.label,
            "fuel_type_code": fuel_type.code,
        },
    )
    return _to_response(nozzle, fuel_type)


@router.patch("/nozzles/{nozzle_id}", response_model=NozzleResponse)
def update_nozzle(
    nozzle_id: UUID,
    payload: NozzleUpdate,
    actor: Actor = Depends(require_role(Role.admin, resolve_outlet_from_nozzle)),
    db: Session = Depends(get_db),
) -> NozzleResponse:
    """Relabel or deactivate a nozzle. Admin only (§8).

    No DELETE (§3 rule 6) -- a removed nozzle would orphan every reading taken through it.
    """
    nozzle = db.get(Nozzle, nozzle_id)
    if nozzle is None:  # pragma: no cover - the resolver above already 404s
        raise AppError(
            status_code=404, code="NOZZLE_NOT_FOUND", detail="No nozzle with that id."
        )

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
        setattr(nozzle, field, value.strip() if isinstance(value, str) else value)

    db.commit()
    db.refresh(nozzle)

    logger.info(
        "nozzle updated",
        extra={"nozzle_id": str(nozzle.id), "fields": sorted(changes)},
    )
    fuel_type = db.get(FuelType, nozzle.fuel_type_id)
    return _to_response(nozzle, fuel_type)
