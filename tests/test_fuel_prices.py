"""Fuel prices over HTTP (CLAUDE.md §4.1, §5.1, §8, §9).

Covers the three things that would quietly go wrong here: a naive timestamp being assumed
into the wrong timezone, a backdated entry revaluing closed shifts without anyone noticing,
and a page walk that skips or repeats rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

# Well clear of "now" in both directions, so backdated/future-dated assertions do not
# depend on when the suite runs.
PAST = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)
FUTURE = datetime(2030, 1, 1, 6, 0, tzinfo=timezone.utc)


def _payload(fuel_type_id, rate="104.21", effective_from=FUTURE):
    return {
        "fuel_type_id": str(fuel_type_id),
        "rate_per_unit": rate,
        "effective_from": effective_from.isoformat(),
    }


async def test_an_admin_can_enter_a_rate(
    client, make_user, auth_headers, fuel_type_ids, engine
) -> None:
    from sqlalchemy import text

    response = await client.post(
        "/api/v1/fuel-prices",
        headers=auth_headers(make_user("admin")),
        json=_payload(fuel_type_ids["PETROL"]),
    )
    try:
        assert response.status_code == 201
        assert response.json()["rate_per_unit"] == "104.21"
        assert response.json()["is_backdated"] is False
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE fuel_prices DISABLE TRIGGER trg_fuel_prices_append_only")
            )
            connection.execute(text("DELETE FROM fuel_prices"))
            connection.execute(
                text("ALTER TABLE fuel_prices ENABLE TRIGGER trg_fuel_prices_append_only")
            )


async def test_a_manager_may_not_enter_a_rate(
    client, make_user, auth_headers, fuel_type_ids
) -> None:
    """§8 puts "Enter fuel prices and margins" above the manager floor, unlike most writes."""
    response = await client.post(
        "/api/v1/fuel-prices",
        headers=auth_headers(make_user("manager")),
        json=_payload(fuel_type_ids["PETROL"]),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


async def test_a_naive_timestamp_is_rejected(
    client, make_user, auth_headers, fuel_type_ids
) -> None:
    """§3 rule 4. Guessing IST vs UTC would misprice five and a half hours of a shift."""
    response = await client.post(
        "/api/v1/fuel-prices",
        headers=auth_headers(make_user("admin")),
        json={
            "fuel_type_id": str(fuel_type_ids["PETROL"]),
            "rate_per_unit": "104.21",
            "effective_from": "2030-01-01T06:00:00",
        },
    )

    assert response.status_code == 422


@pytest.mark.parametrize("rate", ["0.00", "-5.00"])
async def test_a_non_positive_rate_is_rejected(
    client, make_user, auth_headers, fuel_type_ids, rate: str
) -> None:
    response = await client.post(
        "/api/v1/fuel-prices",
        headers=auth_headers(make_user("admin")),
        json=_payload(fuel_type_ids["PETROL"], rate=rate),
    )

    assert response.status_code == 422


async def test_a_duplicate_effective_from_is_a_conflict(
    client, make_user, auth_headers, fuel_type_ids, make_fuel_price
) -> None:
    """Append-only means this cannot be resolved by overwriting -- say so, don't 500."""
    admin = make_user("admin")
    make_fuel_price(fuel_type_ids["DIESEL"], "90.00", FUTURE, entered_by=admin)

    response = await client.post(
        "/api/v1/fuel-prices",
        headers=auth_headers(admin),
        json=_payload(fuel_type_ids["DIESEL"], rate="91.00"),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "PRICE_ALREADY_EFFECTIVE_AT"


async def test_a_backdated_rate_is_accepted_and_warned_about(
    client, make_user, auth_headers, fuel_type_ids, engine, caplog
) -> None:
    """Allowed on purpose: refusing it leaves a forgotten day mispriced forever.

    But it revalues shifts that already happened, so it must be visible -- flagged in the
    response for the UI and logged at WARNING for whoever reads the logs.
    """
    from sqlalchemy import text

    try:
        with caplog.at_level("WARNING"):
            response = await client.post(
                "/api/v1/fuel-prices",
                headers=auth_headers(make_user("admin")),
                json=_payload(fuel_type_ids["CBG"], rate="75.00", effective_from=PAST),
            )
            assert response.status_code == 201
            assert response.json()["is_backdated"] is True
            assert "backdated fuel price entered" in caplog.text
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE fuel_prices DISABLE TRIGGER trg_fuel_prices_append_only")
            )
            connection.execute(text("DELETE FROM fuel_prices"))
            connection.execute(
                text("ALTER TABLE fuel_prices ENABLE TRIGGER trg_fuel_prices_append_only")
            )


