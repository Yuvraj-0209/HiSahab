"""§6.7's `UNREVIEWED_EXPENSES_EXIST` lock precondition.

Mirrors tests/test_shift_close_collections.py's shape for the analogous check on
`lock_shift`. Unlike that file's absence-vs-mismatch distinction, this one is about
review state: a flag is either cleared or it is not, and locking is refused only for the
second.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from uuid import UUID

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.anyio

DAY = date(2026, 4, 24)


async def test_a_shift_with_an_unreviewed_flagged_expense_cannot_lock(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    admin = make_user("admin")
    shift = make_shift(admin, business_date=DAY, sequence=1, status="closed")
    make_expense(shift, category="maintenance", amount="1500.00", requires_review=True)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/lock", headers=auth_headers(admin)
    )

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "UNREVIEWED_EXPENSES_EXIST"
    # The message names the flagged row -- category and amount -- so an admin blocked
    # from locking knows what to review.
    assert "maintenance" in body["detail"]
    assert "1500.00" in body["detail"]


async def test_a_shift_locks_once_the_flag_is_reviewed(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    admin = make_user("admin")
    shift = make_shift(admin, business_date=DAY, sequence=1, status="closed")
    expense = make_expense(
        shift, category="maintenance", amount="1500.00", requires_review=True
    )

    reviewed = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{expense}/review",
        json={"review_note": "checked the receipt, correct"},
        headers=auth_headers(admin),
    )
    assert reviewed.status_code == 200

    response = await client.patch(
        f"/api/v1/shifts/{shift}/lock", headers=auth_headers(admin)
    )

    assert response.status_code == 200
    assert response.json()["status"] == "locked"


async def test_a_shift_with_no_expenses_locks_normally(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    admin = make_user("admin")
    shift = make_shift(admin, business_date=DAY, sequence=1, status="closed")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/lock", headers=auth_headers(admin)
    )

    assert response.status_code == 200


async def test_an_unflagged_expense_does_not_block_a_lock(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    admin = make_user("admin")
    shift = make_shift(admin, business_date=DAY, sequence=1, status="closed")
    make_expense(shift, amount="200.00", requires_review=False)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/lock", headers=auth_headers(admin)
    )

    assert response.status_code == 200


async def test_a_reversed_flagged_expense_does_not_block_a_lock(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    """§6.7's Phase 7 amendment: it is money a manager formally cancelled. Both rows stay
    in the audit trail, but blocking a lock on cancelled money is friction with no
    control value.

    The original still carries `requires_review = true` -- a bare reversal never clears
    it, since flags are never auto-cleared -- so this only passes because
    `unreviewed_flagged_expenses` excludes rows a reversal already points at, not because
    anything set the flag back to false.
    """
    admin = make_user("admin")
    shift = make_shift(admin, business_date=DAY, sequence=1, status="closed")
    original = make_expense(
        shift, category="maintenance", amount="1500.00", requires_review=True,
    )
    make_expense(
        shift, category="maintenance", amount="-1500.00", requires_review=False,
        reverses_id=original, reversal_reason="duplicate entry",
    )

    response = await client.patch(
        f"/api/v1/shifts/{shift}/lock", headers=auth_headers(admin)
    )

    assert response.status_code == 200


async def test_the_lock_check_is_scoped_to_this_shifts_own_rows(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    """A flagged expense belonging to a *different* shift -- even one the §6.7 aggregate
    rule flagged as part of the same business-date group -- blocks that other shift's
    lock, not this one's. Locking is a per-shift action."""
    admin = make_user("admin")
    other_shift = make_shift(admin, business_date=DAY, sequence=1, status="closed")
    make_expense(
        other_shift, category="maintenance", amount="1500.00", requires_review=True
    )
    this_shift = make_shift(admin, business_date=DAY, sequence=2, status="closed")

    response = await client.patch(
        f"/api/v1/shifts/{this_shift}/lock", headers=auth_headers(admin)
    )

    assert response.status_code == 200


async def test_reviewing_a_flag_on_a_locked_shift_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    """§5.2 is absolute that nothing referencing a locked shift may be modified --
    including the review flag pointing at it. Mirrors readings.py::review_reading's
    locked-shift guard exactly."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="locked")
    expense = make_expense(shift, amount="1500.00", requires_review=True)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{expense}/review",
        json={"review_note": "checked"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_LOCKED"


async def test_reviewing_an_unflagged_expense_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    expense = make_expense(shift, amount="200.00", requires_review=False)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{expense}/review",
        json={"review_note": "nothing to see"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "EXPENSE_NOT_FLAGGED"


async def test_the_flagged_queue_pages_with_a_cursor(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    shift1 = make_shift(manager, business_date=DAY, sequence=1)
    shift2 = make_shift(manager, business_date=date(2026, 4, 25), sequence=1)
    make_expense(shift1, category="salary", amount="1500.00", requires_review=True)
    make_expense(shift2, category="electricity", amount="1500.00", requires_review=True)

    first_page = await client.get(
        "/api/v1/expenses/flagged", params={"limit": 1}, headers=auth_headers(manager)
    )
    assert first_page.status_code == 200
    body = first_page.json()
    assert len(body["items"]) == 1
    assert body["next_cursor"] is not None

    second_page = await client.get(
        "/api/v1/expenses/flagged",
        params={"limit": 1, "cursor": body["next_cursor"]},
        headers=auth_headers(manager),
    )
    assert len(second_page.json()["items"]) == 1
    assert second_page.json()["next_cursor"] is None
