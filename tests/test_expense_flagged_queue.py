"""Phase 30: the flagged queue read by day.

`GET /expenses/flagged` gains a `business_date` filter, and `GET /expenses/flagged/by-date`
answers "which days need me, and how many rows each" for the date strip. Both must agree with
the lock precondition (§6.7), which already ignores a flagged row that has been reversed --
otherwise the admin is shown work the lock does not count, or the reverse.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.anyio

DAY = date(2026, 4, 24)
NEXT_DAY = date(2026, 4, 25)


async def test_the_queue_can_be_filtered_to_one_business_date(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    day_one = make_shift(manager, business_date=DAY, sequence=1)
    day_two = make_shift(manager, business_date=NEXT_DAY, sequence=1)
    wanted = make_expense(
        day_one, category="salary", amount="1500.00", requires_review=True
    )
    make_expense(day_two, category="salary", amount="1600.00", requires_review=True)

    response = await client.get(
        "/api/v1/expenses/flagged",
        params={"business_date": DAY.isoformat()},
        headers=auth_headers(manager),
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == [str(wanted)]
    assert items[0]["business_date"] == DAY.isoformat()


async def test_one_days_rows_read_oldest_first(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    first = make_expense(shift, amount="1100.00", requires_review=True)
    second = make_expense(shift, amount="1200.00", requires_review=True)

    response = await client.get(
        "/api/v1/expenses/flagged",
        params={"business_date": DAY.isoformat()},
        headers=auth_headers(manager),
    )

    assert [item["id"] for item in response.json()["items"]] == [
        str(first),
        str(second),
    ]


async def test_a_reversed_flagged_row_is_not_in_the_queue(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    """The lock ignores a flagged row that was formally cancelled (§6.7). The queue must
    too, or the admin is sent to review money the lock does not count."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    original = make_expense(shift, amount="1500.00", requires_review=True)
    make_expense(
        shift,
        amount="-1500.00",
        reverses_id=original,
        reversal_reason="entered twice",
    )

    response = await client.get(
        "/api/v1/expenses/flagged",
        params={"business_date": DAY.isoformat()},
        headers=auth_headers(manager),
    )

    assert response.json()["items"] == []


async def test_by_date_counts_and_totals_each_day(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    day_one = make_shift(manager, business_date=DAY, sequence=1)
    day_two = make_shift(manager, business_date=NEXT_DAY, sequence=1)
    make_expense(day_one, amount="1100.50", requires_review=True)
    make_expense(day_one, amount="1200.25", requires_review=True)
    make_expense(day_two, amount="2000.00", requires_review=True)
    # Reviewed rows are not work.
    make_expense(day_two, amount="999.00", requires_review=False)

    response = await client.get(
        "/api/v1/expenses/flagged/by-date",
        params={"from": DAY.isoformat(), "to": NEXT_DAY.isoformat()},
        headers=auth_headers(manager),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == [
        {"business_date": "2026-04-24", "count": 2, "total_amount": "2300.75"},
        {"business_date": "2026-04-25", "count": 1, "total_amount": "2000.00"},
    ]


async def test_by_date_excludes_a_reversed_row_from_count_and_total(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    cancelled = make_expense(shift, amount="1500.00", requires_review=True)
    make_expense(
        shift, amount="-1500.00", reverses_id=cancelled, reversal_reason="duplicate"
    )
    make_expense(shift, amount="1300.00", requires_review=True)

    response = await client.get(
        "/api/v1/expenses/flagged/by-date",
        params={"from": DAY.isoformat(), "to": DAY.isoformat()},
        headers=auth_headers(manager),
    )

    assert response.json()["items"] == [
        {"business_date": "2026-04-24", "count": 1, "total_amount": "1300.00"}
    ]


async def test_oldest_business_date_ignores_the_window(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    """The default selection is the oldest day needing review *anywhere*, so a day that
    falls outside the strip's window must still be found."""
    manager = make_user("manager")
    old = make_shift(manager, business_date=date(2026, 3, 2), sequence=1)
    recent = make_shift(manager, business_date=DAY, sequence=1)
    make_expense(old, amount="1500.00", requires_review=True)
    make_expense(recent, amount="1500.00", requires_review=True)

    response = await client.get(
        "/api/v1/expenses/flagged/by-date",
        params={"from": DAY.isoformat(), "to": NEXT_DAY.isoformat()},
        headers=auth_headers(manager),
    )

    body = response.json()
    assert [item["business_date"] for item in body["items"]] == ["2026-04-24"]
    assert body["oldest_business_date"] == "2026-03-02"


async def test_oldest_business_date_is_null_when_nothing_is_flagged(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")

    response = await client.get(
        "/api/v1/expenses/flagged/by-date",
        params={"from": DAY.isoformat(), "to": NEXT_DAY.isoformat()},
        headers=auth_headers(manager),
    )

    assert response.status_code == 200
    assert response.json() == {"items": [], "oldest_business_date": None}


async def test_an_attendant_cannot_read_the_by_date_counts(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")

    response = await client.get(
        "/api/v1/expenses/flagged/by-date",
        params={"from": DAY.isoformat(), "to": NEXT_DAY.isoformat()},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("date_from", "date_to"),
    [
        (NEXT_DAY, DAY),  # backwards
        (date(2026, 1, 1), date(2026, 4, 24)),  # wider than the cap
    ],
)
async def test_by_date_refuses_a_bad_window(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    date_from: date,
    date_to: date,
) -> None:
    manager = make_user("manager")

    response = await client.get(
        "/api/v1/expenses/flagged/by-date",
        params={"from": date_from.isoformat(), "to": date_to.isoformat()},
        headers=auth_headers(manager),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_DATE_RANGE"


@pytest.fixture
def second_outlet(engine: Engine) -> Iterator[UUID]:
    """A second outlet. Requested *first* in a test's signature so it is set up before
    `make_shift` and torn down after it -- the shift fixtures delete their own rows, and an
    outlet cannot be removed while a shift still points at it."""
    outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO outlets (id, name) VALUES (:id, 'Second Outlet')").bindparams(
                id=outlet
            )
        )
    yield outlet
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM expense_categories WHERE outlet_id = :id").bindparams(
                id=outlet
            )
        )
        connection.execute(
            text("DELETE FROM outlets WHERE id = :id").bindparams(id=outlet)
        )


async def test_another_outlets_rows_are_absent(
    second_outlet: UUID,
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    make_expense_category: Callable[..., UUID],
    auth_headers,
) -> None:
    """Asserted by inserting a foreign row, not by inferring from an empty page."""
    manager = make_user("manager")
    foreign_category = make_expense_category("TEA", outlet_id=second_outlet)
    foreign_shift = make_shift(manager, business_date=DAY, sequence=1, outlet_id=second_outlet)
    make_expense(
        foreign_shift,
        category_id=foreign_category,
        amount="1500.00",
        requires_review=True,
    )

    listing = await client.get(
        "/api/v1/expenses/flagged",
        params={"business_date": DAY.isoformat()},
        headers=auth_headers(manager),
    )
    counts = await client.get(
        "/api/v1/expenses/flagged/by-date",
        params={"from": DAY.isoformat(), "to": DAY.isoformat()},
        headers=auth_headers(manager),
    )

    assert listing.json()["items"] == []
    assert counts.json() == {"items": [], "oldest_business_date": None}
