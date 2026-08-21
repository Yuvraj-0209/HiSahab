"""§6.11's receipt rule, end to end: receipt_required = category.requires_receipt OR
amount > EXPENSE_RECEIPT_THRESHOLD. Covers create, PATCH re-evaluation, linking,
immutability, and the reversal-replacement inheritance path.

Default config: EXPENSE_RECEIPT_THRESHOLD = 5000.00.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.anyio

DAY = date(2026, 8, 3)

_REAL_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300"
    "0302020302020303030304030304050805050404050a070706"
    "080c0a0c0c0b0a0b0b0d0e12100d0e110e0b0b1016101113141"
    "51516150c0f171d17141d1114141400ffc9000b080001000101"
    "0100ffcc0006001005f0ffda0008010100003f00d2cf20ffd9"
)


async def _category_id(client: AsyncClient, headers: dict[str, str], code: str) -> str:
    response = await client.get("/api/v1/expense-categories", headers=headers)
    return next(row["id"] for row in response.json() if row["code"] == code.upper())


async def _upload(client: AsyncClient, headers: dict[str, str], *, shift_id: UUID) -> str:
    response = await client.post(
        "/api/v1/uploads/receipt",
        data={"shift_id": str(shift_id)},
        files={"file": ("r.jpg", _REAL_JPEG, "image/jpeg")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["attachment_id"]


async def _reverse(
    client: AsyncClient, headers: dict[str, str], *, shift_id, expense_id, key="rev", **body
):
    return await client.post(
        f"/api/v1/shifts/{shift_id}/expenses/{expense_id}/reversals",
        json={"reason": "receipt rule test reversal", **body},
        headers={**headers, "Idempotency-Key": key},
    )


async def _post_expense(
    client: AsyncClient,
    headers: dict[str, str],
    key: str,
    *,
    shift_id: UUID,
    category_id: str,
    amount: str,
    attachment_id: str | None = None,
):
    body = {
        "category_id": category_id,
        "mode": "cash",
        "amount": amount,
        "description": "receipt rule test",
    }
    if attachment_id is not None:
        body["attachment_id"] = attachment_id
    return await client.post(
        f"/api/v1/shifts/{shift_id}/expenses",
        json=body,
        headers={**headers, "Idempotency-Key": key},
    )


# --- the OR: category flag ------------------------------------------------------------


async def test_a_no_receipt_category_under_threshold_needs_no_attachment(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    """The tea case: MAINTENANCE has requires_receipt=false by seed, and ₹20 is nowhere
    near the ₹5,000 threshold. Must not need a photograph."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)
    category = await _category_id(client, headers, "MAINTENANCE")

    response = await _post_expense(
        client, headers, "tea", shift_id=shift, category_id=category, amount="20.00"
    )

    assert response.status_code == 201
    body = response.json()
    assert body["receipt_required"] is False
    assert body["attachment_id"] is None


