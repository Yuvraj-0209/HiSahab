"""The daily summary's lifecycle (CLAUDE.md §5.2, §6.1, §6.4, §6.5, §8, §13.16).

`test_cash_engine.py` covers the arithmetic. This file covers the rules around it: who may
create, count, finalise and unfinalise a day, what must be true first, and what must never
change afterwards.

The rule worth stating twice is §6.4's: **the variance is recorded, never auto-corrected.**
A count that disagrees does not "fix" `expected_closing` -- the disagreement *is* the signal,
and overwriting it would delete the only thing the count was taken to produce.
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

DAY = date(2026, 6, 1)


def _window(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, 0, 30, tzinfo=timezone.utc)
    return start, start + timedelta(hours=16)


@pytest.fixture
def settled_shift(make_shift):
    """A shift on `day` in whatever status the test needs."""

    def _build(attendant: UUID, day: date, *, status: str = "locked") -> UUID:
        started_at, ended_at = _window(day)
        return make_shift(
            attendant,
            business_date=day,
            sequence=1,
            started_at=started_at,
            ended_at=ended_at,
            status=status,
        )

    return _build


async def _create(client, headers, *, day: date, **body):
    return await client.post(
        "/api/v1/daily-summaries",
        json={"business_date": day.isoformat(), **body},
        headers=headers,
    )


# --- preconditions -----------------------------------------------------------------


async def test_a_day_with_an_open_shift_cannot_be_summarised(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    settled_shift,
    auth_headers,
    engine: Engine,
) -> None:
    """An open shift means the day is still being traded or typed in, and a summary computed
    over it is a snapshot of something still moving."""
    admin = make_user("admin")
    attendant = make_user("attendant")
    settled_shift(attendant, DAY, status="open")

    response = await _create(
        client, auth_headers(admin), day=DAY, opening_balance="0.00"
    )

    assert response.status_code == 409
    assert response.json()["code"] == "DAY_HAS_OPEN_SHIFTS"
    with engine.connect() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM daily_cash_summaries WHERE business_date = :d"
            ).bindparams(d=DAY)
        ).scalar_one()
    assert count == 0


async def test_a_closed_shift_is_enough_to_summarise_but_not_to_finalise(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    settled_shift,
    auth_headers,
) -> None:
    """§6.5 chains days, so a stale `expected_closing` does not stay local -- it propagates
    into every opening balance after it. `locked` is the only state in which §5.2 guarantees
    the inputs cannot move, and it inherits §6.7's unreviewed-expense gate for free."""
    admin = make_user("admin")
    attendant = make_user("attendant")
    day = date(2026, 6, 2)
    settled_shift(attendant, day, status="closed")

    created = await _create(
        client, auth_headers(admin), day=day, opening_balance="0.00"
    )
    assert created.status_code == 201

    finalised = await client.patch(
        f"/api/v1/daily-summaries/{day.isoformat()}/finalise",
        headers=auth_headers(admin),
    )
    assert finalised.status_code == 409
    assert finalised.json()["code"] == "DAY_NOT_LOCKED"


