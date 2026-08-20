"""The shift lifecycle (CLAUDE.md §5.2, §6.8, §4.7).

Every test drives the real HTTP API with a real token, per the policy in
tests/test_permissions.py: overriding get_current_user would skip the verification logic
that most needs testing.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.usefixtures("clean_shifts")

# This outlet trades 06:00-22:00 IST (§4.7). Those are 00:30 and 16:30 UTC.
EXPECTED_START_UTC = "2026-03-10T00:30:00Z"
EXPECTED_END_UTC = "2026-03-10T16:30:00Z"
ON = "2026-03-10"


def _instant(text_value: str) -> datetime:
    return datetime.fromisoformat(text_value.replace("Z", "+00:00"))


async def _open(
    client: AsyncClient, headers: dict[str, str], **payload: object
) -> object:
    body = {"business_date": ON} | payload
    return await client.post("/api/v1/shifts", json=body, headers=headers)


# --- opening -----------------------------------------------------------------


async def test_opening_a_shift_uses_the_outlets_trading_window(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """The template supplies 06:00-22:00 IST so nobody retypes it every morning (§5.1)."""
    attendant = make_user("attendant")

    response = await _open(client, auth_headers(attendant))

    assert response.status_code == 201
    body = response.json()
    assert body["sequence"] == 1
    assert body["status"] == "open"
    assert body["attendant_id"] == str(attendant)
    assert _instant(body["started_at"]) == _instant(EXPECTED_START_UTC)
    assert _instant(body["ended_at"]) == _instant(EXPECTED_END_UTC)


async def test_sequences_are_assigned_by_the_server_in_order(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """§4.7: a day has as many shifts as it has, numbered 1, 2, 3 ...

    Each must be closed before the next opens, which is the §5.2 one-open-shift rule.
    """
    attendant = make_user("attendant")
    manager = make_user("manager")

    # Shift 1 gets its times from the seeded 06:00-22:00 template. Shifts 2 and 3 have no
    # template of their own, so they start where the previous one ended (§4.7's chain
    # applied to the clock) and their end has to be stated at close -- there is nothing to
    # infer it from, and inventing one would be a guess about when trading stopped.
    for expected, ends_at in (
        (1, None),
        (2, "2026-03-10T23:00:00+05:30"),
        (3, "2026-03-11T00:30:00+05:30"),
    ):
        opened = await _open(client, auth_headers(attendant))
        assert opened.status_code == 201, opened.json()
        assert opened.json()["sequence"] == expected

        closed = await client.patch(
            f"/api/v1/shifts/{opened.json()['id']}/close",
            json={} if ends_at is None else {"ended_at": ends_at},
            headers=auth_headers(manager),
        )
        assert closed.status_code == 200, closed.json()


async def test_a_chained_shift_starts_where_the_previous_one_ended(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """§4.7's chain applied to the clock, not just the totalizer.

    The outlet has one template (06:00-22:00). A second shift tacked on after it has no
    template of its own and should not need one -- it starts when shift 1 finished.
    """
    attendant = make_user("attendant")
    manager = make_user("manager")

    first = (await _open(client, auth_headers(attendant))).json()
    await client.patch(
        f"/api/v1/shifts/{first['id']}/close", json={}, headers=auth_headers(manager)
    )

    second = await _open(client, auth_headers(attendant))

    assert second.status_code == 201, second.json()
    assert second.json()["sequence"] == 2
    assert _instant(second.json()["started_at"]) == _instant(first["ended_at"])
    # Genuinely unknown until it closes, so it is left null rather than invented.
    assert second.json()["ended_at"] is None


async def test_closing_a_chained_shift_requires_an_end_time(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    first = (await _open(client, auth_headers(attendant))).json()["id"]
    await client.patch(
        f"/api/v1/shifts/{first}/close", json={}, headers=auth_headers(manager)
    )
    second = (await _open(client, auth_headers(attendant))).json()["id"]

    response = await client.patch(
        f"/api/v1/shifts/{second}/close", json={}, headers=auth_headers(manager)
    )

    assert response.status_code == 422
    assert response.json()["code"] == "SHIFT_END_TIME_REQUIRED"


async def test_the_client_cannot_choose_the_sequence(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """extra="forbid" turns this into a 422 rather than a silent no-op.

    A client-chosen sequence could overwrite or skip a link in §4.7's chain.
    """
    attendant = make_user("attendant")

    response = await _open(client, auth_headers(attendant), sequence=7)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_only_one_shift_may_be_open_at_a_time(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """§5.2. With two open, "the most recent closing reading" has no single answer."""
    attendant = make_user("attendant")
    first = await _open(client, auth_headers(attendant))
    assert first.status_code == 201

    second = await _open(client, auth_headers(attendant))

    assert second.status_code == 409
    assert second.json()["code"] == "SHIFT_ALREADY_OPEN"


async def test_a_shift_cannot_be_spliced_into_the_middle_of_the_chain(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    make_shift: Callable[..., UUID],
) -> None:
    """A shift before the chain's tip would leave the next one two predecessors."""
    attendant = make_user("attendant")
    make_shift(attendant, business_date=date(2026, 3, 10), sequence=1, status="closed")

    response = await _open(client, auth_headers(attendant), business_date="2026-03-09")

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_OUT_OF_SEQUENCE"


