"""A shift's cash position (CLAUDE.md §5.2, §6.3, §6.4, §8, §13.14).

The per-shift half of §6.4, and the calculation that decides whether a debt lands on a real
person's name. Two tests here carry more weight than the rest:

`test_a_card_paid_non_fuel_sale_does_not_make_the_salesman_look_flush` is the one Step 4's
module docstring points at. It is why non-fuel income sits on §6.4's *sales* side rather than
its cash side, and getting it wrong produces a surplus in the salesman's name on every day he
sells a bottle of oil to somebody paying by card.

`test_nothing_is_written_by_reading_the_position` is the §4.7 guarantee: the system computes
and shows, a human books. A gap is not a debt until somebody decides it is.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.anyio

DAY = date(2027, 1, 6)
# This outlet trades 06:00 -> 22:00 IST; 06:00 IST is 00:30 UTC.
SHIFT_START = datetime(2027, 1, 6, 0, 30, tzinfo=timezone.utc)
SHIFT_END = datetime(2027, 1, 6, 16, 30, tzinfo=timezone.utc)
BEFORE = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _position(client: AsyncClient, shift: UUID, headers: dict[str, str]):
    response = await client.get(
        f"/api/v1/shifts/{shift}/cash-position", headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def priced_fuel(make_fuel_type, make_fuel_price, make_user):
    """A fuel with a price and **deliberately no margin**.

    §14's standing open question: petrol and diesel commissions have never been entered at
    this outlet. Every test in this file uses a fuel in that state, so the whole file is a
    standing check that a missing margin cannot block reconciliation (§6.3).
    """
    admin = make_user("admin")
    fuel = make_fuel_type(code="CASHFUEL", unit_of_measure="litre")
    make_fuel_price(fuel, "100.00", BEFORE, entered_by=admin)
    return fuel


# --- the full equation -------------------------------------------------------------


async def test_the_full_per_shift_calculation_with_every_term_non_zero(
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
    priced_fuel,
    auth_headers,
) -> None:
    """§10 names this test explicitly. It is the only one that proves the equation as a
    whole rather than one term at a time.

        1,000 L x ₹100        = ₹1,00,000 metered
        + ₹500 non-fuel                    = 1,00,500
        - ₹20,000 card - ₹10,000 upi - ₹1,000 wallet
        - ₹5,000 udhaar
        + ₹2,000 cash repayment
        + ₹300 shortfall settlement
                                            = ₹66,800 accountable

    **The ₹1,500 cash expense is deliberately absent from that sum** (§6.4, Phase 17). It is
    still recorded, still asserted below, and still subtracted by `expected_closing` -- but a
    bill the pump pays comes out of the locker, not out of what this salesman is accountable
    for. Subtracting it here reported a ₹60,169 phantom surplus on 30 July; see
    `tests/test_locker_expenses.py`.
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant,
        business_date=DAY,
        sequence=1,
        started_at=SHIFT_START,
        ended_at=SHIFT_END,
    )
    nozzle = make_nozzle(priced_fuel)
    make_reading(shift, nozzle, opening_reading="1000.00", closing_reading="2000.00")

    make_non_fuel_sale(shift, amount="500.00")
    make_collection(shift, mode="card", amount="20000.00")
    make_collection(shift, mode="upi", amount="10000.00")
    make_collection(shift, mode="wallet", amount="1000.00")
    customer = make_credit_customer()
    make_credit_sale(shift, customer, make_attachment(attendant), amount="5000.00")
    make_credit_repayment(shift, customer, amount="2000.00", mode="cash")
    make_shortfall_settlement(shift, salesman_id=attendant, amount="300.00")
    make_expense(shift, mode="cash", amount="1500.00")

    body = await _position(client, shift, auth_headers(manager))

    assert Decimal(body["metered_fuel_sales"]) == Decimal("100000.00")
    assert Decimal(body["non_fuel_sales"]) == Decimal("500.00")
    assert Decimal(body["card_total"]) == Decimal("20000.00")
    assert Decimal(body["upi_total"]) == Decimal("10000.00")
    assert Decimal(body["wallet_total"]) == Decimal("1000.00")
    assert Decimal(body["credit_sales_total"]) == Decimal("5000.00")
    assert Decimal(body["cash_credit_repayments"]) == Decimal("2000.00")
    assert Decimal(body["cash_shortfall_settlements"]) == Decimal("300.00")
    assert Decimal(body["cash_expenses"]) == Decimal("1500.00")
    # ₹65,300 before Phase 17, when the expense was subtracted here too.
    assert Decimal(body["accountable_cash"]) == Decimal("66800.00")
    assert body["incomplete"] is False