async def test_paging_sees_every_row_exactly_once(
    client, make_user, auth_headers, fuel_type_ids, make_fuel_price
) -> None:
    """§9's reason for forbidding OFFSET, asserted directly.

    Five rows walked two at a time. The assertion is not just "five results" but that the
    set of ids is exactly the set inserted -- an offset-based implementation under
    concurrent insert would satisfy the count and fail this.
    """
    admin = make_user("admin")
    inserted = {
        make_fuel_price(
            fuel_type_ids["PETROL"], f"10{n}.00", FUTURE + timedelta(days=n),
            entered_by=admin,
        )
        for n in range(5)
    }

    seen: list[str] = []
    cursor = None
    for _ in range(10):  # bounded, so a broken cursor loops finitely rather than forever
        url = "/api/v1/fuel-prices?limit=2" + (f"&cursor={cursor}" if cursor else "")
        body = (await client.get(url, headers=auth_headers(admin))).json()
        seen.extend(row["id"] for row in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert len(seen) == len(set(seen)) == 5
    assert {str(i) for i in inserted} == set(seen)


async def test_pages_are_newest_first(
    client, make_user, auth_headers, fuel_type_ids, make_fuel_price
) -> None:
    admin = make_user("admin")
    for n in range(3):
        make_fuel_price(
            fuel_type_ids["DIESEL"], f"9{n}.00", FUTURE + timedelta(days=n),
            entered_by=admin,
        )

    body = (
        await client.get("/api/v1/fuel-prices", headers=auth_headers(admin))
    ).json()

    timestamps = [row["effective_from"] for row in body["items"]]
    assert timestamps == sorted(timestamps, reverse=True)


async def test_a_malformed_cursor_is_rejected_not_silently_restarted(
    client, make_user, auth_headers
) -> None:
    """Restarting on a bad cursor would show a reader the same rows twice as new ones."""
    response = await client.get(
        "/api/v1/fuel-prices?cursor=not-a-real-cursor",
        headers=auth_headers(make_user("attendant")),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_CURSOR"


async def test_current_rates_use_the_shared_lookup(
    client, make_user, auth_headers, fuel_type_ids, make_fuel_price
) -> None:
    """Values come back as strings, never JSON floats -- §3 rule 1 all the way out."""
    admin = make_user("admin")
    make_fuel_price(fuel_type_ids["CBG"], "76.50", PAST, entered_by=admin)

    body = (
        await client.get("/api/v1/fuel-prices/current", headers=auth_headers(admin))
    ).json()

    rates = {row["fuel_type_code"]: row["rate_per_unit"] for row in body}
    assert rates["CBG"] == "76.50"


async def test_a_fuel_with_no_rate_is_omitted_rather_than_reported_as_zero(
    client, make_user, auth_headers, fuel_type_ids, make_fuel_price
) -> None:
    """A zero here is indistinguishable from a real rate and would value sales at nothing."""
    admin = make_user("admin")
    make_fuel_price(fuel_type_ids["PETROL"], "104.00", PAST, entered_by=admin)

    body = (
        await client.get("/api/v1/fuel-prices/current", headers=auth_headers(admin))
    ).json()

    codes = {row["fuel_type_code"] for row in body}
    assert "PETROL" in codes
    assert "DIESEL" not in codes


async def test_the_price_history_requires_a_token(client) -> None:
    response = await client.get("/api/v1/fuel-prices")

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

    Guessing a timezone here would be the worst kind of wrong -- `?at=` selects which rate
    was in force, so assuming UTC for a value the caller meant as IST shifts the answer by
    five and a half hours and silently returns the *previous* rate across a 06:00 revision
    (§4.1). Refusing is the only honest option.
    """
    response = await client.get(
        "/api/v1/fuel-prices/current",
        headers=auth_headers(make_user("attendant")),
        params={"at": "2027-01-01T06:00:00"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "NAIVE_TIMESTAMP"


async def test_the_history_can_be_filtered_to_one_fuel(
    client, make_user, auth_headers, engine, fuel_type_ids, make_fuel_price
) -> None:
    """`?fuel_type_id=` narrows the history. Untested until now, which mattered more than it
    looks: a filter that silently ignored its argument would return *every* fuel's rates
    under a heading naming one of them."""
    admin = make_user("admin")
    make_fuel_price(fuel_type_ids["PETROL"], "104.21", _FUTURE_AT, entered_by=admin)
    make_fuel_price(fuel_type_ids["CBG"], "88.50", _FUTURE_AT, entered_by=admin)

    response = await client.get(
        "/api/v1/fuel-prices",
        headers=auth_headers(admin),
        params={"fuel_type_id": str(fuel_type_ids["PETROL"])},
    )

    assert response.status_code == 200
    returned = {row["fuel_type_id"] for row in response.json()["items"]}
    assert returned == {str(fuel_type_ids["PETROL"])}


async def test_the_unfiltered_history_returns_every_fuel(
    client, make_user, auth_headers, engine, fuel_type_ids, make_fuel_price
) -> None:
    """The other half, so the filter above is proven to narrow rather than the fixture merely
    having created one row."""
    admin = make_user("admin")
    make_fuel_price(fuel_type_ids["PETROL"], "104.21", _FUTURE_AT, entered_by=admin)
    make_fuel_price(fuel_type_ids["CBG"], "88.50", _FUTURE_AT, entered_by=admin)

    response = await client.get(
        "/api/v1/fuel-prices", headers=auth_headers(admin)
    )

    assert response.status_code == 200
    returned = {row["fuel_type_id"] for row in response.json()["items"]}
    assert returned == {
        str(fuel_type_ids["PETROL"]),
        str(fuel_type_ids["CBG"]),
    }
