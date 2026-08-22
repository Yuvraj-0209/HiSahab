"""Credit repayments over HTTP (CLAUDE.md §4.4, §5.2, §6.4, §6.6, §6.9).

Two things here are not shared with credit sales and carry the weight: a deactivated customer
may still pay (§5.1's asymmetry), and `mode` decides whether the money ever reaches §6.4's
drawer equation.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.anyio

DAY = date(2026, 10, 9)


async def _post(
    client: AsyncClient,
    headers: dict[str, str],
    key: str,
    *,
    shift_id: UUID,
    customer_id: UUID,
    **body,
):
    payload = {
        "credit_customer_id": str(customer_id),
        "amount": "500.00",
        "mode": "cash",
        **body,
    }
    return await client.post(
        f"/api/v1/shifts/{shift_id}/credit-repayments",
        json=payload,
        headers={**headers, "Idempotency-Key": key},
    )


def _outstanding(customer_id: UUID) -> Decimal:
    from app.db.session import SessionLocal
    from app.services import credit as credit_service

    with SessionLocal() as session:
        return credit_service.outstanding(session, customer_id=customer_id)


# --- the happy path ----------------------------------------------------------


async def test_a_repayment_reduces_the_balance(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer()
    make_credit_sale(shift, customer, make_attachment(attendant), amount="2000.00")

    response = await _post(
        client, auth_headers(attendant), "repay-1", shift_id=shift,
        customer_id=customer, amount="800.00",
    )

    assert response.status_code == 201
    assert _outstanding(customer) == Decimal("1200.00")


async def test_a_repayment_needs_no_receipt(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """Unlike a credit sale. The pump writes the receipt for money coming in, so there is no
    counterparty document to demand."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(
        client, auth_headers(attendant), "no-receipt-needed", shift_id=shift,
        customer_id=make_credit_customer(),
    )

    assert response.status_code == 201
    assert response.json()["attachment_id"] is None


async def test_a_repayment_may_carry_a_deposit_slip(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_credit,
) -> None:
    """Optional, but worth keeping when there is one -- and linking it stamps `linked_at`, so
    §7.4's sweep will never reclaim it."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    attachment = make_attachment(attendant, linked_at=None)

    response = await _post(
        client, auth_headers(attendant), "with-slip", shift_id=shift,
        customer_id=make_credit_customer(), mode="bank_transfer",
        attachment_id=str(attachment),
    )

    assert response.status_code == 201
    with engine.connect() as connection:
        linked_at = connection.execute(
            text("SELECT linked_at FROM attachments WHERE id = :id").bindparams(
                id=attachment
            )
        ).scalar_one()
    assert linked_at is not None


@pytest.mark.parametrize("mode", ["cash", "card", "upi", "bank_transfer"])
async def test_every_repayment_mode_is_accepted(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
    mode: str,
) -> None:
    """`credit_repayment_mode` is its own type: `bank_transfer` is here and `wallet` is not,
    unlike `collection_mode` (§5.2). A customer settling a bill can wire money; one buying
    diesel at the pump cannot."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(
        client, auth_headers(attendant), f"mode-{mode}", shift_id=shift,
        customer_id=make_credit_customer(), mode=mode,
    )

    assert response.status_code == 201
    assert response.json()["mode"] == mode


