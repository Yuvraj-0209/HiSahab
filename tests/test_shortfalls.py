"""Salesman shortfalls and their settlement (CLAUDE.md §5.2, §6.4, §6.6, §6.9, §13.14, §14).

The record type §13.14 promised and Phase 9 deliberately did not build. What makes this
table different from every other one in the codebase is that its rows carry a **person's
name**, so the tests that matter most are not about arithmetic:

* `test_a_gap_is_not_booked_until_a_human_books_it` -- the §4.7 guarantee.
* `test_the_salesman_is_read_from_the_shift_not_the_payload` -- a typo must not be able to
  put a debt on the wrong person.
* `test_booking_a_different_amount_from_the_gap_warns_but_writes` -- a manager's judgement is
  not overruled by the arithmetic, and the disagreement stays on the row.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.anyio

DAY = date(2027, 2, 3)
SHIFT_START = datetime(2027, 2, 3, 0, 30, tzinfo=timezone.utc)
SHIFT_END = datetime(2027, 2, 3, 16, 30, tzinfo=timezone.utc)
BEFORE = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def priced_fuel(make_fuel_type, make_fuel_price, make_user):
    admin = make_user("admin")
    fuel = make_fuel_type(code="SHORTFUEL", unit_of_measure="litre")
    make_fuel_price(fuel, "100.00", BEFORE, entered_by=admin)
    return fuel


@pytest.fixture
def short_shift(
    make_user, make_shift, make_nozzle, make_reading, make_collection, priced_fuel
):
    """A shift ₹500 short: 1,000 L at ₹100 metered, ₹99,500 declared.

    Returned as `(attendant, shift)` so a test can assert whose name a booking lands on.
    Built with a fuel that has a price and no margin, so every test here is also a standing
    check that §6.3's split holds.
    """

    def _build(day: date, *, declared: str = "99500.00", label: str = "DU-5/N-1"):
        attendant = make_user("attendant")
        shift = make_shift(
            attendant,
            business_date=day,
            sequence=1,
            started_at=SHIFT_START.replace(day=day.day, month=day.month, year=day.year),
            ended_at=SHIFT_END.replace(day=day.day, month=day.month, year=day.year),
        )
        nozzle = make_nozzle(priced_fuel, label=label)
        make_reading(shift, nozzle, opening_reading="0.00", closing_reading="1000.00")
        if declared is not None:
            make_collection(shift, mode="cash", amount=declared)
        return attendant, shift

    return _build


async def _book(client, headers, key, *, shift, amount="500.00", reason="Counted short"):
    return await client.post(
        f"/api/v1/shifts/{shift}/shortfalls",
        json={"amount": amount, "reason": reason},
        headers={**headers, "Idempotency-Key": key},
    )


# --- §4.7: a gap is not a debt until a human says so -------------------------------


async def test_a_gap_is_not_booked_until_a_human_books_it(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    short_shift,
    auth_headers,
    engine: Engine,
) -> None:
    """Reading the position writes nothing; booking is a separate, deliberate act.

    §4.7: "an assumed opening converts theft into a debt owed by someone who did nothing
    wrong." A ₹500 gap is more often a mistyped reading or an unrecorded udhaar slip than
    theft, and software must not be the thing that decides which.
    """
    manager = make_user("manager")
    _, shift = short_shift(DAY)

    position = await client.get(
        f"/api/v1/shifts/{shift}/cash-position", headers=auth_headers(manager)
    )
    assert Decimal(position.json()["gap"]) == Decimal("500.00")

    with engine.connect() as connection:
        before = connection.execute(
            text("SELECT count(*) FROM salesman_shortfalls WHERE shift_id = :s").bindparams(s=shift)
        ).scalar_one()
    assert before == 0

    booked = await _book(client, auth_headers(manager), "sf-1", shift=shift)
    assert booked.status_code == 201


async def test_the_salesman_is_read_from_the_shift_not_the_payload(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    short_shift,
    auth_headers,
) -> None:
    """§5.2: exactly one name carries the drawer. A client-supplied salesman would let a
    typo put a debt on somebody who was not even working, with no second source of truth to
    catch it -- so the field is refused outright rather than ignored."""
    manager = make_user("manager")
    innocent = make_user("attendant")
    attendant, shift = short_shift(date(2027, 2, 4), label="DU-5/N-2")

    refused = await client.post(
        f"/api/v1/shifts/{shift}/shortfalls",
        json={
            "amount": "500.00",
            "reason": "Counted short",
            "salesman_id": str(innocent),
        },
        headers={**auth_headers(manager), "Idempotency-Key": "sf-typo"},
    )
    assert refused.status_code == 422

    booked = await _book(client, auth_headers(manager), "sf-2", shift=shift)
    assert booked.json()["salesman_id"] == str(attendant)


async def test_a_shortfall_requires_a_reason(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    short_shift,
    auth_headers,
    engine: Engine,
) -> None:
    """A debt against a person with no explanation is the row §6.9 and §4.7 both exist to
    prevent. Whitespace does not count -- `strip_whitespace` runs before the length check."""
    manager = make_user("manager")
    _, shift = short_shift(date(2027, 2, 5), label="DU-5/N-3")

    response = await client.post(
        f"/api/v1/shifts/{shift}/shortfalls",
        json={"amount": "500.00", "reason": "   "},
        headers={**auth_headers(manager), "Idempotency-Key": "sf-blank"},
    )

    assert response.status_code == 422
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM salesman_shortfalls WHERE shift_id = :s").bindparams(s=shift)
        ).scalar_one()
    assert count == 0


async def test_a_shift_with_no_declaration_cannot_be_booked_against(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    short_shift,
    auth_headers,
) -> None:
    """§6.8's distinction reaching its conclusion. Nobody has declared, so there is no gap --
    and booking a debt then would be inventing the salesman's half of the comparison, which
    is exactly what §4.7 refuses to do for a meter reading."""
    manager = make_user("manager")
    _, shift = short_shift(date(2027, 2, 6), declared=None, label="DU-5/N-4")

    response = await _book(client, auth_headers(manager), "sf-nodecl", shift=shift)

    assert response.status_code == 409
    assert response.json()["code"] == "NO_CASH_DECLARED"


# --- the computed gap, stored beside the booked amount -----------------------------


async def test_the_computed_gap_is_recomputed_server_side_and_stored(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    short_shift,
    auth_headers,
) -> None:
    """§3 rule 7 and §4.7's predict-and-confirm shape: the system's figure and the human's,
    side by side on the row, so a disagreement is a fact rather than something nobody
    recorded."""
    manager = make_user("manager")
    _, shift = short_shift(date(2027, 2, 7), label="DU-5/N-5")

    response = await _book(client, auth_headers(manager), "sf-gap", shift=shift)

    assert Decimal(response.json()["computed_gap"]) == Decimal("500.00")
    assert Decimal(response.json()["amount"]) == Decimal("500.00")


async def test_booking_a_different_amount_from_the_gap_warns_but_writes(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    short_shift,
    auth_headers,
    caplog,
) -> None:
    """§6.8's reasoning applied to a judgement call: a manager may know part of the gap is a
    slip he has already corrected. Refusing would send the correction outside the system,
    where nothing can see it. So it warns, writes, and keeps both figures."""
    manager = make_user("manager")
    _, shift = short_shift(date(2027, 2, 8), label="DU-5/N-6")

    with caplog.at_level(logging.WARNING, logger="app.services.shortfalls"):
        response = await _book(
            client, auth_headers(manager), "sf-diff", shift=shift, amount="300.00"
        )

    assert response.status_code == 201
    assert Decimal(response.json()["amount"]) == Decimal("300.00")
    assert Decimal(response.json()["computed_gap"]) == Decimal("500.00")
    assert any(
        "other than the computed gap" in record.message for record in caplog.records
    )


# --- §6.4: the booked figure is subtracted -----------------------------------------


async def test_a_booked_shortfall_appears_in_the_cash_position(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    short_shift,
    auth_headers,
) -> None:
    """The term §6.4 subtracts. Without it the same ₹500 is both the salesman's debt and
    cash the locker does not contain -- see §6.4's worked example."""
    manager = make_user("manager")
    _, shift = short_shift(date(2027, 2, 9), label="DU-5/N-7")

    await _book(client, auth_headers(manager), "sf-pos", shift=shift)
    position = await client.get(
        f"/api/v1/shifts/{shift}/cash-position", headers=auth_headers(manager)
    )

    assert Decimal(position.json()["shortfalls_booked"]) == Decimal("500.00")


