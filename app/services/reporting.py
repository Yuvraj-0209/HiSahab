"""Reporting: reading a day back, and being honest about where each figure came from.

Phase 13. This module answers three questions -- what happened on one day, what happened
across a window of days, and what needs a manager's attention -- and it **writes nothing**.

## The rule this module exists to hold

§5.2 stores `expected_closing` *and every component* on `daily_cash_summaries` so a reader can
see the figure **as it stood**, and §14 forbids recomputing a finalised day. A report is the
one thing whose entire job is to show that record, so it reads the stored row.

But a business date nobody reconciled has **no stored row at all**, and a 7-day view that
silently drops yesterday because nobody created a summary is a report lying by omission -- the
opposite of §4.7's *"the abnormal day becomes visible instead of reassigned."*

So (§13.20): **read the snapshot where one exists, compute live where none does, and say
which.** `DaySource` is part of the answer rather than a decoration -- a computed figure is an
estimate of a day still in motion, a snapshot is a record of what somebody was told, and
reading them as the same number is the mistake the field exists to prevent.

## The half that has no snapshot at all

`daily_cash_summaries` holds `metered_fuel_sales` as **one number**, with no per-fuel split and
no margin. So the fuel breakdown is *necessarily* live, even on a finalised day -- there is
nothing else it could be.

Which produces §13.22, and it is the sharpest thing in this phase: the breakdown is computed
**today** beside a total frozen **then**. A backdated `effective_from` -- which §11 names as
the reason `fuel_prices` needed an audit trail, since it *"can revalue a closed shift"* --
makes them disagree. `DailyReport.breakdown_reconciles` says so and shows both figures rather
than picking a winner. Nothing else in this system notices that a closed day was revalued.

## Why margins are caught here and not relaxed in pricing.py

`margin_at` raises 409 `NO_MARGIN_FOR_DATE`, and that is correct: §5.1's argument is that an
Optional return pushes a null check into every caller and the first one that forgets values a
shift at zero. It stays correct.

But §14 records that petrol and diesel dealer commissions have **never been entered at this
outlet**, so a report that propagated that refusal would show nothing at all on every petrol
day -- which is every day. §6.3 settled the same tension for the cash engine one phase earlier:
*"a caller that wants one must not be refused because the other is missing."*

So the catching lives **here**, in the layer with a reason to tolerate a gap, and it is narrow:
only `NO_MARGIN_FOR_DATE`, only around the margin lookup. Anything else propagates. A fuel with
no margin reports `None` and a reason -- **never `Decimal("0.00")`**, which would be a
plausible-looking figure and completely wrong (§13.7).

And the combined total is **withheld** rather than made partial (§13.21). A fuel with no margin
makes the day's margin *unknowable*, not smaller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.cash import OpeningBalanceSource
from app.core.errors import AppError
from app.core.shifts import ShiftStatus
from app.models.cash import DailyCashSummary
from app.models.fuel import FuelType
from app.models.reading import NozzleReading
from app.models.shift import Shift
from app.services import cash as cash_service
from app.services import expenses as expense_service
from app.services import pricing
from app.services import readings as reading_service
from app.services import sales

logger = logging.getLogger(__name__)

_ZERO = Decimal("0.00")
_MONEY_PLACES = Decimal("0.01")

# The one refusal a report absorbs rather than propagates. Narrow on purpose: a missing
# *price* still refuses, because §6.3 is explicit that "a day valued at zero reconciles to a
# cash surplus nobody can explain".
_MARGIN_MISSING = "NO_MARGIN_FOR_DATE"
_PRICE_MISSING = "NO_PRICE_FOR_DATE"


class DaySource(StrEnum):
    """Where a day's cash figures came from (§13.20).

    Not an implementation detail leaking into the API. `snapshot` and `computed` are
    different *kinds of claim* -- one is a record of what a manager was shown, the other is
    an estimate of a day still being entered -- and a reader who cannot tell them apart will
    average them, compare them, and act on the result.
    """

    snapshot = "snapshot"
    computed = "computed"
    no_trading = "no_trading"
    unavailable = "unavailable"


class AlertKind(StrEnum):
    """§13.23's six kinds, each derived from a signal that is already stored.

    Nothing here is persisted. An alerts table would be a second copy of facts that already
    exist -- `daily_cash_summaries.variance`, three `requires_review` flags and
    `shifts.status` -- free to drift from them, and §14 already forbids a denormalised copy of
    a figure for exactly that reason.
    """

    variance_exceeds_threshold = "variance_exceeds_threshold"
    day_not_reconciled = "day_not_reconciled"
    summary_requires_review = "summary_requires_review"
    unreviewed_flagged_expenses = "unreviewed_flagged_expenses"
    reading_requires_review = "reading_requires_review"
    open_shift_on_a_past_date = "open_shift_on_a_past_date"


# --- the fuel breakdown --------------------------------------------------------


@dataclass(frozen=True)
class FuelLine:
    """One fuel type's contribution to a business date, aggregated across every shift.

    Aggregated by **fuel type**, not by nozzle: two nozzles dispensing petrol are one
    commercial fact, and §5.1 already says a dispenser is a label rather than an entity in V1.

    `rate_per_unit` and `margin_per_unit` are `None` when the day's contributing shifts did
    **not** all resolve to the same figure -- a day that spanned a price revision, or a
    24-hour outlet whose shifts straddle 06:00 (§4.1). The money totals are still exact,
    because each shift was valued at its own rate before being summed; it is only the *label*
    that has no single honest value. Reporting one of the two rates would be inventing a fact.
    """

    fuel_type: FuelType
    quantity: Decimal | None
    rate_per_unit: Decimal | None
    sale_value: Decimal | None
    margin_per_unit: Decimal | None
    gross_fuel_margin: Decimal | None
    margin_unavailable_reason: str | None

    @property
    def unit_of_measure(self) -> str:
        """§4.5: read from the fuel type, never inferred from the number."""
        return str(self.fuel_type.unit_of_measure)


@dataclass(frozen=True)
class FuelBreakdown:
    """Every fuel that moved on one business date, priced and (where possible) margined."""

    lines: list[FuelLine]
    sale_value_total: Decimal | None
    gross_fuel_margin_total: Decimal | None
    fuels_missing_margin: list[str]
    quantity_by_unit: dict[str, Decimal]
    incomplete: bool


def _margin_or_none(
    db: Session,
    *,
    outlet_id: UUID,
    fuel_type_id: UUID,
    at: object,
    cache: dict[tuple[UUID, object], tuple[Decimal | None, str | None]],
    rows: pricing.LookupCache | None = None,
) -> tuple[Decimal | None, str | None]:
    """`margin_at`, with 409 `NO_MARGIN_FOR_DATE` turned into a reported gap.

    The `except` is deliberately narrow -- it re-raises anything that is not that one code.
    A broad `except AppError` here would swallow a genuine failure and report it as "no margin
    entered", which is a lie that looks like a configuration note.

    Cached per `(fuel_type, instant)` because a day has few distinct pairs and a window of 31
    days would otherwise repeat the same lookup once per nozzle.
    """
    key = (fuel_type_id, at)
    if key in cache:
        return cache[key]

    try:
        margin = pricing.margin_at(
            db, outlet_id=outlet_id, fuel_type_id=fuel_type_id, at=at, cache=rows
        )
        result: tuple[Decimal | None, str | None] = (margin, None)
    except AppError as exc:
        if exc.code != _MARGIN_MISSING:
            raise
        result = (None, _MARGIN_MISSING)

    cache[key] = result
    return result


def _priced_lines(
    db: Session, *, shift: Shift, cache: pricing.LookupCache
) -> list[reading_service.SalesLine]:
    """`shift_sales(price_only=True)`, with the rate lookup memoised per process call.

    Phase 19, and it exists for §13.34's reason. `shift_sales` calls `pricing.rate_at` once
    per nozzle per shift and caches nothing, which is right for a single shift and wasteful
    over a window: a rate is constant within a trading day, so a year of days repeats one
    identical indexed lookup a few thousand times.

    The memo is keyed on `(outlet, fuel, instant)` -- the exact tuple `rate_at` is a function
    of -- and lives only for the duration of one report. It cannot go stale, because
    `fuel_prices` is append-only (§5.1) and no report writes.

    **It patches nothing.** An earlier draft monkeypatched `pricing.rate_at` for the duration
    of the walk; that is a lie to every other caller sharing the module and would have made a
    concurrent request read a cache built for a different window.
    """
    return reading_service.shift_sales(db, shift=shift, price_only=True, cache=cache)


def fuel_breakdown(
    db: Session, *, outlet_id: UUID, business_date: date
) -> FuelBreakdown:
    """Price every nozzle on every shift of one business date, then group by fuel type.

    Calls `readings.shift_sales(price_only=True)` and looks the margin up separately, rather
    than using the default form -- see this module's docstring for why. A missing **price**
    still raises 409 `NO_PRICE_FOR_DATE`; the caller decides whether that makes the whole day
    unavailable.
    """
    shifts = cash_service.shifts_on(
        db, outlet_id=outlet_id, business_date=business_date
    )
    return _accumulate_fuel(db, outlet_id=outlet_id, shifts=shifts)


def _accumulate_fuel(
    db: Session, *, outlet_id: UUID, shifts: list[Shift]
) -> FuelBreakdown:
    """Group priced nozzle lines by fuel type, over whatever set of shifts it is given.

    Extracted in Phase 19 so one day and a 366-day window share one accumulator. Two copies
    of this would be two answers to "what did petrol earn", and §6.4 already records what
    that costs: *"two implementations of one equation is the shape that drifts."*
    """
    # fuel_type_id -> accumulator. `dict` preserves insertion order, so the output is ordered
    # by first appearance (shift sequence, then nozzle) rather than arbitrarily.
    acc: dict[UUID, dict[str, object]] = {}
    margin_cache: dict[tuple[UUID, object], tuple[Decimal | None, str | None]] = {}
    # One memo for the whole walk, dying with it (§13.34, `pricing.LookupCache`).
    rate_cache = pricing.LookupCache()
    incomplete = False

    for shift in shifts:
        for line in _priced_lines(db, shift=shift, cache=rate_cache):
            fuel = line.fuel_type
            entry = acc.setdefault(
                fuel.id,
                {
                    "fuel_type": fuel,
                    "quantity": _ZERO,
                    "sale_value": _ZERO,
                    "gross_fuel_margin": _ZERO,
                    "rates": set(),
                    "margins": set(),
                    "margin_reason": None,
                    "margin_known": True,
                    "saw_any": False,
                },
            )

            if line.quantity is None:
                # "Not entered" and "sold nothing" are different facts (§13.7, SalesLine's
                # own docstring). The day is incomplete; the fuel is not zero.
                incomplete = True
                continue

            entry["saw_any"] = True
            entry["quantity"] = entry["quantity"] + line.quantity  # type: ignore[operator]
            entry["sale_value"] = entry["sale_value"] + (line.value or _ZERO)  # type: ignore[operator]
            if line.rate_per_unit is not None:
                entry["rates"].add(line.rate_per_unit)  # type: ignore[union-attr]

            margin, reason = _margin_or_none(
                db,
                outlet_id=outlet_id,
                fuel_type_id=fuel.id,
                at=shift.started_at,
                cache=margin_cache,
                rows=rate_cache,
            )
            if margin is None:
                entry["margin_known"] = False
                entry["margin_reason"] = reason
            else:
                entry["margins"].add(margin)  # type: ignore[union-attr]
                entry["gross_fuel_margin"] = entry["gross_fuel_margin"] + sales.dealer_profit(  # type: ignore[operator]
                    line.quantity, margin
                )

    lines: list[FuelLine] = []
    quantity_by_unit: dict[str, Decimal] = {}
    sale_value_total = _ZERO
    margin_total = _ZERO
    margin_total_known = True
    missing: list[str] = []

    for entry in acc.values():
        fuel = entry["fuel_type"]
        saw_any = bool(entry["saw_any"])
        quantity = entry["quantity"] if saw_any else None
        value = entry["sale_value"] if saw_any else None
        margin_known = bool(entry["margin_known"]) and saw_any

        # A single rate only when every contributing shift agreed -- otherwise the label has
        # no honest value, even though the total does. Same for the margin.
        rates = entry["rates"]
        margins = entry["margins"]
        rate = next(iter(rates)) if len(rates) == 1 else None  # type: ignore[arg-type]
        margin_per_unit = next(iter(margins)) if margin_known and len(margins) == 1 else None  # type: ignore[arg-type]

        lines.append(
            FuelLine(
                fuel_type=fuel,  # type: ignore[arg-type]
                quantity=quantity,  # type: ignore[arg-type]
                rate_per_unit=rate,
                sale_value=value,  # type: ignore[arg-type]
                margin_per_unit=margin_per_unit,
                gross_fuel_margin=entry["gross_fuel_margin"] if margin_known else None,  # type: ignore[arg-type]
                margin_unavailable_reason=None if margin_known else entry["margin_reason"],  # type: ignore[arg-type]
            )
        )

        if not saw_any:
            continue

        sale_value_total = sale_value_total + value  # type: ignore[operator]
        unit = str(fuel.unit_of_measure)  # type: ignore[union-attr]
        quantity_by_unit[unit] = quantity_by_unit.get(unit, Decimal("0.000")) + quantity  # type: ignore[operator]

        if margin_known:
            margin_total = margin_total + entry["gross_fuel_margin"]  # type: ignore[operator]
        else:
            # §13.21: one fuel without a margin makes the DAY's margin unknowable, not
            # smaller. Withhold the total and name the fuel.
            margin_total_known = False
            missing.append(str(fuel.code))  # type: ignore[union-attr]

    return FuelBreakdown(
        lines=lines,
        sale_value_total=sale_value_total if acc else _ZERO,
        gross_fuel_margin_total=margin_total if margin_total_known else None,
        fuels_missing_margin=missing,
        quantity_by_unit=quantity_by_unit,
        incomplete=incomplete,
    )


def fuel_breakdown_range(
    db: Session, *, outlet_id: UUID, date_from: date, date_to: date
) -> FuelBreakdown:
    """`fuel_breakdown`, widened from one business date to a window (Phase 19).

    **Why this is a walk and not a `SUM`.** §6.3 values a shift at the rate effective at its
    own `started_at`, and §4.1 keeps that rate in an effective-dated table rather than in a
    column on the reading -- so there is no `price` to multiply a summed quantity by. A query
    that appeared to do this in one pass would have found a *current* rate somewhere and
    silently revalued history with it, which is the corruption §4.1 exists to prevent. So each
    shift is priced at its own instant and the results are added, which is arithmetic on
    already-correct figures rather than a shortcut around the rule.

    **The cost, and the memo that makes it bearable (§13.34).** This is O(shifts x nozzles),
    and `readings.shift_sales` looks a rate up per nozzle per shift with no caching of its
    own. Over a year that repeats one identical query a few thousand times, because a price is
    constant within a trading day. `_priced_lines` below memoises on `(fuel_type, instant)`,
    which collapses it to roughly one lookup per fuel per revision.

    **`shift_sales` is deliberately not changed.** §6.4 makes the argument for `day_totals`
    and it holds here: two implementations of one valuation is the shape that drifts, and the
    one that drifts would be the one the cash engine depends on. The cache lives out here.

    Every rule `fuel_breakdown` follows is followed identically, because it is the same
    accumulator: a `None` quantity marks the window incomplete rather than counting as zero,
    a single rate or margin label survives only if every contributing shift agreed, quantities
    are grouped by unit and never summed across units (§4.5), and one fuel without a margin
    withholds the *window's* margin total and names the gap (§13.21).
    """
    shifts = _shifts_in_range(
        db, outlet_id=outlet_id, date_from=date_from, date_to=date_to
    )
    return _accumulate_fuel(db, outlet_id=outlet_id, shifts=shifts)


# --- one day's cash ------------------------------------------------------------


@dataclass(frozen=True)
class DayCash:
    """§6.4's terms for one business date, plus the provenance of every one of them."""

    business_date: date
    source: DaySource
    unavailable_reason: str | None
    shift_count: int

    opening_balance: Decimal | None
    opening_balance_source: OpeningBalanceSource | None
    expected_closing: Decimal | None
    actual_counted: Decimal | None
    variance: Decimal | None

    metered_fuel_sales: Decimal | None
    non_fuel_sales_total: Decimal | None
    card_total: Decimal | None
    upi_total: Decimal | None
    wallet_total: Decimal | None
    credit_sales_total: Decimal | None
    cash_credit_repayments: Decimal | None
    card_upi_credit_repayments: Decimal | None
    cash_shortfall_settlements: Decimal | None
    cash_expenses: Decimal | None
    bank_deposits_total: Decimal | None
    shortfalls_booked: Decimal | None

    is_finalised: bool
    requires_review: bool
    review_note: str | None
    incomplete: bool

    @property
    def total_sales(self) -> Decimal | None:
        """§6.4: `metered_fuel_sales + non_fuel_sales_total`, or None if either is unknown."""
        if self.metered_fuel_sales is None or self.non_fuel_sales_total is None:
            return None
        return self.metered_fuel_sales + self.non_fuel_sales_total


