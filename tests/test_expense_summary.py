"""GET /api/v1/expenses/summary (CLAUDE.md §11): month-end category totals, manager floor."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from uuid import UUID

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.anyio


async def test_totals_sum_across_shifts_and_reversals_net_in(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    shift1 = make_shift(manager, business_date=date(2026, 8, 1), sequence=1)
    shift2 = make_shift(manager, business_date=date(2026, 8, 15), sequence=1)
    make_expense(shift1, category="maintenance", amount="400.00")
    make_expense(shift2, category="maintenance", amount="200.00")
    make_expense(shift2, category="electricity", amount="1000.00")
    reversed_row = make_expense(shift2, category="electricity", amount="500.00")
    make_expense(
        shift2, category="electricity", amount="-500.00", reverses_id=reversed_row,
        reversal_reason="wrong bill",
    )

    response = await client.get(
        "/api/v1/expenses/summary",
        params={"from": "2026-08-01", "to": "2026-08-31"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["totals_by_category"]["MAINTENANCE"] == "600.00"
    # 1000 + 500 - 500 (the reversal nets the cancelled 500 back out, not to zero rows)
    assert body["totals_by_category"]["ELECTRICITY"] == "1000.00"
    assert body["total"] == "1600.00"
    assert body["from"] == "2026-08-01"
    assert body["to"] == "2026-08-31"


async def test_shifts_outside_the_range_are_excluded(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    inside = make_shift(manager, business_date=date(2026, 9, 10), sequence=1)
    before = make_shift(manager, business_date=date(2026, 8, 31), sequence=1)
    after = make_shift(manager, business_date=date(2026, 10, 1), sequence=1)
    make_expense(inside, category="salary", amount="300.00")
    make_expense(before, category="salary", amount="9999.00")
    make_expense(after, category="salary", amount="8888.00")

    response = await client.get(
        "/api/v1/expenses/summary",
        params={"from": "2026-09-01", "to": "2026-09-30"},
        headers=auth_headers(manager),
    )

    assert response.json()["totals_by_category"] == {"SALARY": "300.00"}


async def test_amounts_are_json_strings_not_numbers(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    """§3 rule 1 end to end -- a float in a money response is exactly the failure mode
    this whole document exists to prevent."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=date(2026, 8, 5), sequence=1)
    make_expense(shift, category="salary", amount="123.45")

    response = await client.get(
        "/api/v1/expenses/summary",
        params={"from": "2026-08-01", "to": "2026-08-31"},
        headers=auth_headers(manager),
    )

    raw = response.text
    assert '"123.45"' in raw
    assert isinstance(response.json()["totals_by_category"]["SALARY"], str)
    assert isinstance(response.json()["total"], str)


async def test_an_empty_range_returns_zero_totals(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    manager = make_user("manager")

    response = await client.get(
        "/api/v1/expenses/summary",
        params={"from": "2020-01-01", "to": "2020-01-31"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["totals_by_category"] == {}
    assert body["total"] == "0.00"


async def test_a_retired_category_still_appears_in_a_historical_range(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense_category: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    admin = make_user("admin")
    shift = make_shift(admin, business_date=date(2026, 8, 12), sequence=1)
    category = make_expense_category("BOREWELL", display_name="Borewell")
    make_expense(shift, category_id=category, amount="750.00")

    retire = await client.patch(
        f"/api/v1/expense-categories/{category}",
        json={"is_active": False},
        headers=auth_headers(admin),
    )
    assert retire.status_code == 200

    response = await client.get(
        "/api/v1/expenses/summary",
        params={"from": "2026-08-01", "to": "2026-08-31"},
        headers=auth_headers(admin),
    )

    assert response.json()["totals_by_category"]["BOREWELL"] == "750.00"


# --- validation (M16) -------------------------------------------------------------------


async def test_from_after_to_is_refused(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    manager = make_user("manager")

    response = await client.get(
        "/api/v1/expenses/summary",
        params={"from": "2026-08-31", "to": "2026-08-01"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_DATE_RANGE"


async def test_a_range_over_366_days_is_refused(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    manager = make_user("manager")

    response = await client.get(
        "/api/v1/expenses/summary",
        params={"from": "2025-01-01", "to": "2026-12-31"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_DATE_RANGE"


async def test_a_range_of_exactly_366_days_is_accepted(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    manager = make_user("manager")

    response = await client.get(
        "/api/v1/expenses/summary",
        params={"from": "2024-01-01", "to": "2025-01-01"},  # 366 days, leap year
        headers=auth_headers(manager),
    )

    assert response.status_code == 200


async def test_missing_query_params_is_a_422(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    manager = make_user("manager")

    response = await client.get(
        "/api/v1/expenses/summary", headers=auth_headers(manager)
    )

    assert response.status_code == 422


# --- permissions (§8) --------------------------------------------------------------------


async def test_an_attendant_cannot_read_the_summary(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    attendant = make_user("attendant")

    response = await client.get(
        "/api/v1/expenses/summary",
        params={"from": "2026-08-01", "to": "2026-08-31"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


async def test_a_manager_can_read_the_summary(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    manager = make_user("manager")

    response = await client.get(
        "/api/v1/expenses/summary",
        params={"from": "2026-08-01", "to": "2026-08-31"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 200


async def test_an_admin_can_read_the_summary(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    admin = make_user("admin")

    response = await client.get(
        "/api/v1/expenses/summary",
        params={"from": "2026-08-01", "to": "2026-08-31"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200


async def test_an_unauthenticated_request_is_refused(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/expenses/summary", params={"from": "2026-08-01", "to": "2026-08-31"}
    )

    assert response.status_code == 401


async def test_the_summary_query_is_scoped_to_one_outlet(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    engine,
) -> None:
    """§5.0: the query is written outlet-scoped from the start, even though V1 seeds one
    outlet and `require_role(Role.manager)`'s default-outlet resolver means a second
    outlet's manager can never even reach this endpoint through HTTP to prove it that way.
    Exercised directly against `totals_by_category_range` instead, against a second, real
    outlet, so the WHERE clause itself is what gets tested.
    """
    from uuid import uuid4

    from sqlalchemy import text

    from app.services.expenses import totals_by_category_range

    other_outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO outlets (id, name) VALUES (:id, 'Second Outlet')").bindparams(
                id=other_outlet
            )
        )

    try:
        manager = make_user("manager")
        shift = make_shift(manager, business_date=date(2026, 8, 5), sequence=1)
        make_expense(shift, category="salary", amount="1234.00")

        with engine.connect() as connection:
            from sqlalchemy.orm import Session

            with Session(bind=connection) as session:
                totals = totals_by_category_range(
                    session,
                    outlet_id=other_outlet,
                    date_from=date(2026, 8, 1),
                    date_to=date(2026, 8, 31),
                )

        assert totals == {}
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM outlets WHERE id = :id").bindparams(id=other_outlet)
            )
