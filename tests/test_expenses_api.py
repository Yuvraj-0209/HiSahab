"""The expense endpoints (CLAUDE.md §5.2, §6.4, §6.7).

Covers what a row *is* and what the routes refuse. §6.7's flagging rules are
tests/test_expense_flagging.py, §6.9's reversal path is tests/test_expense_reversals.py,
and §6.7's lock precondition is tests/test_shift_lock_expenses.py -- mirroring the
collections split for the same reason: each concern fails for its own reasons.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.anyio

DAY = date(2026, 4, 20)


def _post(client: AsyncClient, shift_id: UUID, headers: dict[str, str], key: str, **body):
    return client.post(
        f"/api/v1/shifts/{shift_id}/expenses",
        json={
            "category": "maintenance",
            "mode": "cash",
            "amount": "500.00",
            "description": "routine upkeep",
            **body,
        },
        headers={**headers, "Idempotency-Key": key},
    )


def _count(engine: Engine, shift_id: UUID) -> int:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT count(*) FROM expenses WHERE shift_id = :s").bindparams(
                s=shift_id
            )
        ).scalar_one()


# --- creating ------------------------------------------------------------------


async def test_an_expense_is_recorded_with_its_category_mode_and_amount(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(
        client, shift, auth_headers(attendant), "k1",
        category="electricity", mode="bank_transfer", amount="820.00",
        description="monthly board bill",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["category"] == "electricity"
    assert body["mode"] == "bank_transfer"
    # A string, not a number -- §3 rule 1 end to end.
    assert body["amount"] == "820.00"
    assert body["reverses_id"] is None
    assert body["is_reversed"] is False
    assert body["requires_review"] is False


async def test_several_expenses_may_share_a_category_on_one_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    """Unlike collections' one-live-row-per-mode rule -- two maintenance call-outs in a
    day is not a mistake."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)

    first = await _post(client, shift, headers, "m1", amount="200.00")
    second = await _post(client, shift, headers, "m2", amount="300.00")
    assert first.status_code == 201
    assert second.status_code == 201

    listing = await client.get(f"/api/v1/shifts/{shift}/expenses", headers=headers)
    assert listing.json()["totals_by_category"] == {"maintenance": "500.00"}


async def test_a_zero_amount_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    """Unlike collections' explicit-zero cash declaration, a ₹0 expense records nothing
    and has no reason to exist (§5.2's Phase 7 amendment: the sign rule is strict)."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(client, shift, auth_headers(attendant), "z", amount="0.00")

    assert response.status_code == 422
    assert _count(engine, shift) == 0


async def test_a_negative_amount_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(client, shift, auth_headers(attendant), "n", amount="-50.00")

    assert response.status_code == 422
    assert _count(engine, shift) == 0


async def test_a_blank_description_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(client, shift, auth_headers(attendant), "d1", description="  ")

    assert response.status_code == 422
    assert _count(engine, shift) == 0


async def test_a_missing_mode_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    """§6.4's Phase 7 amendment made this NOT NULL with no default -- an answer, never an
    omission, exactly as §6.8 requires an explicit ₹0 cash declaration."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await client.post(
        f"/api/v1/shifts/{shift}/expenses",
        json={"category": "maintenance", "amount": "500.00", "description": "upkeep"},
        headers={**auth_headers(attendant), "Idempotency-Key": "no-mode"},
    )

    assert response.status_code == 422
    assert _count(engine, shift) == 0