def summary_for(
    db: Session, *, outlet_id: UUID, business_date: date
) -> DailyCashSummary | None:
    """The stored summary for this date, or None. One query, so callers can share it."""
    return db.execute(
        select(DailyCashSummary).where(
            DailyCashSummary.outlet_id == outlet_id,
            DailyCashSummary.business_date == business_date,
        )
    ).scalar_one_or_none()


def _from_snapshot(summary: DailyCashSummary, *, shift_count: int) -> DayCash:
    """Read the stored row. **Recomputes nothing** -- that is the whole point (§5.2, §14).

    Every figure below is a column read. If this function ever grows a call to `day_totals`
    or an arithmetic expression over the components, it has stopped being a report of what
    the manager was told and become a second opinion wearing its clothes.
    """
    return DayCash(
        business_date=summary.business_date,
        source=DaySource.snapshot,
        unavailable_reason=None,
        shift_count=shift_count,
        opening_balance=summary.opening_balance,
        opening_balance_source=OpeningBalanceSource(summary.opening_balance_source),
        expected_closing=summary.expected_closing,
        actual_counted=summary.actual_counted,
        variance=summary.variance,
        metered_fuel_sales=summary.metered_fuel_sales,
        non_fuel_sales_total=summary.non_fuel_sales_total,
        card_total=summary.card_total,
        upi_total=summary.upi_total,
        wallet_total=summary.wallet_total,
        credit_sales_total=summary.credit_sales_total,
        cash_credit_repayments=summary.cash_credit_repayments,
        card_upi_credit_repayments=summary.card_upi_credit_repayments,
        cash_shortfall_settlements=summary.cash_shortfall_settlements,
        cash_expenses=summary.cash_expenses,
        bank_deposits_total=summary.bank_deposits_total,
        shortfalls_booked=summary.shortfalls_booked,
        is_finalised=summary.is_finalised,
        requires_review=summary.requires_review,
        review_note=summary.review_note,
        incomplete=False,
    )


