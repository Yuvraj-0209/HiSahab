"""The three reporting endpoints over HTTP (CLAUDE.md §8, §13.20-§13.24).

Phase 13, Steps 4-6. `tests/test_reporting_service.py` proves the arithmetic and the
provenance; this file proves the *contract* -- serialisation, role floors, window handling,
tenancy, and that all three reads write nothing.

The assertions worth singling out:

`test_money_serialises_as_a_string_and_null_is_not_zero` is §14's client guardrail seen from
the server side. Every null in these responses means something specific and none of them means
zero.

`test_the_default_window_ends_today_at_the_outlet` pins §6.1 at the one place a UTC default
would silently drop the current trading day from every evening's report.

`test_the_reports_write_nothing` is what makes "it is a report" a fact rather than an
intention.
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

# **Deliberately in the past**, unlike the 2027 dates other cash-engine test modules use.
# These endpoints enforce §6.1 -- a business date in the future is a data-entry error -- so a
# fixture date that has not happened yet is refused with 422 rather than reported on. That is
# the endpoint behaving correctly and the test being wrong, which cost one debugging round.
DAY = date(2026, 3, 10)
BEFORE = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _window(day: date) -> tuple[datetime, datetime]:
    """This outlet trades 06:00 -> 22:00 IST, i.e. 00:30 -> 16:30 UTC."""
    start = datetime(day.year, day.month, day.day, 0, 30, tzinfo=timezone.utc)
    return start, start + timedelta(hours=16)


async def _get(client: AsyncClient, path: str, headers: dict[str, str], **params):
    response = await client.get(f"/api/v1{path}", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def priced_fuel(make_fuel_type, make_fuel_price, make_user):
    """Priced, deliberately unmargined -- petrol and diesel's real state here (§14)."""
    admin = make_user("admin")
    fuel = make_fuel_type(code="APIFUEL", unit_of_measure="litre")
    make_fuel_price(fuel, "100.00", BEFORE, entered_by=admin)
    return fuel


@pytest.fixture
def margined_fuel(make_fuel_type, make_fuel_price, make_fuel_margin, make_user):
    """Priced and margined, standing in for CBG."""
    admin = make_user("admin")
    fuel = make_fuel_type(code="APIGAS", unit_of_measure="kilogram")
    make_fuel_price(fuel, "80.00", BEFORE, entered_by=admin)
    make_fuel_margin(fuel, "2.28", BEFORE, entered_by=admin)
    return fuel


# --- GET /reports/daily/{business_date} ---------------------------------------


