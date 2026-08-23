"""The reporting service: provenance, profit, and the two things that must never be zero.

Phase 13, Step 3. These tests reach `app/services/reporting.py` directly rather than through
HTTP, because the decisions worth pinning here are about *arithmetic and provenance*, and a
router test would prove them through two layers of serialisation.

Three groups matter more than the rest:

`test_a_snapshot_is_read_verbatim_even_after_a_price_revision` is §13.20's whole argument. If
it ever fails, a report has started recomputing what §5.2 stores to prevent recomputation.

`test_a_backdated_price_makes_the_breakdown_disagree_with_the_snapshot` is §13.22, and it is
the only place in this codebase that notices a closed day being revalued.

`test_a_fuel_with_no_margin_reports_none_and_withholds_the_total` is §13.21. A zero there is
the plausible-but-wrong number CLAUDE.md opens by warning about.

## One fixture-ordering trap, written down because it cost an hour

**`priced_fuel` / `margined_fuel` must be named BEFORE any direct `make_fuel_price`,
`make_fuel_margin` or `make_fuel_type` in a test's parameter list.**

pytest tears fixtures down in reverse *setup* order. Naming `make_fuel_price` first means
`make_fuel_type` -- pulled in later, by `priced_fuel` -- is set up later and therefore torn
down **first**, while its price rows still exist. Its teardown then runs
`DELETE FROM fuel_prices`, which the append-only trigger refuses (§5.1), the teardown aborts,
and the fuel type survives to collide on `uq_fuel_types_code` in the *next* test. The symptom
is a dozen unrelated setup errors several tests later, which points nowhere near the cause.

`conftest.py:460` already documents this exact hazard for the `make_reading` / `make_fuel_type`
pair. It is the same trap with different actors, and the fix is the same: order the parameters
so the composite fixture's dependencies are established first.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from app.core.cash import OpeningBalanceSource
from app.services import reporting
from app.services.reporting import DaySource

pytestmark = pytest.mark.anyio

DAY = date(2027, 3, 10)
BEFORE = datetime(2026, 1, 1, tzinfo=timezone.utc)
THRESHOLD = Decimal("100.00")


def _window(day: date) -> tuple[datetime, datetime]:
    """This outlet trades 06:00 -> 22:00 IST, i.e. 00:30 -> 16:30 UTC."""
    start = datetime(day.year, day.month, day.day, 0, 30, tzinfo=timezone.utc)
    return start, start + timedelta(hours=16)


def _outlet() -> UUID:
    from app.core.config import get_settings

    return get_settings().DEFAULT_OUTLET_ID


def _call(fn, **kwargs):
    """Run a service function in its own session, the way `test_sales_valuation.py` does.

    A fresh session per call rather than a shared one, deliberately: these tests write their
    fixtures through `engine.begin()` and a long-lived session would serve them from its
    identity map instead of re-reading, which is exactly what would hide a stale read.
    """
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        return fn(session, **kwargs)


@pytest.fixture
def priced_fuel(make_fuel_type, make_fuel_price, make_user):
    """Priced and **deliberately unmargined** -- §14's standing open question, and the state
    this outlet is genuinely in for petrol and diesel."""
    admin = make_user("admin")
    fuel = make_fuel_type(code="REPFUEL", unit_of_measure="litre")
    make_fuel_price(fuel, "100.00", BEFORE, entered_by=admin)
    return fuel


@pytest.fixture
def margined_fuel(make_fuel_type, make_fuel_price, make_fuel_margin, make_user):
    """Priced *and* margined, standing in for CBG -- the one fuel here that has a commission."""
    admin = make_user("admin")
    fuel = make_fuel_type(code="REPGAS", unit_of_measure="kilogram")
    make_fuel_price(fuel, "80.00", BEFORE, entered_by=admin)
    make_fuel_margin(fuel, "2.28", BEFORE, entered_by=admin)
    return fuel


# --- §13.20: snapshot vs live -------------------------------------------------


async def test_a_day_with_a_summary_reads_as_a_snapshot(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    clean_cash: None,
) -> None:
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended,
               status="closed")
    make_daily_summary(
        business_date=DAY,
        opening_balance="1000.00",
        expected_closing="9000.00",
        metered_fuel_sales="8000.00",
    )

    cash = _call(reporting.day_cash, outlet_id=_outlet(), business_date=DAY)

    assert cash.source is DaySource.snapshot
    assert cash.expected_closing == Decimal("9000.00")
    assert cash.metered_fuel_sales == Decimal("8000.00")
    assert cash.shift_count == 1


async def test_a_day_with_shifts_and_no_summary_is_computed(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    priced_fuel,
    clean_cash: None,
) -> None:
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel), opening_reading="1000.00",
                 closing_reading="1100.00")
    # An anchor on an earlier date, so §6.5's chain has somewhere to start.
    make_daily_summary(business_date=DAY - timedelta(days=1), expected_closing="500.00")

    cash = _call(reporting.day_cash, outlet_id=_outlet(), business_date=DAY)

    assert cash.source is DaySource.computed
    assert cash.metered_fuel_sales == Decimal("10000.00")
    assert cash.opening_balance == Decimal("500.00")
    assert cash.opening_balance_source is OpeningBalanceSource.carried
    assert cash.expected_closing == Decimal("10500.00")


async def test_a_date_with_no_shifts_is_no_trading_with_zero_sales_and_null_reconciliation(
    clean_cash: None,
) -> None:
    """The distinction §6.8 draws, applied to a whole day.

    Sales are a genuine zero -- nothing was dispensed. `expected_closing` is **null**, because
    nobody reconciled: the locker still holds whatever it held, and `0.00` there would claim
    it had been emptied.
    """
    cash = _call(reporting.day_cash, outlet_id=_outlet(), business_date=DAY)

    assert cash.source is DaySource.no_trading
    assert cash.metered_fuel_sales == Decimal("0.00")
    assert cash.non_fuel_sales_total == Decimal("0.00")
    assert cash.expected_closing is None
    assert cash.actual_counted is None
    assert cash.variance is None
    assert cash.shift_count == 0


async def test_a_snapshot_is_read_verbatim_even_after_a_price_revision(
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    make_daily_summary: Callable[..., UUID],
    make_fuel_price: Callable[..., UUID],
    clean_cash: None,
) -> None:
    """§13.20 and §14's new guardrail, as one assertion.

    The stored figure is what the manager was told. Doubling the rate afterwards must not move
    it by a paisa -- if it does, the report has become a second opinion wearing the record's
    clothes, and §6.5 chains days so the rewrite would not even stay local.
    """
    admin = make_user("admin")
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended,
                       status="closed")
    make_reading(shift, make_nozzle(priced_fuel), opening_reading="1000.00",
                 closing_reading="1100.00")
    make_daily_summary(
        business_date=DAY, expected_closing="10000.00", metered_fuel_sales="10000.00"
    )

    # A revision stamped BEFORE the shift started: a backdated price, which the router permits
    # and only warns about (§11).
    make_fuel_price(priced_fuel, "200.00", BEFORE + timedelta(days=1), entered_by=admin)

    cash = _call(reporting.day_cash, outlet_id=_outlet(), business_date=DAY)

    assert cash.source is DaySource.snapshot
    assert cash.metered_fuel_sales == Decimal("10000.00")
    assert cash.expected_closing == Decimal("10000.00")


async def test_a_backdated_price_makes_the_breakdown_disagree_with_the_snapshot(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    make_daily_summary: Callable[..., UUID],
    make_fuel_price: Callable[..., UUID],
    clean_cash: None,
) -> None:
    """§13.22, and the one control in this phase that did not exist before it.

    The fuel breakdown has no snapshot to read -- `daily_cash_summaries` holds one number with
    no per-fuel split -- so it is computed now against a total frozen then. A backdated
    revision makes them disagree, and §11 names that as precisely the hazard `fuel_prices`
    needed an audit trail for. Nothing else in this system notices it.
    """
    admin = make_user("admin")
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended,
                       status="closed")
    make_reading(shift, make_nozzle(priced_fuel), opening_reading="1000.00",
                 closing_reading="1100.00")
    make_daily_summary(
        business_date=DAY, expected_closing="10000.00", metered_fuel_sales="10000.00"
    )
    make_fuel_price(priced_fuel, "200.00", BEFORE + timedelta(days=1), entered_by=admin)

    report = _call(reporting.daily_report, outlet_id=_outlet(), business_date=DAY)

    assert report.breakdown_reconciles is False
    # Both figures survive to the caller. Picking a winner here would throw away the half a
    # human can actually check (§5.2).
    assert report.snapshot_metered_fuel_sales == Decimal("10000.00")
    assert report.fuel.sale_value_total == Decimal("20000.00")


async def test_an_untouched_snapshot_reconciles(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    priced_fuel,
    clean_cash: None,
) -> None:
    """The other half of the pair. Without this, the test above could pass because
    `breakdown_reconciles` was hardcoded to False."""
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended,
                       status="closed")
    make_reading(shift, make_nozzle(priced_fuel), opening_reading="1000.00",
                 closing_reading="1100.00")
    make_daily_summary(
        business_date=DAY, expected_closing="10000.00", metered_fuel_sales="10000.00"
    )

    report = _call(reporting.daily_report, outlet_id=_outlet(), business_date=DAY)

    assert report.breakdown_reconciles is True


async def test_breakdown_reconciles_is_null_on_a_computed_day(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    clean_cash: None,
) -> None:
    """`None`, not `False`. There is nothing stored to reconcile against, and `False` would
    read as "these two disagree" -- a claim about a comparison that never happened."""
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel), opening_reading="1000.00",
                 closing_reading="1100.00")

    report = _call(reporting.daily_report, outlet_id=_outlet(), business_date=DAY)

    assert report.breakdown_reconciles is None
    assert report.snapshot_metered_fuel_sales is None


# --- §13.21: profit per fuel, total withheld ----------------------------------


async def test_a_fuel_with_no_margin_reports_none_and_withholds_the_total(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    clean_cash: None,
) -> None:
    """§13.21. The figure must be `None`, never `Decimal("0.00")`.

    Zero would say the day earned nothing, which is a claim. `None` says nobody has entered
    the commission, which is the truth -- and §14 records that for petrol and diesel here,
    nobody has.
    """
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel), opening_reading="1000.00",
                 closing_reading="1100.00")

    breakdown = _call(reporting.fuel_breakdown, outlet_id=_outlet(), business_date=DAY)

    assert len(breakdown.lines) == 1
    line = breakdown.lines[0]
    assert line.gross_fuel_margin is None
    assert line.margin_per_unit is None
    assert line.margin_unavailable_reason == "NO_MARGIN_FOR_DATE"
    # The value half is unaffected -- §6.3's split, one phase later.
    assert line.sale_value == Decimal("10000.00")
    assert breakdown.gross_fuel_margin_total is None
    assert breakdown.fuels_missing_margin == ["REPFUEL"]


async def test_a_margined_fuel_reports_a_real_figure_and_a_real_total(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    margined_fuel,
    clean_cash: None,
) -> None:
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(margined_fuel), opening_reading="0.00",
                 closing_reading="100.00")

    breakdown = _call(reporting.fuel_breakdown, outlet_id=_outlet(), business_date=DAY)

    line = breakdown.lines[0]
    assert line.margin_per_unit == Decimal("2.28")
    assert line.gross_fuel_margin == Decimal("228.00")
    assert breakdown.gross_fuel_margin_total == Decimal("228.00")
    assert breakdown.fuels_missing_margin == []


async def test_one_unmargined_fuel_withholds_the_total_for_the_whole_day(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    margined_fuel,
    clean_cash: None,
) -> None:
    """The mixed case, which is the real one at this outlet: CBG has a commission and petrol
    does not. The margined fuel keeps its own figure; the DAY's total is withheld.

    Summing what is known and calling it the total would report ₹228 of margin on a day that
    also sold ₹10,000 of petrol -- smaller than the truth, and indistinguishable from it.
    """
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel, label="DU-1/N-1"),
                 opening_reading="1000.00", closing_reading="1100.00")
    make_reading(shift, make_nozzle(margined_fuel, label="DU-2/N-1"),
                 opening_reading="0.00", closing_reading="100.00")

    breakdown = _call(reporting.fuel_breakdown, outlet_id=_outlet(), business_date=DAY)

    by_code = {str(line.fuel_type.code): line for line in breakdown.lines}
    assert by_code["REPGAS"].gross_fuel_margin == Decimal("228.00")
    assert by_code["REPFUEL"].gross_fuel_margin is None
    assert breakdown.gross_fuel_margin_total is None
    assert breakdown.fuels_missing_margin == ["REPFUEL"]
    # The sale value total is NOT withheld -- only the margin is unknowable.
    assert breakdown.sale_value_total == Decimal("18000.00")


def test_a_zero_margin_cannot_exist_which_is_why_no_test_asserts_one(
    engine: Engine,
) -> None:
    """Why there is no "a ₹0.00 margin is a real margin" test above, written down rather than
    left as an absence somebody later fills in wrongly.

    The obvious hazard in `fuel_breakdown` is implementing "no margin" as `if not margin`,
    because `Decimal("0.00")` is falsy -- a fuel sold at exactly cost would then be reported
    as having no commission entered, and the whole day's total would be withheld over a
    figure that is perfectly well known.

    **The schema makes that unreachable**, so the test that would prove it cannot be written
    from data: `ck_fuel_margins_margin_positive` refuses the row. This asserts the constraint
    instead, so that if anybody ever relaxes it, they land here and read the reasoning rather
    than discovering the truthiness bug through a wrong report.

    The service still uses `is None` rather than truthiness. That is a code-level choice this
    test cannot see, and it is deliberate: correctness that depends on a CHECK constraint two
    tables away is correctness waiting to expire.
    """
    with engine.connect() as connection:
        found = connection.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'fuel_margins'::regclass AND contype = 'c'"
            )
        ).scalars().all()

    assert "ck_fuel_margins_margin_positive" in found


async def test_a_margin_entered_later_does_not_revalue_the_day(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    priced_fuel,
    make_reading: Callable[..., UUID],
    make_fuel_margin: Callable[..., UUID],
    clean_cash: None,
) -> None:
    """§4.6/§6.3: the margin in force at `started_at`, not today's.

    A commission entered with an `effective_from` after the shift does not reach back.
    """
    admin = make_user("admin")
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel), opening_reading="1000.00",
                 closing_reading="1100.00")
    make_fuel_margin(
        priced_fuel, "3.00", ended + timedelta(days=1), entered_by=admin
    )

    breakdown = _call(reporting.fuel_breakdown, outlet_id=_outlet(), business_date=DAY)

    assert breakdown.lines[0].gross_fuel_margin is None
    assert breakdown.lines[0].margin_unavailable_reason == "NO_MARGIN_FOR_DATE"


# --- §4.5: units --------------------------------------------------------------


async def test_quantities_are_reported_per_unit_and_never_summed_across_them(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    margined_fuel,
    clean_cash: None,
) -> None:
    """§4.5. 100 litres and 100 kilograms are not 200 of anything.

    The failure this guards against is a `total_quantity` field, which would be a number with
    no unit and therefore no meaning -- and would look entirely reasonable on a screen.
    """
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel, label="DU-1/N-1"),
                 opening_reading="1000.00", closing_reading="1100.00")
    make_reading(shift, make_nozzle(margined_fuel, label="DU-2/N-1"),
                 opening_reading="0.00", closing_reading="100.00")

    breakdown = _call(reporting.fuel_breakdown, outlet_id=_outlet(), business_date=DAY)

    assert breakdown.quantity_by_unit == {
        "litre": Decimal("100.000"),
        "kilogram": Decimal("100.000"),
    }
    assert not hasattr(breakdown, "total_quantity")


async def test_two_nozzles_on_one_fuel_are_one_line(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    clean_cash: None,
) -> None:
    """Aggregated by fuel type, not by nozzle -- two pumps of petrol are one commercial fact."""
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel, label="DU-1/N-1"),
                 opening_reading="0.00", closing_reading="100.00")
    make_reading(shift, make_nozzle(priced_fuel, label="DU-1/N-2"),
                 opening_reading="0.00", closing_reading="50.00")

    breakdown = _call(reporting.fuel_breakdown, outlet_id=_outlet(), business_date=DAY)

    assert len(breakdown.lines) == 1
    assert breakdown.lines[0].quantity == Decimal("150.000")
    assert breakdown.lines[0].sale_value == Decimal("15000.00")


async def test_a_nozzle_with_no_closing_reading_makes_the_day_incomplete_not_zero(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    clean_cash: None,
) -> None:
    """"Not entered" and "sold nothing" are different facts (§4.7, §6.8)."""
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel), opening_reading="1000.00",
                 closing_reading=None)

    breakdown = _call(reporting.fuel_breakdown, outlet_id=_outlet(), business_date=DAY)

    assert breakdown.incomplete is True
    assert breakdown.lines[0].quantity is None
    assert breakdown.lines[0].sale_value is None


# --- §13.20: a missing price degrades one day, not the window ------------------


async def test_a_missing_price_marks_the_day_unavailable_rather_than_raising(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_fuel_type: Callable[..., UUID],
    clean_cash: None,
) -> None:
    """A price gap is a reference-data problem, and it must not take a window down with it.

    Note the asymmetry with the margin, and it is deliberate: a missing margin degrades one
    *field*, a missing price degrades the whole *day*. §6.3 is explicit that a day valued at
    zero "reconciles to a cash surplus nobody can explain", so there is no partial answer here
    worth giving.
    """
    attendant = make_user("attendant")
    unpriced = make_fuel_type(code="REPNOPRICE", unit_of_measure="litre")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(unpriced), opening_reading="0.00",
                 closing_reading="100.00")

    cash = _call(reporting.day_cash, outlet_id=_outlet(), business_date=DAY)

    assert cash.source is DaySource.unavailable
    assert cash.unavailable_reason == "NO_PRICE_FOR_DATE"
    assert cash.metered_fuel_sales is None
    assert cash.expected_closing is None


async def test_a_missing_price_on_one_day_leaves_the_rest_of_the_window_intact(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    make_fuel_type: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    clean_cash: None,
) -> None:
    attendant = make_user("attendant")
    unpriced = make_fuel_type(code="REPNOPRICE2", unit_of_measure="litre")
    good_day, bad_day = DAY, DAY + timedelta(days=1)

    started, ended = _window(good_day)
    good = make_shift(attendant, business_date=good_day, started_at=started, ended_at=ended)
    make_reading(good, make_nozzle(priced_fuel, label="DU-1/N-1"),
                 opening_reading="0.00", closing_reading="100.00")

    started, ended = _window(bad_day)
    bad = make_shift(attendant, business_date=bad_day, started_at=started, ended_at=ended)
    make_reading(bad, make_nozzle(unpriced, label="DU-9/N-9"),
                 opening_reading="0.00", closing_reading="100.00")

    make_daily_summary(business_date=DAY - timedelta(days=1), expected_closing="0.00")

    days = _call(
        reporting.range_report,
        outlet_id=_outlet(),
        date_from=good_day,
        date_to=bad_day,
        threshold=THRESHOLD,
    )

    assert [day.cash.source for day in days] == [
        DaySource.computed,
        DaySource.unavailable,
    ]
    assert days[0].cash.metered_fuel_sales == Decimal("10000.00")


async def test_the_daily_report_survives_a_missing_price(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_fuel_type: Callable[..., UUID],
    clean_cash: None,
) -> None:
    """The whole-day view degrades rather than 500-ing.

    A manager who opens an unpriced day should be told the price is missing, not handed a
    stack trace -- the fix is one admin entering a rate, and the report is where they would
    find out they need to.
    """
    attendant = make_user("attendant")
    unpriced = make_fuel_type(code="REPNOPRICE3", unit_of_measure="litre")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(unpriced), opening_reading="0.00",
                 closing_reading="100.00")

    report = _call(reporting.daily_report, outlet_id=_outlet(), business_date=DAY)

    assert report.cash.source is DaySource.unavailable
    assert report.cash.unavailable_reason == "NO_PRICE_FOR_DATE"
    assert report.fuel.lines == []
    assert report.fuel.sale_value_total is None
    assert report.fuel.incomplete is True
    # And the day's total_sales is None rather than 0.00 -- nothing is guessed at.
    assert report.cash.total_sales is None


async def test_a_failure_that_is_not_a_missing_margin_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    clean_cash: None,
) -> None:
    """The `except` around `margin_at` is narrow, and this is what proves it.

    A broad `except AppError` would swallow a genuine failure and report it as "no margin
    entered" -- a lie that looks like a configuration note, and one nobody would investigate
    because the screen would be telling them something plausible.
    """
    from app.core.errors import AppError
    from app.services import pricing

    def _explode(*args: object, **kwargs: object) -> Decimal:
        raise AppError(status_code=500, code="SOMETHING_ELSE", detail="not a margin gap")

    monkeypatch.setattr(pricing, "margin_at", _explode)

    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel), opening_reading="0.00",
                 closing_reading="100.00")

    with pytest.raises(AppError) as raised:
        _call(reporting.fuel_breakdown, outlet_id=_outlet(), business_date=DAY)

    assert raised.value.code == "SOMETHING_ELSE"


async def test_the_daily_reports_own_price_catch_is_also_narrow(
    monkeypatch: pytest.MonkeyPatch,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    make_daily_summary: Callable[..., UUID],
    clean_cash: None,
) -> None:
    """`daily_report` has a second, independent catch around `fuel_breakdown`.

    Reaching it needs the cash half to *succeed* while the fuel half fails, which happens only
    on a snapshot day -- `day_cash` returns from the stored row without pricing anything, and
    the live breakdown then runs on its own. That asymmetry is exactly §13.22's shape, so the
    path is real rather than theoretical.
    """
    from app.core.errors import AppError
    from app.services import readings as reading_service

    def _explode(*args: object, **kwargs: object):
        raise AppError(status_code=500, code="SOMETHING_ELSE", detail="not a price gap")

    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended,
                       status="closed")
    make_reading(shift, make_nozzle(priced_fuel), opening_reading="0.00",
                 closing_reading="100.00")
    make_daily_summary(business_date=DAY, expected_closing="10000.00")

    monkeypatch.setattr(reading_service, "shift_sales", _explode)

    with pytest.raises(AppError) as raised:
        _call(reporting.daily_report, outlet_id=_outlet(), business_date=DAY)

    assert raised.value.code == "SOMETHING_ELSE"


@pytest.mark.parametrize("entry_point", ["day_cash", "daily_report"])
async def test_a_failure_that_is_not_a_missing_price_still_propagates(
    entry_point: str,
    monkeypatch: pytest.MonkeyPatch,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    clean_cash: None,
) -> None:
    """The same narrowness, on the price side, at both entry points.

    `day_cash` and `daily_report` each catch `NO_PRICE_FOR_DATE` separately, so each needs its
    own proof that it catches *only* that. A day silently reported as "unavailable: price
    missing" when the real cause was something else would send somebody to enter a rate that
    already exists.
    """
    from app.core.errors import AppError
    from app.services import cash as cash_service
    from app.services import readings as reading_service

    def _explode(*args: object, **kwargs: object):
        raise AppError(status_code=500, code="SOMETHING_ELSE", detail="not a price gap")

    monkeypatch.setattr(cash_service, "day_totals", _explode)
    monkeypatch.setattr(reading_service, "shift_sales", _explode)

    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel), opening_reading="0.00",
                 closing_reading="100.00")

    with pytest.raises(AppError) as raised:
        _call(getattr(reporting, entry_point), outlet_id=_outlet(), business_date=DAY)

    assert raised.value.code == "SOMETHING_ELSE"


# --- §6.5: the chain on a computed day ----------------------------------------


async def test_a_computed_day_opens_at_the_prior_count_when_there_is_one(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    clean_cash: None,
) -> None:
    """§6.5's headline rule: the physical count wins wherever there is one."""
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_daily_summary(
        business_date=DAY - timedelta(days=1),
        expected_closing="5000.00",
        actual_counted="4800.00",
    )

    cash = _call(reporting.day_cash, outlet_id=_outlet(), business_date=DAY)

    assert cash.opening_balance == Decimal("4800.00")
    assert cash.opening_balance_source is OpeningBalanceSource.counted


