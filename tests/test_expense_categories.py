"""Expense categories: admin-managed reference data (CLAUDE.md §5.1, §6.11, §8).

Phase 8 replaced the `expense_category` enum with a table, for the reason §5.1 already gave
about `fuel_types`: adding a category you actually spend on is data entry, not a migration.
These tests cover the half of that change the migration cannot -- who may write, what stays
immutable, and the two ways a category can be wrong at write time.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.anyio

DAY = date(2026, 6, 15)


async def _post_expense(
    client: AsyncClient, shift_id, headers, key: str, category_id: str, **body
):
    return await client.post(
        f"/api/v1/shifts/{shift_id}/expenses",
        json={
            "category_id": category_id,
            "mode": "cash",
            "amount": "500.00",
            "description": "routine upkeep",
            **body,
        },
        headers={**headers, "Idempotency-Key": key},
    )


# --- the point of the whole table --------------------------------------------


async def test_an_admin_can_add_a_category_without_a_migration(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_expense_categories,
) -> None:
    """§5.1's entire justification. An outlet buys tea; nobody should need a developer."""
    admin = make_user("admin")

    response = await client.post(
        "/api/v1/expense-categories",
        json={"code": "TEA", "display_name": "Tea and refreshments"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["code"] == "TEA"
    assert body["requires_receipt"] is False
    assert body["is_active"] is True


async def test_a_code_is_normalised_so_one_category_cannot_exist_twice(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_expense_categories,
) -> None:
    """The failure §5.2's "not free text" warning is actually about.

    If `Tea`, `tea` and `TEA` can all be stored, §6.7's aggregate rule never fires: ₹600
    under one spelling plus ₹600 under another never sums to the ₹1,200 that should have
    tripped it, and the control silently becomes theatre.
    """
    admin = make_user("admin")

    first = await client.post(
        "/api/v1/expense-categories",
        json={"code": "  tea  ", "display_name": "Tea"},
        headers=auth_headers(admin),
    )
    assert first.status_code == 201
    assert first.json()["code"] == "TEA"

    second = await client.post(
        "/api/v1/expense-categories",
        json={"code": "Tea", "display_name": "Tea again"},
        headers=auth_headers(admin),
    )
    assert second.status_code == 409
    assert second.json()["code"] == "CATEGORY_CODE_EXISTS"


@pytest.mark.parametrize("code", ["TEA CHAI", "1TEA", "", "TEA-CHAI", "café"])
async def test_a_code_that_is_not_a_safe_identifier_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    code: str,
) -> None:
    admin = make_user("admin")

    response = await client.post(
        "/api/v1/expense-categories",
        json={"code": code, "display_name": "Whatever"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 422


# --- immutability (§5.1) ------------------------------------------------------


async def test_a_code_cannot_be_changed_after_creation(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_expense_category: Callable[..., UUID],
    auth_headers,
) -> None:
    """Changing a code retroactively relabels every expense ever filed under it, and
    history stops meaning what it said -- the same rule and reason as `fuel_types.code`.

    `extra="forbid"` makes this a 422 rather than a silent no-op, which is the dangerous
    version: an admin who "renamed" a category and got a 200 back would believe it worked.
    """
    admin = make_user("admin")
    category = make_expense_category("BOREWELL")

    response = await client.patch(
        f"/api/v1/expense-categories/{category}",
        json={"code": "WATER"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 422


async def test_display_name_requires_receipt_and_is_active_can_all_change(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_expense_category: Callable[..., UUID],
    auth_headers,
) -> None:
    """`requires_receipt` in particular *must* stay editable -- it is the knob §6.11 exists
    to give the admin. Safe only because the answer is snapshotted onto each expense at
    insert, so flipping it never rewrites whether history complied."""
    admin = make_user("admin")
    category = make_expense_category("BOREWELL", display_name="Borewell")

    response = await client.patch(
        f"/api/v1/expense-categories/{category}",
        json={
            "display_name": "Borewell and water",
            "requires_receipt": True,
            "is_active": False,
        },
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "Borewell and water"
    assert body["requires_receipt"] is True
    assert body["is_active"] is False
    assert body["code"] == "BOREWELL"


async def test_a_patch_with_no_fields_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_expense_category: Callable[..., UUID],
    auth_headers,
) -> None:
    admin = make_user("admin")
    category = make_expense_category("BOREWELL")

    response = await client.patch(
        f"/api/v1/expense-categories/{category}",
        json={},
        headers=auth_headers(admin),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "NO_FIELDS_TO_UPDATE"


async def test_an_unknown_category_is_a_404_not_a_permission_error(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
) -> None:
    """The 404 lives in `resolve_outlet_from_expense_category`, which runs *before* the
    role check -- otherwise a request against a nonexistent category would report a
    permission failure instead of a missing row."""
    admin = make_user("admin")

    response = await client.patch(
        f"/api/v1/expense-categories/{uuid4()}",
        json={"display_name": "Nope"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "CATEGORY_NOT_FOUND"


# --- permissions (§8) ---------------------------------------------------------


@pytest.mark.parametrize("role", ["attendant", "manager"])
async def test_only_an_admin_may_create_a_category(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    role: str,
) -> None:
    user = make_user(role)

    response = await client.post(
        "/api/v1/expense-categories",
        json={"code": "TEA", "display_name": "Tea"},
        headers=auth_headers(user),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


@pytest.mark.parametrize("role", ["attendant", "manager"])
async def test_only_an_admin_may_edit_a_category(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_expense_category: Callable[..., UUID],
    auth_headers,
    role: str,
) -> None:
    user = make_user(role)
    category = make_expense_category("BOREWELL")

    response = await client.patch(
        f"/api/v1/expense-categories/{category}",
        json={"display_name": "Nope"},
        headers=auth_headers(user),
    )

    assert response.status_code == 403


@pytest.mark.parametrize("role", ["attendant", "manager", "admin"])
async def test_every_role_can_list_categories(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    role: str,
) -> None:
    """Attendant floor: this list fills the dropdown on the expense form (§8)."""
    user = make_user(role)

    response = await client.get(
        "/api/v1/expense-categories", headers=auth_headers(user)
    )

    assert response.status_code == 200
    assert {"SALARY", "MAINTENANCE", "ELECTRICITY", "OTHER"} <= {
        row["code"] for row in response.json()
    }


# --- retirement, not deletion (§3 rule 6, §5.1) -------------------------------


async def test_a_retired_category_is_hidden_from_the_default_list(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_expense_category: Callable[..., UUID],
    auth_headers,
) -> None:
    admin = make_user("admin")
    make_expense_category("BOREWELL", is_active=False)

    default = await client.get(
        "/api/v1/expense-categories", headers=auth_headers(admin)
    )
    included = await client.get(
        "/api/v1/expense-categories?include_inactive=true",
        headers=auth_headers(admin),
    )

    assert "BOREWELL" not in {row["code"] for row in default.json()}
    assert "BOREWELL" in {row["code"] for row in included.json()}


async def test_a_retired_category_refuses_a_new_expense(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense_category: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    admin = make_user("admin")
    shift = make_shift(admin, business_date=DAY, sequence=1)
    category = make_expense_category("BOREWELL", is_active=False)

    response = await _post_expense(
        client, shift, auth_headers(admin), "retired", str(category)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CATEGORY_INACTIVE"


async def test_retiring_a_category_leaves_historical_expenses_readable(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense_category: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    """§5.1: deactivate rather than delete. The whole point of `is_active` over a DELETE is
    that money already spent under a category keeps reading and keeps reporting -- a
    retired category must block the *future*, never rewrite the past."""
    admin = make_user("admin")
    shift = make_shift(admin, business_date=DAY, sequence=1)
    category = make_expense_category("BOREWELL")
    make_expense(shift, category_id=category, amount="750.00")

    retire = await client.patch(
        f"/api/v1/expense-categories/{category}",
        json={"is_active": False},
        headers=auth_headers(admin),
    )
    assert retire.status_code == 200

    listing = await client.get(
        f"/api/v1/shifts/{shift}/expenses", headers=auth_headers(admin)
    )

    assert listing.status_code == 200
    body = listing.json()
    assert body["items"][0]["category_code"] == "BOREWELL"
    assert body["totals_by_category"] == {"BOREWELL": "750.00"}


# --- outlet scoping (§5.0, §5.1) ---------------------------------------------


async def test_a_category_from_another_outlet_is_a_404_not_a_403(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense_category: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    """Existence is not leaked across tenants -- the posture §7.3 takes for attachments.

    Unlike `fuel_types`, a category belongs to one outlet: PETROL means the same thing
    everywhere, "tea" does not (§5.1).
    """
    other_outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outlets (id, name) VALUES (:id, 'Second Outlet')"
            ).bindparams(id=other_outlet)
        )

    try:
        admin = make_user("admin")
        shift = make_shift(admin, business_date=DAY, sequence=1)
        foreign = make_expense_category("TEA", outlet_id=other_outlet)

        response = await _post_expense(
            client, shift, auth_headers(admin), "foreign-cat", str(foreign)
        )

        assert response.status_code == 404
        assert response.json()["code"] == "CATEGORY_NOT_FOUND"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM expense_categories WHERE outlet_id = :id"
                ).bindparams(id=other_outlet)
            )
            connection.execute(
                text("DELETE FROM outlets WHERE id = :id").bindparams(id=other_outlet)
            )


async def test_the_same_code_may_exist_at_two_outlets(
    make_expense_category: Callable[..., UUID],
    engine: Engine,
) -> None:
    """`UNIQUE (outlet_id, code)`, not `UNIQUE (code)`. One pump's category list is not
    another's, and two outlets both buying tea is not a conflict."""
    other_outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outlets (id, name) VALUES (:id, 'Second Outlet')"
            ).bindparams(id=other_outlet)
        )

    try:
        make_expense_category("TEA")
        make_expense_category("TEA", outlet_id=other_outlet)
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM expense_categories WHERE outlet_id = :id"
                ).bindparams(id=other_outlet)
            )
            connection.execute(
                text("DELETE FROM outlets WHERE id = :id").bindparams(id=other_outlet)
            )


async def test_a_duplicate_code_is_refused_at_the_database_level(
    make_expense_category: Callable[..., UUID],
    engine: Engine,
) -> None:
    """§6.6's belt and braces: the API checks first, but the constraint is what actually
    guarantees one category cannot exist twice at one outlet."""
    from app.core.config import get_settings

    make_expense_category("BOREWELL")

    with pytest.raises(IntegrityError, match="uq_expense_categories_outlet_code"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO expense_categories (outlet_id, code, display_name) "
                    "VALUES (:outlet, 'BOREWELL', 'Duplicate')"
                ).bindparams(outlet=get_settings().DEFAULT_OUTLET_ID)
            )


async def test_a_blank_display_name_is_refused_at_the_database_level(
    engine: Engine,
) -> None:
    from app.core.config import get_settings

    with pytest.raises(
        IntegrityError, match="ck_expense_categories_display_name_not_blank"
    ):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO expense_categories (outlet_id, code, display_name) "
                    "VALUES (:outlet, 'BLANKNAME', :name)"
                ).bindparams(outlet=get_settings().DEFAULT_OUTLET_ID, name="  \t ")
            )


async def test_a_patch_ignores_an_explicit_null_rather_than_clearing_the_field(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_expense_category: Callable[..., UUID],
    auth_headers,
) -> None:
    """Mirrors `fuel_types` and `expenses`: `exclude_unset` keeps "not mentioned" and
    "explicitly null" distinct, and an explicit null is then *ignored* rather than treated
    as "clear this". Clearing `display_name` would hit the not-blank CHECK and surface as a
    500; silently clearing `requires_receipt` would quietly disable a §6.11 control."""
    admin = make_user("admin")
    category = make_expense_category(
        "BOREWELL", display_name="Borewell", requires_receipt=True
    )

    response = await client.patch(
        f"/api/v1/expense-categories/{category}",
        json={"display_name": None, "requires_receipt": None, "is_active": False},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "Borewell"
    assert body["requires_receipt"] is True
    # The one field that was actually set still changed.
    assert body["is_active"] is False
