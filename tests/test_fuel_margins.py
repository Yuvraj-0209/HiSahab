"""Dealer margins over HTTP (CLAUDE.md §4.6, §5.1, §8, §9).

Deliberately mirrors test_fuel_prices.py, because the endpoints mirror each other. The two
tests that are *not* a mirror are at the bottom: the CBG worked example, and the assertion
that a margin survives price revisions untouched -- §4.6, the fact the profit model rests on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

PAST = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)
FUTURE = datetime(2030, 1, 1, 6, 0, tzinfo=timezone.utc)


def _payload(fuel_type_id, margin="2.28", effective_from=FUTURE):
    return {
        "fuel_type_id": str(fuel_type_id),
        "margin_per_unit": margin,
        "effective_from": effective_from.isoformat(),
    }


def _purge(engine) -> None:
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE fuel_margins DISABLE TRIGGER trg_fuel_margins_append_only")
        )
        connection.execute(text("DELETE FROM fuel_margins"))
        connection.execute(
            text("ALTER TABLE fuel_margins ENABLE TRIGGER trg_fuel_margins_append_only")
        )


async def test_an_admin_can_enter_a_margin(
    client, make_user, auth_headers, fuel_type_ids, engine
) -> None:
    response = await client.post(
        "/api/v1/fuel-margins",
        headers=auth_headers(make_user("admin")),
        json=_payload(fuel_type_ids["CBG"]),
    )
    try:
        assert response.status_code == 201
        assert response.json()["margin_per_unit"] == "2.28"
    finally:
        _purge(engine)


@pytest.mark.parametrize("role", ["attendant", "manager"])
async def test_only_an_admin_may_enter_a_margin(
    client, make_user, auth_headers, fuel_type_ids, role: str
) -> None:
    response = await client.post(
        "/api/v1/fuel-margins",
        headers=auth_headers(make_user(role)),
        json=_payload(fuel_type_ids["CBG"]),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


async def test_a_naive_timestamp_is_rejected(
    client, make_user, auth_headers, fuel_type_ids
) -> None:
    response = await client.post(
        "/api/v1/fuel-margins",
        headers=auth_headers(make_user("admin")),
        json={
            "fuel_type_id": str(fuel_type_ids["CBG"]),
            "margin_per_unit": "2.28",
            "effective_from": "2030-01-01T06:00:00",
        },
    )

    assert response.status_code == 422


@pytest.mark.parametrize("margin", ["0.00", "-2.28"])
async def test_a_non_positive_margin_is_rejected(
    client, make_user, auth_headers, fuel_type_ids, margin: str
) -> None:
    response = await client.post(
        "/api/v1/fuel-margins",
        headers=auth_headers(make_user("admin")),
        json=_payload(fuel_type_ids["CBG"], margin=margin),
    )

    assert response.status_code == 422


async def test_a_duplicate_effective_from_is_a_conflict(
    client, make_user, auth_headers, fuel_type_ids, make_fuel_margin
) -> None:
    admin = make_user("admin")
    make_fuel_margin(fuel_type_ids["PETROL"], "3.80", FUTURE, entered_by=admin)

    response = await client.post(
        "/api/v1/fuel-margins",
        headers=auth_headers(admin),
        json=_payload(fuel_type_ids["PETROL"], margin="3.90"),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MARGIN_ALREADY_EFFECTIVE_AT"


async def test_a_backdated_margin_is_accepted_and_warned_about(
    client, make_user, auth_headers, fuel_type_ids, engine, caplog
) -> None:
    try:
        with caplog.at_level("WARNING"):
            response = await client.post(
                "/api/v1/fuel-margins",
                headers=auth_headers(make_user("admin")),
                json=_payload(fuel_type_ids["DIESEL"], margin="2.60", effective_from=PAST),
            )
            assert response.status_code == 201
            assert response.json()["is_backdated"] is True
            assert "backdated fuel margin entered" in caplog.text
    finally:
        _purge(engine)


async def test_paging_sees_every_row_exactly_once(
    client, make_user, auth_headers, fuel_type_ids, make_fuel_margin
) -> None:
    admin = make_user("admin")
    inserted = {
        make_fuel_margin(
            fuel_type_ids["CBG"], f"2.{n}0", FUTURE + timedelta(days=n), entered_by=admin
        )
        for n in range(5)
    }

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        url = "/api/v1/fuel-margins?limit=2" + (f"&cursor={cursor}" if cursor else "")
        body = (await client.get(url, headers=auth_headers(admin))).json()
        seen.extend(row["id"] for row in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert len(seen) == len(set(seen)) == 5
    assert {str(i) for i in inserted} == set(seen)


async def test_a_malformed_cursor_is_rejected(client, make_user, auth_headers) -> None:
    response = await client.get(
        "/api/v1/fuel-margins?cursor=%%%broken%%%",
        headers=auth_headers(make_user("attendant")),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_CURSOR"


async def test_current_margins_report_the_cbg_commission(
    client, make_user, auth_headers, fuel_type_ids, make_fuel_margin
) -> None:
    """The ₹2.28 from §4.5, arriving as a string rather than a JSON float."""
    admin = make_user("admin")
    make_fuel_margin(fuel_type_ids["CBG"], "2.28", PAST, entered_by=admin)

    body = (
        await client.get("/api/v1/fuel-margins/current", headers=auth_headers(admin))
    ).json()

    margins = {row["fuel_type_code"]: row["margin_per_unit"] for row in body}
    assert margins["CBG"] == "2.28"
    # And the worked example it exists to support: 10,000 kg is ₹22,800, exactly.
    assert Decimal(margins["CBG"]) * Decimal("10000") == Decimal("22800.00")


async def test_a_fuel_with_no_margin_is_omitted_rather_than_reported_as_zero(
    client, make_user, auth_headers, fuel_type_ids, make_fuel_margin
) -> None:
    """Zero here would read as "we make nothing on this fuel" -- believable and false."""
    admin = make_user("admin")
    make_fuel_margin(fuel_type_ids["CBG"], "2.28", PAST, entered_by=admin)

    body = (
        await client.get("/api/v1/fuel-margins/current", headers=auth_headers(admin))
    ).json()

    codes = {row["fuel_type_code"] for row in body}
    assert "CBG" in codes
    assert "PETROL" not in codes


async def test_margin_survives_price_revisions_end_to_end(
    client, make_user, auth_headers, fuel_type_ids, make_fuel_margin, make_fuel_price
) -> None:
    """§4.6 over HTTP, not just at the helper level.

    Enter the CBG margin once, revise the price twice, and ask the API again. If this ever
    fails, storing margin directly (rather than deriving it from purchase invoices) was the
    wrong call and profit reporting is wrong with it.
    """
    admin = make_user("admin")
    make_fuel_margin(fuel_type_ids["CBG"], "2.28", PAST, entered_by=admin)
    make_fuel_price(fuel_type_ids["CBG"], "75.00", PAST, entered_by=admin)
    make_fuel_price(
        fuel_type_ids["CBG"], "78.50", PAST + timedelta(days=1), entered_by=admin
    )

    body = (
        await client.get("/api/v1/fuel-margins/current", headers=auth_headers(admin))
    ).json()

    margins = {row["fuel_type_code"]: row["margin_per_unit"] for row in body}
    assert margins["CBG"] == "2.28"


async def test_the_margin_history_requires_a_token(client) -> None:
    response = await client.get("/api/v1/fuel-margins")

    assert response.status_code == 401


# Phase 11: alias matching the name used in tests/test_reference_data_audit.py.
_FUTURE_AT = FUTURE


# --- coverage gaps found auditing Phase 3 (Phase 11 Step 0) -------------------
#
# Three reachable branches with no test. The four Phase 3 routers predate the 100%-coverage
# habit Phases 5-10 hold to; these land with the phase that was editing the files anyway.


async def test_a_naive_timestamp_is_refused_rather_than_assumed(
    client, make_user, auth_headers
) -> None:
    """§3 rule 4: every instant is stored in UTC, and a naive one has no instant in it.

    Guessing a timezone here would be the worst kind of wrong -- `?at=` selects which margin
    was in force, so assuming UTC for a value the caller meant as IST shifts the answer by
    five and a half hours and silently returns the *previous* margin across a 06:00 revision
    (§4.1). Refusing is the only honest option.
    """
    response = await client.get(
        "/api/v1/fuel-margins/current",
        headers=auth_headers(make_user("attendant")),
        params={"at": "2027-01-01T06:00:00"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "NAIVE_TIMESTAMP"


async def test_the_history_can_be_filtered_to_one_fuel(
    client, make_user, auth_headers, engine, fuel_type_ids, make_fuel_margin
) -> None:
    """`?fuel_type_id=` narrows the history. Untested until now, which mattered more than it
    looks: a filter that silently ignored its argument would return *every* fuel's margins
    under a heading naming one of them."""
    admin = make_user("admin")
    make_fuel_margin(fuel_type_ids["PETROL"], "1.85", _FUTURE_AT, entered_by=admin)
    make_fuel_margin(fuel_type_ids["CBG"], "2.28", _FUTURE_AT, entered_by=admin)

    response = await client.get(
        "/api/v1/fuel-margins",
        headers=auth_headers(admin),
        params={"fuel_type_id": str(fuel_type_ids["PETROL"])},
    )

    assert response.status_code == 200
    returned = {row["fuel_type_id"] for row in response.json()["items"]}
    assert returned == {str(fuel_type_ids["PETROL"])}


async def test_the_unfiltered_history_returns_every_fuel(
    client, make_user, auth_headers, engine, fuel_type_ids, make_fuel_margin
) -> None:
    """The other half, so the filter above is proven to narrow rather than the fixture merely
    having created one row."""
    admin = make_user("admin")
    make_fuel_margin(fuel_type_ids["PETROL"], "1.85", _FUTURE_AT, entered_by=admin)
    make_fuel_margin(fuel_type_ids["CBG"], "2.28", _FUTURE_AT, entered_by=admin)

    response = await client.get(
        "/api/v1/fuel-margins", headers=auth_headers(admin)
    )

    assert response.status_code == 200
    returned = {row["fuel_type_id"] for row in response.json()["items"]}
    assert returned == {
        str(fuel_type_ids["PETROL"]),
        str(fuel_type_ids["CBG"]),
    }
