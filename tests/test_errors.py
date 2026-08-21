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


# --- database constraint failures (Phase 6 Step 0) ---------------------------


async def test_an_unmapped_constraint_failure_is_still_a_500() -> None:
    """The IntegrityError handler allowlists; it must never blanket-convert.

    A constraint nobody anticipated firing means the application let through something it
    should have refused -- that is a bug. Turning it into a tidy 409 would invite the
    client to retry a write that will never succeed, and would drop it out of the logs as
    a handled response. So an unrecognised constraint keeps the loud 500 it had before,
    and only names listed in `_CONSTRAINT_ERRORS` get the §3 rule 10 treatment.
    """
    from fastapi import Request
    from sqlalchemy.exc import IntegrityError

    from app.core.errors import integrity_error_handler

    request = Request({"type": "http", "headers": [], "method": "POST", "path": "/"})
    exc = IntegrityError(
        "INSERT ...", {}, Exception('violates check constraint "ck_something_nobody_mapped"')
    )

    response = await integrity_error_handler(request, exc)

    assert response.status_code == 500


async def test_a_mapped_constraint_failure_uses_the_standard_envelope() -> None:
    """The other half of the pair: a listed constraint becomes a readable error."""
    import json

    from fastapi import Request
    from sqlalchemy.exc import IntegrityError

    from app.core.errors import integrity_error_handler

    request = Request({"type": "http", "headers": [], "method": "POST", "path": "/"})
    exc = IntegrityError(
        "INSERT ...",
        {},
        Exception('violates check constraint "ck_nozzle_readings_flags_exclusive"'),
    )

    response = await integrity_error_handler(request, exc)
    body = json.loads(response.body)

    assert response.status_code == 422
    assert set(body) == {"detail", "code", "request_id"}
    assert body["code"] == "METER_FLAGS_MUTUALLY_EXCLUSIVE"


# --- reversal races (Phase 8 Step 0) -----------------------------------------


async def test_a_concurrent_double_reversal_of_an_expense_is_a_409_not_a_500() -> None:
    """P7-1: `uq_expenses_reverses_id` was never added to `_CONSTRAINT_ERRORS`.

    Phase 6 Step 0 found and fixed exactly this on `collections`: the service-level
    ALREADY_REVERSED check passes for both callers when two managers reverse the same row
    at the same instant, and the unique index refuses the second INSERT. Phase 7 copied the
    reversal *shape* onto `expenses` -- unique constraint included -- but not the allowlist
    entry that makes the race readable, so the losing manager got an opaque 500 and no way
    to tell whether their reversal had landed.
    """
    import json

    from fastapi import Request
    from sqlalchemy.exc import IntegrityError

    from app.core.errors import integrity_error_handler

    request = Request({"type": "http", "headers": [], "method": "POST", "path": "/"})
    exc = IntegrityError(
        "INSERT ...",
        {},
        Exception('duplicate key value violates unique constraint "uq_expenses_reverses_id"'),
    )

    response = await integrity_error_handler(request, exc)
    body = json.loads(response.body)

    assert response.status_code == 409
    assert body["code"] == "ALREADY_REVERSED"


def test_every_reversal_unique_constraint_is_mapped_to_a_business_error(engine) -> None:
    """The structural form of the test above, so Phase 9 cannot repeat it.

    §6.9's correction shape is being copied onto one table per phase -- `collections`
    (Phase 6), `expenses` (Phase 7), `credit_sales` next -- and each copy brings a
    `uq_<table>_reverses_id` unique constraint whose whole purpose is to lose a race
    loudly. A constraint that can only be reached by a race is *unreachable through
    single-request testing*, which is precisely why it keeps being forgotten: nothing fails
    until two people click at once in production.

    Asserting over `pg_constraint` rather than a hand-written list means the next table to
    grow a reversal gets this check for free, on the day its migration lands.
    """
    from sqlalchemy import text

    from app.core.errors import _CONSTRAINT_ERRORS

    with engine.connect() as connection:
        names = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE contype = 'u' AND conname ~ '^uq_.*_reverses_id$'"
                )
            )
        }

    assert names, "expected at least one reversal unique constraint to exist"
    assert names - set(_CONSTRAINT_ERRORS) == set(), (
        "every uq_<table>_reverses_id must map to a business error in _CONSTRAINT_ERRORS, "
        "or a concurrent double reversal returns an opaque 500"
    )
