"""Repayments that arrived at the bank, and §6.4's twelfth term. Phase 16.

Written before the code, per §10.

Two rules, and they are the same rule seen from two sides (§5.2):

    A repayment with a shift arrived at the pump.
    A repayment with only a date arrived at the bank.

**At the pump** means the money is inside that shift's collections total, so §6.4 has to
account for it: cash on the cash side as it always did, card and UPI on the *sales* side,
because `card_collections` is the machine's whole-day figure and subtracting it without
adding the settlement back shows the salesman a surplus he is not holding.

**At the bank** means §6.4 must not see it at all. The ledger still must.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.anyio

DAY = date(2026, 7, 8)


def _window(day: date) -> tuple[datetime, datetime]:
    """This outlet trades 06:00 -> 22:00 IST, i.e. 00:30 -> 16:30 UTC."""
    start = datetime(day.year, day.month, day.day, 0, 30, tzinfo=timezone.utc)
    return start, start + timedelta(hours=16)


def _outstanding(customer_id: UUID) -> Decimal:
    from app.db.session import SessionLocal
    from app.services import credit as credit_service

    with SessionLocal() as session:
        return credit_service.outstanding(session, customer_id=customer_id)


async def _position(client, headers, shift_id):
    return await client.get(
        f"/api/v1/shifts/{shift_id}/cash-position", headers=headers
    )


async def _post_dated(client, headers, **body):
    return await client.post(
        "/api/v1/credit-repayments",
        json=body,
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )


# --- the pump/bank rule at the database --------------------------------------


async def test_a_cash_repayment_without_a_shift_is_refused_by_the_database(
    make_credit_customer: Callable[..., UUID],
    engine,
) -> None:
    """`ck_credit_repayments_cash_needs_shift`, the belt to the API's braces (§6.6's habit).

    Cash can only land in a drawer. A cash repayment with nowhere to land is money §6.4
    would never count -- so it is refused below the application, not only inside it.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    customer = make_credit_customer(name="Bank payer", phone="9000000201")

    with pytest.raises(IntegrityError) as caught:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO credit_repayments (credit_customer_id, shift_id, "
                    "business_date, amount, mode) VALUES (:c, NULL, DATE '2026-07-08', "
                    "500.00, 'cash')"
                ).bindparams(c=customer)
            )

    assert "ck_credit_repayments_cash_needs_shift" in str(caught.value)


async def test_a_shift_backed_repayment_takes_its_date_from_the_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """§3 rule 7: the server recomputes it and refuses the client's word for it."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Ramesh", phone="9000000202")

    response = await client.post(
        f"/api/v1/shifts/{shift}/credit-repayments",
        json={
            "credit_customer_id": str(customer),
            "amount": "500.00",
            "mode": "cash",
        },
        headers={**auth_headers(attendant), "Idempotency-Key": str(uuid4())},
    )

    assert response.status_code == 201, response.text
    assert response.json()["business_date"] == DAY.isoformat()


# --- the endpoint that unblocks a locked month -------------------------------


async def test_a_bank_transfer_can_be_recorded_against_a_locked_day(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """The blocker this phase exists for.

    Before Phase 16 `shift_id` was NOT NULL, and §5.2 forbids modifying anything referencing
    a `locked` shift -- so reconstructing a month of ledger history was blocked by the very
    shifts that month already had.
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    make_shift(attendant, business_date=DAY, sequence=1, status="locked")
    customer = make_credit_customer(name="Ramesh", phone="9000000203")

    response = await _post_dated(
        client,
        auth_headers(manager),
        credit_customer_id=str(customer),
        amount="4000.00",
        mode="bank_transfer",
        business_date=DAY.isoformat(),
    )

    assert response.status_code == 201, response.text
    assert response.json()["shift_id"] is None
    assert response.json()["business_date"] == DAY.isoformat()
    assert _outstanding(customer) == Decimal("-4000.00")


