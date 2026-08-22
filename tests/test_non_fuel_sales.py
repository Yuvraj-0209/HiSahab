"""Non-fuel sales over HTTP (CLAUDE.md §6.4, §6.9, §6.10, §8, §13.2).

The term §6.4 named from the beginning and §5.2 gave a column nowhere until Phase 10.

This file covers the table and its routes. **The test that proves *why* the figure belongs on
§6.4's sales side rather than its cash side lives in `tests/test_cash_position.py`**, because
it needs a comparison this module cannot make on its own: a card-paid oil sale must leave
derived cash unchanged, and putting the amount on the cash side instead makes it wrong by
exactly the value of every non-fuel sale the customer did not pay cash for. What this file
pins is the half that travels with the data -- `sales_basis` on the page, saying so.
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

DAY = date(2026, 11, 4)


async def _post(
    client: AsyncClient,
    headers: dict[str, str],
    key: str,
    *,
    shift_id: UUID,
    amount: str = "500.00",
    description: str | None = "Engine oil",
):
    body: dict[str, object] = {"amount": amount}
    if description is not None:
        body["description"] = description
    return await client.post(
        f"/api/v1/shifts/{shift_id}/non-fuel-sales",
        json=body,
        headers={**headers, "Idempotency-Key": key},
    )


# --- the shape of the thing --------------------------------------------------------


async def test_an_attendant_records_a_non_fuel_sale_on_their_own_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(client, auth_headers(attendant), "oil-1", shift_id=shift)

    assert response.status_code == 201
    body = response.json()
    # Money as a string end to end -- §3 rule 1. A float would round-trip through binary
    # floating point before it ever reached NUMERIC.
    assert body["amount"] == "500.00"
    assert body["description"] == "Engine oil"
    assert body["is_reversed"] is False


async def test_a_shift_may_record_several_separate_non_fuel_sales(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """Unlike `collections`, there is no one-live-row rule. Three customers buying three
    bottles of oil is three rows, not a mistake -- the lumping that justifies one row per
    payment channel (one card machine, one QR) has no equivalent here."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 11, 5), sequence=1)

    for index in range(3):
        response = await _post(
            client, auth_headers(attendant), f"oil-{index}", shift_id=shift, amount="120.00"
        )
        assert response.status_code == 201

    page = await client.get(
        f"/api/v1/shifts/{shift}/non-fuel-sales", headers=auth_headers(attendant)
    )

    assert page.status_code == 200
    assert len(page.json()["items"]) == 3
    assert Decimal(page.json()["total"]) == Decimal("360.00")


async def test_a_zero_non_fuel_sale_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """`gt=0`, not `ge=0`. §6.8 needs an explicit ₹0 *cash declaration* -- zero as an answer
    rather than an omission -- but a ₹0 non-fuel sale records nothing at all, which is why
    `expenses` and `credit_sales` use the strict sign rule too."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 11, 6), sequence=1)

    response = await _post(
        client, auth_headers(attendant), "oil-zero", shift_id=shift, amount="0.00"
    )

    assert response.status_code == 422
    with engine.connect() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM non_fuel_sales WHERE shift_id = :s"
            ).bindparams(s=shift)
        ).scalar_one()
    assert count == 0


# --- §6.4: the sales side, not the cash side --------------------------------------


async def test_the_page_says_the_total_belongs_to_sales_not_to_cash(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """The same shape `collections.py` uses for `cash_basis`: the rule travels with the
    figure, so a client cannot put it on the wrong side of §6.4 without reading a sentence
    saying not to."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 11, 7), sequence=1)
    await _post(client, auth_headers(attendant), "oil-basis", shift_id=shift)

    page = await client.get(
        f"/api/v1/shifts/{shift}/non-fuel-sales", headers=auth_headers(attendant)
    )

    basis = page.json()["sales_basis"]
    assert "total_sales" in basis
    assert "NOT to the cash side" in basis