def _no_trading(business_date: date) -> DayCash:
    """A date with no shifts and no summary.

    Sales are a genuine **zero** -- nothing was dispensed, and that is a fact rather than an
    absence. The reconciliation figures are **null**, because nobody reconciled: the locker
    still holds whatever it held, and reporting `0.00` for `expected_closing` would claim it
    was emptied. §6.8's "zero as an answer, never zero as an omission", read in both
    directions on one row.
    """
    return DayCash(
        business_date=business_date,
        source=DaySource.no_trading,
        unavailable_reason=None,
        shift_count=0,
        opening_balance=None,
        opening_balance_source=None,
        expected_closing=None,
        actual_counted=None,
        variance=None,
        metered_fuel_sales=_ZERO,
        non_fuel_sales_total=_ZERO,
        card_total=_ZERO,
        upi_total=_ZERO,
        wallet_total=_ZERO,
        credit_sales_total=_ZERO,
        cash_credit_repayments=_ZERO,
        card_upi_credit_repayments=_ZERO,
        cash_shortfall_settlements=_ZERO,
        cash_expenses=_ZERO,
        bank_deposits_total=_ZERO,
        shortfalls_booked=_ZERO,
        is_finalised=False,
        requires_review=False,
        review_note=None,
        incomplete=False,
    )