async def test_the_shortfall_page_says_the_total_is_subtracted(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    short_shift,
    auth_headers,
) -> None:
    manager = make_user("manager")
    _, shift = short_shift(date(2027, 2, 10), label="DU-5/N-8")
    await _book(client, auth_headers(manager), "sf-basis", shift=shift)

    page = await client.get(
        f"/api/v1/shifts/{shift}/shortfalls", headers=auth_headers(manager)
    )

    assert Decimal(page.json()["total"]) == Decimal("500.00")
    assert "SUBTRACTED" in page.json()["cash_basis"]


# --- §6.6's arithmetic, on staff debt ----------------------------------------------


async def test_outstanding_after_a_partial_settlement(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_shortfall: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 2, 11), sequence=1)
    make_shortfall(shift, salesman_id=attendant, amount="500.00", computed_gap="500.00")

    await client.post(
        f"/api/v1/shifts/{shift}/shortfall-settlements",
        json={"salesman_id": str(attendant), "amount": "200.00"},
        headers={**auth_headers(manager), "Idempotency-Key": "st-1"},
    )

    report = await client.get(
        "/api/v1/salesman-shortfalls/outstanding", headers=auth_headers(manager)
    )
    row = next(r for r in report.json()["items"] if r["salesman_id"] == str(attendant))
    assert Decimal(row["outstanding"]) == Decimal("300.00")


