"""Dealer margins -- append-only, effective-dated (CLAUDE.md §4.6, §5.1).

Deliberately the mirror image of fuel_prices.py, because the two answer the same shape of
question about different numbers. Kept as its own module and its own table rather than
folded into prices: price revises often, margin almost never, and sharing a row would force
re-entry of an unchanged margin on every price change -- where the first forgotten entry
silently nulls that period's profit.

**Why storing margin directly is correct**, rather than deriving it from purchase invoices:
§4.6 -- a retail revision passes straight through to the dealer. When the pump rate rises
₹1/litre the next tanker invoice rises ₹1/litre too, so the gap does not move. For CBG the
same holds by construction: IOCL deducts `(retail − 2.28) × kg` from a ledger balance. That
single fact is why V1 can report fuel profit while holding no purchase, tanker or stock data
at all:

    dealer_profit = quantity_sold × margin_at(fuel_type, at)

What this figure excludes is stock revaluation -- see §13.7, and label it wherever shown.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, condecimal, field_validator
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.api.cursor import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from app.api.deps import Actor, require_role
from app.core.audit import AuditAction
from app.core.errors import AppError
from app.core.roles import Role
from app.db.session import get_db
from app.services import audit
from app.models.fuel import FuelMargin, FuelType
from app.services.pricing import margin_at

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reference data"])


class FuelMarginCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fuel_type_id: UUID
    # ₹ per litre or per kilogram (§4.5). For CBG this is the ₹2.28 IOCL leaves behind.
    margin_per_unit: condecimal(max_digits=12, decimal_places=2, gt=0)
    effective_from: datetime

    @field_validator("effective_from")
    @classmethod
    def _must_be_timezone_aware(cls, value: datetime) -> datetime:
        """Reject naive datetimes rather than guessing a zone -- see fuel_prices.py."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "effective_from must include a timezone offset, e.g. "
                "2026-08-18T06:00:00+05:30"
            )
        return value


class FuelMarginResponse(BaseModel):
    id: UUID
    outlet_id: UUID
    fuel_type_id: UUID
    margin_per_unit: Decimal
    effective_from: datetime
    entered_by: UUID
    is_backdated: bool


class FuelMarginPage(BaseModel):
    items: list[FuelMarginResponse]
    next_cursor: str | None


class CurrentMarginResponse(BaseModel):
    fuel_type_id: UUID
    fuel_type_code: str
    margin_per_unit: Decimal
    at: datetime


def _audit_snapshot(row: FuelMargin) -> dict[str, object]:
    """What §5.3's trail keeps about an appended margin.

    This table is append-only -- no UPDATE, no DELETE, enforced by a trigger -- so there is
    never an `old_values` side to record and every row here is an `insert`. That makes the
    trail look redundant next to `entered_by`, and it is not, for one reason: **backdating**.

    A row whose `effective_from` is in the past silently revalues shifts that are already
    closed (§6.3 values a shift at the rate effective at its `started_at`, recomputed on
    read). The endpoint permits it deliberately -- refusing would leave a stale rate with no
    legal correction -- so the trail is what makes it visible afterwards rather than merely
    logged and forgotten.
    """
    return {
        "fuel_type_id": row.fuel_type_id,
        "margin_per_unit": row.margin_per_unit,
        "effective_from": row.effective_from,
        "entered_by": row.entered_by,
    }


def _to_response(row: FuelMargin, *, is_backdated: bool = False) -> FuelMarginResponse:
    return FuelMarginResponse(
        id=row.id,
        outlet_id=row.outlet_id,
        fuel_type_id=row.fuel_type_id,
        margin_per_unit=row.margin_per_unit,
        effective_from=row.effective_from,
        entered_by=row.entered_by,
        is_backdated=is_backdated,
    )


