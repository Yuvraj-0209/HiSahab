"""Per-outlet role enforcement over real HTTP (CLAUDE.md §8, §9, §10).

These tests go through the ASGI stack rather than calling the dependency directly, because
the wiring is part of what can break: bearer extraction, the dependency chain resolving in
the right order, and the error envelope travelling through the installed exception
handlers. A unit test of `require_role` would pass while any of those was broken.

Tokens are minted locally by the `make_token` fixture. Deliberately not
`app.dependency_overrides[get_current_user]`: overriding it would skip the verification
logic that most needs testing.

The three `_test/*-floor` routes exist only inside this module's app instance. They are how
`require_role` gets exercised at the manager and admin floors, which the real API does not
yet have -- §11 has not asked for those endpoints, so inventing them in production code to
satisfy a test would be scaffolding ahead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from uuid import UUID, uuid4

import pytest
from fastapi import Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Engine, text

from app.api.deps import Actor, require_role
from app.core.roles import Role

ATTENDANT_FLOOR = "/api/v1/_test/attendant-floor"
MANAGER_FLOOR = "/api/v1/_test/manager-floor"
ADMIN_FLOOR = "/api/v1/_test/admin-floor"

_ENVELOPE_KEYS = {"detail", "code", "request_id"}


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """The real app plus one route per role floor.

    Built from create_app() rather than a bare FastAPI() so the middleware and exception
    handlers under test are the production ones.
    """
    from app.main import create_app

    app = create_app()

    @app.get(ATTENDANT_FLOOR)
    def attendant_floor(
        actor: Actor = Depends(require_role(Role.attendant)),
    ) -> dict[str, str]:
        return {"role": actor.role.value, "outlet_id": str(actor.outlet_id)}

    @app.get(MANAGER_FLOOR)
    def manager_floor(
        actor: Actor = Depends(require_role(Role.manager)),
    ) -> dict[str, str]:
        return {"role": actor.role.value}

    @app.get(ADMIN_FLOOR)
    def admin_floor(
        actor: Actor = Depends(require_role(Role.admin)),
    ) -> dict[str, str]:
        return {"role": actor.role.value}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def second_outlet(engine: Engine) -> Iterator[UUID]:
    """A second outlet, so outlet-scoping can actually be tested.

    Teardown removes any memberships pointing at it before the outlet itself, so it does
    not depend on fixture teardown ordering relative to `make_user`.
    """
    outlet_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outlets (id, name) VALUES (:id, 'Second Outlet')"
            ).bindparams(id=outlet_id)
        )

    yield outlet_id

    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM outlet_memberships WHERE outlet_id = :id"
            ).bindparams(id=outlet_id)
        )
        connection.execute(
            text("DELETE FROM outlets WHERE id = :id").bindparams(id=outlet_id)
        )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- 401: we do not know who you are ---------------------------------------------


async def test_missing_authorization_header_is_unauthenticated(
    client: AsyncClient,
) -> None:
    response = await client.get(ATTENDANT_FLOOR)

    assert response.status_code == 401
    body = response.json()
    assert set(body) == _ENVELOPE_KEYS
    assert body["code"] == "NOT_AUTHENTICATED"


async def test_non_bearer_scheme_is_unauthenticated(client: AsyncClient) -> None:
    """HTTPBearer(auto_error=False) returns None for a non-bearer scheme too."""
    response = await client.get(
        ATTENDANT_FLOOR, headers={"Authorization": "Basic dXNlcjpwYXNz"}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "NOT_AUTHENTICATED"


async def test_expired_token_is_rejected_over_http(
    client: AsyncClient, make_token: Callable[..., str], make_user: Callable[..., UUID]
) -> None:
    """The user is entirely valid; only the token has aged out."""
    user_id = make_user(Role.admin)

    response = await client.get(
        ATTENDANT_FLOOR, headers=_auth(make_token(user_id, expires_in=-3600))
    )

    assert response.status_code == 401
    assert response.json()["code"] == "TOKEN_EXPIRED"


async def test_tampered_token_is_rejected(
    client: AsyncClient, make_token: Callable[..., str], make_user: Callable[..., UUID]
) -> None:
    """Flip one character of the signature and the whole token is worthless."""
    user_id = make_user(Role.admin)
    token = make_token(user_id)
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")

    response = await client.get(ATTENDANT_FLOOR, headers=_auth(tampered))

    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_TOKEN"


async def test_token_signed_with_another_secret_is_rejected(
    client: AsyncClient, make_token: Callable[..., str], make_user: Callable[..., UUID]
) -> None:
    user_id = make_user(Role.admin)
    forged = make_token(user_id, secret="an-attackers-secret-at-least-32-bytes")

    response = await client.get(ATTENDANT_FLOOR, headers=_auth(forged))

    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_TOKEN"


# --- 403: we know who you are, and the answer is no ------------------------------


async def test_valid_token_without_a_profile_is_forbidden(
    client: AsyncClient, make_token: Callable[..., str]
) -> None:
    """Supabase authenticated them; this application has never heard of them.

    403 rather than 401 on purpose: the token is fine, so retrying with a fresh one would
    not help. Someone has to run app/jobs/provision_user.py.
    """
    response = await client.get(ATTENDANT_FLOOR, headers=_auth(make_token(uuid4())))

    assert response.status_code == 403
    body = response.json()
    assert set(body) == _ENVELOPE_KEYS
    assert body["code"] == "PROFILE_NOT_PROVISIONED"


async def test_inactive_profile_is_forbidden(
    client: AsyncClient, make_token: Callable[..., str], make_user: Callable[..., UUID]
) -> None:
    user_id = make_user(Role.manager, is_active=False)

    response = await client.get(ATTENDANT_FLOOR, headers=_auth(make_token(user_id)))

    assert response.status_code == 403
    assert response.json()["code"] == "PROFILE_INACTIVE"


async def test_profile_without_a_membership_is_forbidden(
    client: AsyncClient, make_token: Callable[..., str], make_user: Callable[..., UUID]
) -> None:
    """A real, active user with no role assigned at this outlet."""
    user_id = make_user(with_membership=False)

    response = await client.get(ATTENDANT_FLOOR, headers=_auth(make_token(user_id)))

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_A_MEMBER"


async def test_inactive_membership_is_forbidden(
    client: AsyncClient, make_token: Callable[..., str], make_user: Callable[..., UUID]
) -> None:
    user_id = make_user(Role.manager, membership_active=False)

    response = await client.get(ATTENDANT_FLOOR, headers=_auth(make_token(user_id)))

    assert response.status_code == 403
    assert response.json()["code"] == "MEMBERSHIP_INACTIVE"


async def test_membership_at_another_outlet_does_not_grant_access(
    client: AsyncClient,
    make_token: Callable[..., str],
    make_user: Callable[..., UUID],
    second_outlet: UUID,
) -> None:
    """The test that proves §5.0/§8 actually work.

    This user is an admin -- but at a different outlet. If `require_role` ever ignores its
    outlet_id argument and degrades into "is this user an admin", this is the test that
    catches it. Worth writing even though V1 seeds a single outlet.
    """
    user_id = make_user(Role.admin, outlet_id=second_outlet)

    response = await client.get(ATTENDANT_FLOOR, headers=_auth(make_token(user_id)))

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_A_MEMBER"


# --- The role hierarchy, enforced ------------------------------------------------


async def test_attendant_passes_the_attendant_floor(
    client: AsyncClient, make_token: Callable[..., str], make_user: Callable[..., UUID]
) -> None:
    user_id = make_user(Role.attendant)

    response = await client.get(ATTENDANT_FLOOR, headers=_auth(make_token(user_id)))

    assert response.status_code == 200
    assert response.json()["role"] == "attendant"


async def test_attendant_is_refused_the_manager_floor(
    client: AsyncClient, make_token: Callable[..., str], make_user: Callable[..., UUID]
) -> None:
    """§8: closing a shift, reading all shifts and recording deposits are manager+."""
    user_id = make_user(Role.attendant)

    response = await client.get(MANAGER_FLOOR, headers=_auth(make_token(user_id)))

    assert response.status_code == 403
    body = response.json()
    assert set(body) == _ENVELOPE_KEYS
    assert body["code"] == "INSUFFICIENT_ROLE"


async def test_manager_passes_the_manager_floor(
    client: AsyncClient, make_token: Callable[..., str], make_user: Callable[..., UUID]
) -> None:
    user_id = make_user(Role.manager)

    response = await client.get(MANAGER_FLOOR, headers=_auth(make_token(user_id)))

    assert response.status_code == 200


async def test_manager_passes_the_attendant_floor(
    client: AsyncClient, make_token: Callable[..., str], make_user: Callable[..., UUID]
) -> None:
    """The hierarchy is a superset ladder, not exact matching."""
    user_id = make_user(Role.manager)

    response = await client.get(ATTENDANT_FLOOR, headers=_auth(make_token(user_id)))

    assert response.status_code == 200


async def test_admin_passes_the_manager_floor(
    client: AsyncClient, make_token: Callable[..., str], make_user: Callable[..., UUID]
) -> None:
    user_id = make_user(Role.admin)

    response = await client.get(MANAGER_FLOOR, headers=_auth(make_token(user_id)))

    assert response.status_code == 200


async def test_manager_is_refused_the_admin_floor(
    client: AsyncClient, make_token: Callable[..., str], make_user: Callable[..., UUID]
) -> None:
    """The role-floor half of §10's "manager locking a shift -> 403".

    The shift-specific half needs the shifts table and arrives in Phase 4.
    """
    user_id = make_user(Role.manager)

    response = await client.get(ADMIN_FLOOR, headers=_auth(make_token(user_id)))

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


async def test_admin_passes_the_admin_floor(
    client: AsyncClient, make_token: Callable[..., str], make_user: Callable[..., UUID]
) -> None:
    user_id = make_user(Role.admin)

    response = await client.get(ADMIN_FLOOR, headers=_auth(make_token(user_id)))

    assert response.status_code == 200
    assert response.json()["role"] == "admin"


async def test_the_resolved_outlet_is_the_configured_default(
    client: AsyncClient, make_token: Callable[..., str], make_user: Callable[..., UUID]
) -> None:
    """V1 resolves every create-shaped check against DEFAULT_OUTLET_ID."""
    from app.core.config import get_settings

    user_id = make_user(Role.attendant)

    response = await client.get(ATTENDANT_FLOOR, headers=_auth(make_token(user_id)))

    assert response.json()["outlet_id"] == str(get_settings().DEFAULT_OUTLET_ID)


async def test_unconfigured_jwt_secret_refuses_instead_of_allowing(
    make_token: Callable[..., str], make_user: Callable[..., UUID]
) -> None:
    """A server with no JWT secret must refuse traffic, not authenticate everyone.

    This is the one failure mode where a wrong default is catastrophic rather than
    inconvenient, so it gets an explicit test: 500, not 200. The prod config validator
    makes it unreachable in production, but the guard stays because "unreachable" is a
    property of today's config code, not a guarantee.

    dependency_overrides is used here deliberately -- unlike the tests above, the subject
    is the *configuration*, not the token path.
    """
    from app.core.config import get_settings
    from app.main import create_app

    user_id = make_user(Role.admin)
    app = create_app()

    @app.get(ATTENDANT_FLOOR)
    def guarded(actor: Actor = Depends(require_role(Role.attendant))) -> dict[str, str]:
        return {"role": actor.role.value}

    unconfigured = get_settings().model_copy(update={"SUPABASE_JWT_SECRET": None})
    app.dependency_overrides[get_settings] = lambda: unconfigured

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get(ATTENDANT_FLOOR, headers=_auth(make_token(user_id)))

    assert response.status_code == 500
    assert response.json()["code"] == "AUTH_NOT_CONFIGURED"