async def test_outstanding_after_a_reversed_shortfall_and_a_reversed_settlement(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_shortfall: Callable[..., UUID],
    auth_headers,
) -> None:
    """§6.6's convention: the negative rows net out on their own. Filtering them away would
    make a cancelled shortfall reappear as debt (§14)."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 2, 12), sequence=1)
    shortfall = make_shortfall(
        shift, salesman_id=attendant, amount="500.00", computed_gap="500.00"
    )

    settlement = await client.post(
        f"/api/v1/shifts/{shift}/shortfall-settlements",
        json={"salesman_id": str(attendant), "amount": "200.00"},
        headers={**auth_headers(manager), "Idempotency-Key": "st-2"},
    )
    await client.post(
        f"/api/v1/shifts/{shift}/shortfall-settlements/{settlement.json()['id']}/reversals",
        json={"reason": "Cash was never actually handed over"},
        headers={**auth_headers(manager), "Idempotency-Key": "st-2r"},
    )
    await client.post(
        f"/api/v1/shifts/{shift}/shortfalls/{shortfall}/reversals",
        json={"reason": "The reading was mistyped; he was never short"},
        headers={**auth_headers(manager), "Idempotency-Key": "sf-2r"},
    )

    report = await client.get(
        "/api/v1/salesman-shortfalls/outstanding", headers=auth_headers(manager)
    )
    row = next(r for r in report.json()["items"] if r["salesman_id"] == str(attendant))
    assert Decimal(row["outstanding"]) == Decimal("0.00")


async def test_a_settlement_larger_than_outstanding_is_accepted(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_shortfall: Callable[..., UUID],
    auth_headers,
) -> None:
    """§6.6 makes this decision for customers and it transfers: a salesman rounding ₹480 up
    to ₹500 is real, and the pump then owes him ₹20. Recorded as a decision rather than
    discovered as a minus sign."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 2, 13), sequence=1)
    make_shortfall(shift, salesman_id=attendant, amount="480.00", computed_gap="480.00")

    response = await client.post(
        f"/api/v1/shifts/{shift}/shortfall-settlements",
        json={"salesman_id": str(attendant), "amount": "500.00"},
        headers={**auth_headers(manager), "Idempotency-Key": "st-over"},
    )

    assert response.status_code == 201
    report = await client.get(
        "/api/v1/salesman-shortfalls/outstanding", headers=auth_headers(manager)
    )
    row = next(r for r in report.json()["items"] if r["salesman_id"] == str(attendant))
    assert Decimal(row["outstanding"]) == Decimal("-20.00")


