"""Bank deposits over HTTP (CLAUDE.md §5.2, §5.3, §6.4, §6.9, §6.10, §8).

Three things here are not shared with the other shift-scoped money tables and carry the
weight: the floor is a *manager* rather than an attendant (§8 -- attendants take money in,
they do not take it out), `business_date` is set server-side from the shift (§3 rule 7), and
the deposit slip gets §5.3's one-attachment-one-live-row protection in full, because one
photograph of a ₹1,00,000 pay-in must not justify two deposits.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.anyio

DAY = date(2026, 12, 2)

_REAL_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300"
    "0302020302020303030304030304050805050404050a070706"
    "080c0a0c0c0b0a0b0b0d0e12100d0e110e0b0b1016101113141"
    "51516150c0f171d17141d1114141400ffc9000b080001000101"
    "0100ffcc0006001005f0ffda0008010100003f00d2cf20ffd9"
)


async def _upload(client: AsyncClient, headers: dict[str, str], *, shift_id: UUID) -> str:
    response = await client.post(
        "/api/v1/uploads/receipt",
        data={"shift_id": str(shift_id)},
        files={"file": ("slip.jpg", _REAL_JPEG, "image/jpeg")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["attachment_id"]


async def _post(
    client: AsyncClient,
    headers: dict[str, str],
    key: str,
    *,
    shift_id: UUID,
    amount: str = "100000.00",
    **extra,
):
    return await client.post(
        f"/api/v1/shifts/{shift_id}/bank-deposits",
        json={"amount": amount, **extra},
        headers={**headers, "Idempotency-Key": key},
    )


# --- §8: a manager's act -----------------------------------------------------------


async def test_a_manager_records_a_deposit(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(client, auth_headers(manager), "dep-1", shift_id=shift)

    assert response.status_code == 201
    assert response.json()["amount"] == "100000.00"


async def test_an_attendant_cannot_record_a_deposit_even_on_their_own_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """§8: recording a deposit is a manager's act. Ownership does not lift a role floor --
    the two axes are independent, and this is the case that proves it for this table."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 3), sequence=1)

    response = await _post(client, auth_headers(attendant), "dep-2", shift_id=shift)

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM bank_deposits WHERE shift_id = :s").bindparams(
                s=shift
            )
        ).scalar_one()
    assert count == 0


async def test_an_attendant_cannot_read_the_deposit_list(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 4), sequence=1)

    response = await client.get(
        f"/api/v1/shifts/{shift}/bank-deposits", headers=auth_headers(attendant)
    )

    assert response.status_code == 403


# --- §3 rule 7: business_date is derived, never accepted ---------------------------


async def test_the_business_date_comes_from_the_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """§4.7: the day is typed in after the fact, so "today" is routinely the following
    calendar day. Reading it off the shift is the only source that means anything."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 5), sequence=1)

    response = await _post(client, auth_headers(manager), "dep-date", shift_id=shift)

    assert response.status_code == 201
    assert response.json()["business_date"] == "2026-12-05"


async def test_a_client_supplied_business_date_is_refused_outright(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """`extra="forbid"`: not ignored, refused. A silently-dropped field leaves the caller
    believing they set something they did not."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 6), sequence=1)

    response = await client.post(
        f"/api/v1/shifts/{shift}/bank-deposits",
        json={"amount": "100000.00", "business_date": "2020-01-01"},
        headers={**auth_headers(manager), "Idempotency-Key": "dep-baddate"},
    )

    assert response.status_code == 422


# --- §6.4: subtracted, and the sign says so ----------------------------------------


