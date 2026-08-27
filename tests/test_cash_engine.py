"""§6.4's daily equation and §6.5's rolling balance (CLAUDE.md §5.2, §6.4, §6.5, §8, §13.16).

The phase's point. Two tests here matter more than the rest of the file:

`test_a_booked_shortfall_reduces_expected_closing_by_exactly_its_amount` is the single most
important assertion in Phase 10. Without that term the same ₹500 is both the salesman's debt
and cash the locker does not contain, and every count afterwards is wrong by it.

`test_a_shortage_on_monday_is_absent_from_tuesdays_opening` is §6.5's own worked example,
written as a test because that paragraph is the one the section says is "important and easy
to get wrong".
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.anyio

BEFORE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _window(day: date) -> tuple[datetime, datetime]:
    """This outlet trades 06:00 -> 22:00 IST, i.e. 00:30 -> 16:30 UTC."""
    start = datetime(day.year, day.month, day.day, 0, 30, tzinfo=timezone.utc)
    return start, start + timedelta(hours=16)


@pytest.fixture
def priced_fuel(make_fuel_type, make_fuel_price, make_user):
    """Priced, and deliberately no dealer margin -- §14's standing open question, and the
    state this outlet is genuinely in. Every test in this file is therefore also a check
    that §6.3's split holds all the way up to the daily summary."""
    admin = make_user("admin")
    fuel = make_fuel_type(code="ENGINEFUEL", unit_of_measure="litre")
    make_fuel_price(fuel, "100.00", BEFORE, entered_by=admin)
    return fuel


@pytest.fixture
def trading_day(make_shift, make_nozzle, make_reading, make_collection, priced_fuel):
    """A closed, locked-on-request shift that metered `litres` x ₹100 and declared cash."""
    counter = {"n": 0}

    def _build(
        attendant: UUID,
        day: date,
        *,
        litres: str = "1000.00",
        declared: str | None = "100000.00",
        status: str = "locked",
    ) -> UUID:
        counter["n"] += 1
        started_at, ended_at = _window(day)
        shift = make_shift(
            attendant,
            business_date=day,
            sequence=1,
            started_at=started_at,
            ended_at=ended_at,
            status=status,
        )
        nozzle = make_nozzle(priced_fuel, label=f"DU-8/N-{counter['n']}")
        make_reading(shift, nozzle, opening_reading="0.00", closing_reading=litres)
        if declared is not None:
            make_collection(shift, mode="cash", amount=declared)
        return shift

    return _build


async def _create(client, headers, *, day: date, **body):
    return await client.post(
        "/api/v1/daily-summaries",
        json={"business_date": day.isoformat(), **body},
        headers=headers,
    )


# --- §6.4's equation, end to end ---------------------------------------------------