async def test_wallet_is_not_a_repayment_mode(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """The other half of the enum decision: `wallet` belongs to `collection_mode` and nobody
    settles an old bill with it."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(
        client, auth_headers(attendant), "wallet-mode", shift_id=shift,
        customer_id=make_credit_customer(), mode="wallet",
    )

    assert response.status_code == 422


async def test_only_cash_repayments_count_towards_the_drawer(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """§6.4 and §10's named case: a cash repayment increases expected cash, a UPI one does
    not. Phase 10 assembles that equation; `cash_total` is the term it will read, computed
    here next to the enum that defines the modes.
    """
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer()
    headers = auth_headers(attendant)

    await _post(
        client, headers, "drawer-cash", shift_id=shift, customer_id=customer,
        amount="1000.00", mode="cash",
    )
    await _post(
        client, headers, "drawer-upi", shift_id=shift, customer_id=customer,
        amount="4000.00", mode="upi",
    )

    response = await client.get(
        f"/api/v1/shifts/{shift}/credit-repayments", headers=headers
    )

    body = response.json()
    assert Decimal(body["total"]) == Decimal("5000.00")
    assert Decimal(body["cash_total"]) == Decimal("1000.00")


# --- §5.1's asymmetry --------------------------------------------------------


async def test_a_deactivated_customer_may_still_repay(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """The asymmetry §5.1 spells out, and the one that would be easiest to get backwards.

    You deactivate somebody precisely to stop the debt growing while they pay off what they
    owe. Refusing their money would strand a balance nothing could ever clear.
    """
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Retired But Owing")
    make_credit_sale(shift, customer, make_attachment(attendant), amount="3000.00")

    # Retire them after the debt exists.
    from app.db.session import SessionLocal
    from app.models.credit import CreditCustomer

    with SessionLocal() as session:
        session.get(CreditCustomer, customer).is_active = False
        session.commit()

    response = await _post(
        client, auth_headers(attendant), "retired-repays", shift_id=shift,
        customer_id=customer, amount="3000.00",
    )

    assert response.status_code == 201
    assert _outstanding(customer) == Decimal("0.00")


async def test_an_unknown_customer_is_404(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(
        client, auth_headers(attendant), "unknown", shift_id=shift, customer_id=uuid4()
    )

    assert response.status_code == 404
    assert response.json()["code"] == "CREDIT_CUSTOMER_NOT_FOUND"


async def test_an_unknown_attachment_is_404_and_writes_nothing(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_credit,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(
        client, auth_headers(attendant), "bad-slip", shift_id=shift,
        customer_id=make_credit_customer(), attachment_id=str(uuid4()),
    )

    assert response.status_code == 404
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM credit_repayments")
        ).scalar_one()
    assert count == 0


# --- corrections -------------------------------------------------------------


async def test_a_repayment_can_be_corrected_while_the_shift_is_open(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    repayment = make_credit_repayment(
        shift, make_credit_customer(), amount="500.00", mode="cash"
    )

    response = await client.patch(
        f"/api/v1/shifts/{shift}/credit-repayments/{repayment}",
        json={"amount": "550.00", "mode": "upi"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["amount"] == "550.00"
    assert body["mode"] == "upi"


async def test_an_empty_patch_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    repayment = make_credit_repayment(shift, make_credit_customer())

    response = await client.patch(
        f"/api/v1/shifts/{shift}/credit-repayments/{repayment}",
        json={},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "NO_FIELDS_TO_UPDATE"


async def test_an_unknown_repayment_id_is_a_404(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/credit-repayments/{uuid4()}",
        json={"amount": "100.00"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "CREDIT_REPAYMENT_NOT_FOUND"


async def test_a_repayment_from_another_shift_is_a_409(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    first = make_shift(attendant, business_date=DAY, sequence=1, status="closed")
    second = make_shift(attendant, business_date=date(2026, 10, 10), sequence=1)
    repayment = make_credit_repayment(first, make_credit_customer())

    response = await client.patch(
        f"/api/v1/shifts/{second}/credit-repayments/{repayment}",
        json={"amount": "100.00"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CREDIT_REPAYMENT_NOT_IN_SHIFT"


# --- reversals (§6.9) --------------------------------------------------------


async def test_a_bounced_cheque_puts_the_debt_back(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer()
    make_credit_sale(shift, customer, make_attachment(attendant), amount="4000.00")
    repayment = make_credit_repayment(shift, customer, amount="4000.00")
    assert _outstanding(customer) == Decimal("0.00")

    response = await client.post(
        f"/api/v1/shifts/{shift}/credit-repayments/{repayment}/reversals",
        json={"reason": "cheque bounced"},
        headers={**auth_headers(manager), "Idempotency-Key": "bounce"},
    )

    assert response.status_code == 201
    assert response.json()["reversal"]["amount"] == "-4000.00"
    assert _outstanding(customer) == Decimal("4000.00")


async def test_a_reversal_can_carry_a_replacement_amount(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    """Applied in the same transaction, because without it a correction on a *closed* shift
    is impossible: the reversal lands and the follow-up POST is refused by `writable=True`."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1, status="closed")
    customer = make_credit_customer()
    repayment = make_credit_repayment(shift, customer, amount="1000.00", mode="upi")

    response = await client.post(
        f"/api/v1/shifts/{shift}/credit-repayments/{repayment}/reversals",
        json={"reason": "counted wrong", "replacement_amount": "900.00"},
        headers={**auth_headers(manager), "Idempotency-Key": "replace"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["replacement"]["amount"] == "900.00"
    # The replacement inherits the mode -- a correction is the same payment, right amount.
    assert body["replacement"]["mode"] == "upi"
    assert _outstanding(customer) == Decimal("-900.00")


async def test_a_repayment_cannot_be_reversed_twice(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    repayment = make_credit_repayment(shift, make_credit_customer())
    headers = auth_headers(manager)

    first = await client.post(
        f"/api/v1/shifts/{shift}/credit-repayments/{repayment}/reversals",
        json={"reason": "first reversal"},
        headers={**headers, "Idempotency-Key": "rev-1"},
    )
    second = await client.post(
        f"/api/v1/shifts/{shift}/credit-repayments/{repayment}/reversals",
        json={"reason": "second reversal"},
        headers={**headers, "Idempotency-Key": "rev-2"},
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["code"] == "ALREADY_REVERSED"


async def test_a_reversal_cannot_itself_be_reversed(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    repayment = make_credit_repayment(shift, make_credit_customer())
    headers = auth_headers(manager)
    reversal_id = (
        await client.post(
            f"/api/v1/shifts/{shift}/credit-repayments/{repayment}/reversals",
            json={"reason": "the first one"},
            headers={**headers, "Idempotency-Key": "undo-1"},
        )
    ).json()["reversal"]["id"]

    response = await client.post(
        f"/api/v1/shifts/{shift}/credit-repayments/{reversal_id}/reversals",
        json={"reason": "undoing the undo"},
        headers={**headers, "Idempotency-Key": "undo-2"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CANNOT_REVERSE_A_REVERSAL"


async def test_a_reversed_repayment_can_no_longer_be_patched(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    repayment = make_credit_repayment(shift, make_credit_customer())
    await client.post(
        f"/api/v1/shifts/{shift}/credit-repayments/{repayment}/reversals",
        json={"reason": "cancelled"},
        headers={**auth_headers(manager), "Idempotency-Key": "patch-after"},
    )

    response = await client.patch(
        f"/api/v1/shifts/{shift}/credit-repayments/{repayment}",
        json={"amount": "600.00"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CREDIT_REPAYMENT_ALREADY_REVERSED"


async def test_a_reversal_row_cannot_be_patched(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    repayment = make_credit_repayment(shift, make_credit_customer())
    reversal_id = (
        await client.post(
            f"/api/v1/shifts/{shift}/credit-repayments/{repayment}/reversals",
            json={"reason": "cancelled"},
            headers={**auth_headers(manager), "Idempotency-Key": "patch-rev"},
        )
    ).json()["reversal"]["id"]

    response = await client.patch(
        f"/api/v1/shifts/{shift}/credit-repayments/{reversal_id}",
        json={"amount": "600.00"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CANNOT_EDIT_A_REVERSAL"


async def test_a_manager_cannot_reverse_on_a_locked_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1, status="locked")
    repayment = make_credit_repayment(shift, make_credit_customer())

    response = await client.post(
        f"/api/v1/shifts/{shift}/credit-repayments/{repayment}/reversals",
        json={"reason": "too late"},
        headers={**auth_headers(manager), "Idempotency-Key": "locked-rev"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "LOCKED_SHIFT_REVERSAL_REQUIRES_ADMIN"


async def test_an_admin_can_reverse_on_a_locked_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    admin = make_user("admin")
    shift = make_shift(attendant, business_date=DAY, sequence=1, status="locked")
    repayment = make_credit_repayment(shift, make_credit_customer())

    response = await client.post(
        f"/api/v1/shifts/{shift}/credit-repayments/{repayment}/reversals",
        json={"reason": "admin correction"},
        headers={**auth_headers(admin), "Idempotency-Key": "admin-locked"},
    )

    assert response.status_code == 201


# --- idempotency and permissions ---------------------------------------------


async def test_the_same_key_twice_records_one_payment(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_credit,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer()
    headers = auth_headers(attendant)

    first = await _post(
        client, headers, "retry-pay", shift_id=shift, customer_id=customer
    )
    second = await _post(
        client, headers, "retry-pay", shift_id=shift, customer_id=customer
    )

    assert first.json() == second.json()
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM credit_repayments")
        ).scalar_one()
    assert count == 1


async def test_a_missing_idempotency_key_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await client.post(
        f"/api/v1/shifts/{shift}/credit-repayments",
        json={
            "credit_customer_id": str(make_credit_customer()),
            "amount": "100.00",
            "mode": "cash",
        },
        headers=auth_headers(attendant),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_an_attendant_cannot_record_a_payment_on_another_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    owner = make_user("attendant")
    other = make_user("attendant")
    shift = make_shift(owner, business_date=DAY, sequence=1)

    response = await _post(
        client, auth_headers(other), "not-mine", shift_id=shift,
        customer_id=make_credit_customer(),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_YOUR_SHIFT"


async def test_a_locked_shift_refuses_a_new_payment(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1, status="locked")

    response = await _post(
        client, auth_headers(attendant), "locked-pay", shift_id=shift,
        customer_id=make_credit_customer(),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_LOCKED"


async def test_an_attendant_cannot_reverse_a_payment(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    repayment = make_credit_repayment(shift, make_credit_customer())

    response = await client.post(
        f"/api/v1/shifts/{shift}/credit-repayments/{repayment}/reversals",
        json={"reason": "not allowed"},
        headers={**auth_headers(attendant), "Idempotency-Key": "attendant-rev"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


async def test_reversing_a_payment_is_idempotent(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """A retried reversal must not create a second negative row -- which would turn a
    cancelled ₹500 payment into ₹500 of debt the customer never incurred."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer()
    make_credit_sale(shift, customer, make_attachment(attendant), amount="1000.00")
    repayment = make_credit_repayment(shift, customer, amount="500.00")
    headers = {**auth_headers(manager), "Idempotency-Key": "idem-pay-rev"}
    body = {"reason": "recorded against the wrong customer"}

    first = await client.post(
        f"/api/v1/shifts/{shift}/credit-repayments/{repayment}/reversals",
        json=body, headers=headers,
    )
    second = await client.post(
        f"/api/v1/shifts/{shift}/credit-repayments/{repayment}/reversals",
        json=body, headers=headers,
    )

    assert first.status_code == 201
    assert first.json() == second.json()
    assert _outstanding(customer) == Decimal("1000.00")
    with engine.connect() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM credit_repayments WHERE reverses_id = :id"
            ).bindparams(id=repayment)
        ).scalar_one()
    assert count == 1


# --- the page totals must sum the table, not the page ------------------------------
#
# P10 Step 0. Phase 9 computed both `total` and `cash_total` inline with a Python `sum`
# over `rows` -- *after* `rows` had been truncated to `_MAX_ROWS`. Every sibling router
# already avoids this by calling a service that aggregates in SQL over the whole shift
# (`expenses.totals_by_category`, `collections.totals_by_mode`,
# `credit_sales.credit_sales_total`); this one file diverged.
#
# `cash_total` is a term of §6.4's cash equation. An understated one does not look wrong --
# it makes the salesman appear to be holding less than he is, which is the plausible-but-
# wrong number CLAUDE.md exists to prevent, and Phase 10 was about to consume it.


async def test_the_page_totals_sum_the_whole_shift_not_just_the_first_page(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    """101 repayments, `_MAX_ROWS` = 100. The 101st must still be inside both totals."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 10, 10), sequence=1)
    customer = make_credit_customer()

    for _ in range(101):
        make_credit_repayment(shift, customer, amount="10.00", mode="cash")

    response = await client.get(
        f"/api/v1/shifts/{shift}/credit-repayments", headers=auth_headers(manager)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["truncated"] is True
    assert len(body["items"]) == 100
    assert Decimal(body["total"]) == Decimal("1010.00")
    assert Decimal(body["cash_total"]) == Decimal("1010.00")


async def test_the_cash_total_still_excludes_non_drawer_modes_when_truncated(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    """The fix must not widen the filter: a bank transfer moves no money through the
    drawer, and §6.4 must never see it (§5.2's note on `credit_repayment_mode`)."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 10, 11), sequence=1)
    customer = make_credit_customer()

    for _ in range(100):
        make_credit_repayment(shift, customer, amount="10.00", mode="cash")
    make_credit_repayment(shift, customer, amount="5000.00", mode="bank_transfer")

    response = await client.get(
        f"/api/v1/shifts/{shift}/credit-repayments", headers=auth_headers(manager)
    )

    assert response.status_code == 200
    body = response.json()
    assert Decimal(body["total"]) == Decimal("6000.00")
    assert Decimal(body["cash_total"]) == Decimal("1000.00")