def _unavailable(business_date: date, *, shift_count: int, reason: str) -> DayCash:
    """Live computation refused. Every figure is null -- nothing is guessed at."""
    return DayCash(
        business_date=business_date,
        source=DaySource.unavailable,
        unavailable_reason=reason,
        shift_count=shift_count,
        opening_balance=None,
        opening_balance_source=None,
        expected_closing=None,
        actual_counted=None,
        variance=None,
        metered_fuel_sales=None,
        non_fuel_sales_total=None,
        card_total=None,
        upi_total=None,
        wallet_total=None,
        credit_sales_total=None,
        cash_credit_repayments=None,
        card_upi_credit_repayments=None,
        cash_shortfall_settlements=None,
        cash_expenses=None,
        bank_deposits_total=None,
        shortfalls_booked=None,
        is_finalised=False,
        requires_review=False,
        review_note=None,
        incomplete=True,
    )


def day_cash(db: Session, *, outlet_id: UUID, business_date: date) -> DayCash:
    """§13.20's decision, in one function.

    The order matters. A stored summary wins over everything, always -- even for a date whose
    shifts were later reopened (§13.16 flags the row rather than moving it, and this reports
    the flag beside the stale figure exactly as intended).
    """
    summary = summary_for(db, outlet_id=outlet_id, business_date=business_date)
    shifts = cash_service.shifts_on(
        db, outlet_id=outlet_id, business_date=business_date
    )

    if summary is not None:
        return _from_snapshot(summary, shift_count=len(shifts))

    if not shifts:
        return _no_trading(business_date)

    try:
        totals = cash_service.day_totals(
            db, outlet_id=outlet_id, business_date=business_date
        )
    except AppError as exc:
        if exc.code != _PRICE_MISSING:
            raise
        # One day's missing price must not take down a whole window (§13.20). The other days
        # in the range are unaffected and still render.
        logger.warning(
            "reporting: day cannot be computed",
            extra={"business_date": str(business_date), "code": exc.code},
        )
        return _unavailable(business_date, shift_count=len(shifts), reason=exc.code)

    # §6.5's chain. `previous_summary` finds the most recent summary, not literally
    # yesterday -- the outlet is shut some days and a calendar gap must not break the chain.
    previous = cash_service.previous_summary(
        db, outlet_id=outlet_id, business_date=business_date
    )
    if previous is None:
        # No anchor anywhere: §6.5's very first day needs an admin-seeded opening balance and
        # nobody has created one. The day's SALES are still perfectly valid -- only the
        # closing figure is unknowable, so it is null rather than the whole day being
        # unavailable. Reporting 0.00 here would silently claim an empty locker.
        opening: Decimal | None = None
        opening_source: OpeningBalanceSource | None = None
        closing: Decimal | None = None
    else:
        opening, opening_source = cash_service.opening_balance_from(previous)
        closing = cash_service.expected_closing(
            opening_balance=opening, totals=totals
        )

    return DayCash(
        business_date=business_date,
        source=DaySource.computed,
        unavailable_reason=None,
        shift_count=len(shifts),
        opening_balance=opening,
        opening_balance_source=opening_source,
        expected_closing=closing,
        # Both null by construction on a computed day: `actual_counted` only exists on a
        # summary row, and §5.2 generates `variance` from it. A computed day has neither, and
        # null here means "nobody counted", never "counted and found nothing wrong".
        actual_counted=None,
        variance=None,
        metered_fuel_sales=totals.metered_fuel_sales,
        non_fuel_sales_total=totals.non_fuel_sales_total,
        card_total=totals.card_total,
        upi_total=totals.upi_total,
        wallet_total=totals.wallet_total,
        credit_sales_total=totals.credit_sales_total,
        cash_credit_repayments=totals.cash_credit_repayments,
        card_upi_credit_repayments=totals.card_upi_credit_repayments,
        cash_shortfall_settlements=totals.cash_shortfall_settlements,
        cash_expenses=totals.cash_expenses,
        bank_deposits_total=totals.bank_deposits_total,
        shortfalls_booked=totals.shortfalls_booked,
        is_finalised=False,
        requires_review=False,
        review_note=None,
        incomplete=totals.incomplete,
    )


