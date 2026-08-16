"""Request correlation (CLAUDE.md §9)."""

from __future__ import annotations

import uuid

from httpx import AsyncClient


async def test_inbound_request_id_is_echoed(client: AsyncClient) -> None:
    """A correlation id set by a proxy or mobile client must survive."""
    response = await client.get(
        "/api/v1/health", headers={"X-Request-ID": "abc123"}
    )

    assert response.headers["X-Request-ID"] == "abc123"


async def test_request_id_is_generated_when_absent(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    # Must be a real uuid4, not a placeholder.
    uuid.UUID(response.headers["X-Request-ID"])


async def test_request_ids_are_unique_per_request(client: AsyncClient) -> None:
    first = await client.get("/api/v1/health")
    second = await client.get("/api/v1/health")

    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]