async def test_a_traded_day_reports_cash_fuel_and_expenses(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    margined_fuel,
    make_expense: Callable[..., UUID],
    make_non_fuel_sale: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
    clean_expenses: None,
) -> None:
    """One day end to end, with both a margined and an unmargined fuel on it.

        1,000 L petrol x ₹100 = ₹1,00,000   (no commission entered -- null, never 0)
          100 kg CBG   x  ₹80 =    ₹8,000   x ₹2.28 = ₹228 margin
        + ₹500 non-fuel                     = ₹1,08,500 total sales
        - ₹300 cash expense
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel, label="DU-1/N-1"),
                 opening_reading="0.00", closing_reading="1000.00")
    make_reading(shift, make_nozzle(margined_fuel, label="DU-2/N-1"),
                 opening_reading="0.00", closing_reading="100.00")
    make_non_fuel_sale(shift, amount="500.00")
    make_expense(shift, mode="cash", amount="300.00")
    make_daily_summary(business_date=DAY - timedelta(days=1), expected_closing="0.00")

    body = await _get(client, f"/reports/daily/{DAY.isoformat()}", auth_headers(manager))

    assert body["cash"]["source"] == "computed"
    assert Decimal(body["cash"]["metered_fuel_sales"]) == Decimal("108000.00")
    assert Decimal(body["cash"]["non_fuel_sales_total"]) == Decimal("500.00")
    assert Decimal(body["cash"]["total_sales"]) == Decimal("108500.00")
    assert Decimal(body["cash"]["cash_expenses"]) == Decimal("300.00")

    by_code = {line["code"]: line for line in body["fuel"]}
    assert Decimal(by_code["APIFUEL"]["sale_value"]) == Decimal("100000.00")
    assert by_code["APIFUEL"]["unit_of_measure"] == "litre"
    assert by_code["APIFUEL"]["gross_fuel_margin"] is None
    assert by_code["APIFUEL"]["margin_unavailable_reason"] == "NO_MARGIN_FOR_DATE"

    assert by_code["APIGAS"]["unit_of_measure"] == "kilogram"
    assert Decimal(by_code["APIGAS"]["gross_fuel_margin"]) == Decimal("228.00")

    # §13.21: one unmargined fuel withholds the DAY's total and names the gap.
    assert body["gross_fuel_margin_total"] is None
    assert body["fuels_missing_margin"] == ["APIFUEL"]

    # §4.5: never one summed number.
    assert body["quantity_by_unit"] == {"litre": "1000.000", "kilogram": "100.000"}
    assert "total_quantity" not in body

    assert Decimal(body["expenses_total"]) == Decimal("300.00")
    assert body["shifts"][0]["id"] == str(shift)


async def test_a_day_that_never_traded_is_200_not_404(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
) -> None:
    """A quiet Sunday is a real answer to "how did this day go".

    A 404 would make a reader wonder whether they typed the date wrong, and send them looking
    for data that correctly does not exist.
    """
    manager = make_user("manager")

    body = await _get(client, f"/reports/daily/{DAY.isoformat()}", auth_headers(manager))

    assert body["cash"]["source"] == "no_trading"
    assert Decimal(body["cash"]["metered_fuel_sales"]) == Decimal("0.00")
    assert body["cash"]["expected_closing"] is None
    assert body["fuel"] == []


async def test_the_profit_label_is_present_and_says_what_it_excludes(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
) -> None:
    """§13.7: "Label it accordingly wherever it is displayed -- an unlabelled 'profit' figure
    here is exactly the plausible-but-wrong number this document exists to prevent."

    Asserted on the response rather than trusted to the screen, because a mobile client (§2)
    gets the same fields and the same obligation.
    """
    manager = make_user("manager")

    body = await _get(client, f"/reports/daily/{DAY.isoformat()}", auth_headers(manager))

    assert "gross fuel margin on quantity sold" in body["profit_basis"].lower()
    assert "stock revaluation" in body["profit_basis"]
    assert "never summed across" in body["fuel_basis"].lower()


async def test_a_future_business_date_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
) -> None:
    """§6.1. Trading has not happened, so asking is a data-entry error rather than a gap."""
    manager = make_user("manager")
    future = date.today() + timedelta(days=2)

    response = await client.get(
        f"/api/v1/reports/daily/{future.isoformat()}", headers=auth_headers(manager)
    )

    assert response.status_code == 422
    assert response.json()["code"] == "BUSINESS_DATE_IN_FUTURE"


async def test_a_malformed_date_is_a_422_not_a_500(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")

    response = await client.get(
        "/api/v1/reports/daily/not-a-date", headers=auth_headers(manager)
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


# --- GET /reports/range -------------------------------------------------------


async def test_an_outlet_that_has_never_traded_gets_the_seven_days_ending_today(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
) -> None:
    """§6.1 and §11's "7-day rolling view", together -- the fallback branch of §13.30.

    **Today at the outlet, not today in UTC.** At 23:00 IST the UTC date is still yesterday,
    so a UTC default would silently drop the current trading day from every evening's report --
    the one day a manager is most likely to be looking at.

    With no shift anywhere there is no trading day to anchor to, so §13.30 falls back to
    exactly the behaviour this test has always asserted.
    """
    from app.services import shifts as shift_service

    manager = make_user("manager")
    expected_end = shift_service.outlet_today("Asia/Kolkata")

    body = await _get(client, "/reports/range", auth_headers(manager))

    assert len(body["days"]) == 7
    assert body["to"] == expected_end.isoformat()
    assert body["from"] == (expected_end - timedelta(days=6)).isoformat()
    assert [day["business_date"] for day in body["days"]] == [
        (expected_end - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)
    ]


async def test_the_default_window_ends_on_the_most_recent_trading_day(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
) -> None:
    """§13.30, and it is the whole reason that section exists.

    §4.7 says the day is typed in **after the fact**, in one sitting, often long afterwards.
    An outlet catching up on July in late August opened this endpoint onto seven days of
    `no_trading` -- a report about a week in which nothing happened.
    """
    from app.services import shifts as shift_service

    manager = make_user("manager")
    traded = shift_service.outlet_today("Asia/Kolkata") - timedelta(days=40)
    make_shift(manager, business_date=traded, status="closed")

    body = await _get(client, "/reports/range", auth_headers(manager))

    assert body["to"] == traded.isoformat()
    assert body["from"] == (traded - timedelta(days=6)).isoformat()
    assert len(body["days"]) == 7
    # And the day itself is in the window rather than 40 days off the end of it.
    assert body["days"][-1]["business_date"] == traded.isoformat()
    assert body["days"][-1]["source"] != "no_trading"


async def test_the_alerts_window_follows_the_same_anchor(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
) -> None:
    """The two screens must agree about which week they are describing.

    This is the case that started §13.30: the `day_not_reconciled` alert exists for exactly a
    day that traded and was never reconciled, and it is windowed (§13.23) -- so anchoring the
    window on today hid the alert for the very day somebody was looking for.
    """
    from app.services import shifts as shift_service

    manager = make_user("manager")
    traded = shift_service.outlet_today("Asia/Kolkata") - timedelta(days=40)
    make_shift(manager, business_date=traded, status="locked")

    body = await _get(client, "/reports/variance-alerts", auth_headers(manager))

    assert body["to"] == traded.isoformat()
    assert body["from"] == (traded - timedelta(days=6)).isoformat()
    assert any(
        alert["kind"] == "day_not_reconciled" and alert["business_date"] == traded.isoformat()
        for alert in body["items"]
    )


async def test_every_date_in_the_window_appears_including_untraded_ones(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
) -> None:
    manager = make_user("manager")
    start, end = DAY, DAY + timedelta(days=4)

    body = await _get(
        client, "/reports/range", auth_headers(manager),
        **{"from": start.isoformat(), "to": end.isoformat()},
    )

    assert len(body["days"]) == 5
    assert all(day["source"] == "no_trading" for day in body["days"])
    assert all(day["bar_height_pct"] == "0.00%" for day in body["days"])


async def test_bar_heights_come_from_the_server_and_are_relative_to_the_tallest(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    make_daily_summary: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
) -> None:
    """§14's new guardrail, from the client's point of view.

    The percentage arrives ready to assign. A client that divided `total_sales` by the largest
    value to get the same number would be doing arithmetic on money in a language with no
    decimal type -- and would produce a chart that disagrees with its own table.
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    make_daily_summary(business_date=DAY - timedelta(days=1), expected_closing="0.00")
    for offset, litres in ((0, "1000.00"), (1, "250.00")):
        current = DAY + timedelta(days=offset)
        started, ended = _window(current)
        shift = make_shift(attendant, business_date=current, started_at=started,
                           ended_at=ended)
        make_reading(shift, make_nozzle(priced_fuel, label=f"DU-{offset}/N-1"),
                     opening_reading="0.00", closing_reading=litres)

    body = await _get(
        client, "/reports/range", auth_headers(manager),
        **{"from": DAY.isoformat(), "to": (DAY + timedelta(days=1)).isoformat()},
    )

    assert body["days"][0]["bar_height_pct"] == "100.00%"
    assert body["days"][1]["bar_height_pct"] == "25.00%"
    # And the figures the bars are drawn from are in the same payload, so the chart and the
    # table cannot disagree.
    assert Decimal(body["days"][0]["total_sales"]) == Decimal("100000.00")
    assert Decimal(body["days"][1]["total_sales"]) == Decimal("25000.00")


