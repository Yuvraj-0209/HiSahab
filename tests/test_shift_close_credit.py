"""§6.8's third close precondition: CREDIT_SALE_MISSING_RECEIPT (CLAUDE.md §6.6, §6.8).

The unusual thing about this check is that **no HTTP request can make it fail**.
`credit_sales.attachment_id` is `NOT NULL` and every write path calls
`attachment_service.link()`, which stamps `linked_at` -- so any row this application creates
satisfies it by construction.

It is kept because a row written outside the API can still reach it: a fixture, a data
migration, or the bulk import of the paper register this outlet will eventually want. These
tests therefore provoke it the only way it can be provoked, by writing the row directly, and
also pin the ordinary case so the check cannot start refusing legitimate closes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.anyio

DAY = date(2026, 10, 16)


async def _close(
    client: AsyncClient,
    headers: dict[str, str],
    shift_id: UUID,
    *,
    on: date = DAY,
):
    """`make_shift` leaves `ended_at` NULL and these tests do not go through the template
    path, so the close has to supply one or it is refused with SHIFT_END_TIME_REQUIRED
    before any precondition runs. 22:00 IST is this outlet's actual closing time (§4.7)."""
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    ended_at = datetime.combine(on, time(22, 0), tzinfo=ZoneInfo("Asia/Kolkata"))
    return await client.patch(
        f"/api/v1/shifts/{shift_id}/close",
        json={"ended_at": ended_at.isoformat()},
        headers=headers,
    )


async def test_a_shift_with_a_properly_linked_credit_sale_closes(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
) -> None:
    """The ordinary case, and the one that matters most: this precondition must never make a
    legitimate day unclosable."""
    from datetime import datetime, timezone

    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_credit_sale(
        shift,
        make_credit_customer(),
        make_attachment(manager, linked_at=datetime.now(tz=timezone.utc)),
        amount="1500.00",
    )
    # §6.8's other precondition -- no fuel moved, but an explicit cash declaration keeps this
    # test about the credit check rather than about MISSING_COLLECTIONS.
    make_collection(shift, mode="cash", amount="0.00")

    response = await _close(client, auth_headers(manager), shift)

    assert response.status_code == 200
    assert response.json()["status"] == "closed"


async def test_a_credit_sale_whose_receipt_was_never_linked_blocks_the_close(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
) -> None:
    """The provocation. `make_attachment` leaves `linked_at` NULL by default, matching a
    fresh upload nobody ever claimed -- which is exactly the state §7.4's sweep treats as
    abandoned. A credit sale pointing at one is a debt with no evidence behind it.
    """
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_credit_sale(
        shift,
        make_credit_customer(),
        make_attachment(manager, linked_at=None),
        amount="1500.00",
    )
    make_collection(shift, mode="cash", amount="0.00")

    response = await _close(client, auth_headers(manager), shift)

    assert response.status_code == 409
    assert response.json()["code"] == "CREDIT_SALE_MISSING_RECEIPT"


async def test_the_shift_is_left_open_when_the_check_refuses(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """`close_shift` mutates `ended_at` before the preconditions run, on purpose (§6.2's
    ceiling needs the duration). Nothing is committed until the end, so a refusal must leave
    the shift exactly as it was -- open, and with no closed_by stamp."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_credit_sale(
        shift,
        make_credit_customer(),
        make_attachment(manager, linked_at=None),
        amount="900.00",
    )
    make_collection(shift, mode="cash", amount="0.00")

    await _close(client, auth_headers(manager), shift)

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, closed_by, closed_at FROM shifts WHERE id = :id"
            ).bindparams(id=shift)
        ).one()
    assert row.status == "open"
    assert row.closed_by is None
    assert row.closed_at is None


async def test_a_reversed_credit_sale_still_counts_towards_the_check(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
) -> None:
    """Deliberately not filtered to live rows, unlike §6.7's lock precondition which
    excludes a reversed expense.

    The difference is what each check is *for*. §6.7 asks "does a human still need to look
    at this?", and a cancelled expense needs nobody. This asks "is there evidence for every
    udhaar line on this day?", and a reversal row is still a line in the ledger pointing at
    a receipt -- §6.9 keeps both rows precisely so the record is complete, so an unlinked
    receipt is just as much a gap on the cancelled row as on the live one.
    """
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    customer = make_credit_customer()
    unlinked = make_attachment(manager, linked_at=None)
    original = make_credit_sale(shift, customer, unlinked, amount="900.00")
    make_credit_sale(
        shift,
        customer,
        unlinked,
        amount="-900.00",
        reverses_id=original,
        reversal_reason="cancelled",
    )
    make_collection(shift, mode="cash", amount="0.00")

    response = await _close(client, auth_headers(manager), shift)

    assert response.status_code == 409
    assert response.json()["code"] == "CREDIT_SALE_MISSING_RECEIPT"


async def test_a_shift_with_no_credit_sales_at_all_closes(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
) -> None:
    """The empty case: most days at most outlets issue no udhaar, and the check must be
    silent rather than merely fast."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_collection(shift, mode="cash", amount="0.00")

    response = await _close(client, auth_headers(manager), shift)

    assert response.status_code == 200


async def test_a_sale_on_another_shift_does_not_block_this_close(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
) -> None:
    """The check is shift-scoped. A gap on yesterday's day is yesterday's problem, and
    letting it block every future close would make one bad row freeze the outlet."""
    manager = make_user("manager")
    yesterday = make_shift(
        manager, business_date=date(2026, 10, 15), sequence=1, status="closed"
    )
    today = make_shift(manager, business_date=DAY, sequence=1)
    make_credit_sale(
        yesterday,
        make_credit_customer(),
        make_attachment(manager, linked_at=None),
        amount="500.00",
    )
    make_collection(today, mode="cash", amount="0.00")

    response = await _close(client, auth_headers(manager), today)

    assert response.status_code == 200