async def test_the_salesman_is_the_shifts_attendant(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """§5.2: exactly one name carries the drawer. This is where a booked shortfall gets the
    name it lands on, so it must come from the shift and nowhere else."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 1, 7), sequence=1)

    body = await _position(client, shift, auth_headers(manager))

    assert body["salesman_id"] == str(attendant)


# --- the gap, and the three things it is not ---------------------------------------


async def test_a_short_declaration_produces_a_positive_gap(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    priced_fuel,
    auth_headers,
) -> None:
    """₹1,00,000 through the meters, ₹99,500 declared. Positive is short."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant,
        business_date=date(2027, 1, 8),
        sequence=1,
        started_at=SHIFT_START,
        ended_at=SHIFT_END,
    )
    nozzle = make_nozzle(priced_fuel)
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="1000.00")
    make_collection(shift, mode="cash", amount="99500.00")

    body = await _position(client, shift, auth_headers(manager))

    assert Decimal(body["accountable_cash"]) == Decimal("100000.00")
    assert Decimal(body["declared_cash"]) == Decimal("99500.00")
    assert Decimal(body["gap"]) == Decimal("500.00")


async def test_an_over_declaration_produces_a_negative_gap_and_is_not_booked(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    priced_fuel,
    auth_headers,
) -> None:
    """A surplus is real and is reported as a negative gap. It is a signal too -- a salesman
    consistently declaring more than the meters imply is worth asking about -- but §13.14
    gives shortfalls a record type and surpluses none, so nothing is written either way."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant,
        business_date=date(2027, 1, 9),
        sequence=1,
        started_at=SHIFT_START,
        ended_at=SHIFT_END,
    )
    nozzle = make_nozzle(priced_fuel)
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="1000.00")
    make_collection(shift, mode="cash", amount="100200.00")

    body = await _position(client, shift, auth_headers(manager))

    assert Decimal(body["gap"]) == Decimal("-200.00")
    assert Decimal(body["shortfalls_booked"]) == Decimal("0.00")


async def test_an_undeclared_shift_has_a_null_gap_not_a_zero_one(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    auth_headers,
) -> None:
    """§6.8's distinction, carried into the arithmetic. Nobody has declared, so there is no
    disagreement to measure -- and a zero here would read as "he counted exactly right",
    which is the opposite of the truth. This is where a shortfall would otherwise hide."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant,
        business_date=date(2027, 1, 10),
        sequence=1,
        started_at=SHIFT_START,
        ended_at=SHIFT_END,
    )
    nozzle = make_nozzle(priced_fuel)
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="1000.00")

    body = await _position(client, shift, auth_headers(manager))

    assert body["declared_cash"] is None
    assert body["gap"] is None