async def test_a_shortage_on_monday_is_absent_from_tuesdays_opening(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    clean_cash: None,
) -> None:
    """§6.5's own worked example, which that section calls "important and easy to get wrong".

    ₹200 short on Monday must show in Monday's variance and be **absent** from Tuesday's
    opening -- the shortage carries forward as a smaller locker, not as an explained-away
    number.
    """
    attendant = make_user("attendant")
    monday, tuesday = DAY, DAY + timedelta(days=1)
    started, ended = _window(tuesday)
    make_shift(attendant, business_date=tuesday, started_at=started, ended_at=ended)
    make_daily_summary(
        business_date=monday, expected_closing="5000.00", actual_counted="4800.00"
    )

    monday_cash = _call(reporting.day_cash, outlet_id=_outlet(), business_date=monday)
    tuesday_cash = _call(reporting.day_cash, outlet_id=_outlet(), business_date=tuesday)

    assert monday_cash.variance == Decimal("-200.00")
    assert tuesday_cash.opening_balance == Decimal("4800.00")


async def test_the_chain_finds_the_most_recent_summary_not_literally_yesterday(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    clean_cash: None,
) -> None:
    """The outlet is shut on some days; a calendar gap must not break the chain (§6.5)."""
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_daily_summary(
        business_date=DAY - timedelta(days=9), expected_closing="7777.00"
    )

    cash = _call(reporting.day_cash, outlet_id=_outlet(), business_date=DAY)

    assert cash.opening_balance == Decimal("7777.00")


