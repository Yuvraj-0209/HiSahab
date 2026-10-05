"""The Summary tab rebuilt for reading (CLAUDE.md §6.6, §8, §13.44 -- Phase 26).

Three things the server learned to say:

* **The expense drill-down** -- `GET /reports/summary/expenses`. The list behind one bar,
  grouped by business date, with every subtotal computed here. The test that matters most is
  `test_the_drill_down_total_is_the_bars_figure`: one query feeds both, so they cannot drift.
* **Expense categories largest first**, named, with a bar scaled to the largest.
* **The udhaar bridge**, read from the billing statement. The defect it fixes is pinned by
  `test_a_bank_transfer_repayment_is_collected`: the old card summed two §6.4 drawer terms and
  never saw a repayment that reached the bank.

Money arrives in fixtures as strings and is compared as `Decimal` (§3 rule 1).
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

# In the past, because every report refuses a future business date (§6.1).
DAY = date(2026, 3, 10)
F = date(2026, 3, 1)
T = date(2026, 3, 15)
OTHER_OUTLET = UUID("00000000-0000-0000-0000-0000000000fe")


def _window(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, 0, 30, tzinfo=timezone.utc)
    return start, start + timedelta(hours=16)


async def _get(client: AsyncClient, path: str, headers: dict[str, str], **params):
    response = await client.get(f"/api/v1{path}", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _span(date_from: date = F, date_to: date = T) -> dict[str, str]:
    return {"from": date_from.isoformat(), "to": date_to.isoformat()}


def _other_outlet(engine: Engine) -> UUID:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outlets (id, name, is_active) "
                "VALUES (:id, 'Other Outlet', true) ON CONFLICT DO NOTHING"
            ).bindparams(id=OTHER_OUTLET)
        )
    return OTHER_OUTLET


def _closed_shift(make_shift, attendant: UUID, day: date, **kwargs) -> UUID:
    started, ended = _window(day)
    return make_shift(
        attendant, business_date=day, started_at=started, ended_at=ended,
        status="closed", **kwargs,
    )


# --- expense categories on the Summary ---------------------------------------


async def test_expense_categories_are_largest_first_named_and_scaled_to_the_largest(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense_category: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
    clean_expenses: None,
) -> None:
    """₹1,200 electricity, ₹600 tea, ₹300 maintenance: largest first, bars 100 / 50 / 25."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = _closed_shift(make_shift, attendant, DAY)
    tea = make_expense_category("PHTEA", display_name="Tea and snacks")
    make_expense(shift, category="maintenance", amount="300.00")
    make_expense(shift, category_id=tea, amount="600.00")
    make_expense(shift, category="electricity", amount="1200.00")

    body = await _get(client, "/reports/summary", auth_headers(manager), **_span())

    rows = body["expenses_by_category"]
    assert [row["code"] for row in rows] == ["ELECTRICITY", "PHTEA", "MAINTENANCE"]
    assert [row["bar_pct"] for row in rows] == ["100.00%", "50.00%", "25.00%"]
    # `share_pct` is still the part of ALL expenses: 600 / 2,100.
    assert rows[1]["share_pct"] == "28.57%"
    assert rows[1]["display_name"] == "Tea and snacks"
    assert Decimal(body["expenses_total"]) == Decimal("2100.00")


async def test_the_money_arrived_split_has_left_the_summary(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
) -> None:
    """The owner removed the card; nothing else read `payment_mix`, so it went too (§11)."""
    manager = make_user("manager")

    body = await _get(client, "/reports/summary", auth_headers(manager), **_span())

    assert "payment_mix" not in body
    # The §6.4 terms themselves stay: they are the window's cash figures, not the chart.
    assert "card_total" in body
    assert "cash_sales" in body


# --- GET /reports/summary/expenses ---------------------------------------------


