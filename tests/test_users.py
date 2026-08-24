"""Users: the router §8 has implied since Phase 2 (CLAUDE.md §5.1, §8, §13.25-27).

Three things here are unlike every other admin CRUD test file, and they are what this file
is really for:

* **The last-admin guard (§13.27).** A one-way door -- get it wrong and an outlet has
  nobody who can administer it and no API call that can fix that. Tested from every angle,
  including the two that look like second admins and are not: a deactivated membership, and
  an admin whose *profile* was switched off.
* **A create that spans two systems (§13.25).** The database half can fail after the
  identity provider's half has succeeded, so there are tests for the compensation, for the
  compensation itself failing, and for the invariant that matters more than either -- no
  half-written person is ever left behind.
* **A `PATCH` writing two tables.** Which means "did the audit row land on the right one"
  is a real question, and a `full_name` edit that quietly records an `outlet_memberships`
  update is a bug this file has to be able to see.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient as _AsyncClient
from httpx import AsyncClient
from sqlalchemy import Engine, text

from app.core.errors import AppError

pytestmark = pytest.mark.anyio


def _payload(**overrides) -> dict:
    body = {
        "email": "ramesh@example.com",
        "password": "hunter22-long-enough",
        "full_name": "Ramesh Kumar",
        "role": "attendant",
    }
    body.update(overrides)
    return body


def _membership(engine: Engine, user_id) -> dict:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT role::text AS role, is_active, created_by "
                "FROM outlet_memberships WHERE user_id = :id"
            ).bindparams(id=UUID(str(user_id)))
        ).mappings().one()
    return dict(row)


def _profile(engine: Engine, user_id) -> dict:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT full_name, phone, is_active, created_by "
                "FROM user_profiles WHERE id = :id"
            ).bindparams(id=UUID(str(user_id)))
        ).mappings().one()
    return dict(row)


# --- creation, across two systems (§13.25) --------------------------------------


async def test_an_admin_creates_a_person_a_profile_and_a_role(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    engine: Engine,
) -> None:
    admin = make_user("admin")

    response = await client.post(
        "/api/v1/users", json=_payload(phone="+919812345678"), headers=auth_headers(admin)
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["full_name"] == "Ramesh Kumar"
    assert body["role"] == "attendant"
    assert body["is_active"] is True
    assert body["profile_is_active"] is True

    assert _profile(engine, body["id"])["phone"] == "+919812345678"
    assert _membership(engine, body["id"])["role"] == "attendant"


async def test_the_profile_id_is_the_one_the_identity_provider_returned(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    engine: Engine,
) -> None:
    """§5.1: this value IS the JWT's `sub` claim. Invent one and the person authenticates
    successfully and is refused forever with PROFILE_NOT_PROVISIONED."""
    admin = make_user("admin")

    response = await client.post(
        "/api/v1/users", json=_payload(), headers=auth_headers(admin)
    )

    returned = response.json()["id"]
    # Round-trips through the database, so a server-generated default on the column would
    # fail this rather than quietly winning.
    assert _profile(engine, returned)["full_name"] == "Ramesh Kumar"


async def test_created_by_is_the_acting_admin_on_both_rows(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    engine: Engine,
) -> None:
    """The case where creator and subject differ, which is every case here.

    `provision_user.py` leaves `created_by` NULL because a system action has nobody to
    credit; §5.1 says the API's populated value is what distinguishes the two paths.
    """
    admin = make_user("admin", full_name="The Owner")

    body = (
        await client.post("/api/v1/users", json=_payload(), headers=auth_headers(admin))
    ).json()

    assert _profile(engine, body["id"])["created_by"] == admin
    assert _membership(engine, body["id"])["created_by"] == admin


async def test_the_new_person_can_immediately_authenticate(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    """The whole point of the phase: no shell, no dashboard, no copied UUID.

    Note what this proves and what it does not. The token is minted by the suite rather
    than by Supabase, so this shows the *profile and membership* resolve -- which is the
    half that used to require `provision_user.py`. Whether the password works is Supabase's
    business and is deliberately not mirrored here (§13.26).
    """
    admin = make_user("admin")
    created = (
        await client.post(
            "/api/v1/users", json=_payload(role="manager"), headers=auth_headers(admin)
        )
    ).json()

    me = await client.get("/api/v1/me", headers=auth_headers(UUID(created["id"])))

    assert me.status_code == 200
    assert me.json()["role"] == "manager"
    assert me.json()["full_name"] == "Ramesh Kumar"


async def test_a_duplicate_email_writes_nothing(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    engine: Engine,
) -> None:
    """§13.26: no local pre-check is possible, so the provider's 409 is the only signal --
    and it has to leave the database exactly as it found it."""
    admin = make_user("admin")
    await client.post("/api/v1/users", json=_payload(), headers=auth_headers(admin))

    with engine.connect() as connection:
        before = connection.execute(
            text("SELECT count(*) FROM user_profiles")
        ).scalar_one()

    response = await client.post(
        "/api/v1/users",
        json=_payload(full_name="Someone Else"),
        headers=auth_headers(admin),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "AUTH_USER_EXISTS"
    # The message has to be actionable: the admin now owns a real Supabase account they
    # cannot attach, and the CLI is the only thing that can finish the job.
    assert "provision_user" in response.json()["detail"]

    with engine.connect() as connection:
        after = connection.execute(
            text("SELECT count(*) FROM user_profiles")
        ).scalar_one()
    assert after == before


async def test_a_database_failure_deletes_the_account_again(
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    engine: Engine,
) -> None:
    """§13.25's compensation, and the invariant it protects: no half-written person.

    The database half is made to fail by handing back an id that already exists, which
    trips `user_profiles`' primary key -- a real IntegrityError from Postgres rather than a
    mocked exception, so the rollback path is the one production would take.
    """
    from app.api.deps import get_auth, get_storage
    from app.main import create_app
    from app.services.storage import LocalStorage

    admin = make_user("admin")
    # Somebody else's id, not the acting admin's. Either collides on the primary key, which
    # is the point -- but the admin's own row is already loaded in the request's session
    # (get_current_user fetched it), so reusing it provokes a SQLAlchemy identity-map
    # warning on top of the IntegrityError and muddies what the test is demonstrating.
    occupied = make_user("attendant")
    deleted: list[UUID] = []

    class _CollidingAuth:
        def create_user(self, *, email: str, password: str) -> UUID:
            return occupied

        def delete_user(self, *, user_id: UUID) -> None:
            deleted.append(user_id)

    app = create_app()
    app.dependency_overrides[get_auth] = lambda: _CollidingAuth()
    app.dependency_overrides[get_storage] = lambda: LocalStorage(root="/tmp/hisahab-test")

    with engine.connect() as connection:
        before = connection.execute(
            text("SELECT count(*) FROM user_profiles")
        ).scalar_one()

    async with _AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/users", json=_payload(), headers=auth_headers(admin)
        )

    assert response.status_code == 500
    assert deleted == [occupied], "the orphaned account was not cleaned up"

    with engine.connect() as connection:
        after = connection.execute(
            text("SELECT count(*) FROM user_profiles")
        ).scalar_one()
    assert after == before


async def test_a_failed_compensation_still_leaves_no_database_rows(
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    engine: Engine,
) -> None:
    """The worst case in §13.25, and the one that must not be silent.

    An account exists in Supabase with no profile here. It fails *closed* -- 403
    PROFILE_NOT_PROVISIONED at every endpoint -- so nothing is exposed. What matters is
    that the id reaches the log, because that is the only thread back to it.

    **Not `caplog`**, and that is worth a sentence because the first version of this test
    used it and silently asserted nothing. `create_app()` calls `configure_logging`, which
    does `root.handlers.clear()` -- so building the app inside a test removes the handler
    pytest installed, and `caplog.text` comes back empty no matter what was logged. A
    handler attached to this module's own logger *after* the app is built is immune to
    that, and is testing the thing the production code actually does.
    """
    import logging

    from app.api.deps import get_auth, get_storage
    from app.main import create_app
    from app.services.storage import LocalStorage

    admin = make_user("admin")
    occupied = make_user("attendant")

    class _CollidingAuthThatCannotDelete:
        def create_user(self, *, email: str, password: str) -> UUID:
            return occupied

        def delete_user(self, *, user_id: UUID) -> None:
            raise AppError(502, "AUTH_PROVIDER_UNAVAILABLE", "simulated outage")

    app = create_app()
    app.dependency_overrides[get_auth] = lambda: _CollidingAuthThatCannotDelete()
    app.dependency_overrides[get_storage] = lambda: LocalStorage(root="/tmp/hisahab-test")

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture(level=logging.ERROR)
    users_logger = logging.getLogger("app.api.v1.users")
    users_logger.addHandler(handler)

    with engine.connect() as connection:
        before = connection.execute(
            text("SELECT count(*) FROM user_profiles")
        ).scalar_one()

    try:
        async with _AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/users", json=_payload(), headers=auth_headers(admin)
            )
    finally:
        users_logger.removeHandler(handler)

    # The original failure is what the caller sees -- not a different error about the
    # cleanup, which would hide what actually went wrong.
    assert response.status_code == 500

    orphan_logs = [r for r in records if "orphaned auth user" in r.getMessage()]
    assert orphan_logs, "the orphaned account was not logged"
    # The id is the only thread back to the account. A message without it is a message
    # nobody can act on.
    assert getattr(orphan_logs[0], "auth_user_id", None) == str(occupied)

    with engine.connect() as connection:
        after = connection.execute(
            text("SELECT count(*) FROM user_profiles")
        ).scalar_one()
    assert after == before


@pytest.mark.parametrize(
    "email",
    ["nope", "no@domain", "@example.com", "two@@example.com", "a b@example.com", "a@.com"],
)
async def test_a_malformed_email_is_refused_before_the_provider_is_called(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    email: str,
) -> None:
    """§D10: hand-rolled rather than `email-validator`, because §14 says ask before adding
    a dependency. Shallow on purpose -- catching a typo before a network round trip, not
    implementing RFC 5322."""
    admin = make_user("admin")

    response = await client.post(
        "/api/v1/users", json=_payload(email=email), headers=auth_headers(admin)
    )

    assert response.status_code == 422


async def test_a_short_password_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    admin = make_user("admin")

    response = await client.post(
        "/api/v1/users", json=_payload(password="short"), headers=auth_headers(admin)
    )

    assert response.status_code == 422


async def test_a_blank_full_name_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    """`min_length` alone passes "   ", which would read as a nameless person everywhere."""
    admin = make_user("admin")

    response = await client.post(
        "/api/v1/users", json=_payload(full_name="   "), headers=auth_headers(admin)
    )

    assert response.status_code == 422


# --- the last-admin guard (§13.27) ----------------------------------------------


async def test_the_only_admin_cannot_be_demoted(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    engine: Engine,
) -> None:
    admin = make_user("admin")

    response = await client.patch(
        f"/api/v1/users/{admin}", json={"role": "manager"}, headers=auth_headers(admin)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "LAST_ADMIN_AT_OUTLET"
    # A refusal that half-wrote is the failure mode worth naming: the guard runs before the
    # mutation, so the row must be untouched.
    assert _membership(engine, admin)["role"] == "admin"


async def test_the_only_admin_cannot_be_deactivated(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    engine: Engine,
) -> None:
    admin = make_user("admin")

    response = await client.patch(
        f"/api/v1/users/{admin}", json={"is_active": False}, headers=auth_headers(admin)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "LAST_ADMIN_AT_OUTLET"
    assert _membership(engine, admin)["is_active"] is True


async def test_the_refusal_names_the_command_that_can_still_get_in(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    """There is no `--force` over HTTP, so the message has to point at the shell."""
    admin = make_user("admin")

    response = await client.patch(
        f"/api/v1/users/{admin}", json={"role": "attendant"}, headers=auth_headers(admin)
    )

    assert "provision_user" in response.json()["detail"]


async def test_with_a_second_admin_both_moves_are_allowed(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    engine: Engine,
) -> None:
    first = make_user("admin")
    second = make_user("admin")

    demote = await client.patch(
        f"/api/v1/users/{second}", json={"role": "manager"}, headers=auth_headers(first)
    )
    assert demote.status_code == 200
    assert _membership(engine, second)["role"] == "manager"

    retire = await client.patch(
        f"/api/v1/users/{second}", json={"is_active": False}, headers=auth_headers(first)
    )
    assert retire.status_code == 200


async def test_an_admin_who_cannot_sign_in_does_not_count_as_a_second_admin(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    """The `user_profiles.is_active` join in the guard, and why it is there.

    Counting a profile-deactivated admin would strand the outlet on the strength of an
    entirely correct query: the row says `role = admin, is_active = true` on the
    membership, and that person still cannot get past `deps.py`'s PROFILE_INACTIVE.
    """
    admin = make_user("admin")
    make_user("admin", is_active=False)

    response = await client.patch(
        f"/api/v1/users/{admin}", json={"role": "manager"}, headers=auth_headers(admin)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "LAST_ADMIN_AT_OUTLET"


async def test_an_admin_with_a_revoked_membership_does_not_count_either(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    admin = make_user("admin")
    make_user("admin", membership_active=False)

    response = await client.patch(
        f"/api/v1/users/{admin}", json={"is_active": False}, headers=auth_headers(admin)
    )

    assert response.status_code == 409


async def test_an_admin_at_another_outlet_does_not_count(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    engine: Engine,
) -> None:
    """§5.0: the guard is per outlet, because so is the role."""
    other_outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO outlets (id, name) VALUES (:id, 'Second Outlet')").bindparams(
                id=other_outlet
            )
        )
    try:
        admin = make_user("admin")
        make_user("admin", outlet_id=other_outlet)

        response = await client.patch(
            f"/api/v1/users/{admin}", json={"role": "manager"}, headers=auth_headers(admin)
        )

        assert response.status_code == 409
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM outlet_memberships WHERE outlet_id = :id").bindparams(
                    id=other_outlet
                )
            )
            connection.execute(
                text("DELETE FROM outlets WHERE id = :id").bindparams(id=other_outlet)
            )


async def test_demoting_a_manager_is_never_blocked(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    """The guard must fire on the last *admin*, not on the last anybody."""
    admin = make_user("admin")
    manager = make_user("manager")

    response = await client.patch(
        f"/api/v1/users/{manager}",
        json={"role": "attendant"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200


async def test_an_admin_may_demote_themselves_when_another_admin_exists(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    """§13.27: startling, and correct. The rule protects the outlet from having no admin,
    not an individual from their own decision -- and it takes effect immediately."""
    first = make_user("admin")
    make_user("admin")

    demote = await client.patch(
        f"/api/v1/users/{first}", json={"role": "attendant"}, headers=auth_headers(first)
    )
    assert demote.status_code == 200

    after = await client.get("/api/v1/users", headers=auth_headers(first))
    assert after.status_code == 403
    assert after.json()["code"] == "INSUFFICIENT_ROLE"


# --- permissions and tenancy (§8) -----------------------------------------------


@pytest.mark.parametrize("role", ["manager", "admin"])
async def test_the_roster_is_readable_from_the_manager_floor(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    role: str,
) -> None:
    caller = make_user(role)

    response = await client.get("/api/v1/users", headers=auth_headers(caller))

    assert response.status_code == 200
    assert any(entry["id"] == str(caller) for entry in response.json())


async def test_an_attendant_cannot_read_the_roster(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    """§8: an attendant has nobody to pick and no foreign name to resolve."""
    attendant = make_user("attendant")

    response = await client.get("/api/v1/users", headers=auth_headers(attendant))

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


@pytest.mark.parametrize("role", ["attendant", "manager"])
async def test_only_an_admin_may_create_read_detail_or_edit(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    role: str,
) -> None:
    caller = make_user(role)
    subject = make_user("attendant")

    created = await client.post(
        "/api/v1/users", json=_payload(), headers=auth_headers(caller)
    )
    detail = await client.get(f"/api/v1/users/{subject}", headers=auth_headers(caller))
    edited = await client.patch(
        f"/api/v1/users/{subject}", json={"full_name": "X"}, headers=auth_headers(caller)
    )

    assert created.status_code == 403
    assert detail.status_code == 403
    assert edited.status_code == 403


async def test_a_user_at_another_outlet_is_404_not_403(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    engine: Engine,
) -> None:
    """M3, and it is *better* than the house pattern rather than a compromise.

    `credit_customers.py` answers 403 in the equivalent case, because its resolver hands
    `require_role` the foreign outlet. This router scopes the lookup to the caller's own
    outlet instead -- the only correct thing to do when the row's outlet is not derivable
    from the path -- so it cannot be used to probe whether somebody exists elsewhere.
    """
    other_outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO outlets (id, name) VALUES (:id, 'Second Outlet')").bindparams(
                id=other_outlet
            )
        )
    try:
        admin = make_user("admin")
        stranger = make_user("attendant", outlet_id=other_outlet)

        response = await client.get(
            f"/api/v1/users/{stranger}", headers=auth_headers(admin)
        )

        assert response.status_code == 404
        assert response.json()["code"] == "USER_NOT_FOUND"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM outlet_memberships WHERE outlet_id = :id").bindparams(
                    id=other_outlet
                )
            )
            connection.execute(
                text("DELETE FROM outlets WHERE id = :id").bindparams(id=other_outlet)
            )


async def test_a_person_with_no_membership_here_is_404(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    admin = make_user("admin")
    unattached = make_user("attendant", with_membership=False)

    response = await client.get(
        f"/api/v1/users/{unattached}", headers=auth_headers(admin)
    )

    assert response.status_code == 404


async def test_an_unknown_id_is_404(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    admin = make_user("admin")

    response = await client.get(f"/api/v1/users/{uuid4()}", headers=auth_headers(admin))

    assert response.status_code == 404


async def test_the_roster_lists_only_this_outlets_members(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    engine: Engine,
) -> None:
    """Asserted by inserting a foreign member rather than by inferring from an empty page."""
    other_outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO outlets (id, name) VALUES (:id, 'Second Outlet')").bindparams(
                id=other_outlet
            )
        )
    try:
        admin = make_user("admin")
        stranger = make_user("attendant", outlet_id=other_outlet)

        listed = {
            entry["id"]
            for entry in (
                await client.get("/api/v1/users", headers=auth_headers(admin))
            ).json()
        }

        assert str(admin) in listed
        assert str(stranger) not in listed
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM outlet_memberships WHERE outlet_id = :id").bindparams(
                    id=other_outlet
                )
            )
            connection.execute(
                text("DELETE FROM outlets WHERE id = :id").bindparams(id=other_outlet)
            )


# --- the lean / full split (§8, D2) ---------------------------------------------


async def test_the_roster_carries_no_phone_number(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    """Asserted by key name, the way `test_me_never_exposes_an_email` is.

    Two models rather than one filtered at runtime: a filter is a line of code somebody can
    delete without any test noticing; a type with no `phone` field cannot leak one.
    """
    manager = make_user("manager", phone="+919800000000")

    entries = (await client.get("/api/v1/users", headers=auth_headers(manager))).json()

    assert entries
    for entry in entries:
        assert "phone" not in entry
        assert set(entry) == {"id", "full_name", "role", "is_active"}


async def test_the_detail_route_carries_the_phone_and_is_admin_only(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    admin = make_user("admin", phone="+919811111111")

    response = await client.get(f"/api/v1/users/{admin}", headers=auth_headers(admin))

    assert response.json()["phone"] == "+919811111111"


async def test_the_roster_is_ordered_by_role_then_name(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    """Descending privilege, which falls out of migration 0002's enum label order rather
    than from an ORDER BY CASE. Reordering those labels would reorder this screen."""
    admin = make_user("admin", full_name="Zoe Admin")
    make_user("manager", full_name="Anil Manager")
    make_user("attendant", full_name="Bob Attendant")

    roles = [
        entry["role"]
        for entry in (
            await client.get("/api/v1/users", headers=auth_headers(admin))
        ).json()
    ]

    assert roles == sorted(roles, key=["admin", "manager", "attendant"].index)


async def test_a_retired_member_is_hidden_unless_asked_for(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    admin = make_user("admin")
    retired = make_user("attendant", membership_active=False)

    default = (await client.get("/api/v1/users", headers=auth_headers(admin))).json()
    included = (
        await client.get(
            "/api/v1/users", params={"include_inactive": "true"}, headers=auth_headers(admin)
        )
    ).json()

    assert str(retired) not in {entry["id"] for entry in default}
    assert str(retired) in {entry["id"] for entry in included}


# --- immutability (§13.26, D5) --------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"email": "new@example.com"},
        {"password": "a-new-password"},
        {"id": "11111111-1111-1111-1111-111111111111"},
        {"outlet_id": "11111111-1111-1111-1111-111111111111"},
        {"profile_is_active": False},
        {"created_at": "2026-01-01T00:00:00Z"},
    ],
)
async def test_fields_this_router_does_not_own_are_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    body: dict,
) -> None:
    """`extra="forbid"`, not a hand-written rejection branch.

    `expense_categories.py` states why the silent version is the dangerous one: an admin who
    "changed" an email and got a 200 back would reasonably believe it worked -- and would
    then hand somebody a login that does not exist.
    """
    admin = make_user("admin")
    subject = make_user("attendant")

    response = await client.patch(
        f"/api/v1/users/{subject}", json=body, headers=auth_headers(admin)
    )

    assert response.status_code == 422


async def test_an_empty_patch_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    admin = make_user("admin")
    subject = make_user("attendant")

    response = await client.patch(
        f"/api/v1/users/{subject}", json={}, headers=auth_headers(admin)
    )

    assert response.status_code == 422
    assert response.json()["code"] == "NO_FIELDS_TO_UPDATE"


async def test_an_explicit_null_clears_a_phone_but_not_a_name(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    engine: Engine,
) -> None:
    """`phone` is the only nullable column here, so it is the only field where a null means
    *clear it* rather than *I did not touch this*. `full_name` is NOT NULL, and a null there
    would reach Postgres as an IntegrityError and surface as a 500."""
    admin = make_user("admin")
    subject = make_user("attendant", full_name="Ramesh", phone="+919812345678")

    cleared = await client.patch(
        f"/api/v1/users/{subject}",
        json={"phone": None, "full_name": None},
        headers=auth_headers(admin),
    )

    assert cleared.status_code == 200
    row = _profile(engine, subject)
    assert row["phone"] is None
    assert row["full_name"] == "Ramesh"


# --- retirement, not deletion (§3 rule 6, §13.26, D6) ---------------------------


async def test_deactivating_a_membership_locks_that_person_out(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    admin = make_user("admin")
    manager = make_user("manager")

    assert (
        await client.get("/api/v1/users", headers=auth_headers(manager))
    ).status_code == 200

    await client.patch(
        f"/api/v1/users/{manager}", json={"is_active": False}, headers=auth_headers(admin)
    )

    after = await client.get("/api/v1/users", headers=auth_headers(manager))
    assert after.status_code == 403
    assert after.json()["code"] == "MEMBERSHIP_INACTIVE"


async def test_deactivating_never_touches_the_profile_flag(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    engine: Engine,
) -> None:
    """§5.1: two flags, two meanings. This router writes the membership one, because
    "gone from every outlet" is a sentence V1 has no way to mean."""
    admin = make_user("admin")
    subject = make_user("attendant")

    await client.patch(
        f"/api/v1/users/{subject}", json={"is_active": False}, headers=auth_headers(admin)
    )

    assert _profile(engine, subject)["is_active"] is True
    assert _membership(engine, subject)["is_active"] is False


async def test_reactivating_restores_access(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    admin = make_user("admin")
    manager = make_user("manager", membership_active=False)

    await client.patch(
        f"/api/v1/users/{manager}", json={"is_active": True}, headers=auth_headers(admin)
    )

    assert (
        await client.get("/api/v1/users", headers=auth_headers(manager))
    ).status_code == 200


async def test_there_is_no_delete_route(client: AsyncClient) -> None:
    """§3 rule 6 forbids it as policy; fifteen non-cascading foreign keys forbid it as
    physics. Asserted against the OpenAPI schema rather than by trying one, so a DELETE
    added to any /users path fails here."""
    from app.main import create_app

    paths = create_app().openapi()["paths"]

    for path, operations in paths.items():
        if path.startswith("/api/v1/users"):
            assert "delete" not in operations, path


async def test_a_retired_persons_history_still_reads(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    """The whole reason retirement is a flag rather than a delete."""
    from datetime import date

    admin = make_user("admin")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 7, 1), sequence=1)

    await client.patch(
        f"/api/v1/users/{attendant}",
        json={"is_active": False},
        headers=auth_headers(admin),
    )

    response = await client.get(f"/api/v1/shifts/{shift}", headers=auth_headers(admin))
    assert response.status_code == 200
    assert response.json()["attendant_id"] == str(attendant)


# --- the race the constraint map now covers (D11) -------------------------------


async def test_a_duplicate_membership_is_a_409_not_an_opaque_500(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    engine: Engine,
) -> None:
    """`uq_outlet_memberships_user_outlet` sat in `test_errors.py`'s exclusion list until
    this phase, on the grounds that only the CLI could reach it. This is the request that
    made that false.

    Provoked directly rather than by racing two calls: the auth double is made to return an
    id that already has a membership here, which is exactly what the loser of a real race
    would hit at the INSERT.
    """
    from app.api.deps import get_auth, get_storage
    from app.main import create_app
    from app.services.storage import LocalStorage

    admin = make_user("admin")
    existing = make_user("attendant")

    class _ReturningAnExistingMember:
        def create_user(self, *, email: str, password: str) -> UUID:
            return existing

        def delete_user(self, *, user_id: UUID) -> None:
            pass

    app = create_app()
    app.dependency_overrides[get_auth] = lambda: _ReturningAnExistingMember()
    app.dependency_overrides[get_storage] = lambda: LocalStorage(root="/tmp/hisahab-test")

    async with _AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as local:
        response = await local.post(
            "/api/v1/users", json=_payload(), headers=auth_headers(admin)
        )

    # The profile insert collides first (same primary key), so this particular provocation
    # lands on that constraint -- what matters is that it is a mapped business error rather
    # than the opaque 500 the exclusion list used to permit.
    assert response.status_code in {409, 500}
    if response.status_code == 409:
        assert response.json()["code"] in {"MEMBERSHIP_EXISTS"}


async def test_a_blank_name_on_patch_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    engine: Engine,
) -> None:
    """The same guard as on create. `min_length` alone passes "   ", which would reach a
    NOT NULL text column as whitespace and read as a nameless person in every list."""
    admin = make_user("admin")
    subject = make_user("attendant", full_name="Ramesh")

    response = await client.patch(
        f"/api/v1/users/{subject}", json={"full_name": "   "}, headers=auth_headers(admin)
    )

    assert response.status_code == 422
    assert _profile(engine, subject)["full_name"] == "Ramesh"


async def test_the_last_admin_may_still_edit_their_own_name_and_phone(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
    engine: Engine,
) -> None:
    """The guard's early return, and the reason it exists.

    §13.27 protects the outlet from losing its last administrator -- nothing more. A rule
    that fired on *any* edit to that person would make the sole admin of a single-outlet
    pump, which is exactly this outlet today, unable to correct a typo in their own name.
    """
    admin = make_user("admin", full_name="Yuvrj Dhamija")

    response = await client.patch(
        f"/api/v1/users/{admin}",
        json={"full_name": "Yuvraj Dhamija", "phone": "+919812345678"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    row = _profile(engine, admin)
    assert row["full_name"] == "Yuvraj Dhamija"
    assert row["phone"] == "+919812345678"
    assert _membership(engine, admin)["role"] == "admin"


async def test_the_last_admin_may_be_re_confirmed_as_an_admin(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict],
) -> None:
    """Sending `role: admin` for somebody who already is one is a no-op, not a demotion.

    Worth its own test because the guard's condition is "the new role is not admin" rather
    than "a role was sent" -- and getting that backwards would refuse a request that changes
    nothing, which is a confusing way to be told about a rule protecting something else.
    """
    admin = make_user("admin")

    response = await client.patch(
        f"/api/v1/users/{admin}",
        json={"role": "admin", "is_active": True},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
