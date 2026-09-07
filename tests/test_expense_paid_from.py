"""Where a cash expense's money came from (CLAUDE.md §5.2, §6.4, §11 phase 17, §13.33).

`expenses.mode` says *how* the money left. `paid_from` says *whose pile it left from*, and
§6.4 needs both because its two equations ask different questions:

* **`accountable_cash`** — what should this one salesman be holding? Subtracts only the cash
  expenses he paid out of his own takings (`paid_from = shift_cash`).
* **`expected_closing`** — what should be in the locker? Subtracts **every** cash expense,
  whoever handed the notes over, because the locker is genuinely lighter by all of it.

The file exists because of a real trading day. On 30 July this outlet took ₹302,827 of
metered fuel, ₹265,617 of it on card and Paytm, and issued ₹19,610 of udhaar — leaving
₹17,600 of cash, which the salesman declared correctly. The day's ₹60,170 of bills were then
paid from the locker's opening balance, because the day's own cash could not cover them.
Charging all of that to a shift that had taken ₹17,600 drove `accountable_cash` to −₹42,569
and reported the salesman **₹60,169 in surplus** — holding money nobody gave him.

`test_the_thirtieth_of_july` is that day, and it is the regression test for the whole phase.
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

DAY = date(2027, 3, 1)
# This outlet trades 06:00 -> 22:00 IST; 06:00 IST is 00:30 UTC.
SHIFT_START = datetime(2027, 3, 1, 0, 30, tzinfo=timezone.utc)
SHIFT_END = datetime(2027, 3, 1, 16, 30, tzinfo=timezone.utc)
BEFORE = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _position(client: AsyncClient, shift: UUID, headers: dict[str, str]):
    response = await client.get(
        f"/api/v1/shifts/{shift}/cash-position", headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def priced_fuel(make_fuel_type, make_fuel_price, make_user):
    """A fuel priced at ₹100/litre with **deliberately no margin** (§6.3, §14)."""
    admin = make_user("admin")
    fuel = make_fuel_type(code="PAIDFROM", unit_of_measure="litre")
    make_fuel_price(fuel, "100.00", BEFORE, entered_by=admin)
    return fuel


# --- the day that produced the bug -------------------------------------------------


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

        3,028.27 L x ₹100     = ₹3,02,827 metered
        - ₹2,65,617 card+upi
        - ₹19,610 udhaar
                                = ₹17,600 accountable   ← and he declared ₹17,600
        - ₹60,170 of bills paid FROM THE LOCKER          ← excluded, not his money

    Before this phase the bills were subtracted here too, giving −₹42,569 accountable
    against a ₹17,600 declaration — a ₹60,169 surplus in a salesman's name.
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
    make_credit_sale(
        shift, customer, make_attachment(attendant), amount="19610.00"
    )
    # The bills. Paid from the locker's opening balance, because the day's own cash could
    # not cover them -- which is the whole fact this column exists to record.
    make_expense(shift, mode="cash", amount="60170.00", paid_from="locker_cash")

    body = await _position(client, shift, auth_headers(manager))

    assert Decimal(body["metered_fuel_sales"]) == Decimal("302827.00")
    assert Decimal(body["card_total"]) == Decimal("265617.00")
    assert Decimal(body["credit_sales_total"]) == Decimal("19610.00")

    # The figure that was −42,569.02 before this phase.
    assert Decimal(body["accountable_cash"]) == Decimal("17600.00")
    assert Decimal(body["declared_cash"]) == Decimal("17600.00")
    # The ₹60,169.02 phantom surplus is gone. The day reconciles exactly.
    assert Decimal(body["gap"]) == Decimal("0.00")
    assert Decimal(body["locker_funded_expenses"]) == Decimal("60170.00")


async def test_a_locker_funded_expense_is_excluded_from_accountable_cash(
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
    """§6.4: a bill paid from the locker never passed through the salesman's hands.

    ₹1,00,000 of metered fuel, all of it cash, and a ₹60,000 bill paid from the locker.
    He is accountable for the whole ₹1,00,000 — the bill is not his.
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant,
        business_date=date(2027, 3, 2),
        sequence=1,
        started_at=SHIFT_START,
        ended_at=SHIFT_END,
    )
    nozzle = make_nozzle(priced_fuel)
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="1000.00")
    make_collection(shift, mode="cash", amount="100000.00")
    make_expense(shift, mode="cash", amount="60000.00", paid_from="locker_cash")

    body = await _position(client, shift, auth_headers(manager))

    assert Decimal(body["accountable_cash"]) == Decimal("100000.00")
    assert Decimal(body["gap"]) == Decimal("0.00")
    # Reported in full, so the day equation and the reader both still see it.
    assert Decimal(body["cash_expenses"]) == Decimal("60000.00")
    assert Decimal(body["locker_funded_expenses"]) == Decimal("60000.00")


