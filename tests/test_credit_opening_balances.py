"""§5.2's `credit_opening_balances` and §6.6's third term. Phase 16.

Written before the code, per §10.

The rule under test is not arithmetic but a distinction: **an absent opening balance and a
₹0.00 one are different facts**, and §6.8's "zero as an answer, never zero as an omission"
is what makes the difference matter. A customer with no row is one nobody has looked at; a
customer with a ₹0.00 row is one somebody checked and found square.

Service-level assertions go through `SessionLocal` rather than a mock, following
`test_credit_outstanding.py`: the question is what the database sums.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.anyio

AS_OF = date(2026, 7, 1)
DAY = date(2026, 7, 3)
EARLIER = date(2026, 6, 28)


def _outstanding(customer_id: UUID) -> Decimal:
    from app.db.session import SessionLocal
    from app.services import credit as credit_service

    with SessionLocal() as session:
        return credit_service.outstanding(session, customer_id=customer_id)


def _live_opening(customer_id: UUID):
    from app.db.session import SessionLocal
    from app.services import credit as credit_service

    with SessionLocal() as session:
        row = credit_service.live_opening_balance(session, customer_id=customer_id)
        return None if row is None else (row.amount, row.as_of_date)


def _count() -> int:
    from sqlalchemy import text

    from app.db.session import SessionLocal

    with SessionLocal() as session:
        return session.execute(
            text("SELECT count(*) FROM credit_opening_balances")
        ).scalar_one()


async def _post(client, headers, *, customer_id, amount="12400.00", as_of=AS_OF):
    return await client.post(
        "/api/v1/credit-opening-balances",
        json={
            "credit_customer_id": str(customer_id),
            "amount": amount,
            "as_of_date": as_of.isoformat(),
        },
        headers=headers,
    )


# --- §6.6's third term -------------------------------------------------------


async def test_an_opening_balance_is_the_whole_outstanding_when_nothing_else_exists(
    make_credit_customer: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
) -> None:
    """The defect this table exists for: the software's ledger began after the pump's."""
    customer = make_credit_customer(name="Ramesh", phone="9000000101")
    make_credit_opening_balance(customer, amount="12400.00", as_of_date=AS_OF)

    assert _outstanding(customer) == Decimal("12400.00")


async def test_an_opening_balance_adds_to_later_sales_and_repayments(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Suresh", phone="9000000102")

    make_credit_opening_balance(customer, amount="3150.00", as_of_date=AS_OF)
    make_credit_sale(shift, customer, make_attachment(attendant), amount="1000.00")
    make_credit_repayment(shift, customer, amount="800.00", mode="cash")

    assert _outstanding(customer) == Decimal("3350.00")


async def test_a_repayment_no_longer_drives_a_real_debtor_negative(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
) -> None:
    """The owner's own words: nobody is good enough to pay back in advance.

    Without the opening balance this is -5,000 and the pump appears to owe him money.
    """
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Vikram", phone="9000000103")

    make_credit_opening_balance(customer, amount="12400.00", as_of_date=AS_OF)
    make_credit_repayment(shift, customer, amount="5000.00", mode="cash")

    assert _outstanding(customer) == Decimal("7400.00")


async def test_zero_is_an_answer_and_absence_is_not(
    make_credit_customer: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
) -> None:
    """§6.8, one table over. Both outstanding figures are ₹0.00 and the facts differ."""
    checked = make_credit_customer(name="Checked", phone="9000000104")
    never_looked_at = make_credit_customer(name="Unknown", phone="9000000105")
    make_credit_opening_balance(checked, amount="0.00", as_of_date=AS_OF)

    assert _outstanding(checked) == Decimal("0.00")
    assert _outstanding(never_looked_at) == Decimal("0.00")

    # The distinction the screen has to be able to draw.
    assert _live_opening(checked) == (Decimal("0.00"), AS_OF)
    assert _live_opening(never_looked_at) is None


async def test_a_negative_opening_balance_is_accepted(
    make_credit_customer: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
) -> None:
    """§6.6 already permits a negative outstanding -- a customer who paid in advance.

    This is why the table carries no sign CHECK: the sign cannot distinguish an original
    from a reversal here, so `reverses_id` carries that alone (§5.2).
    """
    customer = make_credit_customer(name="Prepaid", phone="9000000106")
    make_credit_opening_balance(customer, amount="-450.00", as_of_date=AS_OF)

    assert _outstanding(customer) == Decimal("-450.00")


async def test_a_reversed_opening_balance_nets_out(
    make_credit_customer: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
) -> None:
    """Sum every row, reversals included -- §6.6's convention, unchanged."""
    customer = make_credit_customer(name="Corrected", phone="9000000107")
    original = make_credit_opening_balance(customer, amount="12400.00", as_of_date=AS_OF)
    make_credit_opening_balance(
        customer,
        amount="-12400.00",
        as_of_date=AS_OF,
        reverses_id=original,
        reversal_reason="Wrong figure taken from the old register.",
    )
    make_credit_opening_balance(customer, amount="14200.00", as_of_date=AS_OF)

    assert _outstanding(customer) == Decimal("14200.00")
    assert _live_opening(customer) == (Decimal("14200.00"), AS_OF)