async def test_a_receipt_required_category_with_no_attachment_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    """OTHER requires a receipt by seed (§5.1's migration 0010 seed)."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)
    category = await _category_id(client, headers, "OTHER")

    response = await _post_expense(
        client, headers, "other-no-receipt", shift_id=shift, category_id=category,
        amount="100.00",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "EXPENSE_REQUIRES_RECEIPT"
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM expenses WHERE shift_id = :s").bindparams(s=shift)
        ).scalar_one()
    assert count == 0


async def test_a_receipt_required_category_with_an_attachment_succeeds(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)
    category = await _category_id(client, headers, "OTHER")
    attachment = await _upload(client, headers, shift_id=shift)

    response = await _post_expense(
        client, headers, "other-with-receipt", shift_id=shift, category_id=category,
        amount="100.00", attachment_id=attachment,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["receipt_required"] is True
    assert body["attachment_id"] == attachment


# --- the OR: amount threshold ----------------------------------------------------------


async def test_a_no_receipt_category_over_threshold_still_needs_a_receipt(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    """The gap the threshold half closes: a no-receipt category is not a hiding place for
    a large amount."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)
    category = await _category_id(client, headers, "MAINTENANCE")

    response = await _post_expense(
        client, headers, "large-maintenance", shift_id=shift, category_id=category,
        amount="50000.00",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "EXPENSE_REQUIRES_RECEIPT"


@pytest.mark.parametrize(
    "amount,should_require",
    [("5000.00", False), ("5000.01", True)],
)
async def test_the_threshold_boundary_is_strictly_greater_than(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
    amount: str,
    should_require: bool,
) -> None:
    """§10: 'Boundary: exactly at the threshold does not require a receipt; one paisa
    over does.' Matches §6.7's own comparison, strictly `>`."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)
    category = await _category_id(client, headers, "MAINTENANCE")

    kwargs = {}
    if should_require:
        kwargs["attachment_id"] = await _upload(client, headers, shift_id=shift)

    response = await _post_expense(
        client, headers, f"boundary-{amount}", shift_id=shift, category_id=category,
        amount=amount, **kwargs,
    )

    if should_require and not kwargs:
        assert response.status_code == 422
    else:
        assert response.status_code == 201
        assert response.json()["receipt_required"] is should_require


# --- PATCH re-evaluation ----------------------------------------------------------------


async def test_a_patch_raising_the_amount_past_the_threshold_starts_requiring_a_receipt(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)
    category = await _category_id(client, headers, "MAINTENANCE")

    created = await _post_expense(
        client, headers, "raise-me", shift_id=shift, category_id=category, amount="900.00"
    )
    assert created.status_code == 201
    assert created.json()["receipt_required"] is False
    expense_id = created.json()["id"]

    response = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{expense_id}",
        json={"amount": "9000.00"},
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "EXPENSE_REQUIRES_RECEIPT"


async def test_the_same_patch_succeeds_when_it_also_attaches_a_receipt(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)
    category = await _category_id(client, headers, "MAINTENANCE")

    created = await _post_expense(
        client, headers, "raise-with-receipt", shift_id=shift, category_id=category,
        amount="900.00",
    )
    expense_id = created.json()["id"]
    attachment = await _upload(client, headers, shift_id=shift)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{expense_id}",
        json={"amount": "9000.00", "attachment_id": attachment},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["receipt_required"] is True
    assert body["attachment_id"] == attachment


async def test_a_patch_that_does_not_touch_amount_is_not_re_evaluated(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)
    category = await _category_id(client, headers, "MAINTENANCE")

    created = await _post_expense(
        client, headers, "no-amount-change", shift_id=shift, category_id=category,
        amount="900.00",
    )
    expense_id = created.json()["id"]

    response = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{expense_id}",
        json={"paid_to": "Someone Else"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["receipt_required"] is False


# --- attachment immutability (§5.3, D3) --------------------------------------------------


async def test_a_patch_may_set_attachment_id_while_it_is_null(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)
    category = await _category_id(client, headers, "MAINTENANCE")

    created = await _post_expense(
        client, headers, "attach-later", shift_id=shift, category_id=category,
        amount="20.00",
    )
    expense_id = created.json()["id"]
    attachment = await _upload(client, headers, shift_id=shift)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{expense_id}",
        json={"attachment_id": attachment},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["attachment_id"] == attachment


async def test_a_patch_cannot_swap_an_already_set_attachment(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)
    category = await _category_id(client, headers, "OTHER")
    first = await _upload(client, headers, shift_id=shift)
    second = await _upload(client, headers, shift_id=shift)

    created = await _post_expense(
        client, headers, "no-swap", shift_id=shift, category_id=category,
        amount="20.00", attachment_id=first,
    )
    expense_id = created.json()["id"]

    response = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{expense_id}",
        json={"attachment_id": second},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ATTACHMENT_ALREADY_SET"


async def test_one_attachment_cannot_be_linked_to_two_live_expenses(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)
    category = await _category_id(client, headers, "MAINTENANCE")
    attachment = await _upload(client, headers, shift_id=shift)

    first = await _post_expense(
        client, headers, "reuse-1", shift_id=shift, category_id=category,
        amount="20.00", attachment_id=attachment,
    )
    assert first.status_code == 201

    second = await _post_expense(
        client, headers, "reuse-2", shift_id=shift, category_id=category,
        amount="30.00", attachment_id=attachment,
    )

    assert second.status_code == 409
    assert second.json()["code"] == "ATTACHMENT_ALREADY_LINKED"


async def test_an_unknown_attachment_id_on_create_is_404_and_writes_nothing(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)
    category = await _category_id(client, headers, "MAINTENANCE")

    response = await _post_expense(
        client, headers, "unknown-attachment", shift_id=shift, category_id=category,
        amount="20.00", attachment_id=str(uuid4()),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "ATTACHMENT_NOT_FOUND"
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM expenses WHERE shift_id = :s").bindparams(s=shift)
        ).scalar_one()
    assert count == 0


async def test_a_patch_with_an_unknown_attachment_id_is_404(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)
    category = await _category_id(client, headers, "MAINTENANCE")

    created = await _post_expense(
        client, headers, "patch-unknown-attachment", shift_id=shift, category_id=category,
        amount="20.00",
    )
    expense_id = created.json()["id"]

    response = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{expense_id}",
        json={"attachment_id": str(uuid4())},
        headers=headers,
    )

    assert response.status_code == 404
    assert response.json()["code"] == "ATTACHMENT_NOT_FOUND"


async def test_an_attachment_from_another_outlet_cannot_be_linked_on_create(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    """`attachment_service.link`'s own outlet check (§7.3's posture, applied to writes
    too) -- an id that is real, just not at this outlet, must not be linkable."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)
    category = await _category_id(client, headers, "MAINTENANCE")
    attachment = await _upload(client, headers, shift_id=shift)

    other_outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO outlets (id, name) VALUES (:id, 'Second Outlet')").bindparams(
                id=other_outlet
            )
        )
        connection.execute(
            text(
                "UPDATE attachments SET outlet_id = :outlet WHERE id = CAST(:id AS uuid)"
            ).bindparams(outlet=other_outlet, id=attachment)
        )

    try:
        response = await _post_expense(
            client, headers, "cross-outlet-link", shift_id=shift, category_id=category,
            amount="20.00", attachment_id=attachment,
        )

        assert response.status_code == 404
        assert response.json()["code"] == "ATTACHMENT_NOT_FOUND"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM attachments WHERE outlet_id = :id").bindparams(
                    id=other_outlet
                )
            )
            connection.execute(
                text("DELETE FROM outlets WHERE id = :id").bindparams(id=other_outlet)
            )