async def test_a_shift_funded_expense_still_reduces_accountable_cash(
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
    """The ordinary case, unbroken. He paid it out of his own takings, so his hand is
    lighter by exactly that much and the comparison must mirror it (§6.4)."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant,
        business_date=date(2027, 3, 3),
        sequence=1,
        started_at=SHIFT_START,
        ended_at=SHIFT_END,
    )
    nozzle = make_nozzle(priced_fuel)
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="1000.00")
    make_collection(shift, mode="cash", amount="98500.00")
    make_expense(shift, mode="cash", amount="1500.00", paid_from="shift_cash")

    body = await _position(client, shift, auth_headers(manager))

    assert Decimal(body["accountable_cash"]) == Decimal("98500.00")
    assert Decimal(body["gap"]) == Decimal("0.00")
    assert Decimal(body["locker_funded_expenses"]) == Decimal("0.00")


async def test_the_default_is_shift_cash(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    priced_fuel,
    auth_headers,
    engine: Engine,
) -> None:
    """§5.2: the server default, which is what makes every pre-phase-17 row backfill
    correctly — that assumption is exactly what the old code encoded."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant,
        business_date=date(2027, 3, 4),
        sequence=1,
        started_at=SHIFT_START,
        ended_at=SHIFT_END,
    )
    nozzle = make_nozzle(priced_fuel)
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="1000.00")
    expense = make_expense(shift, mode="cash", amount="1000.00")

    with engine.connect() as connection:
        stored = connection.execute(
            text("SELECT paid_from FROM expenses WHERE id = :id").bindparams(id=expense)
        ).scalar_one()
    assert stored == "shift_cash"

    body = await _position(client, shift, auth_headers(manager))
    # Defaulted, so it behaves exactly as it did before the column existed.
    assert Decimal(body["accountable_cash"]) == Decimal("99000.00")


async def test_paid_from_is_ignored_for_a_non_cash_expense(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    priced_fuel,
    auth_headers,
) -> None:
    """§5.2: meaningful only when `mode = cash`. A bank-paid bill touches neither figure,
    whatever `paid_from` says — which is why no CHECK ties the two columns together."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant,
        business_date=date(2027, 3, 5),
        sequence=1,
        started_at=SHIFT_START,
        ended_at=SHIFT_END,
    )
    nozzle = make_nozzle(priced_fuel)
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="1000.00")
    make_expense(
        shift, mode="bank_transfer", amount="40000.00", paid_from="locker_cash"
    )

    body = await _position(client, shift, auth_headers(manager))

    assert Decimal(body["accountable_cash"]) == Decimal("100000.00")
    assert Decimal(body["cash_expenses"]) == Decimal("0.00")
    assert Decimal(body["locker_funded_expenses"]) == Decimal("0.00")


async def test_a_reversal_inherits_paid_from(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    priced_fuel,
    auth_headers,
    engine: Engine,
) -> None:
    """§6.9's reversal cancels the original **from the same pile**.

    Defaulting the reversal to `shift_cash` would credit the salesman with a locker-funded
    bill he never held — the Phase 17 phantom in the opposite direction, and it nets to a
    ₹60,000 error rather than cancelling to zero.
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant,
        business_date=date(2027, 3, 6),
        sequence=1,
        started_at=SHIFT_START,
        ended_at=SHIFT_END,
    )
    nozzle = make_nozzle(priced_fuel)
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="1000.00")
    make_collection(shift, mode="cash", amount="100000.00")
    expense = make_expense(
        shift, mode="cash", amount="60000.00", paid_from="locker_cash"
    )

    response = await client.post(
        f"/api/v1/shifts/{shift}/expenses/{expense}/reversals",
        headers={**auth_headers(manager), "Idempotency-Key": "rev-paid-from-1"},
        json={"reason": "Filed against the wrong day."},
    )
    assert response.status_code == 201, response.text

    with engine.connect() as connection:
        stored = connection.execute(
            text(
                "SELECT paid_from FROM expenses WHERE reverses_id = :id"
            ).bindparams(id=expense)
        ).scalar_one()
    assert stored == "locker_cash"

    body = await _position(client, shift, auth_headers(manager))
    # Both rows are locker-funded, so they net to zero on BOTH sides of the equation.
    assert Decimal(body["accountable_cash"]) == Decimal("100000.00")
    assert Decimal(body["cash_expenses"]) == Decimal("0.00")
    assert Decimal(body["locker_funded_expenses"]) == Decimal("0.00")