async def test_a_cash_settlement_increases_the_expected_cash_of_the_shift_it_arrived_on(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """Every settlement is cash (§5.2), so it reaches §6.4's drawer on the shift the money
    physically arrived -- which may be weeks after the shift that produced the debt."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    later = make_shift(attendant, business_date=date(2027, 2, 14), sequence=1)

    await client.post(
        f"/api/v1/shifts/{later}/shortfall-settlements",
        json={"salesman_id": str(attendant), "amount": "300.00"},
        headers={**auth_headers(manager), "Idempotency-Key": "st-cash"},
    )

    position = await client.get(
        f"/api/v1/shifts/{later}/cash-position", headers=auth_headers(manager)
    )
    assert Decimal(position.json()["cash_shortfall_settlements"]) == Decimal("300.00")
    assert Decimal(position.json()["accountable_cash"]) == Decimal("300.00")


async def test_the_bulk_report_and_the_single_figure_agree(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_shortfall: Callable[..., UUID],
    make_shortfall_settlement: Callable[..., UUID],
    auth_headers,
) -> None:
    """Two implementations of one rule is the shape that drifts, so it is pinned. Covers all
    three cases the join has to survive: booked only, settled only, and both."""
    from app.core.config import get_settings
    from app.db.session import SessionLocal
    from app.services import shortfalls as shortfall_service

    manager = make_user("manager")
    booked_only = make_user("attendant")
    settled_only = make_user("attendant")
    both = make_user("attendant")
    shift = make_shift(manager, business_date=date(2027, 2, 15), sequence=1)

    make_shortfall(shift, salesman_id=booked_only, amount="1200.00", computed_gap="1200.00")
    make_shortfall_settlement(shift, salesman_id=settled_only, amount="300.00")
    make_shortfall(shift, salesman_id=both, amount="2000.00", computed_gap="2000.00")
    make_shortfall_settlement(shift, salesman_id=both, amount="750.00")

    with SessionLocal() as session:
        report = shortfall_service.outstanding_by_salesman(
            session, outlet_id=get_settings().DEFAULT_OUTLET_ID
        )
        for salesman in (booked_only, settled_only, both):
            assert report[salesman] == shortfall_service.outstanding(
                session, salesman_id=salesman
            ), salesman

    assert report[booked_only] == Decimal("1200.00")
    assert report[settled_only] == Decimal("-300.00")
    assert report[both] == Decimal("1250.00")


# --- the ledger --------------------------------------------------------------------


async def test_the_ledger_interleaves_both_tables_newest_first(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_shortfall: Callable[..., UUID],
    auth_headers,
) -> None:
    """The evidence behind the outstanding figure. A debt held against an employee that
    nobody can walk line by line is an accusation rather than a control."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 2, 16), sequence=1)
    make_shortfall(shift, salesman_id=attendant, amount="500.00", computed_gap="500.00")
    await client.post(
        f"/api/v1/shifts/{shift}/shortfall-settlements",
        json={"salesman_id": str(attendant), "amount": "200.00"},
        headers={**auth_headers(manager), "Idempotency-Key": "led-1"},
    )

    ledger = await client.get(
        f"/api/v1/salesman-shortfalls/{attendant}/ledger", headers=auth_headers(manager)
    )

    kinds = [item["kind"] for item in ledger.json()["items"]]
    assert set(kinds) == {"shortfall", "settlement"}
    deltas = {item["kind"]: Decimal(item["balance_delta"]) for item in ledger.json()["items"]}
    # A shortfall adds to the debt; a settlement reduces it.
    assert deltas["shortfall"] == Decimal("500.00")
    assert deltas["settlement"] == Decimal("-200.00")


async def test_a_reversal_appears_as_its_own_ledger_line(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_shortfall: Callable[..., UUID],
    auth_headers,
) -> None:
    """§6.9: both rows remain visible. A salesman disputing a debt is entitled to see that
    one was raised and cancelled, not an account that silently never mentions it."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 2, 17), sequence=1)
    shortfall = make_shortfall(
        shift, salesman_id=attendant, amount="500.00", computed_gap="500.00"
    )
    await client.post(
        f"/api/v1/shifts/{shift}/shortfalls/{shortfall}/reversals",
        json={"reason": "Reading was mistyped"},
        headers={**auth_headers(manager), "Idempotency-Key": "led-rev"},
    )

    ledger = await client.get(
        f"/api/v1/salesman-shortfalls/{attendant}/ledger", headers=auth_headers(manager)
    )

    items = ledger.json()["items"]
    assert len(items) == 2
    assert any(item["is_reversal"] for item in items)
    assert sum(Decimal(item["balance_delta"]) for item in items) == Decimal("0.00")


async def test_the_ledger_paginates_by_cursor(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_shortfall: Callable[..., UUID],
    auth_headers,
) -> None:
    """§9 forbids offset pagination: an insert during a scroll shifts every later row down
    by one, so the reader sees a duplicate and misses one entirely."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 2, 18), sequence=1)
    for _ in range(5):
        make_shortfall(
            shift, salesman_id=attendant, amount="100.00", computed_gap="100.00"
        )

    first = await client.get(
        f"/api/v1/salesman-shortfalls/{attendant}/ledger?limit=2",
        headers=auth_headers(manager),
    )
    assert len(first.json()["items"]) == 2
    cursor = first.json()["next_cursor"]
    assert cursor is not None

    second = await client.get(
        f"/api/v1/salesman-shortfalls/{attendant}/ledger?limit=2&cursor={cursor}",
        headers=auth_headers(manager),
    )
    first_ids = {item["id"] for item in first.json()["items"]}
    second_ids = {item["id"] for item in second.json()["items"]}
    assert first_ids.isdisjoint(second_ids)


async def test_a_mangled_cursor_is_refused_rather_than_restarting(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
) -> None:
    """Never fall back to "start from the beginning": silently restarting a page walk is how
    a reader sees the same rows twice and believes they are different records."""
    manager = make_user("manager")

    response = await client.get(
        f"/api/v1/salesman-shortfalls/{uuid4()}/ledger?cursor=not-a-cursor",
        headers=auth_headers(manager),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_CURSOR"


async def test_the_outstanding_report_lists_only_staff_who_have_been_short(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_shortfall: Callable[..., UUID],
    auth_headers,
) -> None:
    """Listing every employee at ₹0 would turn a short exception report into a roster."""
    manager = make_user("manager")
    short = make_user("attendant")
    clean = make_user("attendant")
    shift = make_shift(short, business_date=date(2027, 2, 19), sequence=1)
    make_shortfall(shift, salesman_id=short, amount="500.00", computed_gap="500.00")

    report = await client.get(
        "/api/v1/salesman-shortfalls/outstanding", headers=auth_headers(manager)
    )

    listed = {row["salesman_id"] for row in report.json()["items"]}
    assert str(short) in listed
    assert str(clean) not in listed


async def test_the_outstanding_report_names_the_salesman(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_shortfall: Callable[..., UUID],
    auth_headers,
) -> None:
    """A uuid is not a name, and this is a report a human reads to decide who to speak to."""
    manager = make_user("manager")
    attendant = make_user("attendant", full_name="Ramesh Kumar")
    shift = make_shift(attendant, business_date=date(2027, 2, 20), sequence=1)
    make_shortfall(shift, salesman_id=attendant, amount="500.00", computed_gap="500.00")

    report = await client.get(
        "/api/v1/salesman-shortfalls/outstanding", headers=auth_headers(manager)
    )

    row = next(r for r in report.json()["items"] if r["salesman_id"] == str(attendant))
    assert row["full_name"] == "Ramesh Kumar"


# --- §14: never a credit sale ------------------------------------------------------


async def test_booking_a_shortfall_creates_no_credit_customer_or_credit_sale(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    short_shift,
    auth_headers,
    engine: Engine,
) -> None:
    """§14's guardrail, asserted rather than described. Staff debt in a customer's ledger
    means "what does this customer owe me" -- the figure §14 says the owner checks first --
    stops having an answer."""
    manager = make_user("manager")
    _, shift = short_shift(date(2027, 2, 21), label="DU-5/N-9")

    await _book(client, auth_headers(manager), "sf-guard", shift=shift)

    with engine.connect() as connection:
        sales = connection.execute(
            text("SELECT count(*) FROM credit_sales WHERE shift_id = :s").bindparams(s=shift)
        ).scalar_one()
        customers = connection.execute(
            text("SELECT count(*) FROM credit_customers")
        ).scalar_one()
    assert sales == 0
    assert customers == 0


# --- §8 permissions ----------------------------------------------------------------


async def test_an_attendant_cannot_book_settle_or_read_the_ledger(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    short_shift,
    auth_headers,
) -> None:
    """A debt with somebody's name on it is not part of the sheet that person fills in."""
    attendant, shift = short_shift(date(2027, 2, 22), label="DU-5/N-10")
    headers = auth_headers(attendant)

    booked = await _book(client, headers, "sf-perm", shift=shift)
    settled = await client.post(
        f"/api/v1/shifts/{shift}/shortfall-settlements",
        json={"salesman_id": str(attendant), "amount": "100.00"},
        headers={**headers, "Idempotency-Key": "st-perm"},
    )
    listed = await client.get(f"/api/v1/shifts/{shift}/shortfalls", headers=headers)
    outstanding = await client.get(
        "/api/v1/salesman-shortfalls/outstanding", headers=headers
    )
    ledger = await client.get(
        f"/api/v1/salesman-shortfalls/{attendant}/ledger", headers=headers
    )

    for response in (booked, settled, listed, outstanding, ledger):
        assert response.status_code == 403, response.text


async def test_reversing_on_a_locked_shift_is_an_admin_action(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_shortfall: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    admin = make_user("admin")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant, business_date=date(2027, 2, 23), sequence=1, status="locked"
    )
    shortfall = make_shortfall(
        shift, salesman_id=attendant, amount="500.00", computed_gap="500.00"
    )

    refused = await client.post(
        f"/api/v1/shifts/{shift}/shortfalls/{shortfall}/reversals",
        json={"reason": "Booked in error"},
        headers={**auth_headers(manager), "Idempotency-Key": "sf-l1"},
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "LOCKED_SHIFT_REVERSAL_REQUIRES_ADMIN"

    allowed = await client.post(
        f"/api/v1/shifts/{shift}/shortfalls/{shortfall}/reversals",
        json={"reason": "Booked in error"},
        headers={**auth_headers(admin), "Idempotency-Key": "sf-l2"},
    )
    assert allowed.status_code == 201


# --- §6.9 and §6.10 ----------------------------------------------------------------


async def test_a_reversal_carries_the_original_computed_gap(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_shortfall: Callable[..., UUID],
    auth_headers,
) -> None:
    """The gap is a fact about what the system said at booking time. Recomputing it during a
    correction would quietly rewrite the system's own half of §4.7's pair."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 2, 24), sequence=1)
    shortfall = make_shortfall(
        shift, salesman_id=attendant, amount="500.00", computed_gap="500.00"
    )

    response = await client.post(
        f"/api/v1/shifts/{shift}/shortfalls/{shortfall}/reversals",
        json={"reason": "Overstated", "replacement_amount": "200.00"},
        headers={**auth_headers(manager), "Idempotency-Key": "sf-carry"},
    )

    assert Decimal(response.json()["reversal"]["amount"]) == Decimal("-500.00")
    assert Decimal(response.json()["reversal"]["computed_gap"]) == Decimal("500.00")
    assert Decimal(response.json()["replacement"]["amount"]) == Decimal("200.00")
    assert Decimal(response.json()["replacement"]["computed_gap"]) == Decimal("500.00")
    assert response.json()["replacement"]["salesman_id"] == str(attendant)


async def test_double_reversal_and_reversing_a_reversal_are_both_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_shortfall: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    other = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 2, 25), sequence=1)
    shortfall = make_shortfall(
        shift, salesman_id=attendant, amount="500.00", computed_gap="500.00"
    )

    first = await client.post(
        f"/api/v1/shifts/{shift}/shortfalls/{shortfall}/reversals",
        json={"reason": "Booked in error"},
        headers={**auth_headers(manager), "Idempotency-Key": "sf-d1"},
    )
    reversal_id = first.json()["reversal"]["id"]

    second = await client.post(
        f"/api/v1/shifts/{shift}/shortfalls/{shortfall}/reversals",
        json={"reason": "Booked in error again"},
        headers={**auth_headers(other), "Idempotency-Key": "sf-d2"},
    )
    assert second.status_code == 409
    assert second.json()["code"] == "SHORTFALL_ALREADY_REVERSED"

    third = await client.post(
        f"/api/v1/shifts/{shift}/shortfalls/{reversal_id}/reversals",
        json={"reason": "Undoing the undo"},
        headers={**auth_headers(manager), "Idempotency-Key": "sf-d3"},
    )
    assert third.status_code == 409
    assert third.json()["code"] == "CANNOT_REVERSE_A_REVERSAL"


async def test_a_settlement_reversal_is_refused_twice_over(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_shortfall_settlement: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 2, 26), sequence=1)
    settlement = make_shortfall_settlement(
        shift, salesman_id=attendant, amount="200.00"
    )

    first = await client.post(
        f"/api/v1/shifts/{shift}/shortfall-settlements/{settlement}/reversals",
        json={"reason": "Never handed over"},
        headers={**auth_headers(manager), "Idempotency-Key": "st-d1"},
    )
    assert first.status_code == 201

    second = await client.post(
        f"/api/v1/shifts/{shift}/shortfall-settlements/{settlement}/reversals",
        json={"reason": "Never handed over"},
        headers={**auth_headers(manager), "Idempotency-Key": "st-d2"},
    )
    assert second.status_code == 409
    assert second.json()["code"] == "SETTLEMENT_ALREADY_REVERSED"


async def test_the_same_key_twice_books_one_shortfall(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    short_shift,
    auth_headers,
    engine: Engine,
) -> None:
    """A retried booking must not put the debt on the same person twice."""
    manager = make_user("manager")
    _, shift = short_shift(date(2027, 2, 27), label="DU-5/N-11")

    first = await _book(client, auth_headers(manager), "sf-idem", shift=shift)
    second = await _book(client, auth_headers(manager), "sf-idem", shift=shift)

    assert first.json() == second.json()
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM salesman_shortfalls WHERE shift_id = :s").bindparams(s=shift)
        ).scalar_one()
    assert count == 1


async def test_every_money_post_requires_an_idempotency_key(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    short_shift,
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant, shift = short_shift(date(2027, 2, 28), label="DU-5/N-12")

    booked = await client.post(
        f"/api/v1/shifts/{shift}/shortfalls",
        json={"amount": "500.00", "reason": "Counted short"},
        headers=auth_headers(manager),
    )
    settled = await client.post(
        f"/api/v1/shifts/{shift}/shortfall-settlements",
        json={"salesman_id": str(attendant), "amount": "100.00"},
        headers=auth_headers(manager),
    )

    for response in (booked, settled):
        assert response.status_code == 400
        assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_a_refusal_releases_the_key(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    short_shift,
    auth_headers,
) -> None:
    """A NO_CASH_DECLARED refusal must not wedge the key for 24 hours -- the manager comes
    back the moment the declaration lands."""
    manager = make_user("manager")
    _, shift = short_shift(date(2027, 3, 1), declared=None, label="DU-5/N-13")

    refused = await _book(client, auth_headers(manager), "sf-rel", shift=shift)
    assert refused.status_code == 409

    retry = await _book(client, auth_headers(manager), "sf-rel", shift=shift)
    assert retry.status_code == 409
    assert retry.json()["code"] == "NO_CASH_DECLARED"


async def test_rows_from_another_shift_are_refused_by_the_url(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_shortfall: Callable[..., UUID],
    make_shortfall_settlement: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    first = make_shift(attendant, business_date=date(2027, 3, 2), sequence=1)
    second = make_shift(attendant, business_date=date(2027, 3, 2), sequence=2)
    shortfall = make_shortfall(
        first, salesman_id=attendant, amount="500.00", computed_gap="500.00"
    )
    settlement = make_shortfall_settlement(first, salesman_id=attendant, amount="100.00")

    wrong_shortfall = await client.post(
        f"/api/v1/shifts/{second}/shortfalls/{shortfall}/reversals",
        json={"reason": "Wrong shift in the URL"},
        headers={**auth_headers(manager), "Idempotency-Key": "sf-url"},
    )
    wrong_settlement = await client.post(
        f"/api/v1/shifts/{second}/shortfall-settlements/{settlement}/reversals",
        json={"reason": "Wrong shift in the URL"},
        headers={**auth_headers(manager), "Idempotency-Key": "st-url"},
    )

    assert wrong_shortfall.status_code == 409
    assert wrong_shortfall.json()["code"] == "SHORTFALL_NOT_IN_SHIFT"
    assert wrong_settlement.status_code == 409
    assert wrong_settlement.json()["code"] == "SETTLEMENT_NOT_IN_SHIFT"


async def test_unknown_ids_are_404s(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 3, 3), sequence=1)

    shortfall = await client.post(
        f"/api/v1/shifts/{shift}/shortfalls/{uuid4()}/reversals",
        json={"reason": "Pointing at nothing"},
        headers={**auth_headers(manager), "Idempotency-Key": "sf-404"},
    )
    settlement = await client.post(
        f"/api/v1/shifts/{shift}/shortfall-settlements/{uuid4()}/reversals",
        json={"reason": "Pointing at nothing"},
        headers={**auth_headers(manager), "Idempotency-Key": "st-404"},
    )

    assert shortfall.status_code == 404
    assert shortfall.json()["code"] == "SHORTFALL_NOT_FOUND"
    assert settlement.status_code == 404
    assert settlement.json()["code"] == "SETTLEMENT_NOT_FOUND"


# --- the settlement list, replays, and the replacement path ------------------------


async def test_the_settlement_list_shows_both_rows_after_a_reversal(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_shortfall_settlement: Callable[..., UUID],
    auth_headers,
) -> None:
    """§6.9: both rows stay visible and the total nets to zero, so a settlement that never
    arrived stops feeding §6.4 without disappearing from the record."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 3, 4), sequence=1)
    settlement = make_shortfall_settlement(shift, salesman_id=attendant, amount="200.00")

    await client.post(
        f"/api/v1/shifts/{shift}/shortfall-settlements/{settlement}/reversals",
        json={"reason": "Money never actually arrived"},
        headers={**auth_headers(manager), "Idempotency-Key": "st-list"},
    )

    page = await client.get(
        f"/api/v1/shifts/{shift}/shortfall-settlements", headers=auth_headers(manager)
    )

    assert page.status_code == 200
    assert len(page.json()["items"]) == 2
    assert Decimal(page.json()["total"]) == Decimal("0.00")
    assert any(item["is_reversed"] for item in page.json()["items"])
    assert "ADDED" in page.json()["cash_basis"]


async def test_the_settlement_list_truncates_and_still_totals_the_whole_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_shortfall_settlement: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 3, 5), sequence=1)
    for _ in range(101):
        make_shortfall_settlement(shift, salesman_id=attendant, amount="10.00")

    page = await client.get(
        f"/api/v1/shifts/{shift}/shortfall-settlements", headers=auth_headers(manager)
    )

    assert page.json()["truncated"] is True
    assert len(page.json()["items"]) == 100
    assert Decimal(page.json()["total"]) == Decimal("1010.00")


async def test_a_settlement_reversal_can_carry_its_replacement(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_shortfall_settlement: Callable[..., UUID],
    auth_headers,
) -> None:
    """Without the replacement in the same transaction a correction on a closed shift is
    impossible: the reversal lands and the follow-up POST is refused by `writable=True`."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(
        attendant, business_date=date(2027, 3, 6), sequence=1, status="closed"
    )
    settlement = make_shortfall_settlement(shift, salesman_id=attendant, amount="200.00")

    response = await client.post(
        f"/api/v1/shifts/{shift}/shortfall-settlements/{settlement}/reversals",
        json={"reason": "He paid 250, not 200", "replacement_amount": "250.00"},
        headers={**auth_headers(manager), "Idempotency-Key": "st-repl"},
    )

    assert response.status_code == 201
    assert Decimal(response.json()["replacement"]["amount"]) == Decimal("250.00")
    assert response.json()["replacement"]["salesman_id"] == str(attendant)

    page = await client.get(
        f"/api/v1/shifts/{shift}/shortfall-settlements", headers=auth_headers(manager)
    )
    assert Decimal(page.json()["total"]) == Decimal("250.00")


async def test_replaying_a_shortfall_reversal_key_returns_the_first_answer(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_shortfall: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """A retried reversal must not append a second negative row -- which on this table would
    drive the salesman's balance negative and make the pump appear to owe him money."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 3, 7), sequence=1)
    shortfall = make_shortfall(
        shift, salesman_id=attendant, amount="500.00", computed_gap="500.00"
    )

    first = await client.post(
        f"/api/v1/shifts/{shift}/shortfalls/{shortfall}/reversals",
        json={"reason": "Booked in error"},
        headers={**auth_headers(manager), "Idempotency-Key": "sf-replay"},
    )
    second = await client.post(
        f"/api/v1/shifts/{shift}/shortfalls/{shortfall}/reversals",
        json={"reason": "Booked in error"},
        headers={**auth_headers(manager), "Idempotency-Key": "sf-replay"},
    )

    assert first.json() == second.json()
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM salesman_shortfalls WHERE shift_id = :s").bindparams(s=shift)
        ).scalar_one()
    assert count == 2