async def test_the_page_says_the_total_is_subtracted(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_bank_deposit: Callable[..., UUID],
    auth_headers,
) -> None:
    """The rule travels with the figure. A deposit read as income would turn a ₹1,00,000
    pay-in into a ₹2,00,000 error in a single step."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 7), sequence=1)
    make_bank_deposit(shift, business_date=date(2026, 12, 7), amount="100000.00")

    page = await client.get(
        f"/api/v1/shifts/{shift}/bank-deposits", headers=auth_headers(manager)
    )

    assert Decimal(page.json()["total"]) == Decimal("100000.00")
    assert "SUBTRACTED" in page.json()["cash_basis"]


async def test_a_reversed_deposit_puts_the_money_back(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_bank_deposit: Callable[..., UUID],
    auth_headers,
) -> None:
    """A deposit that never cleared. §6.9: both rows stay visible and the negative nets out,
    so the locker balance goes back up rather than the row simply disappearing."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 8), sequence=1)
    deposit = make_bank_deposit(
        shift, business_date=date(2026, 12, 8), amount="100000.00"
    )

    response = await client.post(
        f"/api/v1/shifts/{shift}/bank-deposits/{deposit}/reversals",
        json={"reason": "Cheque bounced, never credited"},
        headers={**auth_headers(manager), "Idempotency-Key": "dep-rev"},
    )

    assert response.status_code == 201
    assert Decimal(response.json()["reversal"]["amount"]) == Decimal("-100000.00")

    page = await client.get(
        f"/api/v1/shifts/{shift}/bank-deposits", headers=auth_headers(manager)
    )
    assert Decimal(page.json()["total"]) == Decimal("0.00")


# --- §5.3: the deposit slip --------------------------------------------------------


async def test_a_deposit_can_carry_its_slip(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 9), sequence=1)
    slip = await _upload(client, auth_headers(manager), shift_id=shift)

    response = await _post(
        client, auth_headers(manager), "dep-slip", shift_id=shift, attachment_id=slip
    )

    assert response.status_code == 201
    assert response.json()["attachment_id"] == slip


async def test_one_slip_cannot_justify_two_deposits(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """§5.3, and the reason it matters here: two deposits against one slip would subtract
    ₹1,00,000 from the locker twice, and the day would read a lakh short with nothing
    pointing at why."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 10), sequence=1)
    slip = await _upload(client, auth_headers(manager), shift_id=shift)

    first = await _post(
        client, auth_headers(manager), "dep-s1", shift_id=shift, attachment_id=slip
    )
    second = await _post(
        client, auth_headers(manager), "dep-s2", shift_id=shift, attachment_id=slip
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["code"] == "ATTACHMENT_ALREADY_LINKED"
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM bank_deposits WHERE shift_id = :s").bindparams(
                s=shift
            )
        ).scalar_one()
    assert count == 1


async def test_a_deposit_slip_cannot_be_reused_by_an_expense(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    expense_category_ids,
    auth_headers,
) -> None:
    """The rule runs in both directions -- §10 asks for exactly this, and it is what makes
    `link()` a single gate rather than three tables each guarding themselves."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 11), sequence=1)
    slip = await _upload(client, auth_headers(manager), shift_id=shift)

    await _post(
        client, auth_headers(manager), "dep-x1", shift_id=shift, attachment_id=slip
    )
    expense = await client.post(
        f"/api/v1/shifts/{shift}/expenses",
        json={
            "category_id": str(expense_category_ids["MAINTENANCE"]),
            "mode": "cash",
            "amount": "500.00",
            "description": "Reusing the deposit slip",
            "attachment_id": slip,
        },
        headers={**auth_headers(manager), "Idempotency-Key": "dep-x2"},
    )

    assert expense.status_code == 409
    assert expense.json()["code"] == "ATTACHMENT_ALREADY_LINKED"


async def test_an_expense_receipt_cannot_be_reused_by_a_deposit(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    expense_category_ids,
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 12), sequence=1)
    receipt = await _upload(client, auth_headers(manager), shift_id=shift)

    await client.post(
        f"/api/v1/shifts/{shift}/expenses",
        json={
            "category_id": str(expense_category_ids["MAINTENANCE"]),
            "mode": "cash",
            "amount": "500.00",
            "description": "A genuine expense",
            "attachment_id": receipt,
        },
        headers={**auth_headers(manager), "Idempotency-Key": "dep-y1"},
    )
    deposit = await _post(
        client, auth_headers(manager), "dep-y2", shift_id=shift, attachment_id=receipt
    )

    assert deposit.status_code == 409
    assert deposit.json()["code"] == "ATTACHMENT_ALREADY_LINKED"