# --- the guard that matters most ---------------------------------------------------


async def test_expected_closing_is_unchanged_by_paid_from(
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
    """**The single most important test in this file.**

    §6.4's day equation was correct before phase 17 and must stay byte-identical: the locker
    is lighter by every cash expense, whoever paid it. Two days identical but for
    `paid_from` must produce the same `expected_closing` — and different `accountable_cash`.
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    admin = make_user("admin")
    # One nozzle, reused across both days -- labels are unique per outlet, and the meter
    # chains from one day's close to the next exactly as §4.7 describes.
    nozzle = make_nozzle(priced_fuel)

    async def _day(
        business_date: date,
        paid_from: str,
        opening: str,
        closing: str,
        seed_opening: str | None,
    ) -> tuple[Decimal, Decimal]:
        shift = make_shift(
            attendant,
            business_date=business_date,
            sequence=1,
            started_at=datetime(
                business_date.year,
                business_date.month,
                business_date.day,
                0,
                30,
                tzinfo=timezone.utc,
            ),
            ended_at=datetime(
                business_date.year,
                business_date.month,
                business_date.day,
                16,
                30,
                tzinfo=timezone.utc,
            ),
            # §5.2: a summary cannot be created while the date has an open shift.
            status="closed",
        )
        make_reading(
            shift, nozzle, opening_reading=opening, closing_reading=closing
        )
        make_collection(shift, mode="cash", amount="40000.00")
        make_expense(shift, mode="cash", amount="60000.00", paid_from=paid_from)

        position = await _position(client, shift, auth_headers(manager))

        # §6.5: only the first day is seeded. The second chains its opening from the first,
        # and supplying one there is refused with OPENING_BALANCE_IS_CHAINED.
        payload: dict[str, str] = {"business_date": business_date.isoformat()}
        if seed_opening is not None:
            payload["opening_balance"] = seed_opening
        response = await client.post(
            "/api/v1/daily-summaries",
            headers=auth_headers(admin),
            json=payload,
        )
        assert response.status_code in (200, 201), response.text
        summary = response.json()
        # The day's own movement, with the chained opening taken back out -- the two days
        # start from different openings (§6.5 chains the second), so comparing the raw
        # closing figures would be comparing the chain, not this column.
        movement = Decimal(summary["expected_closing"]) - Decimal(
            summary["opening_balance"]
        )
        return movement, Decimal(position["accountable_cash"])

    shift_movement, shift_accountable = await _day(
        date(2026, 3, 8), "shift_cash", "0.00", "1000.00", "79790.00"
    )
    locker_movement, locker_accountable = await _day(
        date(2026, 3, 9), "locker_cash", "1000.00", "2000.00", None
    )

    # The locker lost ₹60,000 either way. **This is the guard**: §6.4's day equation is
    # correct today and phase 17 must leave it byte-identical.
    assert shift_movement == locker_movement

    # But only one of them was the salesman's money.
    assert shift_accountable == Decimal("40000.00")
    assert locker_accountable == Decimal("100000.00")
