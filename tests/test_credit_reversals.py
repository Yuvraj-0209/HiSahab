"""§6.9's correction path on credit sales, and the inheritance that makes it possible.

The load-bearing case here is `test_a_reversal_inherits_the_originals_attachment`. Everywhere
else in this codebase a reversal simply omits the receipt -- §6.11 exempts expense reversals
by CHECK, because a cancellation is not a spend. `credit_sales.attachment_id` is `NOT NULL`
with no such exemption (§5.2, and §14 forbids adding one), so inheritance is the only way a
reversal row can exist at all. If that ever breaks, the correction path for udhaar stops
working entirely rather than degrading quietly.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.anyio

DAY = date(2026, 10, 2)


async def _reverse(
    client: AsyncClient,
    headers: dict[str, str],
    key: str,
    *,
    shift_id: UUID,
    sale_id: UUID,
    reason: str = "recorded against the wrong customer",
    **body,
):
    return await client.post(
        f"/api/v1/shifts/{shift_id}/credit-sales/{sale_id}/reversals",
        json={"reason": reason, **body},
        headers={**headers, "Idempotency-Key": key},
    )


def _outstanding(customer_id: UUID) -> Decimal:
    from app.db.session import SessionLocal
    from app.services import credit as credit_service

    with SessionLocal() as session:
        return credit_service.outstanding(session, customer_id=customer_id)


# --- the reversal itself -----------------------------------------------------


async def test_a_manager_can_reverse_a_credit_sale(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer()
    sale = make_credit_sale(
        shift, customer, make_attachment(attendant), amount="1500.00"
    )

    response = await _reverse(
        client, auth_headers(manager), "rev-1", shift_id=shift, sale_id=sale
    )

    assert response.status_code == 201
    body = response.json()
    assert body["reversal"]["amount"] == "-1500.00"
    assert body["reversal"]["reverses_id"] == str(sale)
    assert body["original"]["is_reversed"] is True
    assert body["replacement"] is None
    assert _outstanding(customer) == Decimal("0.00")


async def test_a_reversal_inherits_the_originals_attachment(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """The rule the whole design turns on (§5.2, §14).

    `attachment_id` is NOT NULL with no reversal exemption, so the reversal cannot simply
    leave it empty the way an expense reversal does. It carries the original's -- the same
    photograph, the same piece of evidence -- and nobody is asked to photograph a
    cancellation.
    """
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    attachment = make_attachment(attendant)
    sale = make_credit_sale(shift, make_credit_customer(), attachment, amount="700.00")

    response = await _reverse(
        client, auth_headers(manager), "rev-inherit", shift_id=shift, sale_id=sale
    )

    assert response.status_code == 201
    assert response.json()["reversal"]["attachment_id"] == str(attachment)


async def test_a_replacement_also_inherits_it_without_a_second_upload(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """§5.3: nobody photographs one piece of paper twice."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer()
    attachment = make_attachment(attendant)
    sale = make_credit_sale(shift, customer, attachment, amount="3000.00")

    response = await _reverse(
        client, auth_headers(manager), "rev-replace", shift_id=shift, sale_id=sale,
        replacement_amount="2800.00",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["replacement"]["attachment_id"] == str(attachment)
    assert body["replacement"]["amount"] == "2800.00"
    assert _outstanding(customer) == Decimal("2800.00")


async def test_exactly_one_live_row_holds_the_attachment_throughout(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """§5.3's rule survives the correction: three rows end up pointing at one attachment, and
    exactly one of them -- the replacement -- is live.

    "Live" is §5.2's vocabulary: not itself a reversal, and not referenced by one.
    """
    from app.db.session import SessionLocal
    from app.services import credit as credit_service

    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    attachment = make_attachment(attendant)
    sale = make_credit_sale(
        shift, make_credit_customer(), attachment, amount="1000.00"
    )

    response = await _reverse(
        client, auth_headers(manager), "rev-live", shift_id=shift, sale_id=sale,
        replacement_amount="1100.00",
    )
    replacement_id = response.json()["replacement"]["id"]

    with SessionLocal() as session:
        live = credit_service.live_credit_sale_for_attachment(
            session, attachment_id=attachment
        )
    assert str(live) == replacement_id


async def test_the_quantity_is_negated_alongside_the_amount(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """A reversal cancels the whole line, so a per-fuel udhaar report nets to zero the same
    way the money does. Leaving the quantity positive would show 40 litres sold on credit
    with no money owed against them."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    sale = make_credit_sale(
        shift,
        make_credit_customer(),
        make_attachment(attendant),
        amount="3600.00",
        fuel_type_id=fuel_type_ids["DIESEL"],
        quantity="40.000",
    )

    response = await _reverse(
        client, auth_headers(manager), "rev-quantity", shift_id=shift, sale_id=sale
    )

    assert response.json()["reversal"]["quantity"] == "-40.000"


async def test_a_non_fuel_sale_reverses_with_a_null_quantity(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """Negating None would be a TypeError; the branch exists and this is what covers it."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    sale = make_credit_sale(
        shift, make_credit_customer(), make_attachment(attendant), amount="400.00"
    )

    response = await _reverse(
        client, auth_headers(manager), "rev-no-quantity", shift_id=shift, sale_id=sale
    )

    assert response.status_code == 201
    assert response.json()["reversal"]["quantity"] is None


# --- what a reversal refuses -------------------------------------------------


async def test_a_sale_cannot_be_reversed_twice(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    sale = make_credit_sale(
        shift, make_credit_customer(), make_attachment(attendant), amount="500.00"
    )
    headers = auth_headers(manager)

    first = await _reverse(client, headers, "twice-1", shift_id=shift, sale_id=sale)
    second = await _reverse(client, headers, "twice-2", shift_id=shift, sale_id=sale)

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["code"] == "ALREADY_REVERSED"


async def test_a_reversal_cannot_itself_be_reversed(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """To undo a reversal, record the correct figure as a new sale -- negating the negation
    would leave three rows and no way to read what actually happened."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    sale = make_credit_sale(
        shift, make_credit_customer(), make_attachment(attendant), amount="500.00"
    )
    headers = auth_headers(manager)
    reversal_id = (
        await _reverse(client, headers, "undo-1", shift_id=shift, sale_id=sale)
    ).json()["reversal"]["id"]

    response = await _reverse(
        client, headers, "undo-2", shift_id=shift, sale_id=UUID(reversal_id)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CANNOT_REVERSE_A_REVERSAL"


async def test_a_blank_reason_is_refused_by_pydantic(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    sale = make_credit_sale(
        shift, make_credit_customer(), make_attachment(attendant), amount="500.00"
    )

    response = await _reverse(
        client, auth_headers(manager), "blank", shift_id=shift, sale_id=sale, reason="   "
    )

    assert response.status_code == 422


async def test_the_database_refuses_a_blank_reason_too(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    engine: Engine,
) -> None:
    """Belt and braces (§6.6). Migration 0007 had to strengthen NOT NULL into a real regex
    on `collections` after a whitespace-only reason reached the database through the API;
    `credit_sales` gets the strengthened form from birth, and this asserts it directly rather
    than trusting the API to be the only writer."""
    from sqlalchemy.exc import IntegrityError

    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    attachment = make_attachment(attendant)
    sale = make_credit_sale(
        shift, make_credit_customer(), attachment, amount="500.00"
    )

    with pytest.raises(IntegrityError, match="ck_credit_sales_reversal_has_reason"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO credit_sales (shift_id, credit_customer_id, amount, "
                    "attachment_id, reverses_id, reversal_reason) SELECT shift_id, "
                    "credit_customer_id, -amount, attachment_id, id, '   ' "
                    "FROM credit_sales WHERE id = :id"
                ).bindparams(id=sale)
            )


async def test_a_reversal_on_a_closed_shift_is_the_whole_point(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """§4.7: the whole day is typed in after the fact, so a mistyped udhaar found once the
    shift is closed is the normal case, not an edge one. The route is deliberately not
    `writable=True`."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1, status="closed")
    sale = make_credit_sale(
        shift, make_credit_customer(), make_attachment(attendant), amount="500.00"
    )

    response = await _reverse(
        client, auth_headers(manager), "closed-rev", shift_id=shift, sale_id=sale
    )

    assert response.status_code == 201


async def test_a_manager_cannot_reverse_on_a_locked_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """Locking is the point at which a day stops being anybody else's to change."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1, status="locked")
    sale = make_credit_sale(
        shift, make_credit_customer(), make_attachment(attendant), amount="500.00"
    )

    response = await _reverse(
        client, auth_headers(manager), "locked-rev", shift_id=shift, sale_id=sale
    )

    assert response.status_code == 403
    assert response.json()["code"] == "LOCKED_SHIFT_REVERSAL_REQUIRES_ADMIN"


async def test_an_admin_can_reverse_on_a_locked_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    admin = make_user("admin")
    shift = make_shift(attendant, business_date=DAY, sequence=1, status="locked")
    sale = make_credit_sale(
        shift, make_credit_customer(), make_attachment(attendant), amount="500.00"
    )

    response = await _reverse(
        client, auth_headers(admin), "admin-locked-rev", shift_id=shift, sale_id=sale
    )

    assert response.status_code == 201


async def test_an_attendant_cannot_reverse_at_all(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """Cancelling money owed is a manager's act, even on your own shift."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    sale = make_credit_sale(
        shift, make_credit_customer(), make_attachment(attendant), amount="500.00"
    )

    response = await _reverse(
        client, auth_headers(attendant), "attendant-rev", shift_id=shift, sale_id=sale
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


async def test_a_sale_from_another_shift_is_a_409(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """A real id with the wrong shift in the URL is a different mistake from a missing row."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    first = make_shift(attendant, business_date=DAY, sequence=1, status="closed")
    second = make_shift(attendant, business_date=date(2026, 10, 3), sequence=1)
    sale = make_credit_sale(
        first, make_credit_customer(), make_attachment(attendant), amount="500.00"
    )

    response = await _reverse(
        client, auth_headers(manager), "wrong-shift", shift_id=second, sale_id=sale
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CREDIT_SALE_NOT_IN_SHIFT"


async def test_reversing_is_idempotent(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """A retried reversal must not create a second negative row -- which would turn a ₹500
    cancellation into a ₹500 credit balance."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer()
    sale = make_credit_sale(
        shift, customer, make_attachment(attendant), amount="500.00"
    )
    headers = auth_headers(manager)

    first = await _reverse(client, headers, "idem-rev", shift_id=shift, sale_id=sale)
    second = await _reverse(client, headers, "idem-rev", shift_id=shift, sale_id=sale)

    assert first.json() == second.json()
    assert _outstanding(customer) == Decimal("0.00")
    with engine.connect() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM credit_sales WHERE reverses_id = :id"
            ).bindparams(id=sale)
        ).scalar_one()
    assert count == 1


# --- PATCH interactions ------------------------------------------------------


async def test_a_reversed_sale_can_no_longer_be_patched(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """The Phase 7 Step 0 lesson: `reverse` and the PATCH route must ask "has this been
    reversed" the same way, or a cancelled original stays editable."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    sale = make_credit_sale(
        shift, make_credit_customer(), make_attachment(attendant), amount="500.00"
    )
    await _reverse(
        client, auth_headers(manager), "patch-after-rev", shift_id=shift, sale_id=sale
    )

    response = await client.patch(
        f"/api/v1/shifts/{shift}/credit-sales/{sale}",
        json={"amount": "600.00"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CREDIT_SALE_ALREADY_REVERSED"


async def test_a_reversal_row_cannot_be_patched(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    sale = make_credit_sale(
        shift, make_credit_customer(), make_attachment(attendant), amount="500.00"
    )
    reversal_id = (
        await _reverse(
            client, auth_headers(manager), "patch-the-rev", shift_id=shift, sale_id=sale
        )
    ).json()["reversal"]["id"]

    response = await client.patch(
        f"/api/v1/shifts/{shift}/credit-sales/{reversal_id}",
        json={"amount": "600.00"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CANNOT_EDIT_A_REVERSAL"