async def test_a_computed_day_with_no_anchor_reports_sales_but_a_null_closing(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    clean_cash: None,
) -> None:
    """§6.5's very first day: no prior summary anywhere, so no opening balance exists.

    The day's SALES are still perfectly valid -- only the closing figure is unknowable. `0.00`
    there would silently claim an empty locker, which is a statement about money nobody made.
    """
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel), opening_reading="0.00",
                 closing_reading="100.00")

    cash = _call(reporting.day_cash, outlet_id=_outlet(), business_date=DAY)

    assert cash.source is DaySource.computed
    assert cash.metered_fuel_sales == Decimal("10000.00")
    assert cash.opening_balance is None
    assert cash.opening_balance_source is None
    assert cash.expected_closing is None


# --- the window ---------------------------------------------------------------


async def test_every_date_in_the_window_appears_exactly_once_including_empty_ones(
    clean_cash: None,
) -> None:
    """A window that returned only the days something happened would make a reader count rows
    to notice a gap -- and the gap is often the thing worth noticing."""
    days = _call(
        reporting.range_report,
        outlet_id=_outlet(),
        date_from=DAY,
        date_to=DAY + timedelta(days=6),
        threshold=THRESHOLD,
    )

    assert len(days) == 7
    assert [day.business_date for day in days] == [
        DAY + timedelta(days=offset) for offset in range(7)
    ]
    assert all(day.cash.source is DaySource.no_trading for day in days)


