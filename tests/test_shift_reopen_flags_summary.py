"""Reopening a shift beneath a reconciled day (CLAUDE.md §5.2, §6.5, §13.10, §13.16).

§13.10's rule, one table further on. Phase 5 established it for the nozzle chain: a mid-chain
reopen **flags** the downstream reading and leaves it exactly as it was, because §4.7 stores
the carried opening on the row precisely so that correcting one shift cannot silently rewrite
the next shift's history.

The daily summary has the same shape and one extra reason to obey the rule. §5.2 stores
`expected_closing` so that "what did the system tell the manager on the day" survives a later
correction -- and §6.5 chains days, so a recomputed figure would not stay local. It would
propagate into every opening balance after it, and none of them would look wrong.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.anyio

DAY = date(2026, 7, 1)


def _window(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, 0, 30, tzinfo=timezone.utc)
    return start, start + timedelta(hours=16)


@pytest.fixture
def reconciled_day(client, make_shift, make_user, auth_headers):
    """A closed shift with a stored summary, ready to be disturbed."""

    async def _build(day: date, *, opening: str = "5000.00", status: str = "closed"):
        admin = make_user("admin")
        attendant = make_user("attendant")
        started_at, ended_at = _window(day)
        shift = make_shift(
            attendant,
            business_date=day,
            sequence=1,
            started_at=started_at,
            ended_at=ended_at,
            status=status,
        )
        created = await client.post(
            "/api/v1/daily-summaries",
            json={"business_date": day.isoformat(), "opening_balance": opening},
            headers=auth_headers(admin),
        )
        assert created.status_code == 201, created.text
        return admin, shift, created.json()

    return _build


async def test_reopening_a_shift_flags_the_day_and_changes_no_figure(
    client: AsyncClient,
    reconciled_day,
    auth_headers,
) -> None:
    """§13.16. The stale figures stay, visibly stale, with a note naming the shift that moved
    beneath them. A human reconciles numbers they can both see; nothing is invented."""
    admin, shift, before = await reconciled_day(DAY)

    reopened = await client.patch(
        f"/api/v1/shifts/{shift}/reopen",
        json={"reason": "A collection was recorded against the wrong mode"},
        headers=auth_headers(admin),
    )
    assert reopened.status_code == 200

    after = await client.get(
        f"/api/v1/daily-summaries/{DAY.isoformat()}", headers=auth_headers(admin)
    )

    assert after.json()["requires_review"] is True
    assert "reopened" in after.json()["review_note"]
    # Every stored figure is byte-identical.
    for field in (
        "expected_closing",
        "opening_balance",
        "metered_fuel_sales",
        "non_fuel_sales_total",
        "card_total",
        "cash_expenses",
        "bank_deposits_total",
        "shortfalls_booked",
    ):
        assert after.json()[field] == before[field], field


async def test_a_finalised_days_shifts_cannot_be_reopened_at_all(
    client: AsyncClient,
    reconciled_day,
    auth_headers,
) -> None:
    """The §13.16 case that **cannot arise**, and why -- worth a test because the reasoning
    lives in two sections that never mention each other.

    Finalising requires every shift on the date to be `locked`, and §6.8 makes `locked`
    terminal: an admin may move `closed -> open`, never `locked -> open`. So a shift beneath
    a *finalised* day is unreachable by the reopen route, and the flag can only ever fire on
    a summary that has been created but not yet finalised.

    That is a happy accident of two independent rules rather than something either one
    intended, so it is pinned here. If `locked` ever stops being terminal, this test fails
    and whoever changed it has to decide what a reopen means underneath a frozen day.
    """
    day = date(2026, 7, 2)
    admin, shift, _ = await reconciled_day(day, status="locked")
    finalised = await client.patch(
        f"/api/v1/daily-summaries/{day.isoformat()}/finalise",
        headers=auth_headers(admin),
    )
    assert finalised.json()["is_finalised"] is True

    response = await client.patch(
        f"/api/v1/shifts/{shift}/reopen",
        json={"reason": "Trying to reopen underneath a frozen day"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_LOCKED"

    summary = await client.get(
        f"/api/v1/daily-summaries/{day.isoformat()}", headers=auth_headers(admin)
    )
    assert summary.json()["requires_review"] is False


async def test_two_reopens_append_both_notes(
    client: AsyncClient,
    reconciled_day,
    auth_headers,
) -> None:
    """A day whose shifts are reopened twice is exactly the case somebody has to reconstruct.
    Overwriting would keep the most recent question and delete the first."""
    day = date(2026, 7, 3)
    admin, shift, _ = await reconciled_day(day)

    for reason in ("First correction", "Second correction"):
        await client.patch(
            f"/api/v1/shifts/{shift}/reopen",
            json={"reason": reason},
            headers=auth_headers(admin),
        )
        await client.patch(
            f"/api/v1/shifts/{shift}/close",
            json={},
            headers=auth_headers(admin),
        )

    summary = await client.get(
        f"/api/v1/daily-summaries/{day.isoformat()}", headers=auth_headers(admin)
    )

    assert summary.json()["review_note"].count("reopened") == 2


async def test_a_reopen_with_no_summary_yet_is_the_ordinary_case(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """Most reopens happen long before anybody reconciles the day, so finding no summary is
    normal rather than an error."""
    admin = make_user("admin")
    attendant = make_user("attendant")
    day = date(2026, 7, 4)
    started_at, ended_at = _window(day)
    shift = make_shift(
        attendant,
        business_date=day,
        sequence=1,
        started_at=started_at,
        ended_at=ended_at,
        status="closed",
    )

    response = await client.patch(
        f"/api/v1/shifts/{shift}/reopen",
        json={"reason": "Readings were entered against the wrong nozzle"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200


async def test_the_flag_is_audit_logged_against_the_summary(
    client: AsyncClient,
    reconciled_day,
    auth_headers,
    engine: Engine,
) -> None:
    """The audit row carries `expected_closing` as it stood, so the trail records what was
    frozen at the moment somebody disturbed the day -- not merely that they did."""
    day = date(2026, 7, 5)
    admin, shift, before = await reconciled_day(day)

    await client.patch(
        f"/api/v1/shifts/{shift}/reopen",
        json={"reason": "Deposit was against the wrong day"},
        headers=auth_headers(admin),
    )

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT new_values FROM audit_logs "
                "WHERE table_name = 'daily_cash_summaries' AND action = 'update' "
                "ORDER BY changed_at DESC LIMIT 1"
            )
        ).scalar_one()

    assert row["requires_review"] is True
    assert row["reopened_shift_id"] == str(shift)
    assert Decimal(row["expected_closing"]) == Decimal(before["expected_closing"])


async def test_the_review_flag_and_note_are_constrained_together(
    engine: Engine,
    make_user: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
) -> None:
    """Mirrors `nozzle_readings`' review columns: a flag with no note is one nobody can act
    on, because the question it was raising was never written down."""
    from sqlalchemy.exc import IntegrityError

    admin = make_user("admin")
    summary = make_daily_summary(business_date=date(2026, 7, 6), created_by=admin)

    with engine.begin() as connection, pytest.raises(IntegrityError) as exc:
        connection.execute(
            text(
                "UPDATE daily_cash_summaries SET requires_review = true WHERE id = :id"
            ).bindparams(id=summary)
        )

    assert "ck_daily_cash_summaries_review_has_note" in str(exc.value)
