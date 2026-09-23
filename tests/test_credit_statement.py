"""The billing-period statement (CLAUDE.md §6.6, §8, §13.40-42 -- Phase 21).

This outlet bills every udhaar customer on the 16th (for the 1st-15th) and on the 1st (for the
rest). A bill is §6.6's outstanding sum cut at two dates, so almost every test here is an
arithmetic test with the boundaries placed deliberately either side of the window:

    owed_before  = openings(as_of <= T) + sales(< F) - repayments(< F)
    udhaar_in    = sales(F..T)
    repaid_in    = repayments(F..T)
    billed       = owed_before + udhaar_in - repaid_in
    owes_today   = billed + opening_since + udhaar_since - paid_since   (== outstanding())

Money arrives in fixtures as strings and is compared as `Decimal`, never as a float (§3 rule 1).
Other tests' customers may exist at the outlet while these run, so every assertion finds its
own customer's row by id rather than assuming it is alone.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.anyio

# The window every test bills: the first half of August 2026.
F = date(2026, 8, 1)
T = date(2026, 8, 15)


async def _statement(
    client: AsyncClient, headers: dict[str, str], date_from: date = F, date_to: date = T
):
    return await client.get(
        "/api/v1/credit-customers/statement",
        params={"from": date_from.isoformat(), "to": date_to.isoformat()},
        headers=headers,
    )


def _row(body: dict, customer_id: UUID) -> dict | None:
    for row in body["rows"]:
        if row["customer_id"] == str(customer_id):
            return row
    return None


def _outstanding(customer_id: UUID) -> Decimal:
    from app.db.session import SessionLocal
    from app.services import credit as credit_service

    with SessionLocal() as session:
        return credit_service.outstanding(session, customer_id=customer_id)


def _money(row: dict, field: str) -> Decimal:
    return Decimal(row[field])


# --- the arithmetic ----------------------------------------------------------


async def test_every_column_with_every_term_non_zero(
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
    """§6.6's worked statement, each term placed on a different side of the window."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    july = make_shift(attendant, business_date=date(2026, 7, 20), status="closed")
    in_range = make_shift(attendant, business_date=date(2026, 8, 5), status="closed")
    after = make_shift(attendant, business_date=date(2026, 8, 20), status="closed")
    customer = make_credit_customer(name="Ramesh")

    make_credit_opening_balance(customer, amount="12400.00", as_of_date=date(2026, 7, 1))
    make_credit_sale(july, customer, make_attachment(attendant), amount="3000.00")
    make_credit_repayment(july, customer, amount="2000.00")
    make_credit_sale(in_range, customer, make_attachment(attendant), amount="4500.00")
    make_credit_repayment(in_range, customer, amount="1500.00")
    make_credit_sale(after, customer, make_attachment(attendant), amount="800.00")
    make_credit_repayment(after, customer, amount="6000.00")

    response = await _statement(client, auth_headers(manager))

    assert response.status_code == 200, response.text
    row = _row(response.json(), customer)
    assert row is not None
    assert _money(row, "owed_before") == Decimal("13400.00")  # 12,400 + 3,000 - 2,000
    assert _money(row, "udhaar_in") == Decimal("4500.00")
    assert _money(row, "repaid_in") == Decimal("1500.00")
    assert _money(row, "billed") == Decimal("16400.00")
    assert _money(row, "udhaar_since") == Decimal("800.00")
    assert _money(row, "paid_since") == Decimal("6000.00")
    assert _money(row, "opening_since") == Decimal("0.00")
    assert _money(row, "owes_today") == Decimal("11200.00")
    # The identity the "since" figures exist to make exact, and the definition it must equal.
    assert _money(row, "owes_today") == (
        _money(row, "billed")
        + _money(row, "opening_since")
        + _money(row, "udhaar_since")
        - _money(row, "paid_since")
    )
    assert _money(row, "owes_today") == _outstanding(customer)