@pytest.mark.parametrize(
    ("span_days", "expected_status"),
    [(31, 200), (32, 422)],
)
async def test_the_range_cap_is_thirty_one_days_inclusive(
    span_days: int,
    expected_status: int,
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
) -> None:
    """The boundary, both sides. §13.24: an unreconciled day costs a full §6.4 pass, so the
    cap is a cost ceiling rather than only a scan ceiling."""
    manager = make_user("manager")
    start = DAY
    end = start + timedelta(days=span_days - 1)

    response = await client.get(
        "/api/v1/reports/range",
        headers=auth_headers(manager),
        params={"from": start.isoformat(), "to": end.isoformat()},
    )

    assert response.status_code == expected_status
    if expected_status == 422:
        assert response.json()["code"] == "INVALID_DATE_RANGE"


async def test_from_after_to_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")

    response = await client.get(
        "/api/v1/reports/range",
        headers=auth_headers(manager),
        params={"from": DAY.isoformat(), "to": (DAY - timedelta(days=1)).isoformat()},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_DATE_RANGE"


async def test_a_future_window_end_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    future = date.today() + timedelta(days=5)

    response = await client.get(
        "/api/v1/reports/range",
        headers=auth_headers(manager),
        params={"from": (future - timedelta(days=2)).isoformat(), "to": future.isoformat()},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "BUSINESS_DATE_IN_FUTURE"


async def test_a_single_day_window_is_accepted(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
) -> None:
    manager = make_user("manager")

    body = await _get(
        client, "/reports/range", auth_headers(manager),
        **{"from": DAY.isoformat(), "to": DAY.isoformat()},
    )

    assert len(body["days"]) == 1


# --- GET /reports/variance-alerts ---------------------------------------------


async def test_the_alert_threshold_is_reported_and_comes_from_config(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
) -> None:
    """The screen labels a row "over threshold" using this figure, so it must be the same one
    the server compared against -- §6.7/§6.11's "must not require a deploy" rule."""
    manager = make_user("manager")

    body = await _get(client, "/reports/variance-alerts", auth_headers(manager))

    assert Decimal(body["threshold"]) == Decimal("100.00")
    assert isinstance(body["threshold"], str)


async def test_a_short_day_over_the_threshold_alerts(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended,
               status="closed")
    make_daily_summary(
        business_date=DAY, expected_closing="9000.00", actual_counted="8000.00"
    )

    body = await _get(
        client, "/reports/variance-alerts", auth_headers(manager),
        **{"from": DAY.isoformat(), "to": DAY.isoformat()},
    )

    kinds = [item["kind"] for item in body["items"]]
    assert "variance_exceeds_threshold" in kinds
    alert = next(
        item for item in body["items"] if item["kind"] == "variance_exceeds_threshold"
    )
    assert Decimal(alert["amount"]) == Decimal("-1000.00")
    assert "short" in alert["detail"]


async def test_an_uncounted_day_is_not_an_alert(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
) -> None:
    """§6.5's locker model means most days are uncounted. If this fired, every day would
    alert and the list would be worthless within a week."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended,
               status="closed")
    make_daily_summary(business_date=DAY, expected_closing="9000.00")

    body = await _get(
        client, "/reports/variance-alerts", auth_headers(manager),
        **{"from": DAY.isoformat(), "to": DAY.isoformat()},
    )

    assert body["items"] == []


async def test_the_alerts_basis_states_that_null_is_not_a_variance(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
) -> None:
    manager = make_user("manager")

    body = await _get(client, "/reports/variance-alerts", auth_headers(manager))

    assert "nobody counted" in body["basis"]
    assert "dismiss" in body["basis"]


# --- serialisation ------------------------------------------------------------


async def test_money_serialises_as_a_string_and_null_is_not_zero(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
) -> None:
    """§3 rule 1 at the boundary, and §14's `?? 0` guardrail seen from the server side.

    Every null in this response means something specific and none of them means zero:
    `actual_counted` null means nobody counted, `variance` null means there is nothing to
    compare against. A client that coalesces either has invented a reconciled day.
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended,
               status="closed")
    make_daily_summary(business_date=DAY, expected_closing="9000.00")

    response = await client.get(
        f"/api/v1/reports/daily/{DAY.isoformat()}", headers=auth_headers(manager)
    )
    body = response.json()

    assert isinstance(body["cash"]["expected_closing"], str)
    assert '"expected_closing":"9000.00"' in response.text.replace(" ", "")
    assert body["cash"]["actual_counted"] is None
    assert body["cash"]["variance"] is None
    # Not "0.00", and not absent -- absent would be indistinguishable from a client bug.
    assert "actual_counted" in body["cash"]


async def test_a_snapshot_day_reports_its_source_and_reconciliation(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    make_daily_summary: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
) -> None:
    """§13.20 and §13.22 as the client sees them."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended,
                       status="closed")
    make_reading(shift, make_nozzle(priced_fuel), opening_reading="0.00",
                 closing_reading="100.00")
    make_daily_summary(
        business_date=DAY, expected_closing="10000.00", metered_fuel_sales="10000.00"
    )

    body = await _get(client, f"/reports/daily/{DAY.isoformat()}", auth_headers(manager))

    assert body["cash"]["source"] == "snapshot"
    assert body["breakdown_reconciles"] is True
    assert Decimal(body["snapshot_metered_fuel_sales"]) == Decimal("10000.00")


# --- §8: role floors ----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/reports/range",
        "/api/v1/reports/variance-alerts",
        f"/api/v1/reports/daily/{DAY.isoformat()}",
    ],
)
@pytest.mark.parametrize(
    ("role", "expected"),
    [("attendant", 403), ("manager", 200), ("admin", 200)],
)
async def test_role_floors(
    path: str,
    role: str,
    expected: int,
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
) -> None:
    """§8: reports are manager-and-above. An attendant sees their own shift, not the outlet's
    week -- and §8 is explicit that hiding the tab is UX, so the server refuses too."""
    user = make_user(role)

    response = await client.get(path, headers=auth_headers(user))

    assert response.status_code == expected
    if expected == 403:
        assert response.json()["code"] == "INSUFFICIENT_ROLE"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/reports/range",
        "/api/v1/reports/variance-alerts",
        f"/api/v1/reports/daily/{DAY.isoformat()}",
    ],
)
async def test_every_report_requires_a_token(client: AsyncClient, path: str) -> None:
    response = await client.get(path)

    assert response.status_code == 401
    assert response.json()["code"] == "NOT_AUTHENTICATED"


# --- §5.0: tenancy ------------------------------------------------------------


async def test_another_outlets_day_is_absent_from_every_report(
    client: AsyncClient,
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_daily_summary: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
) -> None:
    """Asserted by *inserting* a second outlet's day, never by an empty page.

    An empty result proves nothing about scoping when there was nothing to find -- which is
    why §10 words this requirement the way it does.
    """
    other = UUID("00000000-0000-0000-0000-0000000000fe")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outlets (id, name, is_active) "
                "VALUES (:id, 'Other Outlet', true) ON CONFLICT DO NOTHING"
            ).bindparams(id=other)
        )
    stranger = make_user("manager", outlet_id=other)
    manager = make_user("manager")
    started, ended = _window(DAY)
    make_shift(stranger, business_date=DAY, started_at=started, ended_at=ended,
               outlet_id=other, status="closed")
    make_daily_summary(
        business_date=DAY, outlet_id=other, expected_closing="99999.00",
        actual_counted="1.00", created_by=stranger,
    )

    body = await _get(client, f"/reports/daily/{DAY.isoformat()}", auth_headers(manager))
    alerts = await _get(
        client, "/reports/variance-alerts", auth_headers(manager),
        **{"from": DAY.isoformat(), "to": DAY.isoformat()},
    )

    assert body["cash"]["source"] == "no_trading"
    assert body["cash"]["shift_count"] == 0
    # The other outlet's ₹99,998 variance would be the loudest alert in the system.
    assert alerts["items"] == []


# --- the reads write nothing --------------------------------------------------


async def test_the_reports_write_nothing(
    client: AsyncClient,
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    auth_headers,
    clean_cash: None,
) -> None:
    """"It is a report" made a fact rather than an intention.

    The specific hazard is `daily_cash_summaries`: a computed day looks exactly like a summary
    somebody could persist, and doing so would silently convert an estimate into the record
    §5.2 says must never be recomputed. Nothing may be written, and no audit row either.
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel), opening_reading="0.00",
                 closing_reading="100.00")

    def _counts() -> tuple[int, int]:
        with engine.connect() as connection:
            summaries = connection.execute(
                text("SELECT count(*) FROM daily_cash_summaries")
            ).scalar_one()
            audits = connection.execute(
                text("SELECT count(*) FROM audit_logs")
            ).scalar_one()
        return summaries, audits

    before = _counts()

    await _get(client, f"/reports/daily/{DAY.isoformat()}", auth_headers(manager))
    await _get(
        client, "/reports/range", auth_headers(manager),
        **{"from": DAY.isoformat(), "to": DAY.isoformat()},
    )
    await _get(
        client, "/reports/variance-alerts", auth_headers(manager),
        **{"from": DAY.isoformat(), "to": DAY.isoformat()},
    )

    assert _counts() == before


