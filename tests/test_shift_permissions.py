"""Role and ownership on shifts (CLAUDE.md §8, §10).

§8 states that ownership is a separate axis from role: `require_role` handles the role
floor, and the shift-scoped dependency applies the ownership check **only when the actor's
role is `attendant`**. These are the cases §10 names by hand:

    Attendant writing to another attendant's shift -> 403
    Attendant closing a shift                      -> 403
    Manager locking a shift                        -> 403
    Any write to a locked shift                    -> 409

Real tokens throughout, never a dependency override -- the policy in
tests/test_permissions.py: overriding get_current_user skips the logic most worth testing.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from uuid import UUID

import pytest
from fastapi import Depends
from httpx import AsyncClient

pytestmark = pytest.mark.usefixtures("clean_shifts")

_ENVELOPE_KEYS = {"detail", "code", "request_id"}


# --- ownership ----------------------------------------------------------------


async def test_an_attendant_cannot_read_another_attendants_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    make_shift: Callable[..., UUID],
) -> None:
    """The case §10 calls out specifically.

    If `require_shift_access` ever drops the ownership check and degrades into a pure role
    floor, this is the test that catches it -- both users clear the attendant floor.
    """
    owner = make_user("attendant")
    other = make_user("attendant")
    shift_id = make_shift(owner)

    response = await client.get(
        f"/api/v1/shifts/{shift_id}", headers=auth_headers(other)
    )

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_YOUR_SHIFT"
    assert set(response.json()) == _ENVELOPE_KEYS


async def test_an_attendant_can_read_their_own_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    make_shift: Callable[..., UUID],
) -> None:
    owner = make_user("attendant")
    shift_id = make_shift(owner)

    response = await client.get(
        f"/api/v1/shifts/{shift_id}", headers=auth_headers(owner)
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(shift_id)


@pytest.mark.parametrize("role", ["manager", "admin"])
async def test_managers_and_admins_may_read_any_shift_at_their_outlet(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    make_shift: Callable[..., UUID],
    role: str,
) -> None:
    """Ownership is role-conditional. Applying it unconditionally would lock managers out
    of the shifts they exist to close."""
    owner = make_user("attendant")
    supervisor = make_user(role)
    shift_id = make_shift(owner)

    response = await client.get(
        f"/api/v1/shifts/{shift_id}", headers=auth_headers(supervisor)
    )

    assert response.status_code == 200


async def test_an_attendant_cannot_open_a_shift_in_someone_elses_name(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """Otherwise the attendant floor on POST /shifts would mean "anyone, any name"."""
    attendant = make_user("attendant")
    colleague = make_user("attendant")

    response = await client.post(
        "/api/v1/shifts",
        json={"business_date": "2026-03-10", "attendant_id": str(colleague)},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_YOUR_SHIFT"


# --- role floors ---------------------------------------------------------------


async def test_an_attendant_cannot_close_a_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    make_shift: Callable[..., UUID],
) -> None:
    """§8, and §10 names it. Note the shift is the attendant's OWN -- ownership is not the
    thing being refused here, the manager floor is."""
    attendant = make_user("attendant")
    shift_id = make_shift(attendant)

    response = await client.patch(
        f"/api/v1/shifts/{shift_id}/close", json={}, headers=auth_headers(attendant)
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


async def test_a_manager_cannot_lock_a_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    make_shift: Callable[..., UUID],
) -> None:
    """§8: locking a shift and finalising a day are admin-only. §10 names this one too."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift_id = make_shift(attendant, status="closed")

    response = await client.patch(
        f"/api/v1/shifts/{shift_id}/lock", headers=auth_headers(manager)
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


async def test_a_manager_cannot_reopen_a_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    make_shift: Callable[..., UUID],
) -> None:
    """A backwards transition is an admin action (§5.2)."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift_id = make_shift(attendant, status="closed")

    response = await client.patch(
        f"/api/v1/shifts/{shift_id}/reopen",
        json={"reason": "closed too early"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


async def test_a_manager_may_close(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    make_shift: Callable[..., UUID],
) -> None:
    """The positive half of the ladder -- a floor nothing can reach is not a control."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift_id = make_shift(attendant)

    response = await client.patch(
        f"/api/v1/shifts/{shift_id}/close",
        json={"ended_at": "2026-08-18T22:00:00+05:30"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 200


# --- immutability ---------------------------------------------------------------


async def test_a_locked_shift_refuses_writes(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    make_shift: Callable[..., UUID],
) -> None:
    """§10's "any write to a locked shift -> 409", proved through the dependency that
    Phases 5-10 will hang their own writes off."""
    from app.api.deps import require_shift_access
    from app.core.roles import Role
    from app.main import create_app

    attendant = make_user("attendant")
    shift_id = make_shift(attendant, status="locked")

    # A stand-in for the collection/expense/reading endpoints that do not exist yet, wired
    # to the same dependency they will use. Mirrors how tests/test_permissions.py bolts on
    # synthetic floor routes.
    app = create_app()

    @app.post("/api/v1/_test/shifts/{shift_id}/write")
    def write(access=Depends(require_shift_access(Role.attendant, writable=True))):
        return {"ok": True}

    from httpx import ASGITransport, AsyncClient as Client

    async with Client(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as probe:
        response = await probe.post(
            f"/api/v1/_test/shifts/{shift_id}/write", headers=auth_headers(attendant)
        )

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_LOCKED"


async def test_a_closed_shift_refuses_writes_with_a_recoverable_code(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    make_shift: Callable[..., UUID],
) -> None:
    """SHIFT_NOT_OPEN and SHIFT_LOCKED are separate codes on purpose: an admin can reopen
    the first and never the second, and a client should be able to say which."""
    from app.api.deps import require_shift_access
    from app.core.roles import Role
    from app.main import create_app
    from httpx import ASGITransport, AsyncClient as Client

    attendant = make_user("attendant")
    shift_id = make_shift(attendant, status="closed")

    app = create_app()

    @app.post("/api/v1/_test/shifts/{shift_id}/write")
    def write(access=Depends(require_shift_access(Role.attendant, writable=True))):
        return {"ok": True}

    async with Client(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as probe:
        response = await probe.post(
            f"/api/v1/_test/shifts/{shift_id}/write", headers=auth_headers(attendant)
        )

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_NOT_OPEN"


# --- outlet scoping --------------------------------------------------------------


async def test_an_admin_at_another_outlet_is_not_a_member_here(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    make_shift: Callable[..., UUID],
    engine,
) -> None:
    """§8: the question is always "role R **at the outlet that owns this row**".

    If `resolve_outlet_from_shift` ever returned the configured default instead of the
    row's outlet, this would pass wrongly.
    """
    from sqlalchemy import text
    from uuid import uuid4

    other_outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outlets (id, name) VALUES (:id, 'Second Outlet')"
            ).bindparams(id=other_outlet)
        )

    attendant = make_user("attendant")
    outsider = make_user("admin", outlet_id=other_outlet)
    shift_id = make_shift(attendant)

    response = await client.get(
        f"/api/v1/shifts/{shift_id}", headers=auth_headers(outsider)
    )

    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM outlet_memberships WHERE outlet_id = :id"
            ).bindparams(id=other_outlet)
        )
        connection.execute(
            text("DELETE FROM outlets WHERE id = :id").bindparams(id=other_outlet)
        )

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_A_MEMBER"


# --- read scoping ----------------------------------------------------------------


async def test_the_list_shows_an_attendant_only_their_own_shifts(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    make_shift: Callable[..., UUID],
) -> None:
    """§8's permission table: "Read own shift" yes, "Read all shifts" no."""
    mine = make_user("attendant")
    theirs = make_user("attendant")
    manager = make_user("manager")
    make_shift(mine, business_date=date(2026, 4, 1), sequence=1, status="closed")
    make_shift(theirs, business_date=date(2026, 4, 2), sequence=1, status="closed")

    as_attendant = await client.get("/api/v1/shifts", headers=auth_headers(mine))
    as_manager = await client.get("/api/v1/shifts", headers=auth_headers(manager))

    assert {item["attendant_id"] for item in as_attendant.json()["items"]} == {str(mine)}
    assert {item["attendant_id"] for item in as_manager.json()["items"]} == {
        str(mine),
        str(theirs),
    }
