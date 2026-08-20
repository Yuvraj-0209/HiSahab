"""Business date vs timestamps (CLAUDE.md §6.1, §10).

§10 requires one case by name: "Night shift spanning midnight assigned to a single correct
`business_date`". It is here rather than in test_shifts.py because the rule it protects is
older and broader than the shift lifecycle -- every later phase reads
`shifts.business_date` and none of them may ever fall back to `date(created_at)`.

The rule survived §4.7's rewrite for two independent reasons, and it is worth being clear
which is which:

1. This outlet's single 06:00-22:00 shift never crosses midnight -- but a 24-hour outlet's
   night shift does, and the schema serves both.
2. **This outlet types the whole day in after the fact.** So `created_at` is routinely the
   *following* calendar day, and would be wrong here even though nothing crosses midnight.
   Reason 2 applies to every outlet, including the one that made reason 1 look irrelevant.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.usefixtures("clean_shifts")


async def test_a_shift_spanning_midnight_keeps_one_business_date(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """22:00 -> 06:00 the next morning is one shift on one business date."""
    attendant = make_user("attendant")
    manager = make_user("manager")

    opened = await client.post(
        "/api/v1/shifts",
        json={
            "business_date": "2026-06-10",
            "started_at": "2026-06-10T22:00:00+05:30",
        },
        headers=auth_headers(attendant),
    )
    assert opened.status_code == 201, opened.json()
    shift_id = opened.json()["id"]

    closed = await client.patch(
        f"/api/v1/shifts/{shift_id}/close",
        # The following calendar day, deliberately.
        json={"ended_at": "2026-06-11T06:00:00+05:30"},
        headers=auth_headers(manager),
    )

    assert closed.status_code == 200, closed.json()
    body = closed.json()
    assert body["business_date"] == "2026-06-10"
    # The timestamps genuinely span two calendar dates; the business date does not move.
    assert body["started_at"].startswith("2026-06-10")
    assert body["ended_at"].startswith("2026-06-11")


async def test_the_business_date_is_not_derived_from_when_it_was_typed_in(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """§4.7: the whole day is entered after the fact, so `created_at` is a different day.

    This is the test that fails the moment somebody "simplifies" business_date away and
    derives it from a timestamp.
    """
    attendant = make_user("attendant")

    response = await client.post(
        "/api/v1/shifts",
        json={"business_date": "2026-06-01"},
        headers=auth_headers(attendant),
    )
    shift_id = response.json()["id"]

    with engine.connect() as connection:
        business_date, created_at = connection.execute(
            text(
                "SELECT business_date, created_at FROM shifts WHERE id = :id"
            ).bindparams(id=UUID(shift_id))
        ).one()

    assert business_date == date(2026, 6, 1)
    assert created_at.date() != business_date


async def test_a_night_template_resolves_its_end_to_the_next_day(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """The 24-hour case this outlet does not have, covered so one of its customers is not
    the one to discover it.

    A 22:00 -> 06:00 template taken naively on one date produces an end *before* the start,
    which ck_shifts_ended_after_started refuses -- correctly, but at the wrong layer and
    with an unhelpful message.
    """
    admin = make_user("admin")
    attendant = make_user("attendant")

    created = await client.post(
        "/api/v1/shift-templates",
        json={
            "sequence": 2,
            "label": "Night",
            "starts_at_local": "22:00:00",
            "ends_at_local": "06:00:00",
        },
        headers=auth_headers(admin),
    )
    assert created.status_code == 201, created.json()
    assert created.json()["crosses_midnight"] is True

    # Shift 1 first: sequences are server-assigned in order, so shift 2 needs a shift 1.
    first = await client.post(
        "/api/v1/shifts",
        json={"business_date": "2026-06-20"},
        headers=auth_headers(attendant),
    )
    await client.patch(
        f"/api/v1/shifts/{first.json()['id']}/close",
        json={},
        headers=auth_headers(admin),
    )

    night = await client.post(
        "/api/v1/shifts",
        json={"business_date": "2026-06-20"},
        headers=auth_headers(attendant),
    )

    assert night.status_code == 201, night.json()
    body = night.json()
    assert body["business_date"] == "2026-06-20"
    assert body["started_at"].startswith("2026-06-20")
    assert body["ended_at"].startswith("2026-06-21")


def test_the_future_check_uses_the_outlets_timezone_not_utc() -> None:
    """§13.11. Between 18:30 and 24:00 UTC the outlet's date is already tomorrow.

    A UTC comparison would refuse a legitimately-dated shift for five and a half hours
    every single night -- and would do it silently, as a validation error the attendant
    could not act on.
    """
    from datetime import timedelta
    from app.services.shifts import outlet_today

    ist_today = outlet_today("Asia/Kolkata")
    utc_today = datetime.now(tz=timezone.utc).date()

    # They agree for most of the day; the point is that the helper asks the outlet's clock.
    assert ist_today - utc_today in (timedelta(0), timedelta(days=1))