async def test_a_reversal_inherits_the_slip_without_a_re_upload(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """§5.3's inheritance rule, on a third table. Nobody photographs the same pay-in slip
    twice, and exactly one live row holds it throughout: the original is reversed and so not
    live, the reversal is itself a reversal and so not live, the replacement is the single
    live claimant."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 13), sequence=1)
    slip = await _upload(client, auth_headers(manager), shift_id=shift)
    created = await _post(
        client, auth_headers(manager), "dep-i1", shift_id=shift, attachment_id=slip
    )
    deposit_id = created.json()["id"]

    response = await client.post(
        f"/api/v1/shifts/{shift}/bank-deposits/{deposit_id}/reversals",
        json={"reason": "Typed 100000 instead of 90000", "replacement_amount": "90000.00"},
        headers={**auth_headers(manager), "Idempotency-Key": "dep-i2"},
    )

    assert response.status_code == 201
    assert response.json()["reversal"]["attachment_id"] == slip
    assert response.json()["replacement"]["attachment_id"] == slip
    assert Decimal(response.json()["replacement"]["amount"]) == Decimal("90000.00")


async def test_an_attendant_may_read_a_deposit_slip_on_their_own_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """§7.3's ownership half, extended to the third claiming table. The attendant did not
    upload this and cannot record a deposit at all -- but the day is theirs, and withholding
    the evidence attached to their own shift would be the one gap in that rule."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 14), sequence=1)
    slip = await _upload(client, auth_headers(manager), shift_id=shift)
    await _post(
        client, auth_headers(manager), "dep-read", shift_id=shift, attachment_id=slip
    )

    response = await client.get(
        f"/api/v1/attachments/{slip}/url", headers=auth_headers(attendant)
    )

    assert response.status_code == 200
    assert response.json()["url"]


async def test_an_attendant_cannot_read_a_deposit_slip_from_another_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    owner = make_user("attendant")
    other = make_user("attendant")
    shift = make_shift(owner, business_date=date(2026, 12, 15), sequence=1)
    slip = await _upload(client, auth_headers(manager), shift_id=shift)
    await _post(
        client, auth_headers(manager), "dep-read2", shift_id=shift, attachment_id=slip
    )

    response = await client.get(
        f"/api/v1/attachments/{slip}/url", headers=auth_headers(other)
    )

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_YOUR_ATTACHMENT"


async def test_a_linked_deposit_slip_is_never_swept(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    tmp_path: Path,
) -> None:
    """§7.4: a linked attachment is never deleted, at any age. Ageing the row past the
    cutoff proves `linked_at` was stamped, not merely that the sweep found nothing."""
    from app.jobs.cleanup_attachments import main
    from app.services.storage import LocalStorage

    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 16), sequence=1)
    slip = await _upload(client, auth_headers(manager), shift_id=shift)
    await _post(
        client, auth_headers(manager), "dep-sweep", shift_id=shift, attachment_id=slip
    )

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE attachments SET created_at = now() - interval '30 days' "
                "WHERE id = :id"
            ).bindparams(id=UUID(slip))
        )

    main([], storage=LocalStorage(root=tmp_path / "storage"))

    with engine.connect() as connection:
        survived = connection.execute(
            text("SELECT count(*) FROM attachments WHERE id = :id").bindparams(
                id=UUID(slip)
            )
        ).scalar_one()
    assert survived == 1


# --- corrections, idempotency, routing ---------------------------------------------


