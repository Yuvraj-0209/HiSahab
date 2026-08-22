"""Credit sales over HTTP (CLAUDE.md §5.2, §6.6, §6.9, §6.10, §8).

The receipt rule is the one to watch. Unlike §6.11's conditional expense receipt, this one is
unconditional and enforced by a NOT NULL column -- so the interesting cases are not "when is
it required" but what happens to it across a reversal, and whether one photograph can justify
two rows.
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

DAY = date(2026, 9, 25)


async def _post(
    client: AsyncClient,
    headers: dict[str, str],
    key: str,
    *,
    shift_id: UUID,
    customer_id: UUID,
    attachment_id: UUID,
    **body,
):
    payload = {
        "credit_customer_id": str(customer_id),
        "attachment_id": str(attachment_id),
        "amount": "1200.00",
        **body,
    }
    return await client.post(
        f"/api/v1/shifts/{shift_id}/credit-sales",
        json=payload,
        headers={**headers, "Idempotency-Key": key},
    )


# --- the happy path ----------------------------------------------------------


async def test_an_attendant_can_issue_udhaar_on_their_own_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Ramesh")

    response = await _post(
        client,
        auth_headers(attendant),
        "issue-1",
        shift_id=shift,
        customer_id=customer,
        attachment_id=make_attachment(attendant),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["amount"] == "1200.00"
    assert body["is_reversed"] is False
    assert body["limit_override_reason"] is None


async def test_a_fuel_sale_records_quantity_against_its_fuel_type(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
    clean_credit,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer()

    response = await _post(
        client,
        auth_headers(attendant),
        "fuel-sale",
        shift_id=shift,
        customer_id=customer,
        attachment_id=make_attachment(attendant),
        fuel_type_id=str(fuel_type_ids["DIESEL"]),
        quantity="40.000",
        amount="3600.00",
    )

    assert response.status_code == 201
    assert response.json()["quantity"] == "40.000"


async def test_a_cbg_sale_is_recorded_in_kilograms_without_anything_assuming_litres(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
    engine: Engine,
    clean_credit,
) -> None:
    """§4.5: a quantity is a *measure*, not necessarily a volume. Nothing in the credit path
    converts, labels or validates it as litres -- the unit lives on the fuel type and this
    proves the CBG row survives the round trip unchanged."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer()

    response = await _post(
        client,
        auth_headers(attendant),
        "cbg-sale",
        shift_id=shift,
        customer_id=customer,
        attachment_id=make_attachment(attendant),
        fuel_type_id=str(fuel_type_ids["CBG"]),
        quantity="12.500",
        amount="1250.00",
    )

    assert response.status_code == 201
    with engine.connect() as connection:
        unit = connection.execute(
            text("SELECT unit_of_measure FROM fuel_types WHERE id = :id").bindparams(
                id=fuel_type_ids["CBG"]
            )
        ).scalar_one()
    assert unit == "kilogram"
    assert response.json()["quantity"] == "12.500"


async def test_a_non_fuel_credit_sale_needs_no_fuel_type(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """A can of oil or a puncture repair (§5.2)."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(
        client,
        auth_headers(attendant),
        "non-fuel",
        shift_id=shift,
        customer_id=make_credit_customer(),
        attachment_id=make_attachment(attendant),
        amount="450.00",
    )

    assert response.status_code == 201
    assert response.json()["fuel_type_id"] is None


async def test_a_quantity_without_a_fuel_type_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_credit,
) -> None:
    """§4.5: without a fuel type there is no unit, and 12 could mean litres or kilograms."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(
        client,
        auth_headers(attendant),
        "orphan-quantity",
        shift_id=shift,
        customer_id=make_credit_customer(),
        attachment_id=make_attachment(attendant),
        quantity="12.000",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "QUANTITY_NEEDS_FUEL_TYPE"
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM credit_sales")
        ).scalar_one()
    assert count == 0


# --- the receipt (§6.6) ------------------------------------------------------