async def test_the_drill_down_lists_one_category_by_date_newest_first(
    client: AsyncClient,
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
    clean_expenses: None,
) -> None:
    """Two days of maintenance, one of electricity that must not appear.

    The 12th's rows are entered FIRST and the 5th's afterwards -- a back-entered day, §4.7's
    normal case -- so ordering by `created_at` would put the 5th on top. It must not.
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    fifth = _closed_shift(make_shift, attendant, date(2026, 3, 5))
    twelfth = _closed_shift(make_shift, attendant, date(2026, 3, 12))

    make_expense(twelfth, category="maintenance", amount="500.00",
                 description="Nozzle seal", paid_to="Sharma Pumps")
    make_expense(twelfth, category="electricity", amount="999.00")
    make_expense(fifth, category="maintenance", amount="300.00", description="Grease")
    make_expense(fifth, category="maintenance", amount="200.00", description="Hose clamp")

    def audit_count() -> int:
        with engine.connect() as connection:
            return connection.execute(text("SELECT count(*) FROM audit_logs")).scalar_one()

    before = audit_count()
    body = await _get(
        client, "/reports/summary/expenses", auth_headers(manager),
        category="MAINTENANCE", **_span(),
    )

    assert body["code"] == "MAINTENANCE"
    assert body["from"] == F.isoformat() and body["to"] == T.isoformat()
    assert Decimal(body["total"]) == Decimal("1000.00")
    assert body["row_count"] == 3
    assert body["truncated"] is False

    assert [day["business_date"] for day in body["days"]] == ["2026-03-12", "2026-03-05"]
    twelfth_day, fifth_day = body["days"]
    assert Decimal(twelfth_day["total"]) == Decimal("500.00")
    assert Decimal(fifth_day["total"]) == Decimal("500.00")
    assert twelfth_day["items"][0]["description"] == "Nozzle seal"
    assert twelfth_day["items"][0]["paid_to"] == "Sharma Pumps"
    assert twelfth_day["items"][0]["mode"] == "cash"
    assert {item["description"] for item in fifth_day["items"]} == {"Grease", "Hose clamp"}

    # A report writes nothing, not even an audit row.
    assert audit_count() == before


async def test_the_drill_down_total_is_the_bars_figure_reversals_included(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
    clean_expenses: None,
) -> None:
    """₹800 entered, reversed, re-entered as ₹600: both sides show ₹600 and tag the pair.

    The reversal is listed rather than hidden, as `totals_by_category_range` counts it -- a
    drill-down that dropped it would sum to ₹1,400 beneath a ₹600 bar.
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = _closed_shift(make_shift, attendant, DAY)
    original = make_expense(shift, category="maintenance", amount="800.00")
    reversal = make_expense(
        shift, category="maintenance", amount="-800.00",
        reverses_id=original, reversal_reason="Typed 800 for 600",
    )
    make_expense(shift, category="maintenance", amount="600.00")

    summary = await _get(client, "/reports/summary", auth_headers(manager), **_span())
    drill = await _get(
        client, "/reports/summary/expenses", auth_headers(manager),
        category="MAINTENANCE", **_span(),
    )

    bar = next(row for row in summary["expenses_by_category"] if row["code"] == "MAINTENANCE")
    assert Decimal(drill["total"]) == Decimal(bar["amount"]) == Decimal("600.00")
    assert Decimal(drill["days"][0]["total"]) == Decimal("600.00")

    items = {item["id"]: item for item in drill["days"][0]["items"]}
    assert items[str(original)]["is_reversed"] is True
    assert items[str(original)]["is_reversal"] is False
    assert items[str(reversal)]["is_reversal"] is True
    assert items[str(reversal)]["reversal_reason"] == "Typed 800 for 600"


async def test_the_drill_down_is_cut_only_between_whole_days(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
    clean_expenses: None,
) -> None:
    """§13.44 with a cap of 2: the 12th (two rows) fits, the 5th would make three and is cut.

    The totals are over all three rows regardless -- the grand total is still the bar's.
    """
    from app.api.v1 import reports as reports_module

    monkeypatch.setattr(reports_module, "_MAX_DRILL_ROWS", 2)
    manager = make_user("manager")
    attendant = make_user("attendant")
    fifth = _closed_shift(make_shift, attendant, date(2026, 3, 5))
    twelfth = _closed_shift(make_shift, attendant, date(2026, 3, 12))
    make_expense(twelfth, category="maintenance", amount="100.00")
    make_expense(twelfth, category="maintenance", amount="100.00")
    make_expense(fifth, category="maintenance", amount="50.00")

    body = await _get(
        client, "/reports/summary/expenses", auth_headers(manager),
        category="MAINTENANCE", **_span(),
    )

    assert body["truncated"] is True
    assert [day["business_date"] for day in body["days"]] == ["2026-03-12"]
    assert len(body["days"][0]["items"]) == 2
    assert Decimal(body["total"]) == Decimal("250.00")
    assert body["row_count"] == 3