async def test_the_total_sums_the_whole_shift_not_the_returned_page(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_non_fuel_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """The Phase 9 defect Step 0 fixed on `credit_repayments`, guarded against here on the
    day the table was born rather than a phase later."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 11, 8), sequence=1)

    for _ in range(101):
        make_non_fuel_sale(shift, amount="10.00")

    page = await client.get(
        f"/api/v1/shifts/{shift}/non-fuel-sales", headers=auth_headers(manager)
    )

    assert page.json()["truncated"] is True
    assert len(page.json()["items"]) == 100
    assert Decimal(page.json()["total"]) == Decimal("1010.00")


async def test_the_total_nets_a_reversal_rather_than_hiding_it(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_non_fuel_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """§6.9: both rows stay visible, and the negative nets out. A cancelled sale that
    vanished from the total instead would leave the total and the list disagreeing."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 11, 9), sequence=1)
    original = make_non_fuel_sale(shift, amount="500.00")

    response = await client.post(
        f"/api/v1/shifts/{shift}/non-fuel-sales/{original}/reversals",
        json={"reason": "Customer returned the oil"},
        headers={**auth_headers(manager), "Idempotency-Key": "oil-rev"},
    )

    assert response.status_code == 201
    assert Decimal(response.json()["reversal"]["amount"]) == Decimal("-500.00")
    assert response.json()["original"]["is_reversed"] is True

    page = await client.get(
        f"/api/v1/shifts/{shift}/non-fuel-sales", headers=auth_headers(manager)
    )
    assert Decimal(page.json()["total"]) == Decimal("0.00")
    assert len(page.json()["items"]) == 2


# --- §6.9 corrections --------------------------------------------------------------


async def test_a_reversal_cannot_itself_be_edited_or_reversed(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_non_fuel_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 11, 10), sequence=1)
    original = make_non_fuel_sale(shift, amount="500.00")

    first = await client.post(
        f"/api/v1/shifts/{shift}/non-fuel-sales/{original}/reversals",
        json={"reason": "Wrong amount typed"},
        headers={**auth_headers(manager), "Idempotency-Key": "oil-r1"},
    )
    reversal_id = first.json()["reversal"]["id"]

    second = await client.post(
        f"/api/v1/shifts/{shift}/non-fuel-sales/{reversal_id}/reversals",
        json={"reason": "Undoing the undo"},
        headers={**auth_headers(manager), "Idempotency-Key": "oil-r2"},
    )
    assert second.status_code == 409
    assert second.json()["code"] == "CANNOT_REVERSE_A_REVERSAL"

    edit = await client.patch(
        f"/api/v1/shifts/{shift}/non-fuel-sales/{reversal_id}",
        json={"amount": "300.00"},
        headers=auth_headers(manager),
    )
    assert edit.status_code == 409
    assert edit.json()["code"] == "CANNOT_EDIT_A_REVERSAL"


async def test_an_already_reversed_row_cannot_be_edited(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_non_fuel_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """Editing the original after a reversal leaves the shift netting to a figure neither
    row ever held, and the audit log claiming a row cancelled at ₹500 now reads ₹300."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 11, 11), sequence=1)
    original = make_non_fuel_sale(shift, amount="500.00")

    await client.post(
        f"/api/v1/shifts/{shift}/non-fuel-sales/{original}/reversals",
        json={"reason": "Wrong amount typed"},
        headers={**auth_headers(manager), "Idempotency-Key": "oil-r3"},
    )

    edit = await client.patch(
        f"/api/v1/shifts/{shift}/non-fuel-sales/{original}",
        json={"amount": "300.00"},
        headers=auth_headers(manager),
    )

    assert edit.status_code == 409
    assert edit.json()["code"] == "NON_FUEL_SALE_ALREADY_REVERSED"


async def test_a_reversal_can_carry_its_replacement_in_the_same_transaction(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_non_fuel_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """Without this a correction on a *closed* shift is impossible: the reversal lands and
    the follow-up POST is refused by `writable=True`."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant, business_date=date(2026, 11, 12), sequence=1, status="closed"
    )
    original = make_non_fuel_sale(shift, amount="500.00", description="Engine oil")

    response = await client.post(
        f"/api/v1/shifts/{shift}/non-fuel-sales/{original}/reversals",
        json={"reason": "Typed 500 instead of 450", "replacement_amount": "450.00"},
        headers={**auth_headers(manager), "Idempotency-Key": "oil-r4"},
    )

    assert response.status_code == 201
    assert Decimal(response.json()["replacement"]["amount"]) == Decimal("450.00")
    # The replacement inherits the original's description rather than losing it.
    assert response.json()["replacement"]["description"] == "Engine oil"

    page = await client.get(
        f"/api/v1/shifts/{shift}/non-fuel-sales", headers=auth_headers(manager)
    )
    assert Decimal(page.json()["total"]) == Decimal("450.00")


