"""§6.7's two review-flagging rules and the Phase 7 amendments to them.

Single-row threshold, category aggregate, which rows the aggregate marks, re-evaluation on
PATCH and reversal, and the never-auto-clear rule. §6.9's reversal mechanics themselves are
tests/test_expense_reversals.py; this file only cares what a reversal does to flags.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.anyio

DAY = date(2026, 5, 11)


async def _category_id(client: AsyncClient, headers, code: str) -> str:
    """Resolve a category code to its id through the API. See the twin in
    tests/test_expenses_api.py for why this reads back over HTTP and never caches."""
    response = await client.get("/api/v1/expense-categories", headers=headers)
    return next(row["id"] for row in response.json() if row["code"] == code.upper())


async def _post(client: AsyncClient, shift_id, headers, key: str, **body):
    payload = {
        "mode": "cash",
        "amount": "500.00",
        "description": "routine upkeep",
        **body,
    }
    code = payload.pop("category", "maintenance")
    if "category_id" not in payload:
        payload["category_id"] = await _category_id(client, headers, code)

    return await client.post(
        f"/api/v1/shifts/{shift_id}/expenses",
        json=payload,
        headers={**headers, "Idempotency-Key": key},
    )


# --- §10's named boundary cases: strictly `>` ---------------------------------


@pytest.mark.parametrize(
    ("amount", "flagged"),
    [("999.99", False), ("1000.00", False), ("1000.01", True)],
)
async def test_the_single_row_threshold_is_strictly_greater_than(
    amount: str,
    flagged: bool,
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(
        client, shift, auth_headers(attendant), f"boundary-{amount}",
        category="salary", amount=amount,
    )

    assert response.status_code == 201
    assert response.json()["requires_review"] is flagged


# --- the aggregate rule --------------------------------------------------------


async def test_two_same_category_entries_trip_the_aggregate(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    """A single ₹1,000 rule is trivially defeated by two ₹600 entries; this is the
    aggregate rule that closes it."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)

    first = await _post(client, shift, headers, "agg1", amount="600.00")
    assert first.json()["requires_review"] is False

    second = await _post(client, shift, headers, "agg2", amount="600.00")
    assert second.status_code == 201

    listing = await client.get(f"/api/v1/shifts/{shift}/expenses", headers=headers)
    flags = {item["amount"]: item["requires_review"] for item in listing.json()["items"]}
    # Both rows flagged, not only the one that crossed the line -- CLAUDE.md §6.7's
    # Phase 7 amendment. Flagging only the second ₹600 row would show a manager a
    # trivial-looking figure and hide the ₹1,200 pattern behind it.
    assert flags == {"600.00": True}
    assert all(item["requires_review"] for item in listing.json()["items"])


async def test_a_different_category_does_not_join_the_aggregate(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)

    await _post(client, shift, headers, "m1", category="maintenance", amount="600.00")
    electricity = await _post(
        client, shift, headers, "e1", category="electricity", amount="600.00"
    )

    assert electricity.json()["requires_review"] is False


