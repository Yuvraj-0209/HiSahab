"""The only sanctioned source of a rate or a margin (CLAUDE.md §5.1, §6.3, §4.6).

CLAUDE.md §4.1 is emphatic and it is worth restating, because the wrong version of this
module does not crash -- it quietly returns today's number for a question about last month:

> A price is not an attribute of a fuel. It is a dated record.

So there is no "current price" column anywhere in this schema, and there is exactly one way
to answer "what was fuel F worth at outlet O at time T": take the row with the greatest
`effective_from <= T`. That query lives here, once. Sales (Phase 5), the cash engine
(Phase 10) and reporting (Phase 13) all call it rather than reimplementing it, because
three copies of this rule will disagree eventually and the disagreement will be about money.

`margin_at` is the same question asked of `fuel_margins`. §4.6: retail revisions pass
straight through to the purchase invoice, so the dealer margin -- not the price gap -- is
the constant, which is what makes profit computable from the totalizer alone.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from collections.abc import Callable
from typing import TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.fuel import FuelMargin, FuelPrice

logger = logging.getLogger(__name__)

_Row = TypeVar("_Row", FuelPrice, FuelMargin)


class LookupCache:
    """A per-request memo for the two effective-dated lookups (Phase 19, §13.34).

    **Why this exists.** A rate is constant within a trading day, but `readings.shift_sales`
    asks for one per nozzle per shift. That is right for a single shift and wasteful over a
    window: §13.34 records that a year of days repeats one identical indexed lookup a few
    thousand times, purely because a report walks shifts (and it must walk them -- §6.3
    values each at its own instant, so there is no `SUM` to write instead).

    **Why it is safe to memoise at all.** `fuel_prices` and `fuel_margins` are append-only
    (§5.1) and no report writes, so within one request the answer to
    `(outlet, fuel, instant)` cannot change. The key is the exact tuple the lookup is a
    function of, so two different instants never share an entry -- which is the property
    that keeps a backdated revision (§11 phase 11) visible rather than cached over.

    **Scope is one call, never a module global.** A process-lifetime cache would outlive the
    append that invalidates it, and §4.1 is explicit about what a stale price does: it
    "silently corrupts every historical report". Passing it in means it dies with the report.

    Callers that do not supply one are unchanged -- `cache=None` means every lookup hits the
    database, which is what every write path does and should keep doing.
    """

    __slots__ = ("_rows",)

    def __init__(self) -> None:
        self._rows: dict[tuple[str, UUID, UUID, datetime], object] = {}

    def fetch(
        self,
        key: tuple[str, UUID, UUID, datetime],
        load: "Callable[[], object]",
    ) -> object:
        """Return the memoised row, loading it once on first ask.

        A `None` result is cached too, deliberately: "no rate has ever been entered for this
        fuel" is as stable an answer as a row is, and not caching it would leave the miss
        path -- the one that raises -- as the only uncached case.
        """
        if key not in self._rows:
            self._rows[key] = load()
        return self._rows[key]


def _effective_row_at(
    db: Session,
    model: type[_Row],
    *,
    outlet_id: UUID,
    fuel_type_id: UUID,
    at: datetime,
    cache: LookupCache | None = None,
) -> _Row | None:
    """The row in effect for this fuel, at this outlet, at this instant.

    `FuelPrice` and `FuelMargin` are deliberately identical in shape, so the lookup is
    written once and parameterised by the model. Shared rather than duplicated because
    the subtle parts -- the `<=` rather than `<`, the descending order, the outlet and fuel
    scoping -- are exactly the parts that would drift apart in two copies.

    The comparison is `<=`, not `<`: a rate stamped 06:00 is live *at* 06:00, not from
    06:00.000001. The boundary case has its own test.

    This is a single indexed lookup, not a scan: the unique constraint on
    (outlet_id, fuel_type_id, effective_from) backs an index that serves it exactly.

    `cache`, when supplied, memoises that lookup for the life of one report -- see
    `LookupCache`. Omitted everywhere else, so every write path is byte-for-byte unchanged.
    """

    def load() -> _Row | None:
        return db.execute(
            select(model)
            .where(
                model.outlet_id == outlet_id,
                model.fuel_type_id == fuel_type_id,
                model.effective_from <= at,
            )
            .order_by(model.effective_from.desc())
            .limit(1)
        ).scalar_one_or_none()

    if cache is None:
        return load()
    # The model name is part of the key: a price and a margin for the same fuel at the same
    # instant are two different questions, and sharing a slot would answer one with the other.
    return cache.fetch((model.__name__, outlet_id, fuel_type_id, at), load)  # type: ignore[return-value]


def rate_at(
    db: Session,
    *,
    outlet_id: UUID,
    fuel_type_id: UUID,
    at: datetime,
    cache: LookupCache | None = None,
) -> Decimal:
    """The rate per unit (₹/litre or ₹/kg, per the fuel's unit) in effect at `at`.

    Raises rather than returning None when no rate has ever been entered for this fuel
    before this instant. That is a deliberate choice and the more important half of this
    function's contract: an Optional return pushes a null check into every caller, and the
    first caller that forgets values an entire shift's fuel at zero -- a plausible-looking
    figure, no exception, no log line. A refusal is recoverable; a silent zero is not.

    409 rather than 404: the row is not missing from a URL, the outlet is in a state that
    makes the request unanswerable. The fix is to enter the price, then retry.
    """
    row = _effective_row_at(
        db, FuelPrice, outlet_id=outlet_id, fuel_type_id=fuel_type_id, at=at, cache=cache
    )
    if row is None:
        logger.warning(
            "no fuel price in effect",
            extra={
                "outlet_id": str(outlet_id),
                "fuel_type_id": str(fuel_type_id),
                "at": at.isoformat(),
            },
        )
        raise AppError(
            status_code=409,
            code="NO_PRICE_FOR_DATE",
            detail=(
                "No fuel price has been entered for this fuel effective on or before "
                "this date. An admin must enter the rate before sales can be valued."
            ),
        )
    return row.rate_per_unit


def margin_at(
    db: Session,
    *,
    outlet_id: UUID,
    fuel_type_id: UUID,
    at: datetime,
    cache: LookupCache | None = None,
) -> Decimal:
    """The dealer margin per unit in effect at `at`.

    Note what this is *not* affected by: price revisions. §4.6 -- when retail rises ₹1 the
    next tanker invoice rises ₹1 too, so the margin does not move. A test enters a margin,
    revises the price twice, and asserts this still returns the original figure, because
    that single fact is what the whole profit model rests on.

    Raises for the same reason `rate_at` does. A missing margin must not read as zero
    profit, which is a perfectly plausible number and completely wrong.
    """
    row = _effective_row_at(
        db, FuelMargin, outlet_id=outlet_id, fuel_type_id=fuel_type_id, at=at, cache=cache
    )
    if row is None:
        logger.warning(
            "no fuel margin in effect",
            extra={
                "outlet_id": str(outlet_id),
                "fuel_type_id": str(fuel_type_id),
                "at": at.isoformat(),
            },
        )
        raise AppError(
            status_code=409,
            code="NO_MARGIN_FOR_DATE",
            detail=(
                "No dealer margin has been entered for this fuel effective on or before "
                "this date. An admin must enter the margin before profit can be computed."
            ),
        )
    return row.margin_per_unit


def revisions_within(
    db: Session,
    *,
    outlet_id: UUID,
    fuel_type_id: UUID,
    start: datetime,
    end: datetime,
) -> list[datetime]:
    """Price revisions that fell strictly inside a shift window (§6.3, §13.1).

    Lives here rather than in the sales code so that `fuel_prices` is still queried from
    exactly one module -- the same reason `rate_at` is here at all.

    §6.3 values a whole shift at the rate effective at its `started_at`, which is an
    approximation whenever a revision lands mid-shift. The approximation is not silently
    made: this returns the revisions so the caller can log a warning naming them. §6.3 is
    explicit that we must "not silently pretend it is exact".

    Strictly greater than `start`: a revision stamped exactly at the shift's start instant
    is not mid-shift at all. `rate_at` compares with `<=`, so that revision is already the
    rate the whole shift is valued at -- which is why this outlet's 06:00 shift start makes
    §6.3 exact rather than approximate (§4.7).
    """
    return list(
        db.execute(
            select(FuelPrice.effective_from)
            .where(
                FuelPrice.outlet_id == outlet_id,
                FuelPrice.fuel_type_id == fuel_type_id,
                FuelPrice.effective_from > start,
                FuelPrice.effective_from < end,
            )
            .order_by(FuelPrice.effective_from)
        ).scalars()
    )