async def test_a_blank_reversal_reason_is_refused_by_pydantic_and_by_the_database(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_non_fuel_sale: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """The 0007 lesson: `strip_whitespace` runs before the length check, so "   " is not a
    three-character reason. The CHECK is asserted directly, because a Pydantic-only guard
    would leave a raw SQL writer able to record a ₹500 negation explained by nothing."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 11, 13), sequence=1)
    original = make_non_fuel_sale(shift, amount="500.00")

    response = await client.post(
        f"/api/v1/shifts/{shift}/non-fuel-sales/{original}/reversals",
        json={"reason": "   "},
        headers={**auth_headers(manager), "Idempotency-Key": "oil-blank"},
    )
    assert response.status_code == 422

    from sqlalchemy.exc import IntegrityError

    with engine.begin() as connection, pytest.raises(IntegrityError) as exc:
        connection.execute(
            text(
                "INSERT INTO non_fuel_sales (shift_id, amount, reverses_id, "
                "reversal_reason) VALUES (:s, -500.00, :r, '   ')"
            ).bindparams(s=shift, r=original)
        )
    assert "ck_non_fuel_sales_reversal_has_reason" in str(exc.value)


# --- §6.10 idempotency -------------------------------------------------------------


async def test_the_same_key_twice_records_one_sale(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 11, 14), sequence=1)

    first = await _post(client, auth_headers(attendant), "oil-idem", shift_id=shift)
    second = await _post(client, auth_headers(attendant), "oil-idem", shift_id=shift)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()

    with engine.connect() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM non_fuel_sales WHERE shift_id = :s"
            ).bindparams(s=shift)
        ).scalar_one()
    assert count == 1


async def test_a_missing_idempotency_key_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 11, 15), sequence=1)

    response = await client.post(
        f"/api/v1/shifts/{shift}/non-fuel-sales",
        json={"amount": "500.00"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_the_same_key_with_a_different_body_is_a_client_bug(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 11, 16), sequence=1)

    await _post(client, auth_headers(attendant), "oil-reuse", shift_id=shift, amount="500.00")
    second = await _post(
        client, auth_headers(attendant), "oil-reuse", shift_id=shift, amount="600.00"
    )

    assert second.status_code == 422
    assert second.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_a_refusal_releases_the_key_for_a_corrected_retry(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """A rejected request must not wedge the key at REQUEST_IN_PROGRESS for 24 hours,
    locking the attendant out of an action that never happened."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 11, 17), sequence=1)

    # Refused by _load_sale's 404 path via a reversal on a row that does not exist.
    missing = uuid4()
    first = await client.post(
        f"/api/v1/shifts/{shift}/non-fuel-sales/{missing}/reversals",
        json={"reason": "Pointing at nothing"},
        headers={**auth_headers(attendant), "Idempotency-Key": "oil-release"},
    )
    assert first.status_code in (403, 404)

    manager = make_user("manager")
    retry = await client.post(
        f"/api/v1/shifts/{shift}/non-fuel-sales/{missing}/reversals",
        json={"reason": "Pointing at nothing"},
        headers={**auth_headers(manager), "Idempotency-Key": "oil-release"},
    )
    assert retry.status_code == 404
    assert retry.json()["code"] == "NON_FUEL_SALE_NOT_FOUND"


# --- routing and shift scoping -----------------------------------------------------


async def test_a_sale_from_another_shift_is_refused_by_the_url(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_non_fuel_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """409 rather than 404: the row exists and the caller may be allowed to see it, but the
    URL asserts a parent-child relationship that is not true."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    first = make_shift(attendant, business_date=date(2026, 11, 18), sequence=1)
    second = make_shift(attendant, business_date=date(2026, 11, 18), sequence=2)
    sale = make_non_fuel_sale(first, amount="500.00")

    response = await client.patch(
        f"/api/v1/shifts/{second}/non-fuel-sales/{sale}",
        json={"amount": "300.00"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "NON_FUEL_SALE_NOT_IN_SHIFT"


# --- the PATCH happy path ----------------------------------------------------------


async def test_a_figure_can_be_corrected_while_the_shift_is_open(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_non_fuel_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """No Idempotency-Key on a PATCH: it is idempotent by construction, so sending the same
    amount twice leaves the row in the same state and a retry cannot duplicate money."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 11, 19), sequence=1)
    sale = make_non_fuel_sale(shift, amount="500.00", description="Engine oil")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/non-fuel-sales/{sale}",
        json={"amount": "450.00", "description": "Engine oil (1L)"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 200
    assert response.json()["amount"] == "450.00"
    assert response.json()["description"] == "Engine oil (1L)"


async def test_an_explicit_null_leaves_a_field_alone_rather_than_clearing_it(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_non_fuel_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """Matches `collections.py` and `readings.py`. `description` is the only human context
    the row carries, and a client sending null far more often means "leave it" than
    "erase it"."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 11, 20), sequence=1)
    sale = make_non_fuel_sale(shift, amount="500.00", description="Engine oil")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/non-fuel-sales/{sale}",
        json={"amount": "450.00", "description": None},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 200
    assert response.json()["description"] == "Engine oil"


async def test_an_empty_patch_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_non_fuel_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 11, 21), sequence=1)
    sale = make_non_fuel_sale(shift, amount="500.00")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/non-fuel-sales/{sale}",
        json={},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "NO_FIELDS_TO_UPDATE"


# --- locked shifts, replays, and the double-reversal race --------------------------


async def test_reversing_on_a_locked_shift_is_an_admin_action(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_non_fuel_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """§6.9 names locked shifts as needing the reversal path, and §5.2's "nothing
    referencing a locked shift may be modified" still holds -- a reversal modifies nothing,
    it appends. Locking is the point at which a day stops being anybody else's to change."""
    manager = make_user("manager")
    admin = make_user("admin")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant, business_date=date(2026, 11, 22), sequence=1, status="locked"
    )
    sale = make_non_fuel_sale(shift, amount="500.00")

    refused = await client.post(
        f"/api/v1/shifts/{shift}/non-fuel-sales/{sale}/reversals",
        json={"reason": "Recorded against the wrong day"},
        headers={**auth_headers(manager), "Idempotency-Key": "oil-locked-1"},
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "LOCKED_SHIFT_REVERSAL_REQUIRES_ADMIN"

    allowed = await client.post(
        f"/api/v1/shifts/{shift}/non-fuel-sales/{sale}/reversals",
        json={"reason": "Recorded against the wrong day"},
        headers={**auth_headers(admin), "Idempotency-Key": "oil-locked-2"},
    )
    assert allowed.status_code == 201


async def test_replaying_a_reversal_key_returns_the_first_answer(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_non_fuel_sale: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """A retried reversal must not append a second negative row -- which would take the
    shift's total below zero and look like a refund nobody gave."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 11, 23), sequence=1)
    sale = make_non_fuel_sale(shift, amount="500.00")

    first = await client.post(
        f"/api/v1/shifts/{shift}/non-fuel-sales/{sale}/reversals",
        json={"reason": "Duplicate entry"},
        headers={**auth_headers(manager), "Idempotency-Key": "oil-replay"},
    )
    second = await client.post(
        f"/api/v1/shifts/{shift}/non-fuel-sales/{sale}/reversals",
        json={"reason": "Duplicate entry"},
        headers={**auth_headers(manager), "Idempotency-Key": "oil-replay"},
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()

    with engine.connect() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM non_fuel_sales WHERE shift_id = :s"
            ).bindparams(s=shift)
        ).scalar_one()
    assert count == 2


async def test_a_second_reversal_under_a_different_key_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_non_fuel_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """Two managers reversing the same row. Idempotency only deduplicates the *same* key, so
    the service-level check is what stops the second negation -- and
    `uq_non_fuel_sales_reverses_id` is the backstop when they land in the same instant."""
    manager = make_user("manager")
    other = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 11, 24), sequence=1)
    sale = make_non_fuel_sale(shift, amount="500.00")

    await client.post(
        f"/api/v1/shifts/{shift}/non-fuel-sales/{sale}/reversals",
        json={"reason": "Duplicate entry"},
        headers={**auth_headers(manager), "Idempotency-Key": "oil-race-1"},
    )
    second = await client.post(
        f"/api/v1/shifts/{shift}/non-fuel-sales/{sale}/reversals",
        json={"reason": "Duplicate entry again"},
        headers={**auth_headers(other), "Idempotency-Key": "oil-race-2"},
    )

    assert second.status_code == 409
    assert second.json()["code"] == "NON_FUEL_SALE_ALREADY_REVERSED"


async def test_a_failure_mid_write_releases_the_key_and_leaves_no_row(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    monkeypatch,
) -> None:
    """The `except` around the write is not decoration.

    No business rule can fail *inside* it -- Pydantic has already refused a bad amount --
    so the only things that reach it are real ones: a lost connection, a serialization
    failure, a bug in a later step of this phase adding a check there. The invariant it
    guarantees is what this test pins: nothing is written, and the key is free again, so the
    attendant's retry does real work instead of meeting REQUEST_IN_PROGRESS for 24 hours.
    """
    from app.services import audit as audit_service

    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 11, 25), sequence=1)

    calls = {"n": 0}
    real_record = audit_service.record

    def _explode(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("audit backend unavailable")
        return real_record(*args, **kwargs)

    monkeypatch.setattr("app.api.v1.non_fuel_sales.audit.record", _explode)

    # The exception propagates out of ASGITransport rather than arriving as a 500 body:
    # httpx re-raises app exceptions by default, and `test_errors.py` tests the envelope
    # handler directly for that reason. What matters here is the two invariants below.
    with pytest.raises(RuntimeError, match="audit backend unavailable"):
        await _post(client, auth_headers(attendant), "oil-boom", shift_id=shift)

    with engine.connect() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM non_fuel_sales WHERE shift_id = :s"
            ).bindparams(s=shift)
        ).scalar_one()
    assert count == 0

    # The same key is free again and now does real work.
    retry = await _post(client, auth_headers(attendant), "oil-boom", shift_id=shift)
    assert retry.status_code == 201