async def test_a_credit_sale_with_no_attachment_id_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_credit,
) -> None:
    """§10's named case. Unlike an expense, the receipt is unconditional, so its absence is a
    payload error Pydantic refuses before any handler code runs."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await client.post(
        f"/api/v1/shifts/{shift}/credit-sales",
        json={
            "credit_customer_id": str(make_credit_customer()),
            "amount": "500.00",
        },
        headers={**auth_headers(attendant), "Idempotency-Key": "no-receipt"},
    )

    assert response.status_code == 422
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM credit_sales")
        ).scalar_one()
    assert count == 0


async def test_a_nonexistent_attachment_id_is_404_and_writes_nothing(
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
        client,
        auth_headers(attendant),
        "unknown-attachment",
        shift_id=shift,
        customer_id=make_credit_customer(),
        attachment_id=uuid4(),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "ATTACHMENT_NOT_FOUND"
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM credit_sales")
        ).scalar_one()
    assert count == 0


async def test_one_attachment_cannot_serve_two_live_credit_sales(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """§5.3: one photograph must not justify ten debts, which is the abuse the receipt
    requirement exists to catch."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer()
    attachment = make_attachment(attendant)

    first = await _post(
        client,
        auth_headers(attendant),
        "reuse-1",
        shift_id=shift,
        customer_id=customer,
        attachment_id=attachment,
    )
    assert first.status_code == 201

    second = await _post(
        client,
        auth_headers(attendant),
        "reuse-2",
        shift_id=shift,
        customer_id=customer,
        attachment_id=attachment,
    )

    assert second.status_code == 409
    assert second.json()["code"] == "ATTACHMENT_ALREADY_LINKED"


async def test_an_attachment_already_claimed_by_a_live_expense_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    clean_credit,
    clean_expenses,
) -> None:
    """The cross-table half of §5.3, which only became testable when credit_sales landed --
    `link()` had one claiming table until Phase 9 and now has two."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    attachment = make_attachment(attendant, linked_at=None)
    make_expense(shift, attachment_id=attachment, receipt_required=True)

    response = await _post(
        client,
        auth_headers(attendant),
        "expense-owns-it",
        shift_id=shift,
        customer_id=make_credit_customer(),
        attachment_id=attachment,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ATTACHMENT_ALREADY_LINKED"


async def test_an_expense_cannot_claim_a_live_credit_sales_attachment_either(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    expense_category_ids: dict[str, UUID],
    auth_headers,
    clean_credit,
    clean_expenses,
) -> None:
    """The other direction. `link()` is shared, so this proves the check is genuinely
    bidirectional rather than bolted onto one caller."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    attachment = make_attachment(attendant)
    make_credit_sale(shift, make_credit_customer(), attachment, amount="800.00")

    response = await client.post(
        f"/api/v1/shifts/{shift}/expenses",
        json={
            "category_id": str(expense_category_ids["MAINTENANCE"]),
            "mode": "cash",
            "amount": "300.00",
            "description": "trying to reuse a receipt",
            "attachment_id": str(attachment),
        },
        headers={**auth_headers(attendant), "Idempotency-Key": "expense-steal"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ATTACHMENT_ALREADY_LINKED"


async def test_an_attachment_from_another_outlet_is_404(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_credit,
) -> None:
    """Existence is not leaked across tenants (§7.3's posture, applied to writes)."""
    other_outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outlets (id, name) VALUES (:id, 'Second Outlet')"
            ).bindparams(id=other_outlet)
        )

    try:
        attendant = make_user("attendant")
        shift = make_shift(attendant, business_date=DAY, sequence=1)
        foreign = make_attachment(attendant, outlet_id=other_outlet)

        response = await _post(
            client,
            auth_headers(attendant),
            "cross-outlet-attachment",
            shift_id=shift,
            customer_id=make_credit_customer(),
            attachment_id=foreign,
        )

        assert response.status_code == 404
        assert response.json()["code"] == "ATTACHMENT_NOT_FOUND"
    finally:
        # The attachment has to go before the outlet it points at. `make_attachment`'s own
        # teardown would do it, but fixture teardown runs *after* this block, so the outlet
        # delete would hit a foreign key first.
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM attachments WHERE outlet_id = :id").bindparams(
                    id=other_outlet
                )
            )
            connection.execute(
                text("DELETE FROM outlets WHERE id = :id").bindparams(id=other_outlet)
            )