@router.post("/fuel-margins", response_model=FuelMarginResponse, status_code=201)
def create_fuel_margin(
    payload: FuelMarginCreate,
    actor: Actor = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
) -> FuelMarginResponse:
    """Record a margin revision. Admin only (§8).

    Expected to be used rarely -- once per fuel, then again only when the OMC revises the
    commission. Backdating is accepted and warned about, for the same reason as prices.
    """
    fuel_type = db.get(FuelType, payload.fuel_type_id)
    if fuel_type is None or not fuel_type.is_active:
        raise AppError(
            status_code=409,
            code="FUEL_TYPE_NOT_FOUND",
            detail="That fuel type does not exist or is no longer active.",
        )

    clash = db.execute(
        select(FuelMargin).where(
            FuelMargin.outlet_id == actor.outlet_id,
            FuelMargin.fuel_type_id == payload.fuel_type_id,
            FuelMargin.effective_from == payload.effective_from,
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise AppError(
            status_code=409,
            code="MARGIN_ALREADY_EFFECTIVE_AT",
            detail=(
                "A margin for this fuel is already effective at that exact moment. "
                "Margins are append-only; enter a correction with a later effective_from."
            ),
        )

    is_backdated = payload.effective_from < datetime.now(tz=timezone.utc)

    row = FuelMargin(
        outlet_id=actor.outlet_id,
        fuel_type_id=payload.fuel_type_id,
        margin_per_unit=payload.margin_per_unit,
        effective_from=payload.effective_from,
        entered_by=actor.user.id,
        created_by=actor.user.id,
    )
    db.add(row)
    # flush so the id exists for the audit row; one transaction carries both (§5.3).
    db.flush()

    audit.record(
        db,
        outlet_id=actor.outlet_id,
        table_name="fuel_margins",
        record_id=row.id,
        # Always `insert`. The table is append-only, so a correction is a *later* row with a
        # new `effective_from`, never an edit to this one -- there is no `update` to record.
        action=AuditAction.insert,
        changed_by=actor.user.id,
        # `is_backdated` is folded into the snapshot rather than being a column, the same way
        # shifts.py folds in a reopen reason and credit_sales.py an override reason:
        # AuditAction's labels are fixed at migration time and none of them says "backdated".
        # It belongs here because it is the fact that makes this row consequential -- it is
        # the difference between setting tomorrow's rate and revaluing last week's closed
        # shifts.
        new_values=_audit_snapshot(row) | {"is_backdated": is_backdated},
    )
    db.commit()
    db.refresh(row)

    if is_backdated:
        logger.warning(
            "backdated fuel margin entered",
            extra={
                "fuel_margin_id": str(row.id),
                "fuel_type_code": fuel_type.code,
                "effective_from": row.effective_from.isoformat(),
                "entered_by": str(actor.user.id),
            },
        )
    else:
        logger.info(
            "fuel margin entered",
            extra={
                "fuel_margin_id": str(row.id),
                "fuel_type_code": fuel_type.code,
                "effective_from": row.effective_from.isoformat(),
            },
        )

    return _to_response(row, is_backdated=is_backdated)


# Must stay above any "/fuel-margins/{id}" route -- see the note in fuel_prices.py.
@router.get("/fuel-margins/current", response_model=list[CurrentMarginResponse])
def read_current_margins(
    at: datetime | None = Query(default=None),
    actor: Actor = Depends(require_role(Role.attendant)),
    db: Session = Depends(get_db),
) -> list[CurrentMarginResponse]:
    """The margin in effect for every active fuel, now or at a given instant.

    Goes through `margin_at` so this and Phase 13's profit report can never disagree.
    Fuels with no margin entered yet are omitted -- a zero here would read as "we make
    nothing on this fuel", which is a believable number and a false one.
    """
    moment = at or datetime.now(tz=timezone.utc)
    if moment.tzinfo is None:
        raise AppError(
            status_code=422,
            code="NAIVE_TIMESTAMP",
            detail="`at` must include a timezone offset.",
        )

    fuel_types = (
        db.execute(
            select(FuelType).where(FuelType.is_active.is_(True)).order_by(FuelType.code)
        )
        .scalars()
        .all()
    )

    margins: list[CurrentMarginResponse] = []
    for fuel_type in fuel_types:
        try:
            margin = margin_at(
                db, outlet_id=actor.outlet_id, fuel_type_id=fuel_type.id, at=moment
            )
        except AppError:
            continue
        margins.append(
            CurrentMarginResponse(
                fuel_type_id=fuel_type.id,
                fuel_type_code=fuel_type.code,
                margin_per_unit=margin,
                at=moment,
            )
        )
    return margins


@router.get("/fuel-margins", response_model=FuelMarginPage)
def list_fuel_margins(
    fuel_type_id: UUID | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
    actor: Actor = Depends(require_role(Role.attendant)),
    db: Session = Depends(get_db),
) -> FuelMarginPage:
    """Margin history, newest first, cursor-paginated (§9 -- never `OFFSET`)."""
    statement = (
        select(FuelMargin)
        .where(FuelMargin.outlet_id == actor.outlet_id)
        .order_by(FuelMargin.effective_from.desc(), FuelMargin.id.desc())
    )
    if fuel_type_id is not None:
        statement = statement.where(FuelMargin.fuel_type_id == fuel_type_id)
    if cursor is not None:
        last_effective_from, last_id = decode_cursor(cursor)
        statement = statement.where(
            tuple_(FuelMargin.effective_from, FuelMargin.id)
            < tuple_(last_effective_from, last_id)
        )

    rows = db.execute(statement.limit(limit + 1)).scalars().all()
    has_more = len(rows) > limit
    page = rows[:limit]

    return FuelMarginPage(
        items=[_to_response(row) for row in page],
        next_cursor=(
            encode_cursor(page[-1].effective_from, page[-1].id)
            if has_more and page
            else None
        ),
    )