async def test_a_single_day_window_is_one_row(clean_cash: None) -> None:
    days = _call(
        reporting.range_report,
        outlet_id=_outlet(),
        date_from=DAY,
        date_to=DAY,
        threshold=THRESHOLD,
    )

    assert len(days) == 1


async def test_bar_heights_are_relative_to_the_tallest_day(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    priced_fuel,
    clean_cash: None,
) -> None:
    """§14's new guardrail, proven at the only layer that may do this arithmetic.

    The client assigns these strings and divides nothing. Computing them here also makes the
    chart provably consistent with the table beneath it, since both come from one pass.
    """
    attendant = make_user("attendant")
    make_daily_summary(business_date=DAY - timedelta(days=1), expected_closing="0.00")

    for offset, litres in ((0, "100.00"), (1, "50.00")):
        current = DAY + timedelta(days=offset)
        started, ended = _window(current)
        shift = make_shift(attendant, business_date=current, started_at=started,
                           ended_at=ended)
        make_reading(shift, make_nozzle(priced_fuel, label=f"DU-{offset}/N-1"),
                     opening_reading="0.00", closing_reading=litres)

    days = _call(
        reporting.range_report,
        outlet_id=_outlet(),
        date_from=DAY,
        date_to=DAY + timedelta(days=1),
        threshold=THRESHOLD,
    )

    assert days[0].bar_height_pct == "100.00%"
    assert days[1].bar_height_pct == "50.00%"