async def test_linking_stamps_linked_at_so_the_sweep_never_reclaims_it(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_credit,
) -> None:
    """§7.4: a linked attachment is never deleted, at any age -- and `linked_at IS NULL` is
    the only thing the sweep reads to decide."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    attachment = make_attachment(attendant, linked_at=None)

    await _post(
        client,
        auth_headers(attendant),
        "stamps-linked-at",
        shift_id=shift,
        customer_id=make_credit_customer(),
        attachment_id=attachment,
    )

    with engine.connect() as connection:
        linked_at = connection.execute(
            text("SELECT linked_at FROM attachments WHERE id = :id").bindparams(
                id=attachment
            )
        ).scalar_one()
    assert linked_at is not None


# --- the customer (§5.1, §6.6) -----------------------------------------------


async def test_an_unknown_customer_is_404(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(
        client,
        auth_headers(attendant),
        "unknown-customer",
        shift_id=shift,
        customer_id=uuid4(),
        attachment_id=make_attachment(attendant),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "CREDIT_CUSTOMER_NOT_FOUND"


async def test_a_deactivated_customer_cannot_take_new_udhaar(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    retired = make_credit_customer(name="Retired", is_active=False)

    response = await _post(
        client,
        auth_headers(attendant),
        "retired-customer",
        shift_id=shift,
        customer_id=retired,
        attachment_id=make_attachment(attendant),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CREDIT_CUSTOMER_INACTIVE"


async def test_a_customer_from_another_outlet_is_404(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_credit,
) -> None:
    other_outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outlets (id, name) VALUES (:id, 'Second Outlet')"
            ).bindparams(id=other_outlet)
        )

    try:
        attendant = make_user("attendant")
        shift = make_shift(attendant, business_date=DAY, sequence=1)
        foreign = make_credit_customer(outlet_id=other_outlet)

        response = await _post(
            client,
            auth_headers(attendant),
            "cross-outlet-customer",
            shift_id=shift,
            customer_id=foreign,
            attachment_id=make_attachment(attendant),
        )

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


# --- the credit limit over HTTP (§6.6) ---------------------------------------


async def test_a_sale_over_the_limit_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_credit,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Capped", credit_limit="1000.00")

    response = await _post(
        client,
        auth_headers(attendant),
        "over-limit",
        shift_id=shift,
        customer_id=customer,
        attachment_id=make_attachment(attendant),
        amount="1000.01",
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CREDIT_LIMIT_EXCEEDED"
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM credit_sales")
        ).scalar_one()
    assert count == 0


async def test_an_admin_can_override_the_limit_with_a_reason(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_credit,
) -> None:
    """§6.6: the override is stored on the row AND audit-logged."""
    admin = make_user("admin")
    shift = make_shift(admin, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Trusted", credit_limit="1000.00")

    response = await _post(
        client,
        auth_headers(admin),
        "override-ok",
        shift_id=shift,
        customer_id=customer,
        attachment_id=make_attachment(admin),
        amount="5000.00",
        limit_override_reason="owner approved by phone, harvest season",
    )

    assert response.status_code == 201
    sale_id = response.json()["id"]
    assert response.json()["limit_override_reason"].startswith("owner approved")

    with engine.connect() as connection:
        stored = connection.execute(
            text(
                "SELECT limit_override_reason FROM credit_sales WHERE id = CAST(:id AS uuid)"
            ).bindparams(id=sale_id)
        ).scalar_one()
        audited = connection.execute(
            text(
                "SELECT count(*) FROM audit_logs WHERE table_name = 'credit_sales' "
                "AND record_id = CAST(:id AS uuid)"
            ).bindparams(id=sale_id)
        ).scalar_one()
    assert stored.startswith("owner approved")
    assert audited == 1


async def test_a_manager_cannot_override_the_limit(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """§8: overriding a credit limit is an admin act, alongside the manual-litres override."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(credit_limit="1000.00")

    response = await _post(
        client,
        auth_headers(manager),
        "override-refused",
        shift_id=shift,
        customer_id=customer,
        attachment_id=make_attachment(manager),
        amount="5000.00",
        limit_override_reason="I think it is fine",
    )

    assert response.status_code == 403
    assert response.json()["code"] == "LIMIT_OVERRIDE_REQUIRES_ADMIN"


