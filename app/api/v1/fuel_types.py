"""Fuel types -- what this outlet sells (CLAUDE.md §5.1, §4.5).

Global reference data: a litre is a litre at every outlet, so there is no `outlet_id` here
(§5.0's landing schedule). Reads sit at the attendant floor; writes are admin-only, the new
"Manage fuel types" row in §8's permission table.

**Why this is writable at all.** Migration 0003 seeds the four products sold today, and it
would have been simpler to stop there. But outlets sell things this codebase cannot
anticipate -- XP-95, Extra Green, whatever an OMC launches next -- and adding a product you
already sell should be data entry, not a schema change requiring a developer and a deploy.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, condecimal
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Actor, require_role
from app.core.errors import AppError
from app.core.roles import Role
from app.core.units import UnitOfMeasure
from app.db.session import get_db
from app.models.fuel import FuelType

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reference data"])

# §9 mandates cursor pagination on list endpoints. This one deliberately returns a full
# list instead: fuel types are bounded reference data -- a handful of rows that a station
# adds to once a year -- and a cursor would be ceremony with no reader. The cap below makes
# the bound structural rather than assumed, so this stays a considered exception rather
# than an oversight that grows into a problem.
_MAX_ROWS = 500


class FuelTypeResponse(BaseModel):
    """Note that `unit_of_measure` is always returned.

    §4.5: a client must never assume litres. CBG is sold by the kilogram through the same
    endpoints, so anything rendering a quantity needs the unit from the server that knows
    it -- exactly like §8's argument that hiding a button is UX, not a control.
    """

    id: UUID
    code: str
    display_name: str
    unit_of_measure: UnitOfMeasure
    max_flow_rate_per_minute: Decimal
    is_active: bool


class FuelTypeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Uppercased and stripped on the way in so PETROL and " petrol " cannot become two
    # rows that mean the same thing. The unique constraint is on the stored value.
    code: str = Field(min_length=1, max_length=50)
    display_name: str = Field(min_length=1, max_length=200)
    unit_of_measure: UnitOfMeasure
    # §6.2's sanity ceiling for this fuel. condecimal, not float -- §3 rules 1 and 2.
    max_flow_rate_per_minute: condecimal(max_digits=10, decimal_places=3, gt=0)


class FuelTypeUpdate(BaseModel):
    """What may change after creation -- and, by omission, what may not.

    `code` and `unit_of_measure` are absent on purpose, and `extra="forbid"` turns an
    attempt to send either into a 422 rather than a silent no-op. The silent version is the
    dangerous one: an admin who "changed" a fuel from litres to kilograms and got a 200
    back would reasonably believe it worked.

    Changing a unit would reinterpret every quantity ever recorded against this fuel --
    litres read as kilograms, every historical sale value wrong, no error anywhere. A fuel
    that is genuinely different is a new row.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    max_flow_rate_per_minute: (
        condecimal(max_digits=10, decimal_places=3, gt=0) | None
    ) = None
    is_active: bool | None = None


@router.get("/fuel-types", response_model=list[FuelTypeResponse])
def list_fuel_types(
    include_inactive: bool = Query(default=False),
    actor: Actor = Depends(require_role(Role.attendant)),
    db: Session = Depends(get_db),
) -> list[FuelTypeResponse]:
    """Every fuel this outlet can sell. Attendant floor -- everyone needs the unit."""
    statement = select(FuelType).order_by(FuelType.code)
    if not include_inactive:
        statement = statement.where(FuelType.is_active.is_(True))

    rows = db.execute(statement.limit(_MAX_ROWS)).scalars().all()
    return [
        FuelTypeResponse(
            id=row.id,
            code=row.code,
            display_name=row.display_name,
            unit_of_measure=UnitOfMeasure(row.unit_of_measure),
            max_flow_rate_per_minute=row.max_flow_rate_per_minute,
            is_active=row.is_active,
        )
        for row in rows
    ]


@router.post("/fuel-types", response_model=FuelTypeResponse, status_code=201)
def create_fuel_type(
    payload: FuelTypeCreate,
    actor: Actor = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
) -> FuelTypeResponse:
    """Add a product the outlet has started selling. Admin only (§8)."""
    code = payload.code.strip().upper()

    existing = db.execute(
        select(FuelType).where(FuelType.code == code)
    ).scalar_one_or_none()
    if existing is not None:
        # 409 rather than 422: the payload is well-formed, it conflicts with the world.
        raise AppError(
            status_code=409,
            code="FUEL_TYPE_CODE_EXISTS",
            detail=f"A fuel type with code {code} already exists.",
        )

    row = FuelType(
        code=code,
        display_name=payload.display_name.strip(),
        unit_of_measure=payload.unit_of_measure.value,
        max_flow_rate_per_minute=payload.max_flow_rate_per_minute,
        created_by=actor.user.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    logger.info(
        "fuel type created",
        extra={
            "fuel_type_id": str(row.id),
            "code": row.code,
            "unit_of_measure": row.unit_of_measure,
        },
    )
    return FuelTypeResponse(
        id=row.id,
        code=row.code,
        display_name=row.display_name,
        unit_of_measure=UnitOfMeasure(row.unit_of_measure),
        max_flow_rate_per_minute=row.max_flow_rate_per_minute,
        is_active=row.is_active,
    )


@router.patch("/fuel-types/{fuel_type_id}", response_model=FuelTypeResponse)
def update_fuel_type(
    fuel_type_id: UUID,
    payload: FuelTypeUpdate,
    actor: Actor = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
) -> FuelTypeResponse:
    """Edit the mutable parts of a fuel type. Admin only (§8).

    There is no DELETE, here or anywhere (§3 rule 6). Set `is_active` false instead: the
    fuel stops appearing in the default list while every historical price, nozzle and sale
    that references it stays intact and readable.
    """
    row = db.get(FuelType, fuel_type_id)
    if row is None:
        raise AppError(
            status_code=404,
            code="FUEL_TYPE_NOT_FOUND",
            detail="No fuel type with that id.",
        )

    # exclude_unset so that "not mentioned" and "explicitly set to null" stay distinct --
    # a PATCH that omits is_active must not clear it.
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
        setattr(row, field, value.strip() if isinstance(value, str) else value)

    db.commit()
    db.refresh(row)

    logger.info(
        "fuel type updated",
        extra={"fuel_type_id": str(row.id), "fields": sorted(changes)},
    )
    return FuelTypeResponse(
        id=row.id,
        code=row.code,
        display_name=row.display_name,
        unit_of_measure=UnitOfMeasure(row.unit_of_measure),
        max_flow_rate_per_minute=row.max_flow_rate_per_minute,
        is_active=row.is_active,
    )
