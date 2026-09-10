"""Reporting: one day, a window of days, and what needs a manager's eyes.

Phase 13. Three `GET` routes, all at the **manager** floor (§8: *"Read all shifts / reports"*
is manager-and-above), all outlet-scoped from `actor.outlet_id`, and **none of them writes**.

## Why this is a separate router from daily_summaries.py

Adding `from`/`to` to `GET /daily-summaries` would have been the smaller change and it is the
wrong one. `tests/test_cash_permissions.py::test_the_daily_summary_is_never_recomputed_on_read`
asserts that that router's read routes contain neither `day_totals` nor `expected_closing=`,
and that test is correct: those routes serve **the record**, and §5.2 stores the record
precisely so nothing recomputes it.

Phase 13 needs routes that *do* compute, for the days that have no record (§13.20). Two
different contracts, two different modules. `daily_summaries.py` is untouched by this phase.

## The three answers, and what each is honest about

* `GET /reports/daily/{business_date}` -- one day's cash (stored or live, `source` says which)
  beside a **live** fuel breakdown, plus §13.22's reconciliation check between the two.
* `GET /reports/range` -- every date in a window, gaps included, with a `bar_height_pct` the
  client assigns rather than computes (§14).
* `GET /reports/variance-alerts` -- §13.23's six kinds, derived on every read, stored nowhere.

## No new error codes

Every refusal here reuses one that already exists: 422 `INVALID_DATE_RANGE` (`/expenses/summary`
introduced it), 422 `BUSINESS_DATE_IN_FUTURE` (§6.1's), and the auth codes. `NO_PRICE_FOR_DATE`
and `NO_MARGIN_FOR_DATE` are **absorbed** by the service and surface as fields on the response,
never as HTTP failures -- a report that 409s because one fuel lacks a commission would be
useless at the outlet it was written for (§14's open questions).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import Actor, require_role
from app.core.cash import OpeningBalanceSource
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.roles import Role
from app.db.session import get_db
from app.services import expenses as expense_service
from app.services import reporting
from app.services import shifts as shift_service
from app.services.reporting import AlertKind, DaySource

router = APIRouter(tags=["reports"])

# Deliberately tighter than `/expenses/summary`'s 366 days. That endpoint reads rows; this one
# may run a full §6.4 pass per unreconciled day (§13.24), so the bound is a cost ceiling rather
# than only a scan ceiling. 31 covers both §11's 7-day view and a calendar month.
_MAX_REPORT_RANGE_DAYS = 31

# §11's "7-day rolling view", as the default window when the caller names neither end.
_DEFAULT_WINDOW_DAYS = 7

# Phase 19. The summary dashboard walks readings rather than re-running a §6.4 cash pass per
# unreconciled day, so its ceiling is a different cost from the one above and matches
# `/expenses/summary`'s 366 rather than inheriting 31. §13.34 records what a full year costs,
# and why the two caps must not be collapsed into one number: they bound different work.
_MAX_SUMMARY_RANGE_DAYS = 366
_DEFAULT_SUMMARY_WINDOW_DAYS = 30

_PROFIT_BASIS = (
    "Gross fuel margin on quantity sold: quantity x margin_at(fuel, shift start). This is "
    "NOT business profit. It excludes stock revaluation -- holding 12 kL when the rate rises "
    "₹1 is a real ₹12,000 gain this system never sees, because nothing moved through a "
    "nozzle -- and it excludes non-fuel income and the IOCL ledger entirely (CLAUDE.md "
    "§4.6, §13.7, §13.9). A fuel whose margin has never been entered reports null, never 0."
)

_FUEL_BASIS = (
    "Quantities are grouped by the fuel's own unit_of_measure and are NEVER summed across "
    "units -- litres and kilograms do not add (§4.5). The fuel breakdown is always computed "
    "live, even for a finalised day, because daily_cash_summaries stores metered_fuel_sales "
    "as a single number with no per-fuel split (§13.22)."
)

_CASH_BASIS = (
    "source=snapshot means every figure was read verbatim from the stored daily summary -- "
    "what the manager was told on the day (§5.2). source=computed means no summary exists "
    "and these were derived just now from the shifts, so they can still move. They are "
    "different kinds of claim and must not be compared as though they were the same "
    "(§13.20). Null is never zero: a null expected_closing means the §6.5 chain has no "
    "anchor, and a null variance means nobody counted."
)

_WINDOW_BASIS = (
    "Every figure here is a sum over the days in the window, and each day contributed "
    "whatever kind of claim it already was: a reconciled day contributes the figures the "
    "manager was shown on the day, an unreconciled one contributes an estimate derived just "
    "now. days_by_source is the only thing that can say how much of a total is which, and "
    "two windows are only comparable when their compositions are (§13.20, §13.35). "
    "partial=true means at least one day could not be computed or is still missing readings, "
    "so the totals are a floor rather than a complete figure -- a skipped day is never "
    "counted as zero."
)

_ALERTS_BASIS = (
    "Derived on every read from figures and flags that are already stored; nothing here is "
    "persisted and there is no per-alert dismiss (§13.23). Clear an alert by acting on the "
    "row it points at. A variance alert fires on magnitude, strictly above the threshold, in "
    "either direction -- a surplus usually means an unrecorded udhaar slip. A day nobody "
    "counted has a null variance and is NOT a variance alert."
)


# --- schemas ------------------------------------------------------------------


class ShiftBrief(BaseModel):
    """Just enough of a shift to name it on a report and link to it."""

    id: UUID
    sequence: int
    status: str
    attendant_id: UUID
    started_at: str
    ended_at: str | None


class FuelLineResponse(BaseModel):
    fuel_type_id: UUID
    code: str
    display_name: str
    unit_of_measure: str
    quantity: Decimal | None
    rate_per_unit: Decimal | None
    sale_value: Decimal | None
    margin_per_unit: Decimal | None
    gross_fuel_margin: Decimal | None
    margin_unavailable_reason: str | None


class DayCashResponse(BaseModel):
    """§6.4's terms for a whole business date, with §13.20's provenance attached.

    Every money field is `Decimal | None`, and the null is load-bearing throughout. §14: a
    client that writes `?? 0` here has converted "nobody counted" into "counted and found
    nothing wrong", which is the single most dangerous two characters this project can write
    in a browser.
    """

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
    total_sales: Decimal | None
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


class DailyReportResponse(BaseModel):
    business_date: date
    shifts: list[ShiftBrief]
    cash: DayCashResponse

    fuel: list[FuelLineResponse]
    fuel_sales_total: Decimal | None
    gross_fuel_margin_total: Decimal | None
    fuels_missing_margin: list[str]
    quantity_by_unit: dict[str, Decimal]

    # §13.22. `None` on a non-snapshot day -- there is nothing stored to reconcile against,
    # and `false` would read as "these two disagree".
    breakdown_reconciles: bool | None
    snapshot_metered_fuel_sales: Decimal | None

    expenses_by_category: dict[str, Decimal]
    expenses_total: Decimal

    cash_basis: str = _CASH_BASIS
    fuel_basis: str = _FUEL_BASIS
    profit_basis: str = _PROFIT_BASIS


class RangeDayResponse(BaseModel):
    business_date: date
    source: DaySource
    unavailable_reason: str | None
    shift_count: int

    metered_fuel_sales: Decimal | None
    non_fuel_sales_total: Decimal | None
    total_sales: Decimal | None
    expected_closing: Decimal | None
    actual_counted: Decimal | None
    variance: Decimal | None

    is_finalised: bool
    requires_review: bool
    alert: bool

    # A presentation hint, and the reason it exists is a rule rather than a convenience.
    # A bar chart is `value / max x height`, which is arithmetic on money -- forbidden in
    # JavaScript by §14, because JS has no decimal type. Computing it server-side in Decimal
    # means the client assigns this string and divides nothing, and it makes the chart
    # provably consistent with the table beneath it, since both come from one pass.
    bar_height_pct: str


class RangeReportResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # `from` is a Python keyword, so the field is `from_` and the client sees "from" --
    # `/expenses/summary`'s precedent, echoed back exactly as sent.
    from_: str = Field(alias="from")
    to: str
    threshold: Decimal
    days: list[RangeDayResponse]
    basis: str = _CASH_BASIS


class SummaryFuelLineResponse(BaseModel):
    """One fuel's contribution to the whole window.

    `share_pct` is a ready-made CSS percentage string, for the reason `bar_height_pct` is
    (§14): a pie slice is `value / total`, which is arithmetic on money, and the client is
    forbidden from performing it. `None` means the share is unknowable -- never "0.00%",
    which would draw a wedge asserting this fuel sold nothing.
    """

    fuel_type_id: UUID
    code: str
    display_name: str
    unit_of_measure: str
    quantity: Decimal | None
    rate_per_unit: Decimal | None
    sale_value: Decimal | None
    margin_per_unit: Decimal | None
    gross_fuel_margin: Decimal | None
    margin_unavailable_reason: str | None
    share_pct: str | None


class SummaryCategoryResponse(BaseModel):
    code: str
    amount: Decimal
    share_pct: str | None


class SummaryTrendDayResponse(BaseModel):
    """One column of the trend chart. A thinner `RangeDayResponse` -- the dashboard plots the
    day and links to it, and the full per-day figures already have their own endpoint."""

    business_date: date
    source: DaySource
    total_sales: Decimal | None
    bar_height_pct: str
    alert: bool


class SummaryResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    to: str

    # §13.35. A window mixing reconciled and unreconciled days is part record and part live
    # estimate, and the totals below cannot say which. These two fields are the only thing
    # that can, which is why they are part of the contract rather than a debugging aid.
    days_by_source: dict[str, int]
    trading_days: int
    partial: bool

    fuel: list[SummaryFuelLineResponse]
    fuel_sales_total: Decimal | None
    gross_fuel_margin_total: Decimal | None
    fuels_missing_margin: list[str]
    quantity_by_unit: dict[str, Decimal]

    metered_fuel_sales: Decimal
    non_fuel_sales_total: Decimal
    total_sales: Decimal

    cash_sales: Decimal
    card_total: Decimal
    upi_total: Decimal
    wallet_total: Decimal
    credit_sales_total: Decimal
    cash_credit_repayments: Decimal
    card_upi_credit_repayments: Decimal
    payment_mix: list[SummaryCategoryResponse]

    expenses_by_category: list[SummaryCategoryResponse]
    expenses_total: Decimal
    bank_deposits_total: Decimal
    shortfalls_booked: Decimal

    trend: list[SummaryTrendDayResponse]

    cash_basis: str = _CASH_BASIS
    fuel_basis: str = _FUEL_BASIS
    profit_basis: str = _PROFIT_BASIS
    window_basis: str = _WINDOW_BASIS


class AlertResponse(BaseModel):
    kind: AlertKind
    business_date: date
    detail: str
    amount: Decimal | None
    shift_id: UUID | None
    count: int | None


class AlertsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    to: str
    threshold: Decimal
    items: list[AlertResponse]
    basis: str = _ALERTS_BASIS


# --- helpers ------------------------------------------------------------------


def _resolve_window(
    date_from: date | None,
    date_to: date | None,
    settings: Settings,
    db: Session,
    outlet_id: UUID,
    *,
    max_days: int = _MAX_REPORT_RANGE_DAYS,
    default_days: int = _DEFAULT_WINDOW_DAYS,
) -> tuple[date, date]:
    """Fill in the defaults, then refuse the three bad windows.

    **The default `to` is the outlet's most recent trading day, not today** -- §13.30. §4.7
    says the whole day is typed in after the fact, in one sitting, often long afterwards, so
    an outlet catching up on July in late August was shown seven days of `no_trading`: a
    report about a week in which nothing happened. Worse, `/reports/variance-alerts` is
    windowed (§13.23), so the `day_not_reconciled` alert that exists for exactly the day
    somebody was hunting for was hidden by the same default.

    For a live outlet this changes nothing at all, because the most recent trading day *is*
    today. It differs only for an outlet that is behind, which is the one this was written for.

    **When it falls back, it falls back to today at the outlet, not today in UTC.** §6.1's
    rule, and the original reason this helper existed: at 23:00 IST the UTC date is still
    yesterday, so a UTC default would silently drop the current trading day from every
    evening's report -- the one day a manager is most likely to be looking at.
    """
    today = shift_service.outlet_today(settings.TZ_DISPLAY)

    if date_to is None:
        latest = shift_service.latest_shift(db, outlet_id=outlet_id)
        # `min(..., today)` is belt and braces rather than a live case: §6.1 already refuses a
        # future `business_date` at the point a shift is opened. It is here so that the
        # BUSINESS_DATE_IN_FUTURE guard below can stay a statement about what the *caller*
        # asked for, and never fire on a default this function chose for itself.
        date_to = min(latest.business_date, today) if latest is not None else today
    if date_from is None:
        date_from = date_to - timedelta(days=default_days - 1)

    if date_from > date_to:
        raise AppError(
            status_code=422,
            code="INVALID_DATE_RANGE",
            detail="`from` must not be after `to`.",
        )
    if (date_to - date_from).days + 1 > max_days:
        raise AppError(
            status_code=422,
            code="INVALID_DATE_RANGE",
            detail=(
                f"The range cannot exceed {max_days} days. A longer window would cost more "
                "to compute than a report should (§13.24, §13.34)."
            ),
        )
    if date_to > today:
        raise AppError(
            status_code=422,
            code="BUSINESS_DATE_IN_FUTURE",
            detail=(
                "That date is in the future. Trading has not happened yet, so there is "
                "nothing to report."
            ),
        )

    return date_from, date_to


def _cash_to_response(cash: reporting.DayCash) -> DayCashResponse:
    return DayCashResponse(
        source=cash.source,
        unavailable_reason=cash.unavailable_reason,
        shift_count=cash.shift_count,
        opening_balance=cash.opening_balance,
        opening_balance_source=cash.opening_balance_source,
        expected_closing=cash.expected_closing,
        actual_counted=cash.actual_counted,
        variance=cash.variance,
        metered_fuel_sales=cash.metered_fuel_sales,
        non_fuel_sales_total=cash.non_fuel_sales_total,
        total_sales=cash.total_sales,
        card_total=cash.card_total,
        upi_total=cash.upi_total,
        wallet_total=cash.wallet_total,
        credit_sales_total=cash.credit_sales_total,
        cash_credit_repayments=cash.cash_credit_repayments,
        card_upi_credit_repayments=cash.card_upi_credit_repayments,
        cash_shortfall_settlements=cash.cash_shortfall_settlements,
        cash_expenses=cash.cash_expenses,
        bank_deposits_total=cash.bank_deposits_total,
        shortfalls_booked=cash.shortfalls_booked,
        is_finalised=cash.is_finalised,
        requires_review=cash.requires_review,
        review_note=cash.review_note,
        incomplete=cash.incomplete,
    )


# --- routes -------------------------------------------------------------------
#
# Static paths first. `/reports/range` and `/reports/variance-alerts` do not actually collide
# with `/reports/daily/{business_date}` -- that one is a segment deeper behind a static
# `daily` -- but declaring them first matches the posture `fuel_prices.py`'s "/current" and
# `credit_customers.py`'s "/outstanding" take, where the hazard is real. One ordering rule is
# easier to keep than two.


@router.get("/reports/range", response_model=RangeReportResponse)
def read_range_report(
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    actor: Actor = Depends(require_role(Role.manager)),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> RangeReportResponse:
    """§11's 7-day rolling view, over any window up to 31 days.

    **Every date in the window is returned, including ones with no trading.** A report that
    listed only the days something happened would make a reader count rows to notice a gap --
    and under §6.5's locker model the gap is often the thing worth noticing.
    """
    start, end = _resolve_window(date_from, date_to, settings, db, actor.outlet_id)
    threshold = settings.VARIANCE_ALERT_THRESHOLD

    days = reporting.range_report(
        db,
        outlet_id=actor.outlet_id,
        date_from=start,
        date_to=end,
        threshold=threshold,
    )

    return RangeReportResponse(
        from_=start.isoformat(),
        to=end.isoformat(),
        threshold=threshold,
        days=[
            RangeDayResponse(
                business_date=day.business_date,
                source=day.cash.source,
                unavailable_reason=day.cash.unavailable_reason,
                shift_count=day.cash.shift_count,
                metered_fuel_sales=day.cash.metered_fuel_sales,
                non_fuel_sales_total=day.cash.non_fuel_sales_total,
                total_sales=day.cash.total_sales,
                expected_closing=day.cash.expected_closing,
                actual_counted=day.cash.actual_counted,
                variance=day.cash.variance,
                is_finalised=day.cash.is_finalised,
                requires_review=day.cash.requires_review,
                alert=day.alert,
                bar_height_pct=day.bar_height_pct,
            )
            for day in days
        ],
    )


@router.get("/reports/summary", response_model=SummaryResponse)
def read_summary(
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    actor: Actor = Depends(require_role(Role.manager)),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> SummaryResponse:
    """Phase 19's dashboard: one window, every figure, in one pass.

    **Why this is one endpoint and not four.** Every cross-panel figure a dashboard shows --
    a fuel's share of sales, a category's share of expenses -- is a division of money, which
    §14 forbids in the client. So the shares are computed here, in `Decimal`, beside the
    values they are shares *of*. Four endpoints stitched together in JavaScript would be four
    passes free to disagree, and the disagreement would surface as percentages that do not
    sum to 100 with no obvious cause.

    **The window is capped at 366 days, not 31.** This walks readings; `/reports/range`
    re-runs a full §6.4 cash pass per unreconciled day. Different work, different ceiling --
    §13.34. The rate lookup is memoised for the life of this call (`pricing.LookupCache`),
    which is what makes a year affordable at all.

    **It writes nothing**, like every other route in this module.
    """
    start, end = _resolve_window(
        date_from,
        date_to,
        settings,
        db,
        actor.outlet_id,
        max_days=_MAX_SUMMARY_RANGE_DAYS,
        default_days=_DEFAULT_SUMMARY_WINDOW_DAYS,
    )
    threshold = settings.VARIANCE_ALERT_THRESHOLD

    days = reporting.range_report(
        db,
        outlet_id=actor.outlet_id,
        date_from=start,
        date_to=end,
        threshold=threshold,
    )
    cash = reporting.window_cash(days)

    fuel = reporting.fuel_breakdown_range(
        db, outlet_id=actor.outlet_id, date_from=start, date_to=end
    )
    by_category = expense_service.totals_by_category_range(
        db, outlet_id=actor.outlet_id, date_from=start, date_to=end
    )
    expenses_total = sum(by_category.values(), Decimal("0.00"))

    # Shares are taken against the fuel total rather than against total sales, so the slices
    # of the fuel chart sum to 100% of *fuel*. Mixing non-fuel income into that denominator
    # would make a pie of four fuels that visibly does not close.
    fuel_lines = [
        SummaryFuelLineResponse(
            fuel_type_id=line.fuel_type.id,
            code=str(line.fuel_type.code),
            display_name=str(line.fuel_type.display_name),
            unit_of_measure=line.unit_of_measure,
            quantity=line.quantity,
            rate_per_unit=line.rate_per_unit,
            sale_value=line.sale_value,
            margin_per_unit=line.margin_per_unit,
            gross_fuel_margin=line.gross_fuel_margin,
            margin_unavailable_reason=line.margin_unavailable_reason,
            share_pct=reporting.share_pct(
                line.sale_value, total=fuel.sale_value_total
            ),
        )
        for line in fuel.lines
    ]

    # §6.4's five ways money arrives, as the client will draw them. Ordered largest-first is
    # deliberately NOT done here: a stable order across reloads makes the chart comparable
    # between two windows, and a colour that moves between renders is worse than a long bar.
    mix_rows = [
        ("cash", cash.cash_sales),
        ("card", cash.card_total),
        ("upi", cash.upi_total),
        ("wallet", cash.wallet_total),
        ("credit", cash.credit_sales_total),
    ]
    payment_mix = [
        SummaryCategoryResponse(
            code=code,
            amount=amount,
            share_pct=reporting.share_pct(amount, total=cash.total_sales),
        )
        for code, amount in mix_rows
    ]

    expenses = [
        SummaryCategoryResponse(
            code=code,
            amount=amount,
            share_pct=reporting.share_pct(amount, total=expenses_total),
        )
        for code, amount in sorted(by_category.items())
    ]

    return SummaryResponse(
        from_=start.isoformat(),
        to=end.isoformat(),
        days_by_source=cash.days_by_source,
        trading_days=cash.trading_days,
        partial=cash.partial,
        fuel=fuel_lines,
        fuel_sales_total=fuel.sale_value_total,
        gross_fuel_margin_total=fuel.gross_fuel_margin_total,
        fuels_missing_margin=fuel.fuels_missing_margin,
        quantity_by_unit=fuel.quantity_by_unit,
        metered_fuel_sales=cash.metered_fuel_sales,
        non_fuel_sales_total=cash.non_fuel_sales_total,
        total_sales=cash.total_sales,
        cash_sales=cash.cash_sales,
        card_total=cash.card_total,
        upi_total=cash.upi_total,
        wallet_total=cash.wallet_total,
        credit_sales_total=cash.credit_sales_total,
        cash_credit_repayments=cash.cash_credit_repayments,
        card_upi_credit_repayments=cash.card_upi_credit_repayments,
        payment_mix=payment_mix,
        expenses_by_category=expenses,
        expenses_total=expenses_total,
        bank_deposits_total=cash.bank_deposits_total,
        shortfalls_booked=cash.shortfalls_booked,
        trend=[
            SummaryTrendDayResponse(
                business_date=day.business_date,
                source=day.cash.source,
                total_sales=day.cash.total_sales,
                bar_height_pct=day.bar_height_pct,
                alert=day.alert,
            )
            for day in days
        ],
    )


@router.get("/reports/variance-alerts", response_model=AlertsResponse)
def read_variance_alerts(
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    actor: Actor = Depends(require_role(Role.manager)),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> AlertsResponse:
    """§13.23's six kinds, derived and never stored.

    Windowed, with the consequence stated in §13.23 rather than hidden: a flag older than the
    window is not surfaced here. The dedicated queues -- `/expenses/flagged` among them --
    remain the complete view, and this is the digest.
    """
    start, end = _resolve_window(date_from, date_to, settings, db, actor.outlet_id)
    threshold = settings.VARIANCE_ALERT_THRESHOLD

    found = reporting.alerts(
        db,
        outlet_id=actor.outlet_id,
        date_from=start,
        date_to=end,
        threshold=threshold,
        today=shift_service.outlet_today(settings.TZ_DISPLAY),
    )

    return AlertsResponse(
        from_=start.isoformat(),
        to=end.isoformat(),
        threshold=threshold,
        items=[
            AlertResponse(
                kind=alert.kind,
                business_date=alert.business_date,
                detail=alert.detail,
                amount=alert.amount,
                shift_id=alert.shift_id,
                count=alert.count,
            )
            for alert in found
        ],
    )


@router.get("/reports/daily/{business_date}", response_model=DailyReportResponse)
def read_daily_report(
    business_date: date,
    actor: Actor = Depends(require_role(Role.manager)),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> DailyReportResponse:
    """One business date, end to end.

    **Returns 200 for a date that never traded**, rather than 404. A quiet Sunday is a real
    answer to "how did this day go", and a 404 would make a reader wonder whether the date was
    wrong. `source = no_trading` says it plainly.

    A future date is refused (§6.1) -- there is nothing to report on a day that has not
    happened, and asking is a data-entry error rather than an empty result.
    """
    today = shift_service.outlet_today(settings.TZ_DISPLAY)
    if business_date > today:
        raise AppError(
            status_code=422,
            code="BUSINESS_DATE_IN_FUTURE",
            detail=(
                "That business date is in the future. Trading has not happened yet, so "
                "there is nothing to report."
            ),
        )

    report = reporting.daily_report(
        db, outlet_id=actor.outlet_id, business_date=business_date
    )

    return DailyReportResponse(
        business_date=report.business_date,
        shifts=[
            ShiftBrief(
                id=shift.id,
                sequence=shift.sequence,
                status=shift.status,
                attendant_id=shift.attendant_id,
                started_at=shift.started_at.isoformat(),
                ended_at=shift.ended_at.isoformat() if shift.ended_at else None,
            )
            for shift in report.shifts
        ],
        cash=_cash_to_response(report.cash),
        fuel=[
            FuelLineResponse(
                fuel_type_id=line.fuel_type.id,
                code=line.fuel_type.code,
                display_name=line.fuel_type.display_name,
                unit_of_measure=line.unit_of_measure,
                quantity=line.quantity,
                rate_per_unit=line.rate_per_unit,
                sale_value=line.sale_value,
                margin_per_unit=line.margin_per_unit,
                gross_fuel_margin=line.gross_fuel_margin,
                margin_unavailable_reason=line.margin_unavailable_reason,
            )
            for line in report.fuel.lines
        ],
        fuel_sales_total=report.fuel.sale_value_total,
        gross_fuel_margin_total=report.fuel.gross_fuel_margin_total,
        fuels_missing_margin=report.fuel.fuels_missing_margin,
        quantity_by_unit=report.fuel.quantity_by_unit,
        breakdown_reconciles=report.breakdown_reconciles,
        snapshot_metered_fuel_sales=report.snapshot_metered_fuel_sales,
        expenses_by_category=report.expenses_by_category,
        expenses_total=report.expenses_total,
    )