async def test_a_blank_override_reason_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """§14: an override can never be an unexplained number."""
    admin = make_user("admin")
    shift = make_shift(admin, business_date=DAY, sequence=1)

    response = await _post(
        client,
        auth_headers(admin),
        "blank-override",
        shift_id=shift,
        customer_id=make_credit_customer(credit_limit="100.00"),
        attachment_id=make_attachment(admin),
        amount="5000.00",
        limit_override_reason="   ",
    )

    assert response.status_code == 422


# --- idempotency (§6.10) -----------------------------------------------------


async def test_the_same_key_twice_creates_one_sale_and_replays_the_response(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_credit,
) -> None:
    """Attendants use phones on patchy rural connectivity; a retry after a timeout must not
    record the same ₹5,000 udhaar twice."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer()
    attachment = make_attachment(attendant)
    headers = auth_headers(attendant)

    first = await _post(
        client, headers, "retry-me", shift_id=shift, customer_id=customer,
        attachment_id=attachment,
    )
    second = await _post(
        client, headers, "retry-me", shift_id=shift, customer_id=customer,
        attachment_id=attachment,
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM credit_sales")
        ).scalar_one()
    assert count == 1


async def test_a_missing_idempotency_key_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await client.post(
        f"/api/v1/shifts/{shift}/credit-sales",
        json={
            "credit_customer_id": str(make_credit_customer()),
            "attachment_id": str(make_attachment(attendant)),
            "amount": "100.00",
        },
        headers=auth_headers(attendant),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_the_same_key_with_a_different_body_is_a_client_bug(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer()
    headers = auth_headers(attendant)

    await _post(
        client, headers, "same-key", shift_id=shift, customer_id=customer,
        attachment_id=make_attachment(attendant), amount="100.00",
    )
    response = await _post(
        client, headers, "same-key", shift_id=shift, customer_id=customer,
        attachment_id=make_attachment(attendant), amount="200.00",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_a_refusal_releases_the_key_so_the_corrected_retry_works(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """Otherwise a typo'd customer id would wedge that key for 24 hours, and the retry with
    the right value would be refused as a duplicate."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    attachment = make_attachment(attendant)
    headers = auth_headers(attendant)

    failed = await _post(
        client, headers, "typo-then-fix", shift_id=shift, customer_id=uuid4(),
        attachment_id=attachment,
    )
    assert failed.status_code == 404

    fixed = await _post(
        client, headers, "typo-then-fix", shift_id=shift,
        customer_id=make_credit_customer(), attachment_id=attachment,
    )

    assert fixed.status_code == 201


# --- listing -----------------------------------------------------------------


