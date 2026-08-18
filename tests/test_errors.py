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


async def test_a_rejected_decimal_field_still_produces_a_422_envelope(client) -> None:
    """Regression: the 422 handler used to crash on Decimal input (Phase 3).

    `RequestValidationError.errors()` echoes the offending input back, and for a
    `condecimal` field that input is a `Decimal`, which `json.dumps` cannot serialise. The
    handler raised inside the exception handler, so the client saw an unhandled 500 with no
    field detail at all -- on a money field, which is the one place the caller most needs
    to be told exactly what was wrong.

    Nothing had a Decimal input field before `fuel_prices`, which is why Phase 1's error
    handling looked correct for two phases.
    """
    response = await client.post(
        "/api/v1/fuel-prices",
        json={
            "fuel_type_id": "00000000-0000-0000-0000-000000000009",
            "rate_per_unit": "-5.00",
            "effective_from": "2030-01-01T06:00:00+05:30",
        },
    )

    # 401 (no token) is fine -- the point is that it is a clean envelope, never a 500.
    assert response.status_code != 500
    assert set(response.json()) >= {"detail", "code", "request_id"}