async def test_the_first_day_is_listed_whole_even_past_the_cap(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
    clean_expenses: None,
) -> None:
    """A cap below one day's rows still shows that day entire: an empty list beneath a
    non-zero total would be the worst possible answer."""
    from app.api.v1 import reports as reports_module

    monkeypatch.setattr(reports_module, "_MAX_DRILL_ROWS", 1)
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = _closed_shift(make_shift, attendant, DAY)
    make_expense(shift, category="maintenance", amount="100.00")
    make_expense(shift, category="maintenance", amount="100.00")

    body = await _get(
        client, "/reports/summary/expenses", auth_headers(manager),
        category="MAINTENANCE", **_span(),
    )

    assert body["truncated"] is False
    assert len(body["days"][0]["items"]) == 2


async def test_an_unknown_category_is_404(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")

    response = await client.get(
        "/api/v1/reports/summary/expenses",
        headers=auth_headers(manager),
        params={"category": "NOSUCHTHING", **_span()},
    )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "CATEGORY_NOT_FOUND"


async def test_another_outlets_category_and_rows_are_invisible(
    client: AsyncClient,
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense_category: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    clean_cash: None,
    clean_expenses: None,
) -> None:
    """Asserted by inserting the other outlet's rows, never by an empty page (§10).

    The same code exists at both outlets; only this outlet's rows may come back, and a code
    that exists only elsewhere is 404, not 403 -- existence is not leaked (§7.3).
    """
    other = _other_outlet(engine)
    stranger = make_user("attendant", outlet_id=other)
    manager = make_user("manager")
    attendant = make_user("attendant")

    theirs = make_expense_category("PHSHARED", outlet_id=other)
    make_expense_category("PHONLYTHEM", outlet_id=other)
    ours = make_expense_category("PHSHARED")
    their_shift = _closed_shift(make_shift, stranger, DAY, outlet_id=other)
    our_shift = _closed_shift(make_shift, attendant, DAY)
    make_expense(their_shift, category_id=theirs, amount="99999.00")
    make_expense(our_shift, category_id=ours, amount="40.00")

    body = await _get(
        client, "/reports/summary/expenses", auth_headers(manager),
        category="PHSHARED", **_span(),
    )
    assert Decimal(body["total"]) == Decimal("40.00")
    assert body["row_count"] == 1

    elsewhere = await client.get(
        "/api/v1/reports/summary/expenses",
        headers=auth_headers(manager),
        params={"category": "PHONLYTHEM", **_span()},
    )
    assert elsewhere.status_code == 404
    assert elsewhere.json()["code"] == "CATEGORY_NOT_FOUND"


@pytest.mark.parametrize(("role", "expected"), [("attendant", 403), ("manager", 200), ("admin", 200)])
async def test_the_drill_down_is_manager_floor(
    role: str,
    expected: int,
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
) -> None:
    """§8's new row: a summary is a report, and so is the list behind one of its bars."""
    user = make_user(role)

    response = await client.get(
        "/api/v1/reports/summary/expenses",
        headers=auth_headers(user),
        params={"category": "MAINTENANCE", **_span()},
    )

    assert response.status_code == expected, response.text
    if expected == 403:
        assert response.json()["code"] == "INSUFFICIENT_ROLE"

    anonymous = await client.get(
        "/api/v1/reports/summary/expenses", params={"category": "MAINTENANCE"}
    )
    assert anonymous.status_code == 401


async def test_the_drill_down_refuses_a_bad_window(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
) -> None:
    """The same window rules as the Summary it drills into, because it is the same window."""
    manager = make_user("manager")

    backwards = await client.get(
        "/api/v1/reports/summary/expenses",
        headers=auth_headers(manager),
        params={"category": "MAINTENANCE", "from": T.isoformat(), "to": F.isoformat()},
    )
    assert backwards.status_code == 422
    assert backwards.json()["code"] == "INVALID_DATE_RANGE"

    missing = await client.get(
        "/api/v1/reports/summary/expenses", headers=auth_headers(manager), params=_span()
    )
    assert missing.status_code == 422


# --- the udhaar bridge --------------------------------------------------------


async def test_a_bank_transfer_repayment_is_collected(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
    auth_headers,
) -> None:
    """The defect Phase 26 found: a shift-less bank transfer never reached the Summary.

        owed at start   ₹10,000   (opening balance, 1 Feb)
        + given          ₹4,000   (udhaar on the 5th)
        - collected      ₹3,000   (₹1,000 cash on a shift, ₹2,000 by bank with no shift)
        = owed at end   ₹11,000

    The old card read only the two §6.4 drawer terms, so it said ₹1,000 came back.
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = _closed_shift(make_shift, attendant, date(2026, 3, 5))
    customer = make_credit_customer(name="Ramesh Transport")

    make_credit_opening_balance(customer, amount="10000.00", as_of_date=date(2026, 2, 1))
    make_credit_sale(shift, customer, make_attachment(attendant), amount="4000.00")
    make_credit_repayment(shift, customer, amount="1000.00", mode="cash")
    make_credit_repayment(
        None, customer, amount="2000.00", mode="bank_transfer",
        business_date=date(2026, 3, 8),
    )

    body = await _get(client, "/reports/summary", auth_headers(manager), **_span())
    credit = body["credit"]

    assert Decimal(credit["owed_at_start"]) == Decimal("10000.00")
    assert Decimal(credit["given"]) == Decimal("4000.00")
    assert Decimal(credit["collected"]) == Decimal("3000.00")
    assert Decimal(credit["owed_at_end"]) == Decimal("11000.00")
    assert Decimal(credit["owes_today"]) == Decimal("11000.00")
    # The drawer term is still there and still right about the drawer -- it is simply not
    # the answer to "what did customers pay".
    assert Decimal(body["cash_credit_repayments"]) == Decimal("1000.00")


async def test_the_bridge_is_the_statements_totals_to_the_paisa(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
    auth_headers,
) -> None:
    """Two customers, a reversal, a payment after the window: still one set of figures."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    before = _closed_shift(make_shift, attendant, date(2026, 2, 20))
    inside = _closed_shift(make_shift, attendant, date(2026, 3, 6))
    after = _closed_shift(make_shift, attendant, date(2026, 3, 20))
    first = make_credit_customer(name="Gupta Tractors")
    second = make_credit_customer(name="Verma Logistics")

    make_credit_opening_balance(first, amount="5250.50", as_of_date=date(2026, 1, 1))
    make_credit_sale(before, second, make_attachment(attendant), amount="1200.00")
    slip = make_credit_sale(inside, first, make_attachment(attendant), amount="900.00")
    make_credit_sale(
        inside, first, make_attachment(attendant), amount="-900.00",
        reverses_id=slip, reversal_reason="Wrong customer",
    )
    make_credit_sale(inside, second, make_attachment(attendant), amount="3333.33")
    make_credit_repayment(inside, first, amount="250.50", mode="upi")
    make_credit_repayment(after, second, amount="1000.00", mode="cash")

    summary = await _get(client, "/reports/summary", auth_headers(manager), **_span())
    statement = await _get(
        client, "/credit-customers/statement", auth_headers(manager), **_span()
    )

    credit, totals = summary["credit"], statement["totals"]
    assert Decimal(credit["owed_at_start"]) == Decimal(totals["owed_before"])
    assert Decimal(credit["given"]) == Decimal(totals["udhaar_in"])
    assert Decimal(credit["collected"]) == Decimal(totals["repaid_in"])
    assert Decimal(credit["owed_at_end"]) == Decimal(totals["billed"])
    assert Decimal(credit["owes_today"]) == Decimal(totals["owes_today"])

    # And the figures themselves: 6,450.50 + 3,333.33 - 250.50 = 9,533.33.
    assert Decimal(credit["owed_at_start"]) == Decimal("6450.50")
    assert Decimal(credit["given"]) == Decimal("3333.33")
    assert Decimal(credit["owed_at_end"]) == Decimal("9533.33")
    assert Decimal(credit["owes_today"]) == Decimal("8533.33")


async def test_the_bridge_geometry_is_a_waterfall_computed_here(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
    auth_headers,
) -> None:
    """Start ₹6,000 + given ₹2,000 - collected ₹4,000 = end ₹4,000, over an extent of ₹8,000.

        start      [0%,  75%]
        given      [75%, 25%]
        collected  [50%, 50%]   ending where given ended
        end        [0%,  50%]
    """
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = _closed_shift(make_shift, attendant, DAY)
    customer = make_credit_customer(name="Bridge Customer")
    make_credit_opening_balance(customer, amount="6000.00", as_of_date=date(2026, 2, 1))
    make_credit_sale(shift, customer, make_attachment(attendant), amount="2000.00")
    make_credit_repayment(shift, customer, amount="4000.00", mode="cash")

    body = await _get(client, "/reports/summary", auth_headers(manager), **_span())
    steps = {step["key"]: step for step in body["credit"]["bridge"]}

    assert [step["key"] for step in body["credit"]["bridge"]] == [
        "start", "given", "collected", "end",
    ]
    assert (steps["start"]["offset_pct"], steps["start"]["width_pct"]) == ("0.00%", "75.00%")
    assert (steps["given"]["offset_pct"], steps["given"]["width_pct"]) == ("75.00%", "25.00%")
    assert (steps["collected"]["offset_pct"], steps["collected"]["width_pct"]) == (
        "50.00%", "50.00%",
    )
    assert (steps["end"]["offset_pct"], steps["end"]["width_pct"]) == ("0.00%", "50.00%")
    assert Decimal(steps["collected"]["amount"]) == Decimal("4000.00")


async def test_a_negative_balance_withholds_the_geometry_not_the_figures(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
    auth_headers,
) -> None:
    """§6.6: a customer who paid in advance is owed by the pump. That is legitimate, and it
    has no honest bar on a left-to-right walk -- so no bar, and the figure still printed."""
    manager = make_user("manager")
    customer = make_credit_customer(name="Paid Ahead")
    make_credit_opening_balance(customer, amount="-500.00", as_of_date=date(2026, 2, 1))

    body = await _get(client, "/reports/summary", auth_headers(manager), **_span())
    credit = body["credit"]

    assert Decimal(credit["owed_at_start"]) == Decimal("-500.00")
    assert all(step["width_pct"] is None for step in credit["bridge"])
    assert all(step["offset_pct"] is None for step in credit["bridge"])
    assert credit["customers_owing"] == 0
    assert credit["top_owing"] == []


async def test_the_card_names_the_five_who_owe_most_at_the_windows_end(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
    auth_headers,
) -> None:
    """Six owe something and one is in credit: five named, biggest first, six counted."""
    manager = make_user("manager")
    amounts = ["100.00", "600.00", "300.00", "500.00", "200.00", "400.00"]
    for index, amount in enumerate(amounts):
        customer = make_credit_customer(name=f"Owing {index}")
        make_credit_opening_balance(customer, amount=amount, as_of_date=date(2026, 2, 1))
    in_credit = make_credit_customer(name="In credit")
    make_credit_opening_balance(in_credit, amount="-50.00", as_of_date=date(2026, 2, 1))

    body = await _get(client, "/reports/summary", auth_headers(manager), **_span())
    credit = body["credit"]

    assert credit["customers_owing"] == 6
    assert [row["name"] for row in credit["top_owing"]] == [
        "Owing 1", "Owing 3", "Owing 5", "Owing 2", "Owing 4",
    ]
    assert Decimal(credit["top_owing"][0]["owed_at_end"]) == Decimal("600.00")


def test_an_empty_ledger_draws_no_bridge() -> None:
    """Nothing owed and nothing moved: an extent of zero has no bar to scale against."""
    from app.services.reporting import credit_bridge

    zero = Decimal("0.00")
    steps = credit_bridge(start=zero, given=zero, collected=zero, end=zero)

    assert [step.key for step in steps] == ["start", "given", "collected", "end"]
    assert all(step.width_pct is None and step.offset_pct is None for step in steps)
    assert all(step.amount == zero for step in steps)
