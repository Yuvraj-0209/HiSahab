"""§8's permission matrix for expenses, plus the structural assertions.

Mirrors tests/test_collections_permissions.py. Two axes, and they are separate: role is a
floor (attendant < manager < admin), ownership applies to attendants only.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.anyio

DAY = date(2026, 4, 12)


def _post(client: AsyncClient, shift_id, headers, key: str, **body):
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


# --- ownership ---------------------------------------------------------------


async def test_an_attendant_records_an_expense_on_their_own_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(client, shift, auth_headers(attendant), "own")

    assert response.status_code == 201


async def test_an_attendant_cannot_record_an_expense_on_another_attendants_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """The case §8 names specifically."""
    mine = make_user("attendant")
    theirs = make_user("attendant")
    their_shift = make_shift(theirs, business_date=DAY, sequence=1)

    response = await _post(client, their_shift, auth_headers(mine), "not-mine")

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_YOUR_SHIFT"


async def test_an_attendant_cannot_read_another_attendants_expenses(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    mine = make_user("attendant")
    theirs = make_user("attendant")
    their_shift = make_shift(theirs, business_date=DAY, sequence=1)
    make_expense(their_shift, amount="500.00")

    response = await client.get(
        f"/api/v1/shifts/{their_shift}/expenses", headers=auth_headers(mine)
    )

    assert response.status_code == 403


async def test_a_manager_may_act_on_any_shift_at_the_outlet(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(client, shift, auth_headers(manager), "mgr")

    assert response.status_code == 201


# --- role floors: reversal -----------------------------------------------------


async def test_an_attendant_cannot_reverse_an_expense(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    expense = make_expense(shift, amount="500.00")

    response = await client.post(
        f"/api/v1/shifts/{shift}/expenses/{expense}/reversals",
        json={"reason": "typed the wrong figure"},
        headers={**auth_headers(attendant), "Idempotency-Key": "att"},
    )

    assert response.status_code == 403


async def test_a_manager_may_reverse_on_a_closed_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    expense = make_expense(shift, amount="500.00")

    response = await client.post(
        f"/api/v1/shifts/{shift}/expenses/{expense}/reversals",
        json={"reason": "duplicate entry"},
        headers={**auth_headers(manager), "Idempotency-Key": "mgr-closed"},
    )

    assert response.status_code == 201


async def test_a_manager_cannot_reverse_on_a_locked_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="locked")
    expense = make_expense(shift, amount="500.00")

    response = await client.post(
        f"/api/v1/shifts/{shift}/expenses/{expense}/reversals",
        json={"reason": "duplicate entry"},
        headers={**auth_headers(manager), "Idempotency-Key": "mgr-locked"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "LOCKED_SHIFT_REVERSAL_REQUIRES_ADMIN"


async def test_an_admin_may_reverse_on_a_locked_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    admin = make_user("admin")
    shift = make_shift(admin, business_date=DAY, sequence=1, status="locked")
    expense = make_expense(shift, amount="500.00")

    response = await client.post(
        f"/api/v1/shifts/{shift}/expenses/{expense}/reversals",
        json={"reason": "duplicate entry"},
        headers={**auth_headers(admin), "Idempotency-Key": "admin-locked"},
    )

    assert response.status_code == 201
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT amount FROM expenses WHERE id = :id").bindparams(id=expense)
        ).scalar_one() == Decimal("500.00")


# --- role floors: review --------------------------------------------------------


async def test_an_attendant_cannot_review_a_flagged_expense(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    expense = make_expense(shift, amount="1500.00", requires_review=True)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{expense}/review",
        json={"review_note": "looks fine"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 403


async def test_a_manager_may_review_a_flagged_expense(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    expense = make_expense(shift, amount="1500.00", requires_review=True)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{expense}/review",
        json={"review_note": "checked, correct"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 200


async def test_an_attendant_cannot_read_the_flagged_queue(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")

    response = await client.get(
        "/api/v1/expenses/flagged", headers=auth_headers(attendant)
    )

    assert response.status_code == 403


async def test_an_unauthenticated_request_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await client.get(f"/api/v1/shifts/{shift}/expenses")

    assert response.status_code == 401


# --- structural assertions ---------------------------------------------------


def test_no_ownership_check_is_reimplemented_here() -> None:
    """§8's ownership rule must live in exactly one place: `require_shift_access`."""
    source = Path("app/api/v1/expenses.py").read_text()
    assert "attendant_id !=" not in source
    assert "attendant_id ==" not in source
    assert "require_shift_access" in source


def test_the_outlet_is_never_hardcoded_in_this_router() -> None:
    """§8: the outlet is always resolved from the row, never assumed."""
    source = Path("app/api/v1/expenses.py").read_text()
    assert "DEFAULT_OUTLET_ID" not in source
    assert "get_default_outlet_id" not in source


def test_no_float_appears_in_the_money_path() -> None:
    """§3 rule 1, asserted structurally, mirroring test_collections_permissions.py."""
    for path in (
        "app/api/v1/expenses.py",
        "app/services/expenses.py",
        "app/models/expense.py",
        "app/core/expenses.py",
    ):
        source = Path(path).read_text()
        assert "float(" not in source, path
        assert "sa.Float" not in source, path


def test_no_hardcoded_review_threshold_appears_here() -> None:
    """§6.7: the threshold is a config value, `EXPENSE_REVIEW_THRESHOLD`, never a literal
    `1000` -- changing it must not require a deploy."""
    for path in ("app/api/v1/expenses.py", "app/services/expenses.py"):
        source = Path(path).read_text()
        assert "1000" not in source, path
        assert "EXPENSE_REVIEW_THRESHOLD" in Path("app/api/v1/expenses.py").read_text()


def test_the_lock_precondition_reads_only_this_shifts_own_rows() -> None:
    """§6.7's lock check must be shift-scoped, not business-date-scoped, even though the
    flagging rule that sets a flag can look across shifts. Parsed rather than grepped so a
    comment explaining the distinction cannot itself fail this test."""
    tree = ast.parse(Path("app/services/expenses.py").read_text())
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "unreviewed_flagged_expenses" in functions
    source = ast.get_source_segment(
        Path("app/services/expenses.py").read_text(), functions["unreviewed_flagged_expenses"]
    )
    assert "business_date" not in source