async def test_a_future_business_date_is_refused(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """§6.1: trading has not happened yet, so it is always a typo."""
    attendant = make_user("attendant")
    tomorrow = (date.today() + timedelta(days=400)).isoformat()

    response = await _open(client, auth_headers(attendant), business_date=tomorrow)

    assert response.status_code == 422
    assert response.json()["code"] == "BUSINESS_DATE_IN_FUTURE"


async def test_todays_date_is_accepted_at_the_boundary(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """The future check is evaluated in the outlet's timezone, never UTC (§13.11).

    In UTC, "today at this outlet" is still yesterday between 18:30 and 24:00 UTC, so a
    UTC comparison would refuse a legitimate shift for five and a half hours every night.
    """
    from app.core.config import get_settings
    from app.services.shifts import outlet_today

    attendant = make_user("attendant")
    today = outlet_today(get_settings().TZ_DISPLAY)

    response = await _open(
        client,
        auth_headers(attendant),
        business_date=today.isoformat(),
        started_at=f"{today.isoformat()}T06:00:00+05:30",
    )

    assert response.status_code == 201, response.json()


async def test_an_explicit_start_overrides_the_template(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """For the day the station opened late. The value is stored on the shift row."""
    attendant = make_user("attendant")

    response = await _open(
        client, auth_headers(attendant), started_at=f"{ON}T07:15:00+05:30"
    )

    assert response.status_code == 201
    assert _instant(response.json()["started_at"]) == _instant("2026-03-10T01:45:00Z")


async def test_a_naive_start_is_refused_rather_than_assumed(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """Guessing UTC for a value meant as IST moves a shift 5.5 hours -- across the 06:00
    price revision, revaluing the whole day (§6.3)."""
    attendant = make_user("attendant")

    response = await _open(client, auth_headers(attendant), started_at=f"{ON}T06:00:00")

    assert response.status_code == 422


async def test_a_shift_cannot_end_before_it_starts(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    attendant = make_user("attendant")

    response = await _open(
        client,
        auth_headers(attendant),
        started_at=f"{ON}T06:00:00+05:30",
        ended_at=f"{ON}T05:00:00+05:30",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "SHIFT_ENDS_BEFORE_IT_STARTS"


async def test_a_shift_cannot_be_opened_for_someone_outside_the_outlet(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    manager = make_user("manager")
    stranger = make_user("attendant", with_membership=False)

    response = await _open(
        client, auth_headers(manager), attendant_id=str(stranger)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ATTENDANT_NOT_AT_OUTLET"


async def test_a_manager_may_open_a_shift_in_an_attendants_name(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """Days are typed in after the fact (§4.7), so somebody else routinely enters them."""
    manager = make_user("manager")
    attendant = make_user("attendant")

    response = await _open(client, auth_headers(manager), attendant_id=str(attendant))

    assert response.status_code == 201
    assert response.json()["attendant_id"] == str(attendant)


# --- closing, locking, reopening ---------------------------------------------


async def test_closing_stamps_who_and_when(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift_id = (await _open(client, auth_headers(attendant))).json()["id"]

    response = await client.patch(
        f"/api/v1/shifts/{shift_id}/close", json={}, headers=auth_headers(manager)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "closed"
    assert body["closed_by"] == str(manager)
    assert body["closed_at"] is not None


async def test_closing_an_already_closed_shift_is_refused(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift_id = (await _open(client, auth_headers(attendant))).json()["id"]
    await client.patch(
        f"/api/v1/shifts/{shift_id}/close", json={}, headers=auth_headers(manager)
    )

    response = await client.patch(
        f"/api/v1/shifts/{shift_id}/close", json={}, headers=auth_headers(manager)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_NOT_OPEN"


async def test_an_open_shift_cannot_be_locked(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """§6.8: locking is the step *after* a manager has reconciled and closed."""
    attendant = make_user("attendant")
    admin = make_user("admin")
    shift_id = (await _open(client, auth_headers(attendant))).json()["id"]

    response = await client.patch(
        f"/api/v1/shifts/{shift_id}/lock", headers=auth_headers(admin)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_NOT_CLOSED"


async def test_locking_a_closed_shift_stamps_who_and_when(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    admin = make_user("admin")
    shift_id = (await _open(client, auth_headers(attendant))).json()["id"]
    await client.patch(
        f"/api/v1/shifts/{shift_id}/close", json={}, headers=auth_headers(manager)
    )

    response = await client.patch(
        f"/api/v1/shifts/{shift_id}/lock", headers=auth_headers(admin)
    )

    assert response.status_code == 200
    assert response.json()["status"] == "locked"
    assert response.json()["locked_by"] == str(admin)


async def test_a_locked_shift_is_final(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """§6.8: if locked could be reopened, locking would guarantee nothing."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    admin = make_user("admin")
    shift_id = (await _open(client, auth_headers(attendant))).json()["id"]
    await client.patch(
        f"/api/v1/shifts/{shift_id}/close", json={}, headers=auth_headers(manager)
    )
    await client.patch(f"/api/v1/shifts/{shift_id}/lock", headers=auth_headers(admin))

    response = await client.patch(
        f"/api/v1/shifts/{shift_id}/reopen",
        json={"reason": "manager closed it too early"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_LOCKED"


async def test_reopening_clears_the_close_stamps(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    admin = make_user("admin")
    shift_id = (await _open(client, auth_headers(attendant))).json()["id"]
    await client.patch(
        f"/api/v1/shifts/{shift_id}/close", json={}, headers=auth_headers(manager)
    )

    response = await client.patch(
        f"/api/v1/shifts/{shift_id}/reopen",
        json={"reason": "closing reading was mistyped"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "open"
    assert body["closed_by"] is None
    assert body["closed_at"] is None


async def test_reopening_without_a_reason_is_refused(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """§5.2: a backwards transition with no stated reason defeats the audit log."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    admin = make_user("admin")
    shift_id = (await _open(client, auth_headers(attendant))).json()["id"]
    await client.patch(
        f"/api/v1/shifts/{shift_id}/close", json={}, headers=auth_headers(manager)
    )

    blank = await client.patch(
        f"/api/v1/shifts/{shift_id}/reopen",
        json={"reason": ""},
        headers=auth_headers(admin),
    )
    missing = await client.patch(
        f"/api/v1/shifts/{shift_id}/reopen", json={}, headers=auth_headers(admin)
    )

    assert blank.status_code == 422
    assert missing.status_code == 422


async def test_a_mid_chain_shift_can_be_reopened(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """§13.10, **as revised in Phase 5.** This test previously asserted the opposite.

    Phase 4 refused a mid-chain reopen with 409 NOT_THE_LATEST_SHIFT, because §13.10 said
    Phase 5 would lift the restriction by recomputing the following shift's carried-forward
    opening. Phase 5 established that it must not: §4.7 stores the chained opening on the
    row *precisely* so that correcting one shift cannot silently rewrite the next shift's
    history, and a recomputing cascade is that rewrite.

    So the restriction was lifted the other way -- reopen freely, and flag the downstream
    reading rather than recomputing it. The flagging half is asserted in
    tests/test_reading_chain.py::test_changing_a_closing_reading_flags_the_next_shift.

    Recorded here rather than deleted, because a reader comparing this file against
    CLAUDE.md §13.10 needs to know the behaviour changed deliberately and why.
    """
    attendant = make_user("attendant")
    manager = make_user("manager")
    admin = make_user("admin")

    first = (await _open(client, auth_headers(attendant))).json()["id"]
    await client.patch(
        f"/api/v1/shifts/{first}/close", json={}, headers=auth_headers(manager)
    )
    second = (await _open(client, auth_headers(attendant))).json()["id"]
    await client.patch(
        f"/api/v1/shifts/{second}/close",
        json={"ended_at": "2026-03-10T23:30:00+05:30"},
        headers=auth_headers(manager),
    )

    mid_chain = await client.patch(
        f"/api/v1/shifts/{first}/reopen",
        json={"reason": "wrong reading"},
        headers=auth_headers(admin),
    )

    assert mid_chain.status_code == 200
    assert mid_chain.json()["status"] == "open"
    assert mid_chain.json()["closed_at"] is None


# --- reads --------------------------------------------------------------------


async def test_current_returns_the_open_shift_and_404s_when_there_is_none(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")

    empty = await client.get("/api/v1/shifts/current", headers=auth_headers(attendant))
    assert empty.status_code == 404
    assert empty.json()["code"] == "NO_OPEN_SHIFT"

    shift_id = (await _open(client, auth_headers(attendant))).json()["id"]
    found = await client.get("/api/v1/shifts/current", headers=auth_headers(manager))

    assert found.status_code == 200
    assert found.json()["id"] == shift_id


async def test_an_unknown_shift_is_a_404_not_a_permission_error(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """The outlet resolver 404s before the role check, per app/api/deps.py."""
    attendant = make_user("attendant")

    response = await client.get(
        f"/api/v1/shifts/{uuid4()}", headers=auth_headers(attendant)
    )

    assert response.status_code == 404
    assert response.json()["code"] == "SHIFT_NOT_FOUND"


async def test_the_list_pages_by_keyset_without_repeating_a_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    make_shift: Callable[..., UUID],
) -> None:
    """§9 forbids OFFSET: an insert mid-walk would duplicate one row and skip another."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    for day in range(1, 6):
        make_shift(
            attendant,
            business_date=date(2026, 3, day),
            sequence=1,
            status="closed",
        )

    first = await client.get(
        "/api/v1/shifts?limit=2", headers=auth_headers(manager)
    )
    body = first.json()
    assert first.status_code == 200
    assert [item["business_date"] for item in body["items"]] == [
        "2026-03-05",
        "2026-03-04",
    ]
    assert body["next_cursor"] is not None

    second = await client.get(
        f"/api/v1/shifts?limit=2&cursor={body['next_cursor']}",
        headers=auth_headers(manager),
    )
    assert [item["business_date"] for item in second.json()["items"]] == [
        "2026-03-03",
        "2026-03-02",
    ]


async def test_a_mangled_cursor_is_refused_rather_than_restarting(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """Silently restarting a page walk is how a reader sees the same rows twice."""
    manager = make_user("manager")

    response = await client.get(
        "/api/v1/shifts?cursor=not-a-cursor", headers=auth_headers(manager)
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_CURSOR"


# --- remaining guards ----------------------------------------------------------


async def test_a_naive_end_time_is_refused_at_close(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """Same reasoning as `started_at`: guessing a timezone moves the shift 5.5 hours."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift_id = (await _open(client, auth_headers(attendant))).json()["id"]

    response = await client.patch(
        f"/api/v1/shifts/{shift_id}/close",
        json={"ended_at": f"{ON}T22:00:00"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 422


async def test_closing_with_an_end_before_the_start_is_refused(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """The API refuses it with a named code rather than letting
    ck_shifts_ended_after_started surface as a 500."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift_id = (await _open(client, auth_headers(attendant))).json()["id"]

    response = await client.patch(
        f"/api/v1/shifts/{shift_id}/close",
        json={"ended_at": f"{ON}T05:00:00+05:30"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "SHIFT_ENDS_BEFORE_IT_STARTS"


async def test_an_outlet_with_no_template_must_state_the_start_time(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """A shift with no template and no predecessor is refused rather than defaulted to
    `now()`.

    Days are typed in after the fact (§4.7), so `now()` is routinely the wrong day
    entirely -- and a silently wrong `started_at` picks the wrong fuel rate (§6.3) with no
    error anywhere. Refusing is recoverable; a plausible wrong instant is not.
    """
    attendant = make_user("attendant")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE outlet_shift_templates SET is_active = false")
        )
    try:
        refused = await _open(client, auth_headers(attendant))
        accepted = await _open(
            client, auth_headers(attendant), started_at=f"{ON}T06:00:00+05:30"
        )
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE outlet_shift_templates SET is_active = true")
            )

    assert refused.status_code == 422
    assert refused.json()["code"] == "SHIFT_START_TIME_REQUIRED"
    assert accepted.status_code == 201


async def test_current_hides_another_attendants_open_shift(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """§8: an attendant reads their own shift, not whichever one happens to be open."""
    owner = make_user("attendant")
    other = make_user("attendant")
    await _open(client, auth_headers(owner))

    response = await client.get("/api/v1/shifts/current", headers=auth_headers(other))

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_YOUR_SHIFT"


async def test_the_list_can_be_filtered_to_one_business_date(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    make_shift: Callable[..., UUID],
) -> None:
    """The "show me that day" view, which is how the owner reads the register."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    make_shift(attendant, business_date=date(2026, 3, 3), sequence=1, status="closed")
    make_shift(attendant, business_date=date(2026, 3, 4), sequence=1, status="closed")

    response = await client.get(
        "/api/v1/shifts?business_date=2026-03-04", headers=auth_headers(manager)
    )

    assert response.status_code == 200
    assert [item["business_date"] for item in response.json()["items"]] == ["2026-03-04"]