async def test_an_explicit_zero_declaration_is_a_real_answer(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
) -> None:
    """The other half of the distinction above: ₹0 declared on a day that took no cash is an
    answer, and produces a real gap of ₹0.00 rather than a null."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 1, 11), sequence=1)
    make_collection(shift, mode="cash", amount="0.00")

    body = await _position(client, shift, auth_headers(manager))

    assert Decimal(body["declared_cash"]) == Decimal("0.00")
    assert Decimal(body["gap"]) == Decimal("0.00")


# --- the card-paid oil case: why non-fuel sits on the sales side -------------------


async def test_a_card_paid_non_fuel_sale_does_not_make_the_salesman_look_flush(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    make_non_fuel_sale: Callable[..., UUID],
    priced_fuel,
    auth_headers,
) -> None:
    """§6.4's worked example, as a test. **This is the reason non-fuel income is on the sales
    side of the equation and not the cash side.**

    ₹1,00,000 of fuel, plus a ₹500 bottle of oil the customer paid for by card. The card
    machine therefore reads ₹20,500 -- ₹20,000 of fuel and the oil. The salesman holds
    ₹80,000 in cash and that is exactly what he declares.

    Sales side:  (1,00,000 + 500) - 20,500 = ₹80,000  -> gap ₹0  (correct)
    Cash side:    1,00,000 - 20,500 + 0    = ₹79,500  -> gap -₹500 (a surplus he never had)

    The cash-side form invents a surplus in his name on every day he sells a bottle of oil to
    somebody paying by card, and the number looks entirely reasonable.
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant,
        business_date=date(2027, 1, 12),
        sequence=1,
        started_at=SHIFT_START,
        ended_at=SHIFT_END,
    )
    nozzle = make_nozzle(priced_fuel)
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="1000.00")
    make_non_fuel_sale(shift, amount="500.00", description="Engine oil, paid by card")
    make_collection(shift, mode="card", amount="20500.00")
    make_collection(shift, mode="cash", amount="80000.00")

    body = await _position(client, shift, auth_headers(manager))

    assert Decimal(body["accountable_cash"]) == Decimal("80000.00")
    assert Decimal(body["gap"]) == Decimal("0.00")


async def test_a_cash_paid_non_fuel_sale_also_reconciles(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    make_non_fuel_sale: Callable[..., UUID],
    priced_fuel,
    auth_headers,
) -> None:
    """The other half of §6.4's amendment: the sales-side form is correct **regardless** of
    how the non-fuel sale was paid, which is why the table needs no mode column."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant,
        business_date=date(2027, 1, 13),
        sequence=1,
        started_at=SHIFT_START,
        ended_at=SHIFT_END,
    )
    nozzle = make_nozzle(priced_fuel)
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="1000.00")
    make_non_fuel_sale(shift, amount="500.00", description="Engine oil, paid in cash")
    make_collection(shift, mode="card", amount="20000.00")
    make_collection(shift, mode="cash", amount="80500.00")

    body = await _position(client, shift, auth_headers(manager))

    assert Decimal(body["accountable_cash"]) == Decimal("80500.00")
    assert Decimal(body["gap"]) == Decimal("0.00")


async def test_omitting_the_non_fuel_row_makes_the_salesman_look_short(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    priced_fuel,
    auth_headers,
) -> None:
    """The failure this table exists to prevent, asserted directly rather than described.

    Same day as the test above with the ₹500 oil row simply not recorded: he declares the
    ₹80,500 he is actually holding and the system says he is ₹500 **over**. A surplus is not
    the alarming direction, but it is the same error that runs the other way the moment the
    oil is bought on credit -- and either way the figure is wrong with no trace of why.
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant,
        business_date=date(2027, 1, 14),
        sequence=1,
        started_at=SHIFT_START,
        ended_at=SHIFT_END,
    )
    nozzle = make_nozzle(priced_fuel)
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="1000.00")
    make_collection(shift, mode="card", amount="20000.00")
    make_collection(shift, mode="cash", amount="80500.00")

    body = await _position(client, shift, auth_headers(manager))

    assert Decimal(body["gap"]) == Decimal("-500.00")


# --- §6.3: a missing margin must not block this ------------------------------------