async def test_a_patch_that_raises_the_group_total_trips_the_aggregate(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    """Two ₹300 entries (₹600 total) sit under the ₹1,000 line; an edit of one to ₹800
    (₹1,100 total) must be re-evaluated, not only the amount at insert."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)

    a = await _post(client, shift, headers, "p1", amount="300.00")
    b = await _post(client, shift, headers, "p2", amount="300.00")
    assert not any(r.json()["requires_review"] for r in (a, b))

    patched = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{b.json()['id']}",
        json={"amount": "800.00"},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["requires_review"] is True

    listing = await client.get(f"/api/v1/shifts/{shift}/expenses", headers=headers)
    assert all(item["requires_review"] for item in listing.json()["items"])


async def test_a_patch_that_does_not_touch_amount_does_not_reevaluate(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)

    a = await _post(client, shift, headers, "np1", amount="600.00")

    patched = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{a.json()['id']}",
        json={"description": "just a label change"},
        headers=headers,
    )

    assert patched.json()["requires_review"] is False


# --- flags never auto-clear ----------------------------------------------------


async def test_a_reversal_that_drops_the_total_back_down_does_not_clear_flags(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    """§6.7's Phase 7 amendment: flags are never auto-cleared, including when a reversal
    drops a group back under the threshold. A human clears a flag through the review
    route, the same shape as §13.10's downstream reading flag."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    headers = auth_headers(manager)

    first = await _post(client, shift, headers, "r1", amount="600.00")
    second = await _post(client, shift, headers, "r2", amount="600.00")
    # `first`'s response body is stale -- it was serialised when the group was only
    # ₹600, before the second POST existed to push it over. Re-read via GET, which
    # reflects both rows' current state.
    assert first.json()["requires_review"] is False
    assert second.json()["requires_review"] is True
    before = await client.get(f"/api/v1/shifts/{shift}/expenses", headers=headers)
    assert all(item["requires_review"] for item in before.json()["items"])

    reversed_id = second.json()["id"]
    reversal = await client.post(
        f"/api/v1/shifts/{shift}/expenses/{reversed_id}/reversals",
        json={"reason": "duplicate entry, cancel it"},
        headers={**headers, "Idempotency-Key": "rev-flag"},
    )
    assert reversal.status_code == 201

    # The group total is back to ₹600 -- under the threshold -- but the FIRST row's flag,
    # raised while the total was ₹1,200, is untouched.
    listing = await client.get(f"/api/v1/shifts/{shift}/expenses", headers=headers)
    first_row = next(
        item for item in listing.json()["items"] if item["id"] == first.json()["id"]
    )
    assert first_row["requires_review"] is True


async def test_a_reversal_can_never_itself_newly_cross_the_threshold(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    """A bare reversal only ever subtracts a positive amount from a group's total, so it
    can never trip the aggregate rule on its own."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    headers = auth_headers(manager)

    original = await _post(client, shift, headers, "solo", amount="300.00")
    assert original.json()["requires_review"] is False

    reversal = await client.post(
        f"/api/v1/shifts/{shift}/expenses/{original.json()['id']}/reversals",
        json={"reason": "wrong shift"},
        headers={**headers, "Idempotency-Key": "solo-rev"},
    )
    assert reversal.status_code == 201
    assert reversal.json()["reversal"]["requires_review"] is False


async def test_a_reversal_replacement_is_reevaluated_like_a_fresh_expense(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    headers = auth_headers(manager)

    original = await _post(client, shift, headers, "corr1", amount="500.00")
    assert original.json()["requires_review"] is False

    reversal = await client.post(
        f"/api/v1/shifts/{shift}/expenses/{original.json()['id']}/reversals",
        json={"reason": "amount was wrong", "replacement_amount": "1500.00"},
        headers={**headers, "Idempotency-Key": "corr-rev"},
    )

    assert reversal.status_code == 201
    assert reversal.json()["replacement"]["requires_review"] is True


# --- the aggregate spans shifts, on one business_date --------------------------


async def test_the_aggregate_spans_shifts_on_the_same_business_date(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    """CLAUDE.md §6.7 says "the sum of a single category for one business_date", not "for
    one shift". Two different shifts on the same date, same outlet, must share one group.

    The first shift is created already `locked`, directly via the fixture (bypassing the
    one-open-shift-per-outlet rule, which only the API enforces). Flagging a row on an
    already-locked shift mirrors an existing precedent, `readings.flag_downstream_reading`,
    which has no locked-shift guard because a flag is a signal, not a change to financial
    substance.
    """
    manager = make_user("manager")
    locked_shift = make_shift(
        manager, business_date=DAY, sequence=1, status="locked"
    )
    with engine.begin() as connection:
        first_id = connection.execute(
            text(
                "INSERT INTO expenses (shift_id, category_id, mode, amount, description) "
                "SELECT :s, ec.id, CAST('cash' AS expense_mode), 600.00, "
                "'first shift maintenance' FROM expense_categories ec "
                "JOIN shifts sh ON sh.outlet_id = ec.outlet_id "
                "WHERE sh.id = :s AND ec.code = 'MAINTENANCE' "
                "RETURNING id"
            ).bindparams(s=locked_shift)
        ).scalar_one()

    open_shift = make_shift(manager, business_date=DAY, sequence=2, status="open")
    second = await _post(
        client, open_shift, auth_headers(manager), "cross-shift", amount="600.00"
    )
    assert second.status_code == 201
    # The SECOND shift's own row crossed the aggregate -- expected regardless of scope.
    assert second.json()["requires_review"] is True

    # The FIRST shift's row, sitting in an already-locked shift, is flagged too.
    with engine.connect() as connection:
        first_flag = connection.execute(
            text("SELECT requires_review FROM expenses WHERE id = :id").bindparams(
                id=first_id
            )
        ).scalar_one()
    assert first_flag is True

    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM expenses WHERE id = :id").bindparams(id=first_id)
        )