# --- the endpoint ------------------------------------------------------------


async def test_an_admin_can_set_an_opening_balance(
    client,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    admin = make_user("admin")
    customer = make_credit_customer(name="Ramesh", phone="9000000108")

    response = await _post(client, auth_headers(admin), customer_id=customer)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["amount"] == "12400.00"
    assert body["as_of_date"] == AS_OF.isoformat()
    assert _outstanding(customer) == Decimal("12400.00")


async def test_a_manager_cannot_set_an_opening_balance(
    client,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """§8: admin only, for the reason §6.5 makes the cash locker's seed admin only."""
    manager = make_user("manager")
    customer = make_credit_customer(name="Ramesh", phone="9000000109")
    before = _count()

    response = await _post(client, auth_headers(manager), customer_id=customer)

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"
    assert _count() == before


async def test_a_second_live_opening_balance_is_refused_and_writes_nothing(
    client,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """§5.2: one live row per customer, in the service rather than as a constraint."""
    admin = make_user("admin")
    customer = make_credit_customer(name="Ramesh", phone="9000000110")
    assert (await _post(client, auth_headers(admin), customer_id=customer)).status_code == 201
    before = _count()

    response = await _post(
        client, auth_headers(admin), customer_id=customer, amount="500.00"
    )

    assert response.status_code == 409
    assert response.json()["code"] == "OPENING_BALANCE_ALREADY_SET"
    assert _count() == before
    assert _outstanding(customer) == Decimal("12400.00")


async def test_reversing_frees_the_slot_for_a_replacement(
    client,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    admin = make_user("admin")
    customer = make_credit_customer(name="Ramesh", phone="9000000111")
    created = await _post(client, auth_headers(admin), customer_id=customer)
    balance_id = created.json()["id"]

    reversal = await client.post(
        f"/api/v1/credit-opening-balances/{balance_id}/reversals",
        json={"reason": "Wrong figure taken from the old register."},
        headers={**auth_headers(admin), "Idempotency-Key": str(uuid4())},
    )
    assert reversal.status_code == 201, reversal.text
    assert _outstanding(customer) == Decimal("0.00")
    assert _live_opening(customer) is None

    replacement = await _post(
        client, auth_headers(admin), customer_id=customer, amount="14200.00"
    )
    assert replacement.status_code == 201
    assert _outstanding(customer) == Decimal("14200.00")


async def test_a_customer_from_another_outlet_is_not_found(
    client,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    engine,
) -> None:
    """404, not 403. The customer id arrives in the *body*, so there is no outlet resolver
    on the path -- the lookup is scoped to `actor.outlet_id` and simply does not find it,
    which is `resolve_customer`'s existing behaviour and the right one."""
    from sqlalchemy import text

    admin = make_user("admin")
    other_outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outlets (id, name) VALUES (:id, 'Second Outlet')"
            ).bindparams(id=other_outlet)
        )
    try:
        customer = make_credit_customer(
            name="Elsewhere", phone="9000000112", outlet_id=other_outlet
        )

        response = await _post(client, auth_headers(admin), customer_id=customer)

        assert response.status_code == 404
        assert response.json()["code"] == "CREDIT_CUSTOMER_NOT_FOUND"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM credit_customers WHERE outlet_id = :id").bindparams(
                    id=other_outlet
                )
            )
            connection.execute(
                text("DELETE FROM outlets WHERE id = :id").bindparams(id=other_outlet)
            )