async def test_a_window_of_nothing_gives_every_bar_zero_height(clean_cash: None) -> None:
    """No division by zero, and no bar drawn for a day that sold nothing."""
    days = _call(
        reporting.range_report,
        outlet_id=_outlet(),
        date_from=DAY,
        date_to=DAY + timedelta(days=2),
        threshold=THRESHOLD,
    )

    assert {day.bar_height_pct for day in days} == {"0.00%"}


# --- §13.23: the alert comparison ---------------------------------------------


@pytest.mark.parametrize(
    ("variance", "expected"),
    [
        (None, False),
        (Decimal("0.00"), False),
        (Decimal("100.00"), False),
        (Decimal("100.01"), True),
        (Decimal("-100.00"), False),
        (Decimal("-100.01"), True),
    ],
)
def test_the_variance_comparison_is_strict_and_signless(
    variance: Decimal | None, expected: bool
) -> None:
    """Three rules in one table.

    **Strictly `>`**, matching §6.7's and §6.11's boundary convention. **Magnitude, not
    sign** -- a ₹500 surplus is as much a signal as a ₹500 shortage, and §6.4 records variance
    in both directions without correcting either. And **null is not an alert**: a day nobody
    counted has no variance to be over anything, and treating it as `0` would silently declare
    every uncounted day perfectly reconciled -- which under §6.5's locker model is most days.
    """
    assert reporting.variance_is_alerting(variance, threshold=THRESHOLD) is expected