async def test_a_deposit_can_be_corrected_while_the_shift_is_open(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_bank_deposit: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 17), sequence=1)
    deposit = make_bank_deposit(
        shift, business_date=date(2026, 12, 17), amount="100000.00"
    )

    response = await client.patch(
        f"/api/v1/shifts/{shift}/bank-deposits/{deposit}",
        json={"amount": "90000.00", "bank_reference": "HDFC/2026/1217"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 200
    assert response.json()["amount"] == "90000.00"
    assert response.json()["bank_reference"] == "HDFC/2026/1217"


async def test_an_empty_patch_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_bank_deposit: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 18), sequence=1)
    deposit = make_bank_deposit(shift, business_date=date(2026, 12, 18))

    response = await client.patch(
        f"/api/v1/shifts/{shift}/bank-deposits/{deposit}",
        json={},
        headers=auth_headers(manager),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "NO_FIELDS_TO_UPDATE"


async def test_an_explicit_null_leaves_a_reference_alone(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_bank_deposit: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 19), sequence=1)
    deposit = make_bank_deposit(
        shift, business_date=date(2026, 12, 19), bank_reference="HDFC/1"
    )

    response = await client.patch(
        f"/api/v1/shifts/{shift}/bank-deposits/{deposit}",
        json={"amount": "90000.00", "bank_reference": None},
        headers=auth_headers(manager),
    )

    assert response.json()["bank_reference"] == "HDFC/1"


async def test_a_reversal_and_an_already_reversed_row_are_both_uneditable(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_bank_deposit: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 20), sequence=1)
    deposit = make_bank_deposit(shift, business_date=date(2026, 12, 20))

    reversed_response = await client.post(
        f"/api/v1/shifts/{shift}/bank-deposits/{deposit}/reversals",
        json={"reason": "Entered against the wrong day"},
        headers={**auth_headers(manager), "Idempotency-Key": "dep-u1"},
    )
    reversal_id = reversed_response.json()["reversal"]["id"]

    edit_reversal = await client.patch(
        f"/api/v1/shifts/{shift}/bank-deposits/{reversal_id}",
        json={"amount": "1.00"},
        headers=auth_headers(manager),
    )
    assert edit_reversal.status_code == 409
    assert edit_reversal.json()["code"] == "CANNOT_EDIT_A_REVERSAL"

    edit_original = await client.patch(
        f"/api/v1/shifts/{shift}/bank-deposits/{deposit}",
        json={"amount": "1.00"},
        headers=auth_headers(manager),
    )
    assert edit_original.status_code == 409
    assert edit_original.json()["code"] == "DEPOSIT_ALREADY_REVERSED"

    double = await client.post(
        f"/api/v1/shifts/{shift}/bank-deposits/{deposit}/reversals",
        json={"reason": "Reversing it again"},
        headers={**auth_headers(manager), "Idempotency-Key": "dep-u2"},
    )
    assert double.status_code == 409
    assert double.json()["code"] == "DEPOSIT_ALREADY_REVERSED"

    reverse_the_reversal = await client.post(
        f"/api/v1/shifts/{shift}/bank-deposits/{reversal_id}/reversals",
        json={"reason": "Undoing the undo"},
        headers={**auth_headers(manager), "Idempotency-Key": "dep-u3"},
    )
    assert reverse_the_reversal.status_code == 409
    assert reverse_the_reversal.json()["code"] == "CANNOT_REVERSE_A_REVERSAL"


async def test_reversing_on_a_locked_shift_is_an_admin_action(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_bank_deposit: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    admin = make_user("admin")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant, business_date=date(2026, 12, 21), sequence=1, status="locked"
    )
    deposit = make_bank_deposit(shift, business_date=date(2026, 12, 21))

    refused = await client.post(
        f"/api/v1/shifts/{shift}/bank-deposits/{deposit}/reversals",
        json={"reason": "Never actually banked"},
        headers={**auth_headers(manager), "Idempotency-Key": "dep-l1"},
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "LOCKED_SHIFT_REVERSAL_REQUIRES_ADMIN"

    allowed = await client.post(
        f"/api/v1/shifts/{shift}/bank-deposits/{deposit}/reversals",
        json={"reason": "Never actually banked"},
        headers={**auth_headers(admin), "Idempotency-Key": "dep-l2"},
    )
    assert allowed.status_code == 201


async def test_the_same_key_twice_records_one_deposit(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 22), sequence=1)

    first = await _post(client, auth_headers(manager), "dep-idem", shift_id=shift)
    second = await _post(client, auth_headers(manager), "dep-idem", shift_id=shift)

    assert first.json() == second.json()
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM bank_deposits WHERE shift_id = :s").bindparams(
                s=shift
            )
        ).scalar_one()
    assert count == 1