# --- one day, whole ------------------------------------------------------------


@dataclass(frozen=True)
class DailyReport:
    """Everything about one business date, cash and fuel, with provenance on both halves."""

    business_date: date
    shifts: list[Shift]
    cash: DayCash
    fuel: FuelBreakdown
    expenses_by_category: dict[str, Decimal]
    expenses_total: Decimal
    breakdown_reconciles: bool | None
    snapshot_metered_fuel_sales: Decimal | None


def daily_report(db: Session, *, outlet_id: UUID, business_date: date) -> DailyReport:
    """One day: the stored cash record (or a live estimate), plus a live fuel breakdown.

    **§13.22 lives here.** On a snapshot day the fuel breakdown is computed *now* while
    `metered_fuel_sales` was frozen *then*, so the two can disagree -- and when they do it
    means a price was backdated beneath a closed day, or a reading moved under it.

    `breakdown_reconciles` reports that comparison rather than resolving it. §5.2 warns that
    when *"the total and its own explanation disagree"*, the explanation is the half a human
    can actually check; picking a winner here would throw that away. Both figures go to the
    caller and both reach the screen.

    It is `None`, not `False`, on a non-snapshot day: there is nothing stored to reconcile
    against, and `False` would read as "these disagree".
    """
    cash = day_cash(db, outlet_id=outlet_id, business_date=business_date)
    shifts = cash_service.shifts_on(
        db, outlet_id=outlet_id, business_date=business_date
    )

    try:
        fuel = fuel_breakdown(db, outlet_id=outlet_id, business_date=business_date)
    except AppError as exc:
        if exc.code != _PRICE_MISSING:
            raise
        fuel = FuelBreakdown(
            lines=[],
            sale_value_total=None,
            gross_fuel_margin_total=None,
            fuels_missing_margin=[],
            quantity_by_unit={},
            incomplete=True,
        )

    reconciles: bool | None = None
    snapshot_metered: Decimal | None = None
    if cash.source is DaySource.snapshot and fuel.sale_value_total is not None:
        snapshot_metered = cash.metered_fuel_sales
        reconciles = snapshot_metered == fuel.sale_value_total
        if not reconciles:
            logger.warning(
                "reporting: live fuel breakdown disagrees with the stored snapshot",
                extra={
                    "business_date": str(business_date),
                    "stored": str(snapshot_metered),
                    "computed": str(fuel.sale_value_total),
                },
            )

    by_category = expense_service.totals_by_category_range(
        db, outlet_id=outlet_id, date_from=business_date, date_to=business_date
    )

    return DailyReport(
        business_date=business_date,
        shifts=shifts,
        cash=cash,
        fuel=fuel,
        expenses_by_category=by_category,
        expenses_total=sum(by_category.values(), _ZERO),
        breakdown_reconciles=reconciles,
        snapshot_metered_fuel_sales=snapshot_metered,
    )