async def test_the_window_boundaries_are_inclusive(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    """F-1 is before, F and T are in, T+1 is since. One paisa-sized mistake here moves a
    whole day's udhaar onto the wrong bill."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    day_before = make_shift(attendant, business_date=F - timedelta(days=1), status="closed")
    first = make_shift(attendant, business_date=F, status="closed")
    last = make_shift(attendant, business_date=T, status="closed")
    day_after = make_shift(attendant, business_date=T + timedelta(days=1), status="closed")
    customer = make_credit_customer(name="Boundary")

    make_credit_sale(day_before, customer, make_attachment(attendant), amount="100.00")
    make_credit_sale(first, customer, make_attachment(attendant), amount="200.00")
    make_credit_sale(last, customer, make_attachment(attendant), amount="400.00")
    make_credit_sale(day_after, customer, make_attachment(attendant), amount="800.00")
    make_credit_repayment(day_before, customer, amount="10.00")
    make_credit_repayment(first, customer, amount="20.00")
    make_credit_repayment(last, customer, amount="40.00")
    make_credit_repayment(day_after, customer, amount="80.00")

    row = _row((await _statement(client, auth_headers(manager))).json(), customer)

    assert _money(row, "owed_before") == Decimal("90.00")
    assert _money(row, "udhaar_in") == Decimal("600.00")
    assert _money(row, "repaid_in") == Decimal("60.00")
    assert _money(row, "billed") == Decimal("630.00")
    assert _money(row, "udhaar_since") == Decimal("800.00")
    assert _money(row, "paid_since") == Decimal("80.00")


async def test_a_sale_is_dated_by_its_shift_not_by_when_it_was_typed(
    client: AsyncClient,
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """§4.7: the day is typed in after the fact. A slip for 14 August entered on 20 August
    belongs on the 1-15 bill, and `created_at` must not be able to move it."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 8, 14), status="closed")
    customer = make_credit_customer(name="Late Entry")
    sale = make_credit_sale(shift, customer, make_attachment(attendant), amount="750.00")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE credit_sales SET created_at = :at WHERE id = :id").bindparams(
                at=datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc), id=sale
            )
        )

    row = _row((await _statement(client, auth_headers(manager))).json(), customer)

    assert _money(row, "udhaar_in") == Decimal("750.00")
    assert _money(row, "udhaar_since") == Decimal("0.00")
    assert [line["period"] for line in row["lines"]] == ["in_range"]


async def test_an_opening_balance_inside_the_window_is_owed_before_not_udhaar(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
    auth_headers,
) -> None:
    """§6.6: years of history compressed onto 5 August is not this fortnight's fuel. It is
    counted in owed_before -- and still shown as a line, so it is not hidden."""
    manager = make_user("manager")
    customer = make_credit_customer(name="Started Mid Window")
    balance = make_credit_opening_balance(
        customer, amount="9000.00", as_of_date=date(2026, 8, 5)
    )

    row = _row((await _statement(client, auth_headers(manager))).json(), customer)

    assert _money(row, "owed_before") == Decimal("9000.00")
    assert _money(row, "udhaar_in") == Decimal("0.00")
    assert _money(row, "billed") == Decimal("9000.00")
    assert row["opening_balance_entered"] is True
    assert [(line["kind"], line["id"], line["period"]) for line in row["lines"]] == [
        ("opening", str(balance), "in_range")
    ]


async def test_an_opening_balance_after_the_window_keeps_owes_today_reconcilable(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
    auth_headers,
) -> None:
    """A customer whose ledger starts after the window owed nothing *on this bill*, but owes
    today -- and `opening_since` is the term that keeps the identity exact."""
    manager = make_user("manager")
    customer = make_credit_customer(name="Started Later")
    make_credit_opening_balance(customer, amount="2500.00", as_of_date=date(2026, 8, 20))

    row = _row((await _statement(client, auth_headers(manager))).json(), customer)

    assert _money(row, "billed") == Decimal("0.00")
    assert _money(row, "opening_since") == Decimal("2500.00")
    assert _money(row, "owes_today") == Decimal("2500.00")
    assert row["lines"][0]["period"] == "since"