async def test_calling_the_daily_report_twice_gives_the_same_answer(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    auth_headers,
    clean_cash: None,
) -> None:
    """No hidden state, and no first-call side effect that changes the second."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel), opening_reading="0.00",
                 closing_reading="100.00")

    first = await _get(client, f"/reports/daily/{DAY.isoformat()}", auth_headers(manager))
    second = await _get(client, f"/reports/daily/{DAY.isoformat()}", auth_headers(manager))

    assert first == second


# --- GET /reports/summary (Phase 19) ------------------------------------------


async def test_the_summary_totals_a_window_and_splits_it_by_fuel(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    margined_fuel,
    make_expense: Callable[..., UUID],
    make_non_fuel_sale: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
    clean_expenses: None,
) -> None:
    """Two trading days, two fuels, summed across the window.

        day 1:  1,000 L x ₹100 = ₹1,00,000  |  100 kg x ₹80 = ₹8,000
        day 2:    500 L x ₹100 =   ₹50,000  |   50 kg x ₹80 = ₹4,000
                                 ----------                   ------
                                 ₹1,50,000                    ₹12,000   = ₹1,62,000 fuel
        + ₹500 non-fuel                                                 = ₹1,62,500 sales
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    petrol = make_nozzle(priced_fuel, label="DU-1/N-1")
    gas = make_nozzle(margined_fuel, label="DU-2/N-1")

    day_two = DAY + timedelta(days=1)
    for day, litres, kilos in ((DAY, "1000.00", "100.00"), (day_two, "1500.00", "150.00")):
        started, ended = _window(day)
        shift = make_shift(attendant, business_date=day, started_at=started, ended_at=ended)
        # Readings chain: day 2 opens where day 1 closed (§4.7).
        opening_l = "0.00" if day == DAY else "1000.00"
        opening_k = "0.00" if day == DAY else "100.00"
        make_reading(shift, petrol, opening_reading=opening_l, closing_reading=litres)
        make_reading(shift, gas, opening_reading=opening_k, closing_reading=kilos)
        if day == DAY:
            make_non_fuel_sale(shift, amount="500.00")
            make_expense(shift, mode="cash", amount="300.00")

    body = await _get(
        client,
        "/reports/summary",
        auth_headers(manager),
        **{"from": DAY.isoformat(), "to": day_two.isoformat()},
    )

    assert body["from"] == DAY.isoformat()
    assert body["to"] == day_two.isoformat()
    assert body["trading_days"] == 2

    by_code = {line["code"]: line for line in body["fuel"]}
    assert Decimal(by_code["APIFUEL"]["quantity"]) == Decimal("1500.000")
    assert Decimal(by_code["APIFUEL"]["sale_value"]) == Decimal("150000.00")
    assert Decimal(by_code["APIGAS"]["quantity"]) == Decimal("150.000")
    assert Decimal(by_code["APIGAS"]["sale_value"]) == Decimal("12000.00")

    assert Decimal(body["fuel_sales_total"]) == Decimal("162000.00")
    assert Decimal(body["metered_fuel_sales"]) == Decimal("162000.00")
    assert Decimal(body["non_fuel_sales_total"]) == Decimal("500.00")
    assert Decimal(body["total_sales"]) == Decimal("162500.00")
    assert Decimal(body["expenses_total"]) == Decimal("300.00")

    # §4.5: litres and kilograms are reported separately and never added together.
    assert body["quantity_by_unit"] == {"litre": "1500.000", "kilogram": "150.000"}
    assert "total_quantity" not in body


