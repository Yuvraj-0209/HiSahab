"""Fuel prices -- append-only, effective-dated (CLAUDE.md §4.1, §5.1).

India has used dynamic daily pricing since June 2017: OMCs publish revised rates every
morning at 06:00 IST. In practice a rate holds for weeks and then moves, so this is a
low-churn table -- but it must be effective-dated, because a mutable "current price" column
would silently rewrite the value of every shift ever recorded the moment a rate changed.

There is no PATCH and no DELETE here, and a database trigger enforces the same rule one
level down (§6.6's belt and braces). A correction is a new row with a later
`effective_from`; the original stays visible, which is the only way to answer "what did the
system tell the manager that day".
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
from app.models.fuel import FuelPrice, FuelType
from app.services.pricing import rate_at

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reference data"])


class FuelPriceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fuel_type_id: UUID
    # ₹ per litre or per kilogram, depending on the fuel (§4.5). condecimal, never float --
    # §3 rule 1, and this is the number every sale value is multiplied by.
    rate_per_unit: condecimal(max_digits=12, decimal_places=2, gt=0)
    effective_from: datetime

    @field_validator("effective_from")
    @classmethod
    def _must_be_timezone_aware(cls, value: datetime) -> datetime:
        """Reject naive datetimes rather than guessing a zone.

        §3 rule 4 stores everything in UTC and converts to Asia/Kolkata only for display.
        A naive timestamp here would have to be *assumed* to be one or the other, and the
        assumption is invisible: guess wrong about a 06:00 IST revision and it lands at
        11:30 IST instead, mispricing five and a half hours of a morning shift. Refusing is
        the only safe option -- the client knows its own zone, this server does not.
        """
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "effective_from must include a timezone offset, e.g. "
                "2026-08-18T06:00:00+05:30"
            )
        return value


class FuelPriceResponse(BaseModel):
    id: UUID
    outlet_id: UUID
    fuel_type_id: UUID
    rate_per_unit: Decimal
    effective_from: datetime
    entered_by: UUID
    # True when this rate was entered with an effective_from already in the past, which
    # revalues shifts that have already happened. Surfaced so the UI can confirm rather
    # than let it pass unnoticed; also logged as a warning. See the POST handler.
    is_backdated: bool


class FuelPricePage(BaseModel):
    items: list[FuelPriceResponse]
    next_cursor: str | None


class CurrentRateResponse(BaseModel):
    fuel_type_id: UUID
    fuel_type_code: str
    rate_per_unit: Decimal
    at: datetime


def _audit_snapshot(row: FuelPrice) -> dict[str, object]:
    """What §5.3's trail keeps about an appended rate.

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
        "rate_per_unit": row.rate_per_unit,
        "effective_from": row.effective_from,
        "entered_by": row.entered_by,
    }


def _to_response(row: FuelPrice, *, is_backdated: bool = False) -> FuelPriceResponse:
    return FuelPriceResponse(
        id=row.id,
        outlet_id=row.outlet_id,
        fuel_type_id=row.fuel_type_id,
        rate_per_unit=row.rate_per_unit,
        effective_from=row.effective_from,
        entered_by=row.entered_by,
        is_backdated=is_backdated,
    )