async def test_a_bank_transfer_needs_no_shift_to_exist_at_all(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """A day the outlet was shut has no shift to attach to, and money still arrives."""
    manager = make_user("manager")
    customer = make_credit_customer(name="Ramesh", phone="9000000204")

    response = await _post_dated(
        client,
        auth_headers(manager),
        credit_customer_id=str(customer),
        amount="1000.00",
        mode="bank_transfer",
        business_date=date(2026, 7, 12).isoformat(),
    )

    assert response.status_code == 201, response.text


async def test_a_dated_repayment_refuses_cash(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    manager = make_user("manager")
    customer = make_credit_customer(name="Ramesh", phone="9000000205")

    response = await _post_dated(
        client,
        auth_headers(manager),
        credit_customer_id=str(customer),
        amount="1000.00",
        mode="cash",
        business_date=DAY.isoformat(),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "CASH_REPAYMENT_NEEDS_SHIFT"


async def test_an_attendant_cannot_record_a_dated_repayment(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """§8, manager floor: a shift-less repayment has no ownership axis to check."""
    attendant = make_user("attendant")
    customer = make_credit_customer(name="Ramesh", phone="9000000206")

    response = await _post_dated(
        client,
        auth_headers(attendant),
        credit_customer_id=str(customer),
        amount="1000.00",
        mode="bank_transfer",
        business_date=DAY.isoformat(),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


async def test_a_dated_repayment_is_idempotent(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """§6.10. Attendants and managers use phones on patchy rural connectivity."""
    manager = make_user("manager")
    customer = make_credit_customer(name="Ramesh", phone="9000000207")
    key = str(uuid4())
    body = {
        "credit_customer_id": str(customer),
        "amount": "1000.00",
        "mode": "bank_transfer",
        "business_date": DAY.isoformat(),
    }

    first = await client.post(
        "/api/v1/credit-repayments",
        json=body,
        headers={**auth_headers(manager), "Idempotency-Key": key},
    )
    second = await client.post(
        "/api/v1/credit-repayments",
        json=body,
        headers={**auth_headers(manager), "Idempotency-Key": key},
    )

    assert first.status_code == 201
    assert second.json() == first.json()
    assert _outstanding(customer) == Decimal("-1000.00")


# --- §6.4's twelfth term -----------------------------------------------------


async def test_a_card_repayment_on_a_shift_no_longer_invents_a_surplus(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    """§6.4's worked example, as a test. The owner confirms this happens.

    The salesman took ₹21,000 on the machine, of which ₹1,000 was Ramesh settling an old
    bill rather than buying anything. Nothing else moved. He therefore holds ₹0 in cash, and
    before this term the system said he was ₹1,000 up on money nobody gave him.
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    started_at, ended_at = _window(DAY)
    shift = make_shift(
        attendant,
        business_date=DAY,
        sequence=1,
        started_at=started_at,
        ended_at=ended_at,
    )
    customer = make_credit_customer(name="Ramesh", phone="9000000208")

    make_collection(shift, mode="card", amount="1000.00")
    make_credit_repayment(shift, customer, amount="1000.00", mode="card")

    response = await _position(client, auth_headers(manager), shift)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["card_upi_credit_repayments"] == "1000.00"
    # The card collection and the settlement cancel: no fuel moved, so no cash is due.
    assert body["accountable_cash"] == "0.00"


async def test_a_upi_repayment_on_a_shift_behaves_the_same_way(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    started_at, ended_at = _window(DAY)
    shift = make_shift(
        attendant,
        business_date=DAY,
        sequence=1,
        started_at=started_at,
        ended_at=ended_at,
    )
    customer = make_credit_customer(name="Ramesh", phone="9000000209")

    make_collection(shift, mode="upi", amount="2500.00")
    make_credit_repayment(shift, customer, amount="2500.00", mode="upi")

    body = (await _position(client, auth_headers(manager), shift)).json()

    assert body["card_upi_credit_repayments"] == "2500.00"
    assert body["accountable_cash"] == "0.00"


async def test_a_bank_transfer_repayment_touches_no_cash_term(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    """It reached a bank account. §6.4 must not see it (§14)."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    started_at, ended_at = _window(DAY)
    shift = make_shift(
        attendant,
        business_date=DAY,
        sequence=1,
        started_at=started_at,
        ended_at=ended_at,
    )
    customer = make_credit_customer(name="Ramesh", phone="9000000210")
    make_credit_repayment(shift, customer, amount="7000.00", mode="bank_transfer")

    body = (await _position(client, auth_headers(manager), shift)).json()

    assert body["card_upi_credit_repayments"] == "0.00"
    assert body["cash_credit_repayments"] == "0.00"
    assert body["accountable_cash"] == "0.00"


async def test_a_shift_less_card_repayment_touches_no_cash_term(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """The pump/bank rule's sharp edge: the *presence of a shift* is the whole test.

    A card repayment with no shift never went through this pump's machine, so it is not
    inside any shift's collections total and must not be added back to one.
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    started_at, ended_at = _window(DAY)
    shift = make_shift(
        attendant,
        business_date=DAY,
        sequence=1,
        started_at=started_at,
        ended_at=ended_at,
    )
    customer = make_credit_customer(name="Ramesh", phone="9000000211")
    make_credit_repayment(
        None, customer, amount="3000.00", mode="card", business_date=DAY
    )

    body = (await _position(client, auth_headers(manager), shift)).json()

    assert body["card_upi_credit_repayments"] == "0.00"
    assert body["accountable_cash"] == "0.00"
    # It is still in the ledger, which is the whole point of recording it.
    assert _outstanding(customer) == Decimal("-3000.00")


async def test_the_daily_summary_stores_the_new_component(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
    clean_cash,
) -> None:
    """§5.2 stores every term, not just the total -- so a manager can see which one moved."""
    admin = make_user("admin")
    attendant = make_user("attendant")
    day = date(2026, 7, 15)
    started_at, ended_at = _window(day)
    shift = make_shift(
        attendant,
        business_date=day,
        sequence=1,
        started_at=started_at,
        ended_at=ended_at,
        status="closed",
    )
    customer = make_credit_customer(name="Ramesh", phone="9000000212")
    make_collection(shift, mode="card", amount="1000.00")
    make_credit_repayment(shift, customer, amount="1000.00", mode="card")

    response = await client.post(
        "/api/v1/daily-summaries",
        json={"business_date": day.isoformat(), "opening_balance": "0.00"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 201, response.text
    assert response.json()["card_upi_credit_repayments"] == "1000.00"
    assert Decimal(response.json()["expected_closing"]) == Decimal("0.00")


# --- §6.9's correction path for a row with no shift --------------------------
#
# The hole this section closes was found by probing the real app, not in review: the reversal
# route is `/shifts/{shift_id}/credit-repayments/{id}/reversals`, and a repayment with no
# shift can never reach it. §6.9 makes a reversal the *only* way to correct a money row, so a
# mistyped bank transfer was uncorrectable -- permanently wrong in a customer's ledger.


async def test_a_dated_repayment_can_be_reversed(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    manager = make_user("manager")
    customer = make_credit_customer(name="Ramesh", phone="9000000301")

    created = await _post_dated(
        client,
        auth_headers(manager),
        credit_customer_id=str(customer),
        amount="4000.00",
        mode="bank_transfer",
        business_date=DAY.isoformat(),
    )
    assert created.status_code == 201
    assert _outstanding(customer) == Decimal("-4000.00")

    reversal = await client.post(
        f"/api/v1/credit-repayments/{created.json()['id']}/reversals",
        json={"reason": "Credited to the wrong customer."},
        headers={**auth_headers(manager), "Idempotency-Key": str(uuid4())},
    )

    assert reversal.status_code == 201, reversal.text
    body = reversal.json()
    assert Decimal(body["reversal"]["amount"]) == Decimal("-4000.00")
    assert body["reversal"]["business_date"] == DAY.isoformat()
    assert body["original"]["is_reversed"] is True
    assert _outstanding(customer) == Decimal("0.00")


async def test_a_dated_reversal_can_carry_a_replacement(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """Applied in the same transaction, for the reason every other reversal route gives."""
    manager = make_user("manager")
    customer = make_credit_customer(name="Ramesh", phone="9000000302")
    created = await _post_dated(
        client,
        auth_headers(manager),
        credit_customer_id=str(customer),
        amount="4000.00",
        mode="bank_transfer",
        business_date=DAY.isoformat(),
    )

    reversal = await client.post(
        f"/api/v1/credit-repayments/{created.json()['id']}/reversals",
        json={"reason": "Fat-fingered the amount.", "replacement_amount": "400.00"},
        headers={**auth_headers(manager), "Idempotency-Key": str(uuid4())},
    )

    assert reversal.status_code == 201, reversal.text
    assert Decimal(reversal.json()["replacement"]["amount"]) == Decimal("400.00")
    assert reversal.json()["replacement"]["shift_id"] is None
    assert _outstanding(customer) == Decimal("-400.00")


async def test_the_dated_route_refuses_a_repayment_that_has_a_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """Otherwise this route is a way around the locked-shift guard.

    The shift-scoped reversal route refuses a non-admin on a `locked` shift with 403
    `LOCKED_SHIFT_REVERSAL_REQUIRES_ADMIN`. A second route that reversed the same rows
    without that check would not be a convenience, it would be the hole.
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1, status="locked")
    customer = make_credit_customer(name="Ramesh", phone="9000000303")
    repayment = make_credit_repayment(shift, customer, amount="500.00", mode="cash")

    response = await client.post(
        f"/api/v1/credit-repayments/{repayment}/reversals",
        json={"reason": "Trying to dodge the locked-shift check."},
        headers={**auth_headers(manager), "Idempotency-Key": str(uuid4())},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "REPAYMENT_BELONGS_TO_A_SHIFT"
    assert _outstanding(customer) == Decimal("-500.00")


async def test_an_attendant_cannot_reverse_a_dated_repayment(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    attendant = make_user("attendant")
    customer = make_credit_customer(name="Ramesh", phone="9000000304")
    repayment = make_credit_repayment(
        None, customer, amount="900.00", mode="bank_transfer", business_date=DAY
    )

    response = await client.post(
        f"/api/v1/credit-repayments/{repayment}/reversals",
        json={"reason": "Not my call to make."},
        headers={**auth_headers(attendant), "Idempotency-Key": str(uuid4())},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"