async def test_one_unmargined_fuel_withholds_the_windows_margin_total(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    margined_fuel,
    auth_headers,
    clean_cash: None,
) -> None:
    """§13.21 over a window: unknowable, not smaller -- and never 0.00."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel, label="DU-1/N-1"),
                 opening_reading="0.00", closing_reading="1000.00")
    make_reading(shift, make_nozzle(margined_fuel, label="DU-2/N-1"),
                 opening_reading="0.00", closing_reading="100.00")

    body = await _get(
        client, "/reports/summary", auth_headers(manager),
        **{"from": DAY.isoformat(), "to": DAY.isoformat()},
    )

    by_code = {line["code"]: line for line in body["fuel"]}
    # The margined fuel still reports its own figure...
    assert Decimal(by_code["APIGAS"]["gross_fuel_margin"]) == Decimal("228.00")
    # ...while the unmargined one is null with a reason, never zero.
    assert by_code["APIFUEL"]["gross_fuel_margin"] is None
    assert by_code["APIFUEL"]["margin_unavailable_reason"] == "NO_MARGIN_FOR_DATE"

    assert body["gross_fuel_margin_total"] is None
    assert body["fuels_missing_margin"] == ["APIFUEL"]


async def test_every_fuel_margined_gives_a_window_margin_total(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_fuel_type,
    make_fuel_price,
    make_fuel_margin,
    auth_headers,
    clean_cash: None,
) -> None:
    """The owner's real state since the margins were entered: the total computes.

    Builds its own two fuels rather than adding a margin to the shared `priced_fuel`
    fixture: `fuel_margins` is append-only, so a margin attached to a fixture-owned fuel
    outlives the test that added it and breaks the next one's teardown.
    """
    admin = make_user("admin")
    priced_fuel = make_fuel_type(code="MARGFUEL", unit_of_measure="litre")
    make_fuel_price(priced_fuel, "100.00", BEFORE, entered_by=admin)
    make_fuel_margin(priced_fuel, "3.99", BEFORE, entered_by=admin)
    margined_fuel = make_fuel_type(code="MARGGAS", unit_of_measure="kilogram")
    make_fuel_price(margined_fuel, "80.00", BEFORE, entered_by=admin)
    make_fuel_margin(margined_fuel, "2.28", BEFORE, entered_by=admin)

    manager = make_user("manager")
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel, label="DU-1/N-1"),
                 opening_reading="0.00", closing_reading="1000.00")
    make_reading(shift, make_nozzle(margined_fuel, label="DU-2/N-1"),
                 opening_reading="0.00", closing_reading="100.00")

    body = await _get(
        client, "/reports/summary", auth_headers(manager),
        **{"from": DAY.isoformat(), "to": DAY.isoformat()},
    )

    # 1000 L x ₹3.99 = ₹3,990 ; 100 kg x ₹2.28 = ₹228
    assert body["fuels_missing_margin"] == []
    assert Decimal(body["gross_fuel_margin_total"]) == Decimal("4218.00")


async def test_shares_are_server_computed_strings_that_close_to_a_hundred(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    margined_fuel,
    auth_headers,
    clean_cash: None,
) -> None:
    """§14: a slice is `value / total`, so the server sends it ready-made.

    ₹1,00,000 petrol + ₹8,000 CBG = ₹1,08,000, so the shares are 92.59% / 7.41%.
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel, label="DU-1/N-1"),
                 opening_reading="0.00", closing_reading="1000.00")
    make_reading(shift, make_nozzle(margined_fuel, label="DU-2/N-1"),
                 opening_reading="0.00", closing_reading="100.00")

    body = await _get(
        client, "/reports/summary", auth_headers(manager),
        **{"from": DAY.isoformat(), "to": DAY.isoformat()},
    )

    by_code = {line["code"]: line for line in body["fuel"]}
    assert by_code["APIFUEL"]["share_pct"] == "92.59%"
    assert by_code["APIGAS"]["share_pct"] == "7.41%"

    # Strings the client can only assign -- a percentage, not a number to divide again.
    assert isinstance(by_code["APIFUEL"]["share_pct"], str)

    total = sum(
        Decimal(line["share_pct"].rstrip("%")) for line in body["fuel"]
    )
    assert total == Decimal("100.00")


