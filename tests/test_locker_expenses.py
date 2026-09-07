"""Expenses come out of the locker, not out of the salesman's hands (§5.2, §6.4, §13.33).

`accountable_cash` credits the salesman with the cash his **sales** generated and subtracts
no expense at all. What the pump then spends is a locker question, and `expected_closing`
settles it — so the locker is still lighter by every rupee, and only the per-shift
comparison changes.

The owner's argument, which is the whole design: *whether it gets subtracted before entering
the locker or after entering the locker it's one and the same thing, the total sum would
remain the same.*

`test_the_thirtieth_of_july` is the day that forced it. `test_expected_closing_is_unchanged`
is the guard that the day equation did not move, and is the more important of the two.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.anyio

DAY = date(2026, 4, 1)
# This outlet trades 06:00 -> 22:00 IST; 06:00 IST is 00:30 UTC.
SHIFT_START = datetime(2026, 4, 1, 0, 30, tzinfo=timezone.utc)
SHIFT_END = datetime(2026, 4, 1, 16, 30, tzinfo=timezone.utc)
BEFORE = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _position(client: AsyncClient, shift: UUID, headers: dict[str, str]):
    response = await client.get(
        f"/api/v1/shifts/{shift}/cash-position", headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def priced_fuel(make_fuel_type, make_fuel_price, make_user):
    """A fuel at ₹100/unit with **deliberately no margin** (§6.3, §14)."""
    admin = make_user("admin")
    fuel = make_fuel_type(code="LOCKERFUEL", unit_of_measure="litre")
    make_fuel_price(fuel, "100.00", BEFORE, entered_by=admin)
    return fuel


async def test_the_thirtieth_of_july(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    priced_fuel,
    auth_headers,
) -> None:
    """The regression test for phase 17, with the real figures.

        3,028.27 L x ₹100      = ₹3,02,827 metered
        − ₹2,65,617 card
        − ₹19,610 udhaar
                                 = ₹17,600 accountable   ← and he declared ₹17,600

    The ₹60,170 of bills came out of cash carried from earlier days. Subtracting them here
    gave −₹42,569 accountable and a ₹60,169 phantom surplus in a salesman's name.
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
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="3028.27")

    make_collection(shift, mode="card", amount="265617.00")
    make_collection(shift, mode="cash", amount="17600.00")
    customer = make_credit_customer()
    make_credit_sale(shift, customer, make_attachment(attendant), amount="19610.00")
    make_expense(shift, mode="cash", amount="60170.00")

    body = await _position(client, shift, auth_headers(manager))

    assert Decimal(body["metered_fuel_sales"]) == Decimal("302827.00")
    assert Decimal(body["card_total"]) == Decimal("265617.00")
    assert Decimal(body["credit_sales_total"]) == Decimal("19610.00")

    # The figure that was −42,569.02 before this phase.
    assert Decimal(body["accountable_cash"]) == Decimal("17600.00")
    assert Decimal(body["declared_cash"]) == Decimal("17600.00")
    # The ₹60,169.02 phantom surplus is gone. The day reconciles exactly.
    assert Decimal(body["gap"]) == Decimal("0.00")
    # Still reported -- it is a real expense, and `expected_closing` still subtracts it.
    assert Decimal(body["cash_expenses"]) == Decimal("60170.00")


async def test_a_cash_expense_does_not_move_accountable_cash(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    priced_fuel,
    auth_headers,
) -> None:
    """§6.4: the salesman is accountable for what his sales took, full stop.

    ₹1,00,000 of metered fuel and a ₹1,500 cash expense. He is accountable for ₹1,00,000 --
    and if he paid that bill from his own hand, the ₹1,500 gap is §13.33's, explained by the
    expense row rather than netted silently away.
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant,
        business_date=date(2026, 4, 2),
        sequence=1,
        started_at=SHIFT_START,
        ended_at=SHIFT_END,
    )
    nozzle = make_nozzle(priced_fuel)
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="1000.00")
    make_collection(shift, mode="cash", amount="98500.00")
    make_expense(shift, mode="cash", amount="1500.00")

    body = await _position(client, shift, auth_headers(manager))

    assert Decimal(body["accountable_cash"]) == Decimal("100000.00")
    # He handed over ₹1,500 less because he paid the bill. The gap says so (§13.33).
    assert Decimal(body["gap"]) == Decimal("1500.00")
    assert Decimal(body["cash_expenses"]) == Decimal("1500.00")


async def test_a_non_cash_expense_moves_neither_figure(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    priced_fuel,
    auth_headers,
) -> None:
    """A bank-paid bill never touched the drawer and never touched his hands either."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant,
        business_date=date(2026, 4, 3),
        sequence=1,
        started_at=SHIFT_START,
        ended_at=SHIFT_END,
    )
    nozzle = make_nozzle(priced_fuel)
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="1000.00")
    make_collection(shift, mode="cash", amount="100000.00")
    make_expense(shift, mode="bank_transfer", amount="40000.00")

    body = await _position(client, shift, auth_headers(manager))

    assert Decimal(body["accountable_cash"]) == Decimal("100000.00")
    assert Decimal(body["gap"]) == Decimal("0.00")
    assert Decimal(body["cash_expenses"]) == Decimal("0.00")


# --- the guard that matters most ---------------------------------------------------


async def test_expected_closing_still_subtracts_every_cash_expense(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    priced_fuel,
    auth_headers,
) -> None:
    """**The most important test in this file.**

    §6.4's day equation was correct before phase 17 and must stay byte-identical: the locker
    is lighter by every cash expense, whoever handed the notes over. This is the half of the
    owner's argument that makes the other half safe — *the total sum would remain the same*.

        opening 79,790 + cash_sales 100,000 − expenses 60,000 = 119,790
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    admin = make_user("admin")
    business_date = date(2026, 4, 6)

    shift = make_shift(
        attendant,
        business_date=business_date,
        sequence=1,
        started_at=datetime(2026, 4, 6, 0, 30, tzinfo=timezone.utc),
        ended_at=datetime(2026, 4, 6, 16, 30, tzinfo=timezone.utc),
        # §5.2: a summary cannot be created while the date has an open shift.
        status="closed",
    )
    nozzle = make_nozzle(priced_fuel)
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="1000.00")
    make_collection(shift, mode="cash", amount="40000.00")
    make_expense(shift, mode="cash", amount="60000.00")

    response = await client.post(
        "/api/v1/daily-summaries",
        headers=auth_headers(admin),
        json={
            "business_date": business_date.isoformat(),
            "opening_balance": "79790.00",
        },
    )
    assert response.status_code in (200, 201), response.text
    summary = response.json()

    # Unchanged by phase 17: the expense is still subtracted here, in full.
    assert Decimal(summary["cash_expenses"]) == Decimal("60000.00")
    assert Decimal(summary["expected_closing"]) == Decimal("119790.00")

    # And the salesman is still accountable for the whole ₹1,00,000 his sales took.
    position = await _position(client, shift, auth_headers(manager))
    assert Decimal(position["accountable_cash"]) == Decimal("100000.00")