async def test_replaying_a_settlement_reversal_key_returns_the_first_answer(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_shortfall_settlement: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 3, 8), sequence=1)
    settlement = make_shortfall_settlement(shift, salesman_id=attendant, amount="200.00")

    first = await client.post(
        f"/api/v1/shifts/{shift}/shortfall-settlements/{settlement}/reversals",
        json={"reason": "Never arrived"},
        headers={**auth_headers(manager), "Idempotency-Key": "st-replay"},
    )
    second = await client.post(
        f"/api/v1/shifts/{shift}/shortfall-settlements/{settlement}/reversals",
        json={"reason": "Never arrived"},
        headers={**auth_headers(manager), "Idempotency-Key": "st-replay"},
    )

    assert first.json() == second.json()
    with engine.connect() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM salesman_shortfall_settlements WHERE shift_id = :s"
            ).bindparams(s=shift)
        ).scalar_one()
    assert count == 2


async def test_a_failure_mid_settlement_releases_the_key_and_leaves_no_row(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    monkeypatch,
) -> None:
    """The same invariant `test_non_fuel_sales` pins, on the table where a stuck key would
    hurt most: a salesman standing at the counter with cash in his hand cannot wait 24 hours
    for a reservation to expire."""
    from app.services import audit as audit_service

    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 3, 9), sequence=1)

    calls = {"n": 0}
    real_record = audit_service.record

    def _explode(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("audit backend unavailable")
        return real_record(*args, **kwargs)

    monkeypatch.setattr("app.api.v1.shortfalls.audit.record", _explode)

    body = {"salesman_id": str(attendant), "amount": "200.00"}
    with pytest.raises(RuntimeError, match="audit backend unavailable"):
        await client.post(
            f"/api/v1/shifts/{shift}/shortfall-settlements",
            json=body,
            headers={**auth_headers(manager), "Idempotency-Key": "st-boom"},
        )

    with engine.connect() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM salesman_shortfall_settlements WHERE shift_id = :s"
            ).bindparams(s=shift)
        ).scalar_one()
    assert count == 0

    retry = await client.post(
        f"/api/v1/shifts/{shift}/shortfall-settlements",
        json=body,
        headers={**auth_headers(manager), "Idempotency-Key": "st-boom"},
    )
    assert retry.status_code == 201


async def test_the_same_key_twice_records_one_settlement(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """A retried settlement would clear a debt that was only paid once, and §6.4 would add
    the cash to the locker twice on top."""
    manager = make_user("manager")
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=date(2027, 3, 10), sequence=1)
    body = {"salesman_id": str(attendant), "amount": "200.00"}

    first = await client.post(
        f"/api/v1/shifts/{shift}/shortfall-settlements",
        json=body,
        headers={**auth_headers(manager), "Idempotency-Key": "st-idem"},
    )
    second = await client.post(
        f"/api/v1/shifts/{shift}/shortfall-settlements",
        json=body,
        headers={**auth_headers(manager), "Idempotency-Key": "st-idem"},
    )

    assert first.status_code == 201
    assert first.json() == second.json()
    with engine.connect() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM salesman_shortfall_settlements WHERE shift_id = :s"
            ).bindparams(s=shift)
        ).scalar_one()
    assert count == 1