async def test_a_window_reports_how_its_days_were_arrived_at(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    auth_headers,
    clean_cash: None,
) -> None:
    """§13.35: a total that mixes record and estimate must say so."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel, label="DU-1/N-1"),
                 opening_reading="0.00", closing_reading="1000.00")

    # A three-day window containing one traded day and two the outlet was shut.
    body = await _get(
        client, "/reports/summary", auth_headers(manager),
        **{"from": DAY.isoformat(), "to": (DAY + timedelta(days=2)).isoformat()},
    )

    assert body["days_by_source"]["computed"] == 1
    assert body["days_by_source"]["no_trading"] == 2
    assert body["days_by_source"]["snapshot"] == 0
    assert body["trading_days"] == 1
    assert len(body["trend"]) == 3


@pytest.mark.parametrize(
    ("span_days", "expected_status"),
    [(366, 200), (367, 422)],
)
async def test_the_summary_cap_is_366_days_inclusive(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    span_days: int,
    expected_status: int,
) -> None:
    """Deliberately looser than /reports/range's 31 -- different work, different bound."""
    manager = make_user("manager")
    end = DAY
    start = end - timedelta(days=span_days - 1)

    response = await client.get(
        "/api/v1/reports/summary",
        headers=auth_headers(manager),
        params={"from": start.isoformat(), "to": end.isoformat()},
    )
    assert response.status_code == expected_status, response.text
    if expected_status == 422:
        assert response.json()["code"] == "INVALID_DATE_RANGE"