async def test_a_shift_with_no_dealer_margin_still_reconciles(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    auth_headers,
) -> None:
    """The blocker test, at the level that matters. `priced_fuel` has no margin -- the state
    §14 says this outlet is genuinely in -- and the report route for the same shift 409s."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant,
        business_date=date(2027, 1, 15),
        sequence=1,
        started_at=SHIFT_START,
        ended_at=SHIFT_END,
    )
    nozzle = make_nozzle(priced_fuel)
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="1000.00")

    position = await client.get(
        f"/api/v1/shifts/{shift}/cash-position", headers=auth_headers(manager)
    )
    sales = await client.get(
        f"/api/v1/shifts/{shift}/sales", headers=auth_headers(manager)
    )

    assert position.status_code == 200
    assert Decimal(position.json()["metered_fuel_sales"]) == Decimal("100000.00")
    assert sales.status_code == 409
    assert sales.json()["code"] == "NO_MARGIN_FOR_DATE"


async def test_the_response_says_no_profit_was_computed(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """§13.7: an unlabelled profit figure is the plausible-but-wrong number this project
    exists to prevent, so the absence of one is stated rather than left to be noticed."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 1, 16), sequence=1)

    body = await _position(client, shift, auth_headers(manager))

    assert "rate_at only" in body["margin_basis"]
    assert "profit" not in {key.lower() for key in body}


# --- an in-progress shift ----------------------------------------------------------


async def test_a_nozzle_with_no_closing_reading_marks_the_position_incomplete(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    auth_headers,
) -> None:
    """"Not entered" and "sold nothing" are different facts. A mid-entry shift must not
    report a gap the size of its own unentered readings, so the unknown nozzle contributes
    nothing and the flag says the figure is provisional."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant,
        business_date=date(2027, 1, 17),
        sequence=1,
        started_at=SHIFT_START,
        ended_at=SHIFT_END,
    )
    done = make_nozzle(priced_fuel, label="DU-9/N-1")
    # In scope for the shift, and deliberately given no reading at all.
    make_nozzle(priced_fuel, label="DU-9/N-2")
    make_reading(shift, done, opening_reading="0.00", closing_reading="1000.00")

    body = await _position(client, shift, auth_headers(manager))

    assert body["incomplete"] is True
    assert Decimal(body["metered_fuel_sales"]) == Decimal("100000.00")


# --- §8 and §14 --------------------------------------------------------------------


async def test_an_attendant_cannot_read_their_own_cash_position(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """§8: this is a report, not a data-entry sheet -- and the person a shortfall would be
    booked against is the last one who should run the calculation privately first."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 1, 18), sequence=1)

    response = await client.get(
        f"/api/v1/shifts/{shift}/cash-position", headers=auth_headers(attendant)
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


async def test_a_closed_shift_can_still_be_read(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """The gap is only knowable once the readings are final, so refusing a closed shift
    would make this unreachable exactly when it matters."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant, business_date=date(2027, 1, 19), sequence=1, status="closed"
    )

    response = await client.get(
        f"/api/v1/shifts/{shift}/cash-position", headers=auth_headers(manager)
    )

    assert response.status_code == 200


async def test_nothing_is_written_by_reading_the_position(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    priced_fuel,
    auth_headers,
    engine: Engine,
) -> None:
    """§4.7's guarantee, and §14's new guardrail: the system computes and shows, a human
    books. A ₹500 gap is more often a mistyped reading or an unrecorded udhaar slip than it
    is theft, and software must not be the thing that decides which."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant,
        business_date=date(2027, 1, 20),
        sequence=1,
        started_at=SHIFT_START,
        ended_at=SHIFT_END,
    )
    nozzle = make_nozzle(priced_fuel)
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="1000.00")
    make_collection(shift, mode="cash", amount="99500.00")

    body = await _position(client, shift, auth_headers(manager))
    assert Decimal(body["gap"]) == Decimal("500.00")

    with engine.connect() as connection:
        booked = connection.execute(
            text(
                "SELECT count(*) FROM salesman_shortfalls WHERE shift_id = :s"
            ).bindparams(s=shift)
        ).scalar_one()
        audited = connection.execute(
            text(
                "SELECT count(*) FROM audit_logs WHERE table_name = 'salesman_shortfalls'"
            )
        ).scalar_one()
    assert booked == 0
    assert audited == 0