# --- a window of days ----------------------------------------------------------


@dataclass(frozen=True)
class RangeDay:
    """One row of the rolling view."""

    cash: DayCash
    alert: bool
    bar_height_pct: str

    @property
    def business_date(self) -> date:
        return self.cash.business_date


def bar_height(value: Decimal | None, *, largest: Decimal) -> str:
    """A CSS percentage string, computed in `Decimal` on this side of the wire (§14).

    A bar chart is `value / max x height`, which is arithmetic on money -- and §3 rule 1 does
    not stop at the API boundary. Doing it here means the client assigns a string and divides
    nothing, and it also makes the chart **provably** consistent with the table beneath it,
    since both come from this one pass.

    Negative totals are clamped to zero rather than drawn downward: a net-negative day is a
    reversal artefact, and an inverted bar would read as a loss rather than as a correction.
    The table still shows the real signed figure.
    """
    if value is None or largest <= 0:
        return "0.00%"
    clamped = value if value > 0 else _ZERO
    pct = (clamped / largest * Decimal(100)).quantize(
        _MONEY_PLACES, rounding=ROUND_HALF_UP
    )
    return f"{pct}%"


def share_pct(value: Decimal | None, *, total: Decimal | None) -> str | None:
    """One value's share of a total, as a ready-made CSS percentage string (Phase 19).

    `bar_height`'s rule applied to a different shape, and it exists for the identical reason:
    a pie slice, a share bar and a "% of total" label are all `value / total`, which is
    **arithmetic on money**, and §3 rule 1 does not stop at the API boundary (§14). The client
    turns this string into an arc or a width and divides nothing.

    Returns `None`, never `"0.00%"`, when the share is unknowable -- an absent value or an
    absent/zero total. §6.8's rule reaches the geometry too: a slice drawn at zero says "this
    fuel sold nothing", which is a different claim from "we cannot say", and a reader cannot
    tell them apart once it is a wedge.

    Negatives clamp to zero for `bar_height`'s stated reason: a net-negative total is a
    reversal artefact, and an inverted slice has no meaning at all. The table beside it still
    carries the real signed figure.
    """
    if value is None or total is None or total <= 0:
        return None
    clamped = value if value > 0 else _ZERO
    pct = (clamped / total * Decimal(100)).quantize(
        _MONEY_PLACES, rounding=ROUND_HALF_UP
    )
    return f"{pct}%"