# --- reversal / replacement (§6.9, D3) ---------------------------------------------------


async def test_a_reversal_needs_no_receipt_even_for_a_receipt_required_original(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    """§6.11: a reversal cancels a spend, it is not one -- nothing to photograph.

    The original genuinely needs a real attachment here: `ck_expenses_receipt_required_
    has_attachment` correctly refuses `receipt_required=True` with no attachment_id on any
    non-reversal row, so there is no shortcut past actually uploading one.
    """
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    headers = auth_headers(manager)
    attachment = await _upload(client, headers, shift_id=shift)
    original = make_expense(
        shift, category="other", amount="200.00",
        attachment_id=UUID(attachment), receipt_required=True,
    )

    response = await _reverse(
        client, headers, shift_id=shift, expense_id=original, key="no-receipt-reversal",
    )

    assert response.status_code == 201
    assert response.json()["reversal"]["receipt_required"] is False


async def test_a_replacement_inherits_the_originals_attachment_without_a_reupload(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    headers = auth_headers(manager)
    category = await _category_id(client, headers, "OTHER")
    attachment = await _upload(client, headers, shift_id=shift)

    created = await _post_expense(
        client, headers, "for-reversal", shift_id=shift, category_id=category,
        amount="100.00", attachment_id=attachment,
    )
    expense_id = created.json()["id"]

    response = await _reverse(
        client, headers, shift_id=shift, expense_id=expense_id, key="inherit-attachment",
        replacement_amount="110.00",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["replacement"]["attachment_id"] == attachment
    assert body["replacement"]["receipt_required"] is True


async def test_a_replacement_that_newly_crosses_the_threshold_with_no_receipt_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    """The original never needed a receipt (small amount, no-receipt category); the
    correction pushes it over the threshold with nothing to inherit."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    headers = auth_headers(manager)
    category = await _category_id(client, headers, "MAINTENANCE")

    created = await _post_expense(
        client, headers, "small-original", shift_id=shift, category_id=category,
        amount="900.00",
    )
    expense_id = created.json()["id"]

    response = await _reverse(
        client, headers, shift_id=shift, expense_id=expense_id, key="cross-threshold",
        replacement_amount="50000.00",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "EXPENSE_REQUIRES_RECEIPT"

    # And nothing was left half-written: the reversal itself must not have landed either.
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM expenses WHERE shift_id = :s").bindparams(s=shift)
        ).scalar_one()
    assert count == 1  # only the original


# --- the snapshot never gets rewritten by a category edit --------------------------------


async def test_flipping_a_category_to_requires_receipt_does_not_touch_historical_rows(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense_category: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    admin = make_user("admin")
    shift = make_shift(admin, business_date=DAY, sequence=1)
    category = make_expense_category("BOREWELL", requires_receipt=False)
    old_expense = make_expense(
        shift, category_id=category, amount="500.00", receipt_required=False
    )

    flip = await client.patch(
        f"/api/v1/expense-categories/{category}",
        json={"requires_receipt": True},
        headers=auth_headers(admin),
    )
    assert flip.status_code == 200

    listing = await client.get(
        f"/api/v1/shifts/{shift}/expenses", headers=auth_headers(admin)
    )
    row = next(r for r in listing.json()["items"] if r["id"] == str(old_expense))
    assert row["receipt_required"] is False