async def test_fuel_purchase_is_not_a_valid_category(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    """§5.2's Phase 7 amendment removed it: a tanker restock settles against the IOCL
    ledger and never touches the drawer, so recording one here would be exactly the
    mistake §14 forbids for IOCL/PAD payments."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(
        client, shift, auth_headers(attendant), "fp", category="fuel_purchase"
    )

    assert response.status_code == 422
    assert _count(engine, shift) == 0


async def test_the_amount_sign_rule_is_enforced_at_the_database_level(
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
) -> None:
    """§6.6's belt-and-braces rule: a client can bypass Pydantic, but not a CHECK."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    with pytest.raises(IntegrityError) as caught:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO expenses (shift_id, category, mode, amount, "
                    "description) VALUES (:s, CAST('maintenance' AS expense_category), "
                    "CAST('cash' AS expense_mode), 0.00, 'zero expense')"
                ).bindparams(s=shift)
            )
    assert "ck_expenses_amount_sign" in str(caught.value)


async def test_a_description_of_only_whitespace_is_refused_at_the_database_level(
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
) -> None:
    """`char_length(regexp_replace(description, '\\s', '', 'g'))`, not `btrim` -- 0007
    already found that one-argument `btrim` strips spaces only, so this uses a tab to
    prove the stronger form."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    with pytest.raises(IntegrityError) as caught:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO expenses (shift_id, category, mode, amount, "
                    "description) VALUES (:s, CAST('maintenance' AS expense_category), "
                    "CAST('cash' AS expense_mode), 500.00, :d)"
                ).bindparams(s=shift, d="\t\t")
            )
    assert "ck_expenses_description_length" in str(caught.value)


# --- idempotency (§6.10) --------------------------------------------------------


async def test_the_same_key_twice_creates_one_row(
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

    first = await _post(client, shift, headers, "same-key")
    second = await _post(client, shift, headers, "same-key")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    assert _count(engine, shift) == 1


async def test_a_post_without_the_idempotency_key_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await client.post(
        f"/api/v1/shifts/{shift}/expenses",
        json={
            "category": "maintenance", "mode": "cash", "amount": "500.00",
            "description": "upkeep",
        },
        headers=auth_headers(attendant),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert _count(engine, shift) == 0


# --- updating --------------------------------------------------------------------


async def test_patch_corrects_the_amount_on_an_open_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    expense = make_expense(shift, amount="500.00")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{expense}",
        json={"amount": "650.00"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 200
    assert response.json()["amount"] == "650.00"


async def test_patch_can_change_description_paid_to_and_mode(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    expense = make_expense(shift, amount="500.00", mode="cash")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{expense}",
        json={
            "description": "corrected description",
            "paid_to": "ABC Electricals",
            "mode": "upi",
        },
        headers=auth_headers(attendant),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["description"] == "corrected description"
    assert body["paid_to"] == "ABC Electricals"
    assert body["mode"] == "upi"


async def test_patch_cannot_change_category(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    """§6.7's Phase 7 amendment: changing the category would silently move an expense out
    of the group the aggregate rule already scoped it under."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    expense = make_expense(shift, category="maintenance", amount="500.00")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{expense}",
        json={"category": "electricity"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 422


async def test_patch_with_no_fields_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    expense = make_expense(shift, amount="500.00")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{expense}",
        json={},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "NO_FIELDS_TO_UPDATE"


async def test_patch_on_a_closed_shift_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    expense = make_expense(shift, amount="500.00")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{expense}",
        json={"amount": "600.00"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_NOT_OPEN"


# --- listing -----------------------------------------------------------------


async def test_listing_nets_a_reversal_into_the_category_total(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    original = make_expense(shift, category="maintenance", amount="500.00")
    make_expense(
        shift, category="maintenance", amount="-500.00", reverses_id=original,
        reversal_reason="duplicate entry",
    )

    response = await client.get(
        f"/api/v1/shifts/{shift}/expenses", headers=auth_headers(manager)
    )

    assert response.json()["totals_by_category"] == {"maintenance": "0.00"}
    items = response.json()["items"]
    assert len(items) == 2
    reversed_flags = {item["id"]: item["is_reversed"] for item in items}
    assert reversed_flags[str(original)] is True


async def test_a_description_with_an_internal_space_is_accepted(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    """0008 wrote `ck_expenses_description_length` by stripping *every* whitespace
    character, mirroring 0007's reversal-reason regex. That is wrong for a description:
    "a b" is a genuinely fine three-character string, but collapsing its internal space
    left "ab" -- two characters -- and the database refused input the API had already
    accepted, as an opaque 500. 0009 fixed it to trim only the ends, matching
    `StringConstraints(strip_whitespace=True)`."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(client, shift, auth_headers(attendant), "internal-space", description="a b")

    assert response.status_code == 201
    assert response.json()["description"] == "a b"


async def test_the_description_check_still_trims_leading_and_trailing_whitespace(
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
) -> None:
    """Belt-and-braces: a description of two real characters padded with spaces must
    still fail at the database level, matching what the API already refuses."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    with pytest.raises(IntegrityError) as caught:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO expenses (shift_id, category, mode, amount, "
                    "description) VALUES (:s, CAST('maintenance' AS expense_category), "
                    "CAST('cash' AS expense_mode), 500.00, '  ab  ')"
                ).bindparams(s=shift)
            )
    assert "ck_expenses_description_length" in str(caught.value)


# --- not found / wrong shift ----------------------------------------------------


async def test_a_nonexistent_expense_id_is_404(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    from uuid import uuid4

    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{uuid4()}",
        json={"amount": "10.00"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "EXPENSE_NOT_FOUND"


async def test_an_expense_from_a_different_shift_is_409(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    shift_a = make_shift(manager, business_date=DAY, sequence=1)
    shift_b = make_shift(manager, business_date=DAY, sequence=2)
    expense = make_expense(shift_a, amount="500.00")

    response = await client.patch(
        f"/api/v1/shifts/{shift_b}/expenses/{expense}",
        json={"amount": "10.00"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "EXPENSE_NOT_IN_SHIFT"


# --- failure rollback (§6.10) ----------------------------------------------------


async def test_a_refused_create_releases_its_idempotency_key(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    clean_expenses,
) -> None:
    """The reservation is committed before the handler runs, so a failure inside the try
    block must still release the key -- mirrors
    tests/test_reversals.py::test_a_failure_after_the_reversal_rolls_the_whole_thing_back.

    The failure is injected rather than provoked: every value a client can send that would
    fail is refused earlier, by Pydantic or by a CHECK, so there is no reachable input that
    fails at this exact point. What is asserted is real regardless -- that a genuine
    mid-transaction failure does not leave a wedged reservation, exactly as
    test_a_refused_request_releases_its_key already proves for a business-rule 409.
    """
    import app.api.v1.expenses as expenses_module

    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = {**auth_headers(attendant), "Idempotency-Key": "release-me"}
    body = {
        "category": "maintenance", "mode": "cash", "amount": "500.00",
        "description": "upkeep",
    }

    def _explode(*args, **kwargs):
        raise RuntimeError("storage went away mid-create")

    monkeypatch.setattr(expenses_module.audit, "record", _explode)

    with pytest.raises(RuntimeError):
        await client.post(
            f"/api/v1/shifts/{shift}/expenses", json=body, headers=headers
        )

    monkeypatch.undo()

    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM expenses WHERE shift_id = :s").bindparams(s=shift)
        ).scalar_one()
    assert count == 0

    # The key is free again -- a retry with the same body now succeeds.
    retry = await client.post(
        f"/api/v1/shifts/{shift}/expenses", json=body, headers=headers
    )
    assert retry.status_code == 201


async def test_patch_ignores_an_explicit_null_rather_than_clearing_the_field(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    """Matches collections.py and readings.py: a client that omits a field and one that
    sends null both mean "leave it alone" far more often than "erase it"."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    expense = make_expense(shift, amount="500.00", paid_to="ABC Electricals")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{expense}",
        json={"paid_to": None, "amount": "550.00"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["paid_to"] == "ABC Electricals"
    assert body["amount"] == "550.00"
