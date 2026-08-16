"""Health endpoint behaviour."""

from __future__ import annotations

from httpx import AsyncClient


async def test_health_reports_ok(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_health_carries_a_request_id_header(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.headers.get("X-Request-ID")