async def test_the_full_daily_equation_with_every_term_non_zero(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    make_non_fuel_sale: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_shortfall_settlement: Callable[..., UUID],
    make_bank_deposit: Callable[..., UUID],
    make_shortfall: Callable[..., UUID],
    priced_fuel,
    auth_headers,
) -> None:
    """§10 names this test. The only one that proves the equation as a whole.

        opening                                   10,000
        + cash_sales  (100,000 + 500 - 20,000
                        - 10,000 - 1,000 - 5,000) 64,500
        + cash repayments                          2,000
        + shortfall settlements                      300
        - cash expenses                           -1,500
        - bank deposits                          -50,000
        - shortfalls booked                         -400
                                                 =24,900
    """
    admin = make_user("admin")
    attendant = make_user("attendant")
    day = date(2026, 5, 1)
    started_at, ended_at = _window(day)
    shift = make_shift(
        attendant,
        business_date=day,
        sequence=1,
        started_at=started_at,
        ended_at=ended_at,
        status="closed",
    )
    nozzle = make_nozzle(priced_fuel, label="DU-7/N-1")
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="1000.00")

    make_non_fuel_sale(shift, amount="500.00")
    make_collection(shift, mode="card", amount="20000.00")
    make_collection(shift, mode="upi", amount="10000.00")
    make_collection(shift, mode="wallet", amount="1000.00")
    customer = make_credit_customer()
    make_credit_sale(shift, customer, make_attachment(attendant), amount="5000.00")
    make_credit_repayment(shift, customer, amount="2000.00", mode="cash")
    make_shortfall_settlement(shift, salesman_id=attendant, amount="300.00")
    make_expense(shift, mode="cash", amount="1500.00")
    make_bank_deposit(shift, business_date=day, amount="50000.00")
    make_shortfall(shift, salesman_id=attendant, amount="400.00", computed_gap="400.00")

    response = await _create(
        client, auth_headers(admin), day=day, opening_balance="10000.00"
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert Decimal(body["expected_closing"]) == Decimal("24900.00")
    assert Decimal(body["metered_fuel_sales"]) == Decimal("100000.00")
    assert Decimal(body["non_fuel_sales_total"]) == Decimal("500.00")
    assert Decimal(body["bank_deposits_total"]) == Decimal("50000.00")
    assert Decimal(body["shortfalls_booked"]) == Decimal("400.00")
    assert body["opening_balance_source"] == "seeded"


async def test_a_booked_shortfall_reduces_expected_closing_by_exactly_its_amount(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    trading_day,
    make_shortfall: Callable[..., UUID],
    auth_headers,
) -> None:
    """**The most important assertion in this phase** (§6.4's worked example).

    Two identical days. On the second, ₹500 of the takings is booked against the salesman
    instead of being in the locker. Expected closing must drop by exactly ₹500 -- otherwise
    the ₹500 is both his debt and cash that is not there, and every locker count from then
    on is wrong by it with nothing to explain why.
    """
    admin = make_user("admin")
    attendant = make_user("attendant")

    plain_day = date(2026, 5, 2)
    plain = trading_day(attendant, plain_day, status="closed")
    first = await _create(
        client, auth_headers(admin), day=plain_day, opening_balance="0.00"
    )
    assert first.status_code == 201
    without = Decimal(first.json()["expected_closing"])

    booked_day = date(2026, 5, 3)
    booked = trading_day(attendant, booked_day, declared="99500.00", status="closed")
    make_shortfall(
        booked, salesman_id=attendant, amount="500.00", computed_gap="500.00"
    )
    second = await _create(client, auth_headers(admin), day=booked_day)
    assert second.status_code == 201
    with_shortfall = Decimal(second.json()["expected_closing"])

    # The second day opens where the first closed, so compare the day's own contribution.
    assert Decimal(second.json()["opening_balance"]) == without
    assert with_shortfall - without == without - Decimal("500.00")


async def test_an_unbooked_gap_does_not_reduce_expected_closing(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    trading_day,
    auth_headers,
) -> None:
    """The correct outcome of a manager choosing not to book (§6.4). The gap resurfaces at
    the next physical count as a variance with nobody's name on it -- which is a signal, not
    a hole."""
    admin = make_user("admin")
    attendant = make_user("attendant")
    day = date(2026, 5, 4)
    trading_day(attendant, day, declared="99500.00", status="closed")

    response = await _create(
        client, auth_headers(admin), day=day, opening_balance="0.00"
    )

    assert Decimal(response.json()["expected_closing"]) == Decimal("100000.00")
    assert Decimal(response.json()["shortfalls_booked"]) == Decimal("0.00")


async def test_only_a_cash_repayment_reaches_the_drawer(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    """§10 names this case. A customer settling by bank transfer moves no money through the
    drawer, and adding it would invent a shortfall on the very day they paid.

    **Phase 16 narrowed what this test may claim.** It used to assert that a UPI repayment
    left `expected_closing` untouched, which was true only because the scenario gave the
    shift no UPI *collection* to net it against. In the real world the settlement is inside
    the machine's total, §6.4 subtracts that total, and nothing put the settlement back --
    so the salesman read as holding a surplus nobody gave him. The two now cancel, which is
    what "it does not reach the drawer" actually means, and the ₹9,000 is visible in its own
    stored term rather than being silently absent.

    `bank_transfer` is the mode that genuinely touches nothing: it never went through a
    machine here, so there is no collection to net it against and none is invented.
    """
    admin = make_user("admin")
    attendant = make_user("attendant")
    day = date(2026, 5, 5)
    started_at, ended_at = _window(day)
    shift = make_shift(
        attendant,
        business_date=day,
        sequence=1,
        started_at=started_at,
        ended_at=ended_at,
        status="closed",
    )
    customer = make_credit_customer()
    make_credit_repayment(shift, customer, amount="2000.00", mode="cash")
    make_credit_repayment(shift, customer, amount="9000.00", mode="upi")
    make_credit_repayment(shift, customer, amount="7000.00", mode="bank_transfer")
    # The UPI settlement is inside the QR's day total, exactly as it is on a real day.
    make_collection(shift, mode="upi", amount="9000.00")

    response = await _create(
        client, auth_headers(admin), day=day, opening_balance="0.00"
    )
    body = response.json()

    assert Decimal(body["cash_credit_repayments"]) == Decimal("2000.00")
    assert Decimal(body["card_upi_credit_repayments"]) == Decimal("9000.00")
    # 9,000 added on the sales side, 9,000 subtracted as a UPI collection: net zero.
    # The bank transfer contributes nothing at all. Only the cash repayment moves the figure.
    assert Decimal(body["expected_closing"]) == Decimal("2000.00")


async def test_only_cash_expenses_leave_the_drawer(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    """§6.4's `mode = cash` clause, and the reason Phase 7 added the column. A ₹40,000
    electricity bill paid online read as a ₹40,000 hole in the drawer before it existed --
    and §14 records this outlet would then book that as udhaar against a salesman's name."""
    admin = make_user("admin")
    attendant = make_user("attendant")
    day = date(2026, 5, 6)
    started_at, ended_at = _window(day)
    shift = make_shift(
        attendant,
        business_date=day,
        sequence=1,
        started_at=started_at,
        ended_at=ended_at,
        status="closed",
    )
    make_expense(shift, mode="cash", amount="1500.00")
    make_expense(shift, mode="bank_transfer", amount="40000.00")
    make_expense(shift, mode="upi", amount="600.00")

    response = await _create(
        client, auth_headers(admin), day=day, opening_balance="0.00"
    )

    assert Decimal(response.json()["cash_expenses"]) == Decimal("1500.00")
    assert Decimal(response.json()["expected_closing"]) == Decimal("-1500.00")


async def test_a_day_aggregates_every_shift_on_it(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    auth_headers,
) -> None:
    """§4.7: the number of shifts in a day is data, not schema. This outlet runs one; a
    24-hour outlet runs three, and §5.4's "across both shifts" was stale for a reason."""
    admin = make_user("admin")
    first_attendant = make_user("attendant")
    second_attendant = make_user("attendant")
    day = date(2026, 5, 7)
    started_at, ended_at = _window(day)

    for index, attendant in enumerate((first_attendant, second_attendant), start=1):
        shift = make_shift(
            attendant,
            business_date=day,
            sequence=index,
            started_at=started_at,
            ended_at=ended_at,
            status="closed",
        )
        nozzle = make_nozzle(priced_fuel, label=f"DU-6/N-{index}")
        make_reading(shift, nozzle, opening_reading="0.00", closing_reading="500.00")

    response = await _create(
        client, auth_headers(admin), day=day, opening_balance="0.00"
    )

    assert Decimal(response.json()["metered_fuel_sales"]) == Decimal("100000.00")


# --- §6.5's rolling balance --------------------------------------------------------


async def test_the_first_day_needs_an_admin_seeded_opening(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    trading_day,
    auth_headers,
) -> None:
    """§4.7's anchor argument, applied to money: the first row *is* the record, not a
    separate seed table, because two copies of "where the locker started" would eventually
    disagree."""
    manager = make_user("manager")
    admin = make_user("admin")
    attendant = make_user("attendant")
    day = date(2026, 5, 8)
    trading_day(attendant, day, status="closed")

    missing = await _create(client, auth_headers(admin), day=day)
    assert missing.status_code == 422
    assert missing.json()["code"] == "OPENING_BALANCE_REQUIRED"

    not_admin = await _create(
        client, auth_headers(manager), day=day, opening_balance="10000.00"
    )
    assert not_admin.status_code == 403
    assert not_admin.json()["code"] == "OPENING_BALANCE_REQUIRES_ADMIN"

    seeded = await _create(
        client, auth_headers(admin), day=day, opening_balance="10000.00"
    )
    assert seeded.status_code == 201
    assert seeded.json()["opening_balance_source"] == "seeded"


async def test_a_later_day_cannot_have_its_opening_typed(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    trading_day,
    auth_headers,
) -> None:
    """The figure is derived. Letting somebody type it would let a day quietly disagree with
    the one before it, which is the whole thing §6.5's chain exists to prevent."""
    admin = make_user("admin")
    attendant = make_user("attendant")
    first = date(2026, 5, 9)
    second = date(2026, 5, 10)
    trading_day(attendant, first, status="closed")
    trading_day(attendant, second, status="closed")

    await _create(client, auth_headers(admin), day=first, opening_balance="0.00")
    response = await _create(
        client, auth_headers(admin), day=second, opening_balance="999.00"
    )

    assert response.status_code == 409
    assert response.json()["code"] == "OPENING_BALANCE_IS_CHAINED"


async def test_with_no_count_the_balance_carries_forward_arithmetically(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    trading_day,
    auth_headers,
) -> None:
    """§6.5, restated for a locker. Most days have no count at all, and the original rule
    would have blocked every one of them forever."""
    admin = make_user("admin")
    attendant = make_user("attendant")
    first = date(2026, 5, 11)
    second = date(2026, 5, 12)
    trading_day(attendant, first, status="closed")
    trading_day(attendant, second, status="closed")

    day_one = await _create(
        client, auth_headers(admin), day=first, opening_balance="0.00"
    )
    day_two = await _create(client, auth_headers(admin), day=second)

    assert day_one.json()["actual_counted"] is None
    assert day_one.json()["variance"] is None
    assert day_two.json()["opening_balance_source"] == "carried"
    assert Decimal(day_two.json()["opening_balance"]) == Decimal(
        day_one.json()["expected_closing"]
    )


async def test_a_count_re_anchors_the_chain(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    trading_day,
    auth_headers,
) -> None:
    """§6.5's headline rule survives the locker model intact: **the count wins wherever there
    is one.** A physical count is an occasional audit that re-anchors the chain, exactly as a
    confirmed meter reading re-anchors §4.7's."""
    admin = make_user("admin")
    attendant = make_user("attendant")
    first = date(2026, 5, 13)
    second = date(2026, 5, 14)
    trading_day(attendant, first, status="closed")
    trading_day(attendant, second, status="closed")

    await _create(client, auth_headers(admin), day=first, opening_balance="0.00")
    await client.patch(
        f"/api/v1/daily-summaries/{first.isoformat()}",
        json={"actual_counted": "99700.00"},
        headers=auth_headers(admin),
    )

    day_two = await _create(client, auth_headers(admin), day=second)

    assert day_two.json()["opening_balance_source"] == "counted"
    assert Decimal(day_two.json()["opening_balance"]) == Decimal("99700.00")


async def test_a_shortage_on_monday_is_absent_from_tuesdays_opening(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    trading_day,
    auth_headers,
) -> None:
    """§6.5's own worked example, which that section calls "important and easy to get wrong".

    Monday expects ₹1,00,000 and the locker holds ₹99,800. The ₹200 must appear in **Monday's
    variance** and be **absent from Tuesday's opening** -- otherwise the shortage silently
    disappears and Tuesday starts ₹200 richer than the locker actually is.
    """
    admin = make_user("admin")
    attendant = make_user("attendant")
    monday = date(2026, 5, 15)
    tuesday = date(2026, 5, 16)
    trading_day(attendant, monday, status="closed")
    trading_day(attendant, tuesday, status="closed")

    await _create(client, auth_headers(admin), day=monday, opening_balance="0.00")
    counted = await client.patch(
        f"/api/v1/daily-summaries/{monday.isoformat()}",
        json={"actual_counted": "99800.00"},
        headers=auth_headers(admin),
    )
    assert Decimal(counted.json()["variance"]) == Decimal("-200.00")
    # The variance is recorded, never auto-corrected -- §6.4. The expected figure stands.
    assert Decimal(counted.json()["expected_closing"]) == Decimal("100000.00")

    tuesday_summary = await _create(client, auth_headers(admin), day=tuesday)

    assert Decimal(tuesday_summary.json()["opening_balance"]) == Decimal("99800.00")


async def test_a_gap_in_the_calendar_does_not_break_the_chain(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    trading_day,
    auth_headers,
) -> None:
    """"The previous day" means the most recent summary, not literally `date - 1`. This
    outlet is shut on some days; a chain built on yesterday's date would snap on the first
    one -- the same reasoning §4.7 gives for looking up the most recent reading *for that
    nozzle* rather than the previous shift's."""
    admin = make_user("admin")
    attendant = make_user("attendant")
    first = date(2026, 5, 17)
    much_later = date(2026, 5, 25)
    trading_day(attendant, first, status="closed")
    trading_day(attendant, much_later, status="closed")

    day_one = await _create(
        client, auth_headers(admin), day=first, opening_balance="0.00"
    )
    day_two = await _create(client, auth_headers(admin), day=much_later)

    assert day_two.status_code == 201
    assert Decimal(day_two.json()["opening_balance"]) == Decimal(
        day_one.json()["expected_closing"]
    )


async def test_the_variance_is_null_until_somebody_counts(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    trading_day,
    auth_headers,
) -> None:
    """Null means "no variance known", never a variance of zero -- which would read as a
    perfectly reconciled day nobody ever checked."""
    admin = make_user("admin")
    attendant = make_user("attendant")
    day = date(2026, 5, 18)
    trading_day(attendant, day, status="closed")

    created = await _create(
        client, auth_headers(admin), day=day, opening_balance="0.00"
    )

    assert created.json()["actual_counted"] is None
    assert created.json()["variance"] is None
    assert "NOT a variance of zero" in created.json()["variance_basis"]


# --- §6.3: still no margin needed ---------------------------------------------------


async def test_a_day_of_petrol_with_no_dealer_margin_reconciles(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    trading_day,
    auth_headers,
) -> None:
    """The blocker test at the top of the stack. `priced_fuel` has no margin -- §14 says
    petrol and diesel commissions have never been entered here -- and the whole day
    reconciles anyway."""
    admin = make_user("admin")
    attendant = make_user("attendant")
    day = date(2026, 5, 19)
    trading_day(attendant, day, status="closed")

    response = await _create(
        client, auth_headers(admin), day=day, opening_balance="0.00"
    )

    assert response.status_code == 201
    assert Decimal(response.json()["metered_fuel_sales"]) == Decimal("100000.00")