async def test_a_missing_idempotency_key_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 23), sequence=1)

    response = await client.post(
        f"/api/v1/shifts/{shift}/bank-deposits",
        json={"amount": "100000.00"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_the_same_key_with_a_different_body_is_a_client_bug(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 24), sequence=1)

    await _post(client, auth_headers(manager), "dep-reuse", shift_id=shift, amount="100.00")
    second = await _post(
        client, auth_headers(manager), "dep-reuse", shift_id=shift, amount="200.00"
    )

    assert second.status_code == 422
    assert second.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_an_unknown_attachment_refuses_and_writes_nothing(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """Linked before the row is constructed, so a refusal leaves nothing behind."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 25), sequence=1)

    response = await _post(
        client,
        auth_headers(manager),
        "dep-ghost",
        shift_id=shift,
        attachment_id=str(uuid4()),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "ATTACHMENT_NOT_FOUND"
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM bank_deposits WHERE shift_id = :s").bindparams(
                s=shift
            )
        ).scalar_one()
    assert count == 0


async def test_a_deposit_from_another_shift_is_refused_by_the_url(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_bank_deposit: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    first = make_shift(attendant, business_date=date(2026, 12, 26), sequence=1)
    second = make_shift(attendant, business_date=date(2026, 12, 26), sequence=2)
    deposit = make_bank_deposit(first, business_date=date(2026, 12, 26))

    response = await client.patch(
        f"/api/v1/shifts/{second}/bank-deposits/{deposit}",
        json={"amount": "1.00"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "DEPOSIT_NOT_IN_SHIFT"


async def test_an_unknown_deposit_is_a_404(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 27), sequence=1)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/bank-deposits/{uuid4()}",
        json={"amount": "1.00"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "DEPOSIT_NOT_FOUND"


async def test_the_total_sums_the_whole_shift_not_the_returned_page(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_bank_deposit: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 28), sequence=1)

    for _ in range(101):
        make_bank_deposit(shift, business_date=date(2026, 12, 28), amount="10.00")

    page = await client.get(
        f"/api/v1/shifts/{shift}/bank-deposits", headers=auth_headers(manager)
    )

    assert page.json()["truncated"] is True
    assert Decimal(page.json()["total"]) == Decimal("1010.00")


async def test_replaying_a_reversal_key_returns_the_first_answer(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_bank_deposit: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """A retried reversal must not append a second negative row. On this table that would
    put ₹1,00,000 back into the locker twice, and the day would read a lakh *over* -- a
    surplus, which is the kind of wrong number nobody goes looking for."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 12, 29), sequence=1)
    deposit = make_bank_deposit(
        shift, business_date=date(2026, 12, 29), amount="100000.00"
    )

    first = await client.post(
        f"/api/v1/shifts/{shift}/bank-deposits/{deposit}/reversals",
        json={"reason": "Never actually banked"},
        headers={**auth_headers(manager), "Idempotency-Key": "dep-replay"},
    )
    second = await client.post(
        f"/api/v1/shifts/{shift}/bank-deposits/{deposit}/reversals",
        json={"reason": "Never actually banked"},
        headers={**auth_headers(manager), "Idempotency-Key": "dep-replay"},
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()

    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM bank_deposits WHERE shift_id = :s").bindparams(
                s=shift
            )
        ).scalar_one()
    assert count == 2