async def test_the_list_shows_both_rows_of_a_correction(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """§6.9: both rows remain visible, and the total nets them."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer()
    attachment = make_attachment(attendant)
    original = make_credit_sale(shift, customer, attachment, amount="900.00")
    make_credit_sale(
        shift, customer, attachment, amount="-900.00",
        reverses_id=original, reversal_reason="wrong customer",
    )

    response = await client.get(
        f"/api/v1/shifts/{shift}/credit-sales", headers=auth_headers(attendant)
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    assert Decimal(body["total"]) == Decimal("0.00")
    assert next(i for i in body["items"] if i["id"] == str(original))["is_reversed"]


# --- permissions (§8) --------------------------------------------------------


async def test_an_attendant_cannot_issue_udhaar_on_another_attendants_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """§10 names this case specifically."""
    owner = make_user("attendant")
    other = make_user("attendant")
    shift = make_shift(owner, business_date=DAY, sequence=1)

    response = await _post(
        client,
        auth_headers(other),
        "not-my-shift",
        shift_id=shift,
        customer_id=make_credit_customer(),
        attachment_id=make_attachment(other),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_YOUR_SHIFT"


async def test_a_closed_shift_refuses_new_udhaar(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1, status="closed")

    response = await _post(
        client,
        auth_headers(attendant),
        "closed-shift",
        shift_id=shift,
        customer_id=make_credit_customer(),
        attachment_id=make_attachment(attendant),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_NOT_OPEN"


async def test_a_locked_shift_refuses_new_udhaar(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """§10's immutability case: any write to a locked shift is a 409."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1, status="locked")

    response = await _post(
        client,
        auth_headers(attendant),
        "locked-shift",
        shift_id=shift,
        customer_id=make_credit_customer(),
        attachment_id=make_attachment(attendant),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_LOCKED"


async def test_a_manager_may_issue_udhaar_on_any_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(
        client,
        auth_headers(manager),
        "manager-writes",
        shift_id=shift,
        customer_id=make_credit_customer(),
        attachment_id=make_attachment(manager),
    )

    assert response.status_code == 201


# --- correcting an open shift's sale -----------------------------------------


async def test_an_amount_can_be_corrected_while_the_shift_is_open(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """No Idempotency-Key: a PATCH is idempotent by construction."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    sale = make_credit_sale(
        shift, make_credit_customer(), make_attachment(attendant), amount="1200.00"
    )

    response = await client.patch(
        f"/api/v1/shifts/{shift}/credit-sales/{sale}",
        json={"amount": "1250.00", "vehicle_number": "MH12AB1234"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["amount"] == "1250.00"
    assert body["vehicle_number"] == "MH12AB1234"


async def test_a_patch_cannot_move_a_sale_to_another_customer(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """Moving money to a different customer's ledger is not a correction of this row -- it is
    a different sale, and two balances are wrong until it is recorded as one. §6.9's reversal
    is the path, and `extra="forbid"` makes the attempt a 422 rather than a silent no-op."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    sale = make_credit_sale(
        shift, make_credit_customer(), make_attachment(attendant), amount="500.00"
    )

    response = await client.patch(
        f"/api/v1/shifts/{shift}/credit-sales/{sale}",
        json={"credit_customer_id": str(make_credit_customer())},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 422


async def test_a_patch_cannot_swap_the_receipt(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """§5.2: swapping a receipt either strands the old one as garbage §7.4 can never
    reclaim, or unlinks it and lets the sweep delete evidence for a sale that still
    exists."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    sale = make_credit_sale(
        shift, make_credit_customer(), make_attachment(attendant), amount="500.00"
    )

    response = await client.patch(
        f"/api/v1/shifts/{shift}/credit-sales/{sale}",
        json={"attachment_id": str(make_attachment(attendant))},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 422


async def test_an_empty_patch_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    sale = make_credit_sale(
        shift, make_credit_customer(), make_attachment(attendant), amount="500.00"
    )

    response = await client.patch(
        f"/api/v1/shifts/{shift}/credit-sales/{sale}",
        json={},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "NO_FIELDS_TO_UPDATE"


async def test_a_patch_cannot_leave_a_quantity_without_a_fuel_type(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """§4.5 again, on the edit path: adding a quantity to a non-fuel sale would create a
    measure with no unit. The database's own CHECK would refuse it as a 500; this is the
    readable half."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    sale = make_credit_sale(
        shift, make_credit_customer(), make_attachment(attendant), amount="500.00"
    )

    response = await client.patch(
        f"/api/v1/shifts/{shift}/credit-sales/{sale}",
        json={"quantity": "10.000"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "QUANTITY_NEEDS_FUEL_TYPE"


async def test_an_unknown_sale_id_is_a_404(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/credit-sales/{uuid4()}",
        json={"amount": "100.00"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "CREDIT_SALE_NOT_FOUND"


async def test_an_unknown_fuel_type_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """409, not 404: the missing row is not the one named in the URL, it is a reference the
    payload points at -- matching fuel_prices.py and nozzles.py."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(
        client,
        auth_headers(attendant),
        "unknown-fuel",
        shift_id=shift,
        customer_id=make_credit_customer(),
        attachment_id=make_attachment(attendant),
        fuel_type_id=str(uuid4()),
        quantity="10.000",
    )

    assert response.status_code == 409
    assert response.json()["code"] == "FUEL_TYPE_NOT_FOUND"


# --- the amount-vs-rate sanity check -----------------------------------------


async def test_an_amount_far_from_quantity_times_rate_is_logged_not_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_fuel_price: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
    caplog: pytest.LogCaptureFixture,
    clean_credit,
) -> None:
    """A mistyped digit is the most common data-entry error, and this is the credit-sale
    analogue of §6.2's flow-rate ceiling.

    **It logs and still writes.** The slip is what the customer agreed to owe -- a manual
    discount or a rounded total is real -- and refusing them would teach staff to type
    whatever balances, which is exactly what §6.8 warns against for shift closes.
    """
    from datetime import datetime, timezone

    admin = make_user("admin")
    shift = make_shift(admin, business_date=DAY, sequence=1)
    make_fuel_price(
        fuel_type_ids["DIESEL"],
        "90.00",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        entered_by=admin,
    )

    with caplog.at_level("WARNING"):
        response = await _post(
            client,
            auth_headers(admin),
            "diverging-amount",
            shift_id=shift,
            customer_id=make_credit_customer(),
            attachment_id=make_attachment(admin),
            fuel_type_id=str(fuel_type_ids["DIESEL"]),
            quantity="40.000",
            # 40 x 90 = 3,600. A mistyped extra digit.
            amount="36000.00",
        )

    assert response.status_code == 201
    assert any(
        "diverges" in record.message for record in caplog.records
    ), "the mistyped amount should have been logged"


async def test_an_amount_matching_the_rate_logs_nothing(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_fuel_price: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
    caplog: pytest.LogCaptureFixture,
    clean_credit,
) -> None:
    from datetime import datetime, timezone

    admin = make_user("admin")
    shift = make_shift(admin, business_date=DAY, sequence=1)
    make_fuel_price(
        fuel_type_ids["PETROL"],
        "100.00",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        entered_by=admin,
    )

    with caplog.at_level("WARNING"):
        response = await _post(
            client,
            auth_headers(admin),
            "matching-amount",
            shift_id=shift,
            customer_id=make_credit_customer(),
            attachment_id=make_attachment(admin),
            fuel_type_id=str(fuel_type_ids["PETROL"]),
            quantity="25.000",
            amount="2500.00",
        )

    assert response.status_code == 201
    assert not [r for r in caplog.records if "diverges" in r.message]


async def test_a_fuel_with_no_price_entered_does_not_block_the_sale(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_fuel_type: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """`rate_at` raises NO_PRICE_FOR_DATE when no rate has ever been entered, and a
    reference-data gap must not refuse a real sale -- the same argument §6.8 makes about
    close preconditions inheriting a valuation refusal."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    unpriced = make_fuel_type("XP95_CREDIT", display_name="XP-95")

    response = await _post(
        client,
        auth_headers(attendant),
        "unpriced-fuel",
        shift_id=shift,
        customer_id=make_credit_customer(),
        attachment_id=make_attachment(attendant),
        fuel_type_id=str(unpriced),
        quantity="10.000",
        amount="1100.00",
    )

    assert response.status_code == 201