async def test_an_uncounted_day_is_not_a_variance_alert(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    clean_cash: None,
) -> None:
    """The normal case under §6.5's locker model, and it must be silent.

    A summary with no `actual_counted` is not a problem -- nobody counts the locker nightly.
    If this fired, every single day would alert and the list would be worthless.
    """
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended,
               status="closed")
    make_daily_summary(business_date=DAY, expected_closing="9000.00")

    found = _call(
        reporting.alerts,
        outlet_id=_outlet(),
        date_from=DAY,
        date_to=DAY,
        threshold=THRESHOLD,
        today=DAY + timedelta(days=1),
    )

    assert [alert.kind for alert in found] == []


async def test_a_variance_over_the_threshold_alerts_and_names_its_direction(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    clean_cash: None,
) -> None:
    """A counted day that came up ₹500 short.

    The direction is in the sentence because a bare figure is ambiguous where it matters most:
    §6.4 makes a shortage and a surplus opposite signs of the same field, and a manager
    reading "differs by 500" has to remember which way round the subtraction goes. A surplus
    is not good news either -- it usually means an unrecorded udhaar slip.
    """
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended,
               status="closed")
    make_daily_summary(
        business_date=DAY, expected_closing="9000.00", actual_counted="8500.00"
    )

    found = _call(
        reporting.alerts,
        outlet_id=_outlet(),
        date_from=DAY,
        date_to=DAY,
        threshold=THRESHOLD,
        today=DAY + timedelta(days=1),
    )

    variance = [
        alert
        for alert in found
        if alert.kind is reporting.AlertKind.variance_exceeds_threshold
    ]
    assert len(variance) == 1
    assert variance[0].amount == Decimal("-500.00")
    assert "short" in variance[0].detail