def range_report(
    db: Session,
    *,
    outlet_id: UUID,
    date_from: date,
    date_to: date,
    threshold: Decimal,
) -> list[RangeDay]:
    """Every business date in `[date_from, date_to]`, inclusive, in ascending order.

    **Every date, including ones with no trading.** A window that returned only the days
    something happened would make a reader count rows to notice a gap, and the gap is often
    the thing worth noticing.

    O(days x shifts) for unreconciled days -- see §13.24, and the router's cap.
    """
    days = [
        day_cash(db, outlet_id=outlet_id, business_date=current)
        for current in _dates_between(date_from, date_to)
    ]

    # A second pass, because a bar's height depends on the tallest bar in the window.
    largest = max(
        (day.total_sales for day in days if day.total_sales is not None),
        default=_ZERO,
    )

    return [
        RangeDay(
            cash=day,
            alert=variance_is_alerting(day.variance, threshold=threshold),
            bar_height_pct=bar_height(day.total_sales, largest=largest),
        )
        for day in days
    ]


def _dates_between(date_from: date, date_to: date) -> list[date]:
    """Inclusive at both ends. Small enough to materialise -- the router caps the span."""
    span = (date_to - date_from).days
    return [date.fromordinal(date_from.toordinal() + offset) for offset in range(span + 1)]


@dataclass(frozen=True)
class WindowCash:
    """§6.4's terms summed across a window, with the composition that qualifies them.

    Phase 19. Every field here is a sum over `RangeDay.cash`, which means it inherits each
    day's provenance rather than recomputing anything -- a `snapshot` day contributes the
    figures the manager was shown, a `computed` day contributes a live estimate, and
    `days_by_source` is the only thing that can say how much of the total is which (§13.35).

    **Nulls are skipped, not zeroed, and `partial` records that they were.** A day whose cash
    could not be computed (§13.20's `unavailable`) contributes nothing to these sums -- but
    silently dropping it would report a smaller total as though it were a complete one, which
    is §14's "?? 0" one aggregation level up. A reader who sees `partial` knows the window is
    a floor rather than a figure.
    """

    metered_fuel_sales: Decimal
    non_fuel_sales_total: Decimal
    total_sales: Decimal
    card_total: Decimal
    upi_total: Decimal
    wallet_total: Decimal
    credit_sales_total: Decimal
    cash_credit_repayments: Decimal
    card_upi_credit_repayments: Decimal
    cash_expenses: Decimal
    bank_deposits_total: Decimal
    shortfalls_booked: Decimal

    days_by_source: dict[str, int]
    trading_days: int
    partial: bool

    @property
    def cash_sales(self) -> Decimal:
        """§6.4's residual, over the window.

        Deliberately derived here rather than summed from a per-day field, because there is
        no per-day `cash_sales` column to sum -- §6.4 defines it as a residual and §5.2 is
        emphatic that the `cash` collection row is the *declaration* it gets checked against,
        never a term in it. Summing that row instead would double-count the day's cash.
        """
        return (
            self.total_sales
            - self.card_total
            - self.upi_total
            - self.wallet_total
            - self.credit_sales_total
        )


def window_cash(days: list[RangeDay]) -> WindowCash:
    """Sum §6.4's terms across the window, counting how each day was arrived at."""
    fields = (
        "metered_fuel_sales",
        "non_fuel_sales_total",
        "card_total",
        "upi_total",
        "wallet_total",
        "credit_sales_total",
        "cash_credit_repayments",
        "card_upi_credit_repayments",
        "cash_expenses",
        "bank_deposits_total",
        "shortfalls_booked",
    )
    totals = {name: _ZERO for name in fields}
    total_sales = _ZERO
    by_source: dict[str, int] = {source.value: 0 for source in DaySource}
    trading = 0
    partial = False

    for day in days:
        by_source[day.cash.source.value] += 1
        if day.cash.shift_count:
            trading += 1
        if day.cash.source is DaySource.unavailable or day.cash.incomplete:
            partial = True

        for name in fields:
            value = getattr(day.cash, name)
            if value is None:
                # Not zero. See WindowCash's docstring -- the day is skipped and the window
                # is marked partial, so the shortfall is legible rather than absorbed.
                partial = True
                continue
            totals[name] = totals[name] + value

        if day.cash.total_sales is not None:
            total_sales = total_sales + day.cash.total_sales

    return WindowCash(
        total_sales=total_sales,
        days_by_source=by_source,
        trading_days=trading,
        partial=partial,
        **totals,
    )


def variance_is_alerting(variance: Decimal | None, *, threshold: Decimal) -> bool:
    """§13.23's comparison, in one place so the range and the alert list cannot disagree.

    **`None` is not an alert.** A day nobody counted has no variance to be over anything, and
    §6.8's "zero as an answer, never zero as an omission" cuts the same way here: treating
    null as 0 would silently declare every uncounted day perfectly reconciled -- which under
    §6.5's locker model is most days.

    **Strictly `>`**, matching §6.7's and §6.11's boundary convention: landing exactly on the
    threshold is not over it.

    Magnitude, not sign: a surplus of ₹500 is as much a signal as a shortage of ₹500, and
    §6.4 is explicit that variance is recorded rather than corrected in either direction.
    """
    if variance is None:
        return False
    return abs(variance) > threshold


# --- alerts --------------------------------------------------------------------


