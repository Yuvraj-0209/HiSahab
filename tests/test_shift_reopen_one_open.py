"""Reopening a shift must respect §5.2's one-open-shift rule.

Found in production on 17 November 2026. 6 November was open; an admin reopened 4 November
("ENTRIES MISSED") and the route allowed it, because only `POST /shifts` checked for an
open shift. With two open, `shift_service.open_shift` -- which assumes at most one --
raised MultipleResultsFound, and Today and Cash both failed with a 500 on
`GET /shifts/current`.

Reopen now refuses the same way opening does, with 409 `SHIFT_ALREADY_OPEN`, and the
refusal leaves the shift closed and writes no audit row.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.usefixtures("clean_shifts")

REASON = "Entries missed"


async def _open(client: AsyncClient, headers: dict[str, str], business_date: str) -> str:
    response = await client.post(
        "/api/v1/shifts", json={"business_date": business_date}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _close(client: AsyncClient, shift_id: str, headers: dict[str, str]) -> None:
    response = await client.patch(
        f"/api/v1/shifts/{shift_id}/close", json={}, headers=headers
    )
    assert response.status_code == 200, response.text


def _status(engine: Engine, shift_id: str) -> str:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT status FROM shifts WHERE id = :id").bindparams(id=UUID(shift_id))
        ).scalar_one()


def _reopen_audit_rows(engine: Engine, shift_id: str) -> int:
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT count(*) FROM audit_logs WHERE table_name = 'shifts' "
                "AND record_id = :id AND action = 'status_change' "
                "AND new_values ->> 'status' = 'open'"
            ).bindparams(id=UUID(shift_id))
        ).scalar_one()


async def test_reopening_while_another_shift_is_open_is_refused(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers, engine: Engine
) -> None:
    manager = make_user("manager")
    admin = make_user("admin")
    earlier = await _open(client, auth_headers(manager), "2026-03-04")
    await _close(client, earlier, auth_headers(manager))
    await _open(client, auth_headers(manager), "2026-03-06")

    response = await client.patch(
        f"/api/v1/shifts/{earlier}/reopen",
        json={"reason": REASON},
        headers=auth_headers(admin),
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "SHIFT_ALREADY_OPEN"
    assert "2026-03-06" in body["detail"]
    assert _status(engine, earlier) == "closed"
    assert _reopen_audit_rows(engine, earlier) == 0


async def test_the_current_shift_still_resolves_after_a_refused_reopen(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """The production symptom: Today's request must not become a 500."""
    manager = make_user("manager")
    admin = make_user("admin")
    earlier = await _open(client, auth_headers(manager), "2026-03-04")
    await _close(client, earlier, auth_headers(manager))
    later = await _open(client, auth_headers(manager), "2026-03-06")

    await client.patch(
        f"/api/v1/shifts/{earlier}/reopen",
        json={"reason": REASON},
        headers=auth_headers(admin),
    )
    current = await client.get("/api/v1/shifts/current", headers=auth_headers(manager))

    assert current.status_code == 200, current.text
    assert current.json()["id"] == later


async def test_reopening_succeeds_once_the_other_shift_is_closed(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers, engine: Engine
) -> None:
    """The way out the 409 names: close the open shift, then reopen."""
    manager = make_user("manager")
    admin = make_user("admin")
    earlier = await _open(client, auth_headers(manager), "2026-03-04")
    await _close(client, earlier, auth_headers(manager))
    later = await _open(client, auth_headers(manager), "2026-03-06")
    await _close(client, later, auth_headers(manager))

    response = await client.patch(
        f"/api/v1/shifts/{earlier}/reopen",
        json={"reason": REASON},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200, response.text
    assert _status(engine, earlier) == "open"