async def test_a_surplus_over_the_threshold_also_alerts(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    clean_cash: None,
) -> None:
    """The other direction, because §6.4 records variance without correcting either sign."""
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended,
               status="closed")
    make_daily_summary(
        business_date=DAY, expected_closing="9000.00", actual_counted="9500.00"
    )

    found = _call(
        reporting.alerts,
        outlet_id=_outlet(),
        date_from=DAY,
        date_to=DAY,
        threshold=THRESHOLD,
        today=DAY + timedelta(days=1),
    )

    variance = [
        alert
        for alert in found
        if alert.kind is reporting.AlertKind.variance_exceeds_threshold
    ]
    assert len(variance) == 1
    assert variance[0].amount == Decimal("500.00")
    assert "surplus" in variance[0].detail


async def test_an_unreviewed_flagged_expense_alerts_and_a_reviewed_one_does_not(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    engine: Engine,
    clean_cash: None,
    clean_expenses: None,
) -> None:
    """§6.7's flag, surfaced -- and cleared.

    Reached through `expenses.unreviewed_flagged_expenses` rather than a second copy of the
    predicate, which also inherits its exclusion of a flagged row that was later reversed.
    Reimplementing the query here would eventually forget that, and start reporting money a
    manager formally cancelled as something still needing a look.
    """
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_expense(shift, amount="2500.00", requires_review=True)

    kwargs = dict(
        outlet_id=_outlet(),
        date_from=DAY,
        date_to=DAY,
        threshold=THRESHOLD,
        today=DAY + timedelta(days=1),
    )
    before = _call(reporting.alerts, **kwargs)

    flagged = [
        alert
        for alert in before
        if alert.kind is reporting.AlertKind.unreviewed_flagged_expenses
    ]
    assert len(flagged) == 1
    assert flagged[0].count == 1
    assert flagged[0].shift_id == shift

    # Reviewing clears the flag, and therefore the alert. §13.23 has no dismiss of its own:
    # you clear an alert by acting on the row it points at.
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE expenses SET requires_review = false WHERE shift_id = :s"
            ).bindparams(s=shift)
        )

    after = _call(reporting.alerts, **kwargs)

    assert [
        alert
        for alert in after
        if alert.kind is reporting.AlertKind.unreviewed_flagged_expenses
    ] == []


async def test_a_day_that_traded_and_was_never_reconciled_alerts(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    clean_cash: None,
) -> None:
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended,
               status="closed")

    found = _call(
        reporting.alerts,
        outlet_id=_outlet(),
        date_from=DAY,
        date_to=DAY,
        threshold=THRESHOLD,
        today=DAY + timedelta(days=1),
    )

    kinds = [alert.kind for alert in found]
    assert reporting.AlertKind.day_not_reconciled in kinds