async def test_the_range_report_still_caps_at_thirty_one(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
) -> None:
    """The looser summary cap must not have leaked into the endpoint it shares a helper with."""
    manager = make_user("manager")
    response = await client.get(
        "/api/v1/reports/range",
        headers=auth_headers(manager),
        params={
            "from": (DAY - timedelta(days=31)).isoformat(),
            "to": DAY.isoformat(),
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "INVALID_DATE_RANGE"


async def test_the_summary_refuses_a_backwards_or_future_window(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")

    backwards = await client.get(
        "/api/v1/reports/summary",
        headers=auth_headers(manager),
        params={"from": DAY.isoformat(), "to": (DAY - timedelta(days=1)).isoformat()},
    )
    assert backwards.status_code == 422
    assert backwards.json()["code"] == "INVALID_DATE_RANGE"

    ahead = date.today() + timedelta(days=2)
    future = await client.get(
        "/api/v1/reports/summary",
        headers=auth_headers(manager),
        params={"from": ahead.isoformat(), "to": ahead.isoformat()},
    )
    assert future.status_code == 422
    assert future.json()["code"] == "BUSINESS_DATE_IN_FUTURE"


async def test_the_summary_is_manager_floor(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    response = await client.get(
        "/api/v1/reports/summary", headers=auth_headers(attendant)
    )
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "INSUFFICIENT_ROLE"

    anonymous = await client.get("/api/v1/reports/summary")
    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == "NOT_AUTHENTICATED"


async def test_the_summary_writes_nothing(
    client: AsyncClient,
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    priced_fuel,
    auth_headers,
    clean_cash: None,
) -> None:
    """§8 calls this a report. A report that wrote would be Phase 15's defect returning."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    started, ended = _window(DAY)
    shift = make_shift(attendant, business_date=DAY, started_at=started, ended_at=ended)
    make_reading(shift, make_nozzle(priced_fuel, label="DU-1/N-1"),
                 opening_reading="0.00", closing_reading="1000.00")

    def counts() -> tuple[int, int]:
        with engine.begin() as conn:
            return (
                conn.execute(text("SELECT count(*) FROM daily_cash_summaries")).scalar_one(),
                conn.execute(text("SELECT count(*) FROM audit_logs")).scalar_one(),
            )

    before = counts()
    await _get(
        client, "/reports/summary", auth_headers(manager),
        **{"from": DAY.isoformat(), "to": DAY.isoformat()},
    )
    assert counts() == before