# --- the double-count guard, both directions ---------------------------------


async def test_a_credit_sale_dated_before_the_opening_balance_is_refused(
    client,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """The opening figure already contains it; counting it again is the whole risk."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=EARLIER, sequence=1)
    customer = make_credit_customer(name="Ramesh", phone="9000000113")
    make_credit_opening_balance(customer, amount="12400.00", as_of_date=AS_OF)

    response = await client.post(
        f"/api/v1/shifts/{shift}/credit-sales",
        json={
            "credit_customer_id": str(customer),
            "attachment_id": str(make_attachment(attendant)),
            "amount": "500.00",
        },
        headers={**auth_headers(attendant), "Idempotency-Key": str(uuid4())},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "BEFORE_OPENING_BALANCE_DATE"
    assert _outstanding(customer) == Decimal("12400.00")


async def test_an_opening_balance_after_earlier_entries_exist_is_refused(
    client,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """The same guard from the other side."""
    admin = make_user("admin")
    shift = make_shift(admin, business_date=EARLIER, sequence=1)
    customer = make_credit_customer(name="Ramesh", phone="9000000114")
    make_credit_sale(shift, customer, make_attachment(admin), amount="500.00")
    before = _count()

    response = await _post(client, auth_headers(admin), customer_id=customer)

    assert response.status_code == 409
    assert response.json()["code"] == "ENTRIES_BEFORE_OPENING_BALANCE"
    assert _count() == before


async def test_an_entry_on_the_opening_balance_date_itself_is_allowed(
    client,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """The boundary is strictly *before*, matching §6.6's and §6.7's convention."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=AS_OF, sequence=1)
    customer = make_credit_customer(name="Ramesh", phone="9000000115")
    make_credit_opening_balance(customer, amount="12400.00", as_of_date=AS_OF)

    response = await client.post(
        f"/api/v1/shifts/{shift}/credit-sales",
        json={
            "credit_customer_id": str(customer),
            "attachment_id": str(make_attachment(attendant)),
            "amount": "500.00",
        },
        headers={**auth_headers(attendant), "Idempotency-Key": str(uuid4())},
    )

    assert response.status_code == 201, response.text
    assert _outstanding(customer) == Decimal("12900.00")


# --- it reaches the credit limit ---------------------------------------------


async def test_an_opening_balance_counts_toward_the_credit_limit(
    client,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """§6.6. A customer at 12,400 of history with a 15,000 limit has 2,600 of room."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(
        name="Ramesh", phone="9000000116", credit_limit="15000.00"
    )
    make_credit_opening_balance(customer, amount="12400.00", as_of_date=AS_OF)

    async def sale(amount: str):
        return await client.post(
            f"/api/v1/shifts/{shift}/credit-sales",
            json={
                "credit_customer_id": str(customer),
                "attachment_id": str(make_attachment(attendant)),
                "amount": amount,
            },
            headers={**auth_headers(attendant), "Idempotency-Key": str(uuid4())},
        )

    # One paisa over the remaining room.
    over = await sale("2600.01")
    assert over.status_code == 409
    assert over.json()["code"] == "CREDIT_LIMIT_EXCEEDED"

    # Exactly on the limit is accepted -- §6.6's boundary is strictly `>`.
    exact = await sale("2600.00")
    assert exact.status_code == 201, exact.text
    assert _outstanding(customer) == Decimal("15000.00")