async def test_a_quiet_day_produces_no_alert(clean_cash: None) -> None:
    """A date with no shifts is not a problem. It is a Sunday."""
    found = _call(
        reporting.alerts,
        outlet_id=_outlet(),
        date_from=DAY,
        date_to=DAY,
        threshold=THRESHOLD,
        today=DAY + timedelta(days=1),
    )

    assert found == []


async def test_an_open_shift_on_a_past_date_alerts_but_todays_does_not(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    clean_cash: None,
) -> None:
    """A shift open *today* is just a day in progress -- flagging it would fire every
    afternoon and teach a manager to ignore the list."""
    attendant = make_user("attendant")
    yesterday, today = DAY, DAY + timedelta(days=1)
    for current in (yesterday, today):
        started, ended = _window(current)
        make_shift(attendant, business_date=current, started_at=started, ended_at=ended,
                   status="open")

    found = _call(
        reporting.alerts,
        outlet_id=_outlet(),
        date_from=yesterday,
        date_to=today,
        threshold=THRESHOLD,
        today=today,
    )

    stale = [
        alert
        for alert in found
        if alert.kind is reporting.AlertKind.open_shift_on_a_past_date
    ]
    assert len(stale) == 1
    assert stale[0].business_date == yesterday


async def test_a_flagged_reading_alerts_with_its_shift(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    clean_cash: None,
) -> None:
    """§13.10's flag, surfaced. A reading flagged because a shift moved beneath it is exactly
    the thing §4.7 wants visible before anybody is blamed."""
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel), opening_reading="1000.00",
                 closing_reading="1100.00", requires_review=True)

    found = _call(
        reporting.alerts,
        outlet_id=_outlet(),
        date_from=DAY,
        date_to=DAY,
        threshold=THRESHOLD,
        today=DAY + timedelta(days=1),
    )

    flagged = [
        alert
        for alert in found
        if alert.kind is reporting.AlertKind.reading_requires_review
    ]
    assert len(flagged) == 1
    assert flagged[0].shift_id == shift
    assert flagged[0].count == 1


async def test_a_summary_flagged_for_review_alerts_and_carries_its_note(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    engine: Engine,
    clean_cash: None,
) -> None:
    """§13.16's flag. The note names the shift that moved, and it is the only part a human can
    act on -- dropping it would leave an alert that says only "something happened"."""
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended,
               status="closed")
    make_daily_summary(business_date=DAY, expected_closing="9000.00")
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE daily_cash_summaries SET requires_review = true, "
                "review_note = :note WHERE business_date = :day"
            ).bindparams(note="Shift 1 was reopened beneath this day.", day=DAY)
        )

    found = _call(
        reporting.alerts,
        outlet_id=_outlet(),
        date_from=DAY,
        date_to=DAY,
        threshold=THRESHOLD,
        today=DAY + timedelta(days=1),
    )

    flagged = [
        alert
        for alert in found
        if alert.kind is reporting.AlertKind.summary_requires_review
    ]
    assert len(flagged) == 1
    assert "reopened" in flagged[0].detail


async def test_alerts_are_ordered_deterministically(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    clean_cash: None,
) -> None:
    """An alert list that reshuffles between refreshes is one a manager stops trusting."""
    attendant = make_user("attendant")
    for offset in range(3):
        current = DAY + timedelta(days=offset)
        started, ended = _window(current)
        make_shift(attendant, business_date=current, started_at=started, ended_at=ended,
                   status="closed")

    kwargs = dict(
        outlet_id=_outlet(),
        date_from=DAY,
        date_to=DAY + timedelta(days=2),
        threshold=THRESHOLD,
        today=DAY + timedelta(days=3),
    )
    first = _call(reporting.alerts, **kwargs)
    second = _call(reporting.alerts, **kwargs)

    assert [(a.kind, a.business_date) for a in first] == [
        (a.kind, a.business_date) for a in second
    ]
    # Most recent first.
    assert [alert.business_date for alert in first] == sorted(
        [alert.business_date for alert in first], reverse=True
    )


# --- tenancy ------------------------------------------------------------------


async def test_another_outlets_day_is_invisible(
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    clean_cash: None,
) -> None:
    """§5.0. Asserted by *inserting* a second outlet's day, never by an empty page -- an
    empty result proves nothing about scoping when there was nothing to find."""
    other = UUID("00000000-0000-0000-0000-0000000000ff")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outlets (id, name, is_active) "
                "VALUES (:id, 'Other Outlet', true) ON CONFLICT DO NOTHING"
            ).bindparams(id=other)
        )
    attendant = make_user("attendant", outlet_id=other)
    started, ended = _window(DAY)
    make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended,
               outlet_id=other, status="closed")
    make_daily_summary(
        business_date=DAY, outlet_id=other, expected_closing="99999.00",
        created_by=attendant,
    )

    cash = _call(reporting.day_cash, outlet_id=_outlet(), business_date=DAY)

    assert cash.source is DaySource.no_trading
    assert cash.shift_count == 0