@dataclass(frozen=True)
class Alert:
    kind: AlertKind
    business_date: date
    detail: str
    amount: Decimal | None = None
    shift_id: UUID | None = None
    count: int | None = None


def alerts(
    db: Session,
    *,
    outlet_id: UUID,
    date_from: date,
    date_to: date,
    threshold: Decimal,
    today: date,
) -> list[Alert]:
    """§13.23's six kinds, derived on every read and never stored.

    Ordered by business date descending then kind, so the list is deterministic and the most
    recent problem is first. Deterministic ordering is not cosmetic: an alert list that
    reshuffles between refreshes is one a manager stops trusting.
    """
    found: list[Alert] = []

    summaries = list(
        db.execute(
            select(DailyCashSummary).where(
                DailyCashSummary.outlet_id == outlet_id,
                DailyCashSummary.business_date >= date_from,
                DailyCashSummary.business_date <= date_to,
            )
        )
        .scalars()
        .all()
    )
    reconciled = {summary.business_date for summary in summaries}

    for summary in summaries:
        if variance_is_alerting(summary.variance, threshold=threshold):
            variance = summary.variance
            assert variance is not None  # narrowed by variance_is_alerting
            direction = "short" if variance < 0 else "surplus"
            found.append(
                Alert(
                    kind=AlertKind.variance_exceeds_threshold,
                    business_date=summary.business_date,
                    detail=(
                        f"Counted cash differs from the expected figure by "
                        f"{abs(variance)} ({direction})."
                    ),
                    amount=variance,
                )
            )
        if summary.requires_review:
            found.append(
                Alert(
                    kind=AlertKind.summary_requires_review,
                    business_date=summary.business_date,
                    detail=summary.review_note
                    or "This day's summary is flagged for review.",
                )
            )

    # Dates that traded and were never reconciled. Read from `shifts`, so a day whose summary
    # was never created is visible rather than simply absent from the summaries table.
    traded = db.execute(
        select(Shift.business_date, func.count(Shift.id))
        .where(
            Shift.outlet_id == outlet_id,
            Shift.business_date >= date_from,
            Shift.business_date <= date_to,
        )
        .group_by(Shift.business_date)
    ).all()
    for business_date, shift_count in traded:
        if business_date in reconciled:
            continue
        found.append(
            Alert(
                kind=AlertKind.day_not_reconciled,
                business_date=business_date,
                detail=(
                    "This day traded but has no daily summary, so its cash was never "
                    "reconciled."
                ),
                count=shift_count,
            )
        )

    # Unreviewed flagged expenses, per shift, through the existing §6.7 predicate rather than
    # a second copy of it. `unreviewed_flagged_expenses` also excludes a flagged row that was
    # later reversed -- money a manager formally cancelled -- and reimplementing the query
    # here would eventually forget that.
    for shift in _shifts_in_range(db, outlet_id=outlet_id, date_from=date_from, date_to=date_to):
        flagged = expense_service.unreviewed_flagged_expenses(db, shift=shift)
        if flagged:
            found.append(
                Alert(
                    kind=AlertKind.unreviewed_flagged_expenses,
                    business_date=shift.business_date,
                    detail=(
                        f"{len(flagged)} flagged expense(s) on this shift have not been "
                        "reviewed. The shift cannot be locked until they are."
                    ),
                    shift_id=shift.id,
                    count=len(flagged),
                )
            )
        if ShiftStatus(shift.status) is ShiftStatus.open and shift.business_date < today:
            found.append(
                Alert(
                    kind=AlertKind.open_shift_on_a_past_date,
                    business_date=shift.business_date,
                    detail=(
                        "This shift is still open on a past business date. Nothing "
                        "downstream of it can be reconciled until it is closed."
                    ),
                    shift_id=shift.id,
                )
            )

    flagged_readings = db.execute(
        select(Shift.business_date, NozzleReading.shift_id, func.count(NozzleReading.id))
        .join(Shift, Shift.id == NozzleReading.shift_id)
        .where(
            Shift.outlet_id == outlet_id,
            Shift.business_date >= date_from,
            Shift.business_date <= date_to,
            NozzleReading.requires_review.is_(True),
        )
        .group_by(Shift.business_date, NozzleReading.shift_id)
    ).all()
    for business_date, shift_id, reading_count in flagged_readings:
        found.append(
            Alert(
                kind=AlertKind.reading_requires_review,
                business_date=business_date,
                detail=(
                    f"{reading_count} nozzle reading(s) on this shift are flagged for "
                    "review -- an opening did not match the chain, or a shift moved "
                    "beneath it."
                ),
                shift_id=shift_id,
                count=reading_count,
            )
        )

    found.sort(key=lambda alert: (alert.business_date, alert.kind), reverse=True)
    return found


def _shifts_in_range(
    db: Session, *, outlet_id: UUID, date_from: date, date_to: date
) -> list[Shift]:
    return list(
        db.execute(
            select(Shift)
            .where(
                Shift.outlet_id == outlet_id,
                Shift.business_date >= date_from,
                Shift.business_date <= date_to,
            )
            .order_by(Shift.business_date, Shift.sequence)
        )
        .scalars()
        .all()
    )
