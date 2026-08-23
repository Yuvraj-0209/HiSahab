"""The two config reads a browser makes before it renders (CLAUDE.md §2, §8, §16).

Phase 12. What this file defends beyond "the endpoint returns 200":

* **The secret that must never be returned, asserted by name.** `SUPABASE_SERVICE_KEY` sits
  two lines from `SUPABASE_ANON_KEY` in `Settings`, is the same shape -- a long opaque string
  from the same dashboard -- and grants full database access. The test below asserts its
  *value* is absent from the serialised response rather than counting fields, because the
  failure mode is a field added years from now and a count assertion would simply be updated
  to match.
* **The authentication boundary between the two routes**, which is the reason there are two.
  `/auth-config` must work with no token (a login screen cannot authenticate); `/client-config`
  must not.
* **Money crosses as a string**, per §3 rule 1. A threshold that arrived as a JSON number
  would invite the client to compare it with a float, which is the bug the rule exists to
  prevent, one language further out (§14, §13.18).

## Why these tests build their own app

The `client` fixture's environment has no `SUPABASE_ANON_KEY` -- conftest sets a JWT secret
and a URL but deliberately not an anon key, because nothing before Phase 12 needed one. That
is *useful* here rather than inconvenient: it means the default test app exercises the
not-configured path for free, and the configured path is built explicitly by the helper below.
Each test therefore states the configuration it is testing instead of inheriting one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

# --- helpers ------------------------------------------------------------------


@asynccontextmanager
async def _client_with(**overrides: object) -> AsyncIterator[AsyncClient]:
    """An app whose `get_settings` returns the real settings with `overrides` applied.

    `model_copy` rather than `Settings(**overrides)` so every unrelated value -- the database
    URL above all -- keeps whatever conftest put in the environment. Constructing a fresh
    Settings would re-read the environment and quietly diverge the moment a test wanted to
    override something conftest also sets.
    """
    from app.api.deps import get_storage
    from app.core.config import get_settings
    from app.main import create_app
    from app.services.storage import LocalStorage

    settings = get_settings().model_copy(update=overrides)

    app = create_app(settings)
    # create_app takes the settings object, but the *dependency* resolves get_settings
    # independently, so both have to point at the same object or the routes read the
    # unmodified singleton.
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_storage] = lambda: LocalStorage(root="/tmp/hisahab-test")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


_ANON_KEY = "test-anon-key-public-by-design"
_SERVICE_KEY = "test-service-key-must-never-be-returned"


# --- /auth-config -------------------------------------------------------------


async def test_auth_config_needs_no_token() -> None:
    """A login screen cannot authenticate; requiring a token here cannot work."""
    async with _client_with(
        SUPABASE_URL="https://example.supabase.co", SUPABASE_ANON_KEY=_ANON_KEY
    ) as client:
        response = await client.get("/api/v1/auth-config")

    assert response.status_code == 200
    assert response.json() == {
        "supabase_url": "https://example.supabase.co",
        "supabase_anon_key": _ANON_KEY,
    }


async def test_auth_config_strips_a_trailing_slash_from_the_url() -> None:
    """The client concatenates `/auth/v1/token` onto this; a double slash is avoidable.

    Same normalisation `Settings.supabase_issuer` already applies, for the same reason.
    """
    async with _client_with(
        SUPABASE_URL="https://example.supabase.co/", SUPABASE_ANON_KEY=_ANON_KEY
    ) as client:
        response = await client.get("/api/v1/auth-config")

    assert response.status_code == 200
    assert response.json()["supabase_url"] == "https://example.supabase.co"


async def test_auth_config_never_returns_the_service_key() -> None:
    """The one assertion in this file that would matter at 3am.

    Asserted against the raw response text, so a service key leaking through *any* field
    name -- including one added later -- fails here rather than in production.
    """
    async with _client_with(
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_ANON_KEY=_ANON_KEY,
        SUPABASE_SERVICE_KEY=_SERVICE_KEY,
        SUPABASE_JWT_SECRET="test-jwt-secret-must-never-be-returned-abcdef",
    ) as client:
        response = await client.get("/api/v1/auth-config")

    assert response.status_code == 200
    body = response.text
    assert _SERVICE_KEY not in body
    assert "test-jwt-secret-must-never-be-returned-abcdef" not in body


@pytest.mark.parametrize(
    ("url", "anon"),
    [
        (None, _ANON_KEY),
        ("https://example.supabase.co", None),
        ("   ", _ANON_KEY),
        ("https://example.supabase.co", "   "),
    ],
)
async def test_auth_config_refuses_when_supabase_is_not_configured(
    url: str | None, anon: str | None
) -> None:
    """503 rather than an empty string.

    A frontend handed `{"supabase_url": ""}` fails deep inside a fetch to a malformed URL.
    Being told the server is not configured is a better answer, and matches how /health
    reports a database it cannot reach. Blank-but-present is treated as absent, because a
    half-filled .env is the realistic way this happens.
    """
    async with _client_with(SUPABASE_URL=url, SUPABASE_ANON_KEY=anon) as client:
        response = await client.get("/api/v1/auth-config")

    assert response.status_code == 503
    assert response.json()["code"] == "AUTH_NOT_CONFIGURED"


# --- /client-config -----------------------------------------------------------


async def test_client_config_returns_the_values_the_ui_must_not_hardcode(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict[str, str]],
) -> None:
    """§6.7 and §6.11 both say changing a threshold must not require a deploy."""
    user_id = make_user("attendant")

    response = await client.get(
        "/api/v1/client-config", headers=auth_headers(user_id)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tz_display"] == "Asia/Kolkata"
    assert body["expense_review_threshold"] == "1000.00"
    assert body["expense_receipt_threshold"] == "5000.00"
    assert body["max_upload_bytes"] == 5242880
    assert body["outlet_name"] == "Main Outlet"


async def test_client_config_serialises_money_as_a_string(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict[str, str]],
) -> None:
    """§3 rule 1 does not stop at the API boundary.

    A threshold arriving as a JSON number invites the client to compare it against a float.
    Asserted on the raw text because `response.json()` would have already converted it.
    """
    user_id = make_user("attendant")

    response = await client.get(
        "/api/v1/client-config", headers=auth_headers(user_id)
    )

    assert '"expense_review_threshold":"1000.00"' in response.text.replace(" ", "")
    assert isinstance(response.json()["expense_receipt_threshold"], str)
    assert Decimal(response.json()["expense_receipt_threshold"]) == Decimal("5000.00")


async def test_the_two_thresholds_are_reported_independently(
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict[str, str]],
) -> None:
    """§16: "Never fold them into one value."

    Moving one must not move the other. Every other test in this file would still pass if
    one dial sat behind both names, because both *default* figures are already correct --
    so this is the only test here that can catch that, and it moves them to two figures
    neither of which is a default.
    """
    user_id = make_user("attendant")

    async with _client_with(
        EXPENSE_REVIEW_THRESHOLD=Decimal("250.00"),
        EXPENSE_RECEIPT_THRESHOLD=Decimal("7500.00"),
    ) as client:
        response = await client.get(
            "/api/v1/client-config", headers=auth_headers(user_id)
        )

    assert response.status_code == 200
    body = response.json()
    assert body["expense_review_threshold"] == "250.00"
    assert body["expense_receipt_threshold"] == "7500.00"


async def test_client_config_requires_a_token(client: AsyncClient) -> None:
    """The counterpart to /auth-config's exemption: this one is not public."""
    response = await client.get("/api/v1/client-config")

    assert response.status_code == 401
    assert response.json()["code"] == "NOT_AUTHENTICATED"


async def test_client_config_is_readable_by_every_role(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict[str, str]],
) -> None:
    """Attendant floor. An attendant filing an expense needs the receipt threshold to be
    warned before the server refuses them -- §8's table lists no restriction here."""
    for role in ("attendant", "manager", "admin"):
        user_id = make_user(role)

        response = await client.get(
            "/api/v1/client-config", headers=auth_headers(user_id)
        )

        assert response.status_code == 200, role
        assert response.json()["tz_display"] == "Asia/Kolkata", role


async def test_client_config_never_returns_a_secret(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers: Callable[[UUID], dict[str, str]],
) -> None:
    """Asserted by field name, so adding one of these later fails here.

    `DATABASE_URL` included: it carries the database password, and "config endpoint" is
    exactly the place somebody would eventually think to expose a connection detail.
    """
    user_id = make_user("admin")

    response = await client.get(
        "/api/v1/client-config", headers=auth_headers(user_id)
    )

    body = response.json()
    for forbidden in (
        "supabase_service_key",
        "supabase_jwt_secret",
        "supabase_anon_key",
        "database_url",
        "default_outlet_id",
    ):
        assert forbidden not in body, forbidden