async def test_a_day_cannot_finalise_while_the_previous_one_is_unfinalised(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    settled_shift,
    auth_headers,
) -> None:
    """§6.5's `PRIOR_DAY_NOT_RECONCILED`, narrowed to this meaning. The original -- "day N−1
    has no count" -- would have blocked every day forever at an outlet with a locker."""
    admin = make_user("admin")
    attendant = make_user("attendant")
    first = date(2026, 6, 3)
    second = date(2026, 6, 4)
    settled_shift(attendant, first)
    settled_shift(attendant, second)

    await _create(client, auth_headers(admin), day=first, opening_balance="0.00")
    await _create(client, auth_headers(admin), day=second)

    response = await client.patch(
        f"/api/v1/daily-summaries/{second.isoformat()}/finalise",
        headers=auth_headers(admin),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "PRIOR_DAY_NOT_RECONCILED"


async def test_finalising_in_order_succeeds(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    settled_shift,
    auth_headers,
) -> None:
    admin = make_user("admin")
    attendant = make_user("attendant")
    first = date(2026, 6, 5)
    second = date(2026, 6, 6)
    settled_shift(attendant, first)
    settled_shift(attendant, second)

    await _create(client, auth_headers(admin), day=first, opening_balance="0.00")
    await _create(client, auth_headers(admin), day=second)

    one = await client.patch(
        f"/api/v1/daily-summaries/{first.isoformat()}/finalise",
        headers=auth_headers(admin),
    )
    two = await client.patch(
        f"/api/v1/daily-summaries/{second.isoformat()}/finalise",
        headers=auth_headers(admin),
    )

    assert one.status_code == 200
    assert two.status_code == 200
    assert two.json()["is_finalised"] is True


async def test_a_business_date_in_the_future_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
) -> None:
    """§6.1: trading has not happened yet, so there is nothing to reconcile. Evaluated in the
    outlet's local timezone -- at 23:00 IST the UTC date is still yesterday, and a correct
    entry would otherwise be refused."""
    admin = make_user("admin")
    tomorrow = date.today() + timedelta(days=400)

    response = await _create(
        client, auth_headers(admin), day=tomorrow, opening_balance="0.00"
    )

    assert response.status_code == 422
    assert response.json()["code"] == "BUSINESS_DATE_IN_FUTURE"


async def test_a_second_summary_for_one_date_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    settled_shift,
    auth_headers,
) -> None:
    """`UNIQUE (outlet_id, business_date)` makes this route naturally idempotent, which is
    why it takes no Idempotency-Key -- §6.10's reasoning for nozzle readings, exactly."""
    admin = make_user("admin")
    attendant = make_user("attendant")
    day = date(2026, 6, 7)
    settled_shift(attendant, day)

    first = await _create(
        client, auth_headers(admin), day=day, opening_balance="0.00"
    )
    second = await _create(
        client, auth_headers(admin), day=day, opening_balance="0.00"
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["code"] == "SUMMARY_ALREADY_EXISTS"


# --- the count, and the variance ---------------------------------------------------


async def test_a_count_is_recorded_and_the_expected_figure_is_not_touched(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    settled_shift,
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    make_fuel_type: Callable[..., UUID],
    make_fuel_price: Callable[..., UUID],
    auth_headers,
) -> None:
    """§6.4: "Variance is **recorded, never auto-corrected**. Do not 'fix' the closing
    balance to make it match. The variance *is* the signal.\""""
    admin = make_user("admin")
    attendant = make_user("attendant")
    day = date(2026, 6, 8)
    fuel = make_fuel_type(code="SUMFUEL", unit_of_measure="litre")
    make_fuel_price(fuel, "100.00", datetime(2026, 1, 1, tzinfo=timezone.utc), entered_by=admin)
    shift = settled_shift(attendant, day)
    nozzle = make_nozzle(fuel, label="DU-4/N-1")
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="1000.00")
    make_collection(shift, mode="cash", amount="100000.00")

    await _create(client, auth_headers(admin), day=day, opening_balance="0.00")
    counted = await client.patch(
        f"/api/v1/daily-summaries/{day.isoformat()}",
        json={"actual_counted": "99700.00", "notes": "Counted with Ramesh present"},
        headers=auth_headers(admin),
    )

    assert Decimal(counted.json()["expected_closing"]) == Decimal("100000.00")
    assert Decimal(counted.json()["actual_counted"]) == Decimal("99700.00")
    assert Decimal(counted.json()["variance"]) == Decimal("-300.00")
    assert counted.json()["notes"] == "Counted with Ramesh present"


