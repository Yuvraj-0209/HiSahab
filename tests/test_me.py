"""GET /api/v1/me -- the only protected endpoint in Phase 2."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from httpx import AsyncClient


async def test_me_returns_the_callers_own_profile_and_role(
    client: AsyncClient, make_token: Callable[..., str], make_user: Callable[..., UUID]
) -> None:
    from app.core.config import get_settings

    user_id = make_user(
        "manager", full_name="Yuvraj Dhamija", phone="9876543210"
    )

    response = await client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {make_token(user_id)}"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": str(user_id),
        "full_name": "Yuvraj Dhamija",
        "phone": "9876543210",
        "outlet_id": str(get_settings().DEFAULT_OUTLET_ID),
        "role": "manager",
    }


async def test_me_never_exposes_an_email(
    client: AsyncClient, make_token: Callable[..., str], make_user: Callable[..., UUID]
) -> None:
    """§5.1: email lives in Supabase auth.users and is deliberately not mirrored here."""
    user_id = make_user("attendant")

    response = await client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {make_token(user_id)}"}
    )

    assert "email" not in response.json()


async def test_me_requires_authentication(client: AsyncClient) -> None:
    response = await client.get("/api/v1/me")

    assert response.status_code == 401
    assert response.json()["code"] == "NOT_AUTHENTICATED"


async def test_me_reports_a_null_phone_rather_than_omitting_it(
    client: AsyncClient, make_token: Callable[..., str], make_user: Callable[..., UUID]
) -> None:
    """Phone is nullable (§5.1); the response shape must stay stable regardless."""
    user_id = make_user("attendant", phone=None)

    response = await client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {make_token(user_id)}"}
    )

    assert response.json()["phone"] is None