async def test_a_reversal_nets_inside_its_originals_period_even_when_entered_later(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """§6.9 copies the original's shift onto the reversal, so the pair nets inside 1-15 even
    though it was entered weeks later (§13.41) -- and both lines are shown, tagged."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 8, 10), status="closed")
    customer = make_credit_customer(name="Cancelled Slip")
    receipt = make_attachment(attendant)
    original = make_credit_sale(shift, customer, receipt, amount="1200.00")
    reversal = make_credit_sale(
        shift,
        customer,
        receipt,
        amount="-1200.00",
        reverses_id=original,
        reversal_reason="Typed against the wrong customer",
    )

    row = _row((await _statement(client, auth_headers(manager))).json(), customer)

    assert row is not None, "net zero is still activity (D4)"
    assert _money(row, "udhaar_in") == Decimal("0.00")
    by_id = {line["id"]: line for line in row["lines"]}
    assert by_id[str(original)]["is_reversed"] is True
    assert by_id[str(original)]["is_reversal"] is False
    assert by_id[str(reversal)]["is_reversal"] is True
    assert by_id[str(reversal)]["reversal_reason"] == "Typed against the wrong customer"
    assert by_id[str(reversal)]["amount"] == "-1200.00"


async def test_a_bank_repayment_and_a_shift_repayment_both_count(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
    auth_headers,
) -> None:
    """§5.2: a repayment with only a date arrived at the bank. It is still a repayment."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 8, 3), status="closed")
    customer = make_credit_customer(name="Two Channels")
    make_credit_opening_balance(customer, amount="5000.00", as_of_date=date(2026, 7, 1))
    make_credit_repayment(shift, customer, amount="1000.00", mode="cash")
    make_credit_repayment(
        None, customer, amount="2000.00", mode="bank_transfer", business_date=date(2026, 8, 9)
    )

    row = _row((await _statement(client, auth_headers(manager))).json(), customer)

    assert _money(row, "repaid_in") == Decimal("3000.00")
    assert _money(row, "billed") == Decimal("2000.00")
    modes = sorted(line["mode"] for line in row["lines"])
    assert modes == ["bank_transfer", "cash"]


async def test_billed_agrees_with_the_ledgers_running_balance_on_the_last_day(
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
    """Two screens, one account. `billed` is the ledger's `balance_after` on the last line
    dated on or before T, and a bill that disagreed with the ledger could not be checked."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    before = make_shift(attendant, business_date=date(2026, 8, 4), status="closed")
    later = make_shift(attendant, business_date=date(2026, 8, 25), status="closed")
    customer = make_credit_customer(name="Cross Check")
    make_credit_opening_balance(customer, amount="700.00", as_of_date=date(2026, 7, 1))
    make_credit_sale(before, customer, make_attachment(attendant), amount="1300.00")
    make_credit_repayment(before, customer, amount="500.00")
    make_credit_sale(later, customer, make_attachment(attendant), amount="999.00")

    headers = auth_headers(manager)
    row = _row((await _statement(client, headers)).json(), customer)
    ledger = (
        await client.get(f"/api/v1/credit-customers/{customer}/ledger", headers=headers)
    ).json()
    on_or_before_t = [
        entry for entry in ledger["items"] if date.fromisoformat(entry["business_date"]) <= T
    ]

    assert row["billed"] == on_or_before_t[0]["balance_after"] == "1500.00"


# --- who is listed (D4) ------------------------------------------------------


async def test_a_square_customer_with_no_activity_is_not_listed(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    customer = make_credit_customer(name="Square")
    make_credit_opening_balance(customer, amount="0.00", as_of_date=date(2026, 7, 1))

    body = (await _statement(client, auth_headers(manager))).json()

    assert _row(body, customer) is None


async def test_a_customer_who_only_paid_after_the_bill_is_listed(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    """Cleared in full after the bill: owes nothing today, owed nothing before, but paying
    is exactly what the owner opened this screen to see."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    july = make_shift(attendant, business_date=date(2026, 7, 10), status="closed")
    customer = make_credit_customer(name="Paid Up")
    make_credit_sale(july, customer, make_attachment(attendant), amount="400.00")
    make_credit_repayment(
        None, customer, amount="400.00", mode="bank_transfer", business_date=date(2026, 8, 20)
    )

    row = _row((await _statement(client, auth_headers(manager))).json(), customer)

    assert row is not None
    assert _money(row, "billed") == Decimal("400.00")
    assert _money(row, "paid_since") == Decimal("400.00")
    assert _money(row, "owes_today") == Decimal("0.00")


async def test_a_missing_opening_balance_is_not_the_same_as_zero(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
    auth_headers,
) -> None:
    """§6.8, §14: both sum to ₹0.00 before the window, and only one of them was checked."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 8, 6), status="closed")
    unknown = make_credit_customer(name="Never Entered")
    checked = make_credit_customer(name="Checked Square")
    make_credit_opening_balance(checked, amount="0.00", as_of_date=date(2026, 7, 1))
    make_credit_sale(shift, unknown, make_attachment(attendant), amount="100.00")
    make_credit_sale(shift, checked, make_attachment(attendant), amount="100.00")

    body = (await _statement(client, auth_headers(manager))).json()

    assert _row(body, unknown)["opening_balance_entered"] is False
    assert _row(body, checked)["opening_balance_entered"] is True
    assert _row(body, unknown)["owed_before"] == _row(body, checked)["owed_before"] == "0.00"


async def test_rows_are_ordered_biggest_bill_first_and_totals_are_the_column_sums(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    small = make_credit_customer(name="Small")
    large = make_credit_customer(name="Large")
    make_credit_opening_balance(small, amount="100.00", as_of_date=date(2026, 7, 1))
    make_credit_opening_balance(large, amount="9000.00", as_of_date=date(2026, 7, 1))

    body = (await _statement(client, auth_headers(manager))).json()
    ids = [row["customer_id"] for row in body["rows"]]

    assert ids.index(str(large)) < ids.index(str(small))
    for field in (
        "owed_before",
        "udhaar_in",
        "repaid_in",
        "billed",
        "opening_since",
        "udhaar_since",
        "paid_since",
        "owes_today",
    ):
        assert Decimal(body["totals"][field]) == sum(
            (Decimal(row[field]) for row in body["rows"]), Decimal("0.00")
        ), field


# --- the open-shift warning (D9) -------------------------------------------------


async def test_udhaar_on_an_open_shift_counts_and_is_warned_about(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    clean_shifts,
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 8, 12), status="open")
    customer = make_credit_customer(name="Still Open")
    make_credit_sale(shift, customer, make_attachment(attendant), amount="650.00")

    body = (await _statement(client, auth_headers(manager))).json()

    assert body["open_shift_count"] == 1
    assert _money(_row(body, customer), "udhaar_in") == Decimal("650.00")


async def test_no_warning_when_every_shift_in_the_window_is_closed(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    clean_shifts,
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    make_shift(attendant, business_date=date(2026, 8, 12), status="closed")
    # An open shift *outside* the window is not this bill's problem.
    make_shift(attendant, business_date=date(2026, 8, 20), status="open")

    body = (await _statement(client, auth_headers(manager))).json()

    assert body["open_shift_count"] == 0


# --- sales detail --------------------------------------------------------------------


async def test_a_sale_line_carries_its_unit_and_never_assumes_litres(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§4.5: CBG is sold by the kilogram. A quantity without its unit is meaningless."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 8, 7), status="closed")
    customer = make_credit_customer(name="Gas Van")
    make_credit_sale(
        shift,
        customer,
        make_attachment(attendant),
        amount="890.00",
        fuel_type_id=fuel_type_ids["CBG"],
        quantity="10.000",
        vehicle_number="MH12AB1234",
    )

    row = _row((await _statement(client, auth_headers(manager))).json(), customer)
    line = row["lines"][0]

    assert line["unit_of_measure"] == "kilogram"
    assert line["quantity"] == "10.000"
    assert line["vehicle_number"] == "MH12AB1234"
    assert line["bank_status"] is None


async def test_the_line_cap_truncates_lines_but_never_totals(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    from app.services import credit as credit_service

    monkeypatch.setattr(credit_service, "MAX_STATEMENT_LINES", 2)
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 8, 8), status="closed")
    customer = make_credit_customer(name="Many Slips")
    for _ in range(4):
        make_credit_sale(shift, customer, make_attachment(attendant), amount="100.00")

    body = (await _statement(client, auth_headers(manager))).json()

    assert body["lines_truncated"] is True
    assert sum(len(row["lines"]) for row in body["rows"]) == 2
    assert _money(_row(body, customer), "udhaar_in") == Decimal("400.00")


async def test_money_is_serialised_as_strings(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
    auth_headers,
) -> None:
    """§3 rule 1 does not stop at the API boundary: a float in JSON is a float in JS."""
    manager = make_user("manager")
    customer = make_credit_customer(name="Strings")
    make_credit_opening_balance(customer, amount="10.10", as_of_date=date(2026, 7, 1))

    body = (await _statement(client, auth_headers(manager))).json()
    row = _row(body, customer)

    assert row["billed"] == "10.10"
    assert all(isinstance(value, str) for value in body["totals"].values())


# --- the bank tick (D8, §13.42) --------------------------------------------------------


@pytest.fixture
def bank_account(engine: Engine) -> Iterator[UUID]:
    """One bank account with a statement import covering all of August 2026."""
    from app.core.config import get_settings

    account_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO bank_accounts (id, outlet_id, label, bank_name) "
                "VALUES (:id, :outlet, :label, 'Bank of Baroda')"
            ).bindparams(
                id=account_id,
                outlet=get_settings().DEFAULT_OUTLET_ID,
                label=f"Statement {account_id.hex[:8]}",
            )
        )
    try:
        yield account_id
    finally:
        with engine.begin() as connection:
            for table in ("bank_transactions", "bank_statement_imports"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE bank_account_id = :id").bindparams(
                        id=account_id
                    )
                )
            connection.execute(
                text("DELETE FROM bank_accounts WHERE id = :id").bindparams(id=account_id)
            )


@pytest.fixture
def make_statement_line(
    engine: Engine, bank_account: UUID
) -> Callable[..., UUID]:
    import_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO bank_statement_imports (id, bank_account_id, period_from, "
                "period_to, row_count, imported_count, skipped_count) "
                "VALUES (:id, :account, '2026-08-01', '2026-08-31', 0, 0, 0)"
            ).bindparams(id=import_id, account=bank_account)
        )

    def _make(
        txn_date: date,
        amount: str,
        *,
        narration: str = "NEFT FROM A CUSTOMER",
        credit_repayment_id: UUID | None = None,
    ) -> UUID:
        txn_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO bank_transactions (id, bank_account_id, import_id, "
                    "txn_date, narration, amount, direction, classification, fingerprint, "
                    "credit_repayment_id) VALUES (:id, :account, :import_id, :d, "
                    ":narration, CAST(:amount AS numeric), 'credit', 'udhaar_repayment', "
                    ":fp, :repayment)"
                ).bindparams(
                    id=txn_id,
                    account=bank_account,
                    import_id=import_id,
                    d=txn_date,
                    narration=narration,
                    amount=amount,
                    fp=txn_id.hex,
                    repayment=credit_repayment_id,
                )
            )
        return txn_id

    return _make


def _line(row: dict, line_id: UUID) -> dict:
    return next(line for line in row["lines"] if line["id"] == str(line_id))


async def test_a_linked_repayment_is_verified(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    make_statement_line: Callable[..., UUID],
    auth_headers,
) -> None:
    """Created from a statement line (Phase 20's confirm), so the link is on the row."""
    manager = make_user("manager")
    customer = make_credit_customer(name="Linked")
    repayment = make_credit_repayment(
        None, customer, amount="3100.00", mode="bank_transfer", business_date=date(2026, 8, 4)
    )
    # A linked line is excluded from the live scan, so only the stored link can tick it.
    make_statement_line(date(2026, 8, 4), "3100.00", credit_repayment_id=repayment)

    row = _row((await _statement(client, auth_headers(manager))).json(), customer)

    assert _line(row, repayment)["bank_status"] == "verified"


async def test_a_typed_repayment_verified_by_the_live_match(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    make_statement_line: Callable[..., UUID],
    auth_headers,
) -> None:
    """Nothing stores this answer; the Bank screen and this one both compute it (§13.42)."""
    manager = make_user("manager")
    customer = make_credit_customer(name="Typed")
    repayment = make_credit_repayment(
        None, customer, amount="2750.00", mode="bank_transfer", business_date=date(2026, 8, 6)
    )
    make_statement_line(date(2026, 8, 6), "2750.00")

    row = _row((await _statement(client, auth_headers(manager))).json(), customer)

    assert _line(row, repayment)["bank_status"] == "verified"


async def test_two_identical_payments_are_both_ambiguous_and_neither_is_ticked(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    make_statement_line: Callable[..., UUID],
    auth_headers,
) -> None:
    """§13.37: a wrong tick is worse than none, because it stops anybody looking."""
    manager = make_user("manager")
    one = make_credit_customer(name="Twin One")
    two = make_credit_customer(name="Twin Two")
    first = make_credit_repayment(
        None, one, amount="10000.00", mode="bank_transfer", business_date=date(2026, 8, 7)
    )
    second = make_credit_repayment(
        None, two, amount="10000.00", mode="bank_transfer", business_date=date(2026, 8, 7)
    )
    make_statement_line(date(2026, 8, 7), "10000.00")

    body = (await _statement(client, auth_headers(manager))).json()

    assert _line(_row(body, one), first)["bank_status"] == "ambiguous"
    assert _line(_row(body, two), second)["bank_status"] == "ambiguous"


async def test_a_bank_reference_breaks_the_tie(
    client: AsyncClient,
    engine: Engine,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    make_statement_line: Callable[..., UUID],
    auth_headers,
) -> None:
    """Two identical payments, but one carries the UTR the narration quotes. That one is
    verified; the other is simply not on the statement -- no longer a tie, so not ambiguous."""
    manager = make_user("manager")
    one = make_credit_customer(name="With UTR")
    two = make_credit_customer(name="Without UTR")
    referenced = make_credit_repayment(
        None, one, amount="5000.00", mode="bank_transfer", business_date=date(2026, 8, 12)
    )
    other = make_credit_repayment(
        None, two, amount="5000.00", mode="bank_transfer", business_date=date(2026, 8, 12)
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE credit_repayments SET bank_reference = 'UTR998877' WHERE id = :id"
            ).bindparams(id=referenced)
        )
    make_statement_line(date(2026, 8, 12), "5000.00", narration="NEFT UTR998877 SHARMA")

    body = (await _statement(client, auth_headers(manager))).json()

    assert _line(_row(body, one), referenced)["bank_status"] == "verified"
    assert _line(_row(body, two), other)["bank_status"] == "not_on_statement"


async def test_covered_but_absent_is_not_the_same_as_never_uploaded(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    make_statement_line: Callable[..., UUID],
    auth_headers,
) -> None:
    """August has a statement; July does not. "The bank did not see it" and "we have not
    looked" are different facts, and a never-uploaded month is not a list of missing money."""
    manager = make_user("manager")
    customer = make_credit_customer(name="Unmatched")
    make_statement_line(date(2026, 8, 2), "1.00")  # ensures the August import exists
    august = make_credit_repayment(
        None, customer, amount="600.00", mode="bank_transfer", business_date=date(2026, 8, 11)
    )
    july = make_credit_repayment(
        None, customer, amount="600.00", mode="bank_transfer", business_date=date(2026, 7, 11)
    )

    headers = auth_headers(manager)
    august_row = _row((await _statement(client, headers)).json(), customer)
    july_row = _row(
        (
            await _statement(client, headers, date(2026, 7, 1), date(2026, 7, 15))
        ).json(),
        customer,
    )

    assert _line(august_row, august)["bank_status"] == "not_on_statement"
    assert _line(july_row, july)["bank_status"] == "no_statement"


async def test_cash_and_reversal_rows_carry_no_bank_status(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2026, 8, 9), status="closed")
    customer = make_credit_customer(name="Cash Payer")
    cash = make_credit_repayment(shift, customer, amount="300.00", mode="cash")
    transfer = make_credit_repayment(
        None, customer, amount="450.00", mode="bank_transfer", business_date=date(2026, 8, 9)
    )
    reversal = make_credit_repayment(
        None,
        customer,
        amount="-450.00",
        mode="bank_transfer",
        business_date=date(2026, 8, 9),
        reverses_id=transfer,
        reversal_reason="Bounced",
    )

    row = _row((await _statement(client, auth_headers(manager))).json(), customer)

    assert _line(row, cash)["bank_status"] is None
    assert _line(row, reversal)["bank_status"] is None
    assert _line(row, transfer)["bank_status"] == "no_statement"
    assert _line(row, transfer)["is_reversed"] is True


# --- validation --------------------------------------------------------------------------


async def test_from_after_to_is_refused(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    response = await _statement(client, auth_headers(make_user("manager")), T, F)

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_DATE_RANGE"


async def test_a_window_over_366_days_is_refused(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    response = await _statement(
        client, auth_headers(make_user("manager")), date(2025, 8, 1), date(2026, 8, 2)
    )

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_DATE_RANGE"


async def test_a_future_end_date_is_refused(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """§6.1, evaluated at the outlet, not in UTC."""
    from app.core.config import get_settings
    from app.services.shifts import outlet_today

    today = outlet_today(get_settings().TZ_DISPLAY)
    response = await _statement(
        client, auth_headers(make_user("manager")), today, today + timedelta(days=1)
    )

    assert response.status_code == 422
    assert response.json()["code"] == "BUSINESS_DATE_IN_FUTURE"


async def test_both_dates_are_required(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """A bill is a window somebody chose; there is no trading-anchored default here."""
    response = await client.get(
        "/api/v1/credit-customers/statement",
        params={"from": F.isoformat()},
        headers=auth_headers(make_user("manager")),
    )

    assert response.status_code == 422


# --- permissions and tenancy (§8, §5.0) ----------------------------------------------------


async def test_an_attendant_cannot_read_the_statement(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """A customer's balance has always been above the attendant floor (§8)."""
    response = await _statement(client, auth_headers(make_user("attendant")))

    assert response.status_code == 403


@pytest.fixture
def other_outlet(engine: Engine) -> Iterator[UUID]:
    outlet_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO outlets (id, name) VALUES (:id, 'Other Pump')").bindparams(
                id=outlet_id
            )
        )
    yield outlet_id
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM outlets WHERE id = :id").bindparams(id=outlet_id)
        )


async def test_another_outlets_customers_are_absent(
    client: AsyncClient,
    other_outlet: UUID,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_opening_balance: Callable[..., UUID],
    auth_headers,
) -> None:
    """Asserted by inserting one, not by inferring from an empty page (§10)."""
    manager = make_user("manager")
    foreign = make_credit_customer(name="Elsewhere", outlet_id=other_outlet)
    make_credit_opening_balance(foreign, amount="5000.00", as_of_date=date(2026, 7, 1))

    body = (await _statement(client, auth_headers(manager))).json()

    assert _row(body, foreign) is None


async def test_the_statement_route_is_not_swallowed_by_the_customer_id_route(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """Registered before `/{customer_id}`; otherwise "statement" is parsed as a UUID."""
    response = await _statement(client, auth_headers(make_user("manager")))

    assert response.status_code == 200, response.text
    assert set(response.json()) == {
        "from",
        "to",
        "today",
        "rows",
        "totals",
        "lines_truncated",
        "open_shift_count",
    }