async def test_an_empty_patch_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    settled_shift,
    auth_headers,
) -> None:
    admin = make_user("admin")
    attendant = make_user("attendant")
    day = date(2026, 6, 9)
    settled_shift(attendant, day)
    await _create(client, auth_headers(admin), day=day, opening_balance="0.00")

    response = await client.patch(
        f"/api/v1/daily-summaries/{day.isoformat()}",
        json={},
        headers=auth_headers(admin),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "NO_FIELDS_TO_UPDATE"


async def test_an_explicit_null_does_not_erase_a_count(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    settled_shift,
    auth_headers,
) -> None:
    """Clearing a count that was genuinely taken would silently break §6.5's chain for every
    day after it -- the next day would quietly switch from `counted` back to `carried`."""
    admin = make_user("admin")
    attendant = make_user("attendant")
    day = date(2026, 6, 10)
    settled_shift(attendant, day)
    await _create(
        client, auth_headers(admin), day=day, opening_balance="0.00",
        actual_counted="500.00",
    )

    response = await client.patch(
        f"/api/v1/daily-summaries/{day.isoformat()}",
        json={"actual_counted": None, "notes": "Just a note"},
        headers=auth_headers(admin),
    )

    assert Decimal(response.json()["actual_counted"]) == Decimal("500.00")


# --- finalise / unfinalise ---------------------------------------------------------


async def test_a_finalised_day_refuses_every_edit(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    settled_shift,
    auth_headers,
) -> None:
    admin = make_user("admin")
    attendant = make_user("attendant")
    day = date(2026, 6, 11)
    settled_shift(attendant, day)
    await _create(client, auth_headers(admin), day=day, opening_balance="0.00")
    await client.patch(
        f"/api/v1/daily-summaries/{day.isoformat()}/finalise",
        headers=auth_headers(admin),
    )

    edit = await client.patch(
        f"/api/v1/daily-summaries/{day.isoformat()}",
        json={"actual_counted": "1.00"},
        headers=auth_headers(admin),
    )
    again = await client.patch(
        f"/api/v1/daily-summaries/{day.isoformat()}/finalise",
        headers=auth_headers(admin),
    )

    assert edit.status_code == 409
    assert edit.json()["code"] == "SUMMARY_FINALISED"
    assert again.status_code == 409


async def test_unfinalising_needs_a_reason_and_leaves_the_figures_alone(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    settled_shift,
    auth_headers,
    engine: Engine,
) -> None:
    """§6.8's shift-reopen shape, and §13.16: a day is unfinalised so a human can look at it,
    not so the system can quietly rewrite what it said."""
    admin = make_user("admin")
    attendant = make_user("attendant")
    day = date(2026, 6, 12)
    settled_shift(attendant, day)
    created = await _create(
        client, auth_headers(admin), day=day, opening_balance="7500.00"
    )
    before = created.json()["expected_closing"]
    await client.patch(
        f"/api/v1/daily-summaries/{day.isoformat()}/finalise",
        headers=auth_headers(admin),
    )

    blank = await client.patch(
        f"/api/v1/daily-summaries/{day.isoformat()}/unfinalise",
        json={"reason": "  "},
        headers=auth_headers(admin),
    )
    assert blank.status_code == 422

    response = await client.patch(
        f"/api/v1/daily-summaries/{day.isoformat()}/unfinalise",
        json={"reason": "A deposit was recorded against the wrong day"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    assert response.json()["is_finalised"] is False
    assert response.json()["expected_closing"] == before

    with engine.connect() as connection:
        reason = connection.execute(
            text(
                "SELECT new_values->>'reason' FROM audit_logs "
                "WHERE table_name = 'daily_cash_summaries' AND action = 'status_change' "
                "ORDER BY changed_at DESC LIMIT 1"
            )
        ).scalar_one()
    assert reason == "A deposit was recorded against the wrong day"


async def test_unfinalising_a_day_that_is_not_finalised_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    settled_shift,
    auth_headers,
) -> None:
    admin = make_user("admin")
    attendant = make_user("attendant")
    day = date(2026, 6, 13)
    settled_shift(attendant, day)
    await _create(client, auth_headers(admin), day=day, opening_balance="0.00")

    response = await client.patch(
        f"/api/v1/daily-summaries/{day.isoformat()}/unfinalise",
        json={"reason": "Nothing to undo"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SUMMARY_NOT_FINALISED"


# --- reads and §8 ------------------------------------------------------------------


async def test_a_summary_is_read_back_as_stored_never_recomputed(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    settled_shift,
    make_non_fuel_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """§5.2's whole reason for storing the figure: fixing a calculation six months from now
    must not destroy the record of what the manager was told on the day.

    Here a non-fuel sale is added *after* the summary was computed. The stored figure must
    not move -- and it must be visible that it did not.
    """
    admin = make_user("admin")
    manager = make_user("manager")
    attendant = make_user("attendant")
    day = date(2026, 6, 14)
    shift = settled_shift(attendant, day)
    created = await _create(
        client, auth_headers(admin), day=day, opening_balance="1000.00"
    )
    assert Decimal(created.json()["expected_closing"]) == Decimal("1000.00")

    make_non_fuel_sale(shift, amount="500.00")

    reread = await client.get(
        f"/api/v1/daily-summaries/{day.isoformat()}", headers=auth_headers(manager)
    )

    assert Decimal(reread.json()["expected_closing"]) == Decimal("1000.00")
    assert Decimal(reread.json()["non_fuel_sales_total"]) == Decimal("0.00")


async def test_the_list_returns_days_newest_first(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    for day in (date(2026, 6, 15), date(2026, 6, 16), date(2026, 6, 17)):
        make_daily_summary(business_date=day, created_by=manager)

    response = await client.get("/api/v1/daily-summaries", headers=auth_headers(manager))

    dates = [item["business_date"] for item in response.json()["items"]]
    assert dates == sorted(dates, reverse=True)


async def test_the_list_truncates(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    for offset in range(4):
        make_daily_summary(
            business_date=date(2026, 6, 18) + timedelta(days=offset), created_by=manager
        )

    response = await client.get(
        "/api/v1/daily-summaries?limit=2", headers=auth_headers(manager)
    )

    assert len(response.json()["items"]) == 2
    assert response.json()["truncated"] is True


async def test_an_unknown_date_is_a_404(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")

    response = await client.get(
        "/api/v1/daily-summaries/2026-06-30", headers=auth_headers(manager)
    )

    assert response.status_code == 404
    assert response.json()["code"] == "SUMMARY_NOT_FOUND"


async def test_role_floors(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    settled_shift,
    auth_headers,
) -> None:
    """§8: a manager creates and records the count; **admin** finalises and unfinalises; an
    attendant sees none of it."""
    admin = make_user("admin")
    manager = make_user("manager")
    attendant = make_user("attendant")
    day = date(2026, 6, 22)
    settled_shift(attendant, day)

    assert (
        await client.get("/api/v1/daily-summaries", headers=auth_headers(attendant))
    ).status_code == 403
    assert (
        await _create(client, auth_headers(attendant), day=day, opening_balance="0.00")
    ).status_code == 403

    await _create(client, auth_headers(admin), day=day, opening_balance="0.00")

    manager_counts = await client.patch(
        f"/api/v1/daily-summaries/{day.isoformat()}",
        json={"actual_counted": "100.00"},
        headers=auth_headers(manager),
    )
    assert manager_counts.status_code == 200

    manager_finalises = await client.patch(
        f"/api/v1/daily-summaries/{day.isoformat()}/finalise",
        headers=auth_headers(manager),
    )
    assert manager_finalises.status_code == 403
    assert manager_finalises.json()["code"] == "INSUFFICIENT_ROLE"

    await client.patch(
        f"/api/v1/daily-summaries/{day.isoformat()}/finalise",
        headers=auth_headers(admin),
    )
    manager_unfinalises = await client.patch(
        f"/api/v1/daily-summaries/{day.isoformat()}/unfinalise",
        json={"reason": "Trying it on"},
        headers=auth_headers(manager),
    )
    assert manager_unfinalises.status_code == 403