@router.post("/fuel-prices", response_model=FuelPriceResponse, status_code=201)
def create_fuel_price(
    payload: FuelPriceCreate,
    actor: Actor = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
) -> FuelPriceResponse:
    """Record a rate revision. Admin only -- §8 puts this above the manager floor.

    **Backdating is allowed, and warned about.** Refusing it would be tidier for the audit
    trail, but it leaves a real failure mode with no remedy: if nobody enters Tuesday's
    revision until Thursday, a forward-only rule means Tuesday and Wednesday are valued at
    the stale rate permanently, with no legal way to correct them. So it is accepted, the
    response says so, and a warning goes to the log -- visible, not silent.
    """
    fuel_type = db.get(FuelType, payload.fuel_type_id)
    if fuel_type is None or not fuel_type.is_active:
        raise AppError(
            status_code=409,
            code="FUEL_TYPE_NOT_FOUND",
            detail="That fuel type does not exist or is no longer active.",
        )

    clash = db.execute(
        select(FuelPrice).where(
            FuelPrice.outlet_id == actor.outlet_id,
            FuelPrice.fuel_type_id == payload.fuel_type_id,
            FuelPrice.effective_from == payload.effective_from,
        )
    ).scalar_one_or_none()
    if clash is not None:
        # The table is append-only, so this cannot be resolved by overwriting. Say so
        # rather than letting the unique constraint surface as a 500.
        raise AppError(
            status_code=409,
            code="PRICE_ALREADY_EFFECTIVE_AT",
            detail=(
                "A rate for this fuel is already effective at that exact moment. "
                "Prices are append-only; enter a correction with a later effective_from."
            ),
        )

    is_backdated = payload.effective_from < datetime.now(tz=timezone.utc)

    row = FuelPrice(
        outlet_id=actor.outlet_id,
        fuel_type_id=payload.fuel_type_id,
        rate_per_unit=payload.rate_per_unit,
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
        table_name="fuel_prices",
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
            "backdated fuel price entered",
            extra={
                "fuel_price_id": str(row.id),
                "fuel_type_code": fuel_type.code,
                "effective_from": row.effective_from.isoformat(),
                "entered_by": str(actor.user.id),
            },
        )
    else:
        logger.info(
            "fuel price entered",
            extra={
                "fuel_price_id": str(row.id),
                "fuel_type_code": fuel_type.code,
                "effective_from": row.effective_from.isoformat(),
            },
        )

    return _to_response(row, is_backdated=is_backdated)


# Declared before any "/fuel-prices/{id}" route would be. None exists today; if one is ever
# added it must go below this, or "current" will be parsed as an id and 422 every request.
@router.get("/fuel-prices/current", response_model=list[CurrentRateResponse])
def read_current_rates(
    at: datetime | None = Query(default=None),
    actor: Actor = Depends(require_role(Role.attendant)),
    db: Session = Depends(get_db),
) -> list[CurrentRateResponse]:
    """The rate in effect for every active fuel, now or at a given instant.

    Goes through `rate_at` rather than running its own query, so this endpoint and the
    sales math in Phase 5 can never disagree about what a fuel was worth (§5.1's DRY rule).
    Fuels with no rate yet are omitted rather than reported as zero.
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

    rates: list[CurrentRateResponse] = []
    for fuel_type in fuel_types:
        try:
            rate = rate_at(
                db,
                outlet_id=actor.outlet_id,
                fuel_type_id=fuel_type.id,
                at=moment,
            )
        except AppError:
            # No rate entered for this fuel yet. Omitting it is right: a zero here would
            # be indistinguishable from a genuine rate and would value sales at nothing.
            continue
        rates.append(
            CurrentRateResponse(
                fuel_type_id=fuel_type.id,
                fuel_type_code=fuel_type.code,
                rate_per_unit=rate,
                at=moment,
            )
        )
    return rates


@router.get("/fuel-prices", response_model=FuelPricePage)
def list_fuel_prices(
    fuel_type_id: UUID | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
    actor: Actor = Depends(require_role(Role.attendant)),
    db: Session = Depends(get_db),
) -> FuelPricePage:
    """Rate history, newest first, cursor-paginated (§9 -- never `OFFSET`).

    Keyset predicate on `(effective_from, id)` so a price entered mid-walk cannot make the
    reader skip or repeat a row. See app/api/cursor.py for why the id is part of the key.
    """
    statement = (
        select(FuelPrice)
        .where(FuelPrice.outlet_id == actor.outlet_id)
        .order_by(FuelPrice.effective_from.desc(), FuelPrice.id.desc())
    )
    if fuel_type_id is not None:
        statement = statement.where(FuelPrice.fuel_type_id == fuel_type_id)
    if cursor is not None:
        last_effective_from, last_id = decode_cursor(cursor)
        statement = statement.where(
            tuple_(FuelPrice.effective_from, FuelPrice.id)
            < tuple_(last_effective_from, last_id)
        )

    # One row beyond the page, purely to learn whether another page exists -- cheaper and
    # more accurate than a COUNT, which would also be wrong the moment a row is inserted.
    rows = db.execute(statement.limit(limit + 1)).scalars().all()
    has_more = len(rows) > limit
    page = rows[:limit]

    return FuelPricePage(
        items=[_to_response(row) for row in page],
        next_cursor=(
            encode_cursor(page[-1].effective_from, page[-1].id)
            if has_more and page
            else None
        ),
    )
