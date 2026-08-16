"""Error envelope shape (CLAUDE.md §3 rule 10, §9)."""

from __future__ import annotations

from httpx import AsyncClient


async def test_unknown_route_uses_the_standard_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"detail", "code", "request_id"}
    assert body["code"] == "HTTP_404"


async def test_error_envelope_returns_the_request_id(client: AsyncClient) -> None:
    """§9 requires the request_id in error responses so a user can quote it."""
    response = await client.get(
        "/api/v1/does-not-exist", headers={"X-Request-ID": "trace-me"}
    )

    assert response.json()["request_id"] == "trace-me"
    assert response.headers["X-Request-ID"] == "trace-me"
