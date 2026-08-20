"""Pricing a shift's readings (CLAUDE.md §6.3, §4.6, §4.5, §13.7).

tests/test_pricing.py (Phase 3) proves `rate_at` and `margin_at` resolve the right row.
This file proves the *sales* code actually uses them -- that a shift is valued at the rate
in force when it traded, that a later revision does not reach back and change it, and that
the profit figure carries the label §13.7 requires.

§4.1 is why the second of those matters:

> Storing "current price" as a mutable column silently corrupts every historical report
> the moment the price changes.

There is no such column. These tests are what keeps one from being reintroduced by
accident -- through a lookup that passes `now()` instead of `shift.started_at`, which would
have exactly the same effect and would not fail any other test in the suite.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.anyio

DAY = date(2026, 3, 10)
# This outlet's shift starts at 06:00 IST -- the daily revision instant itself (§4.1).
SHIFT_START = datetime(2026, 3, 10, 0, 30, tzinfo=timezone.utc)
BEFORE = datetime(2026, 1, 1, tzinfo=timezone.utc)
AFTER = datetime(2026, 6, 1, tzinfo=timezone.utc)


async def test_a_shift_is_valued_at_the_rate_in_force_when_it_traded(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_fuel_price: Callable[..., UUID],
    make_fuel_margin: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    petrol = fuel_type_ids["PETROL"]
    make_fuel_price(petrol, "104.50", BEFORE, entered_by=manager)
    make_fuel_margin(petrol, "1.85", BEFORE, entered_by=manager)

    nozzle = make_nozzle(petrol, label="DU-1/N-1")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, opening_reading="1000.00", closing_reading="1500.00")

    response = await client.get(
        f"/api/v1/shifts/{shift}/sales", headers=auth_headers(manager)
    )

    assert response.status_code == 200
    line = response.json()["lines"][0]
    assert line["quantity_sold"] == "500.000"
    assert line["rate_per_unit"] == "104.50"
    assert line["sale_value"] == "52250.00"
    assert line["margin_per_unit"] == "1.85"
    assert line["gross_fuel_margin"] == "925.00"


async def test_a_later_price_revision_does_not_change_a_recorded_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_fuel_price: Callable[..., UUID],
    make_fuel_margin: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§10 names this explicitly, and §4.1 explains why it is the dangerous one.

    A lookup that used `now()` instead of `shift.started_at` would pass every other test in
    this suite and silently revalue every historical shift the next time a price moved.
    """
    manager = make_user("manager")
    petrol = fuel_type_ids["PETROL"]
    make_fuel_price(petrol, "104.50", BEFORE, entered_by=manager)
    make_fuel_price(petrol, "119.90", AFTER, entered_by=manager)  # after the shift
    make_fuel_margin(petrol, "1.85", BEFORE, entered_by=manager)

    nozzle = make_nozzle(petrol)
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, opening_reading="1000.00", closing_reading="1500.00")

    response = await client.get(
        f"/api/v1/shifts/{shift}/sales", headers=auth_headers(manager)
    )

    line = response.json()["lines"][0]
    assert line["rate_per_unit"] == "104.50"
    assert line["sale_value"] == "52250.00"


async def test_the_margin_is_unmoved_by_price_revisions(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_fuel_price: Callable[..., UUID],
    make_fuel_margin: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§4.6, and the single fact the whole V1 profit model rests on.

    When retail rises Rs 1/kg the next IOCL deduction rises Rs 1/kg too, so the dealer's gap
    does not move. That is what makes profit computable from the totalizer alone, with no
    purchase, tanker or stock data anywhere in the system.
    """
    manager = make_user("manager")
    cbg = fuel_type_ids["CBG"]
    make_fuel_price(cbg, "82.00", BEFORE, entered_by=manager)
    make_fuel_price(
        cbg, "86.00", datetime(2026, 2, 1, tzinfo=timezone.utc), entered_by=manager
    )
    make_fuel_price(
        cbg, "90.00", datetime(2026, 3, 1, tzinfo=timezone.utc), entered_by=manager
    )
    # Entered once, never revised -- the real-world case.
    make_fuel_margin(cbg, "2.28", BEFORE, entered_by=manager)

    nozzle = make_nozzle(cbg, label="DU-3/N-1", dispenser_label="DU-3")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, opening_reading="1000.00", closing_reading="1100.00")

    response = await client.get(
        f"/api/v1/shifts/{shift}/sales", headers=auth_headers(manager)
    )

    line = response.json()["lines"][0]
    assert line["rate_per_unit"] == "90.00"  # the price moved twice
    assert line["margin_per_unit"] == "2.28"  # the margin did not
    assert line["gross_fuel_margin"] == "228.00"


async def test_a_revision_inside_the_shift_window_is_logged_not_hidden(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_fuel_price: Callable[..., UUID],
    make_fuel_margin: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
    caplog,
) -> None:
    """§6.3: "Do not silently pretend it is exact."

    §13.1's approximation values the whole shift at the start rate. That is unavoidable in
    V1 -- there is no per-transaction data (§12) -- but it must be visible when it applies.
    A 24-hour outlet's 02:00-10:00 shift straddles the 06:00 revision, which is why §6.3
    forbids deleting this warning on the strength of this outlet's convenient hours.
    """
    manager = make_user("manager")
    petrol = fuel_type_ids["PETROL"]
    make_fuel_price(petrol, "104.50", BEFORE, entered_by=manager)
    # 12:00 IST, four hours into a 06:00-22:00 shift.
    make_fuel_price(
        petrol,
        "106.00",
        datetime(2026, 3, 10, 6, 30, tzinfo=timezone.utc),
        entered_by=manager,
    )
    make_fuel_margin(petrol, "1.85", BEFORE, entered_by=manager)

    nozzle = make_nozzle(petrol)
    shift = make_shift(
        manager,
        business_date=DAY,
        sequence=1,
        ended_at=datetime(2026, 3, 10, 16, 30, tzinfo=timezone.utc),
    )
    make_reading(shift, nozzle, opening_reading="1000.00", closing_reading="1500.00")

    with caplog.at_level(logging.WARNING, logger="app.services.readings"):
        response = await client.get(
            f"/api/v1/shifts/{shift}/sales", headers=auth_headers(manager)
        )

    # The whole shift is still valued at the start rate -- that is the approximation.
    assert response.json()["lines"][0]["rate_per_unit"] == "104.50"
    warnings = [r for r in caplog.records if "price revised inside" in r.message]
    assert warnings, "the §13.1 approximation was applied with no warning"
    assert warnings[0].approximation == "CLAUDE.md §13.1"


async def test_no_warning_when_the_revision_lands_exactly_at_the_shift_start(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_fuel_price: Callable[..., UUID],
    make_fuel_margin: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
    caplog,
) -> None:
    """§6.3: for THIS outlet the approximation is exact, and must not cry wolf.

    Its single shift starts at 06:00 IST, which is the revision instant itself, and
    `rate_at` compares with `<=` -- so the shift picks up the new rate and one rate covers
    the whole day with nothing to apportion. A warning here would be noise on every single
    revision day, which is how a real warning gets ignored.
    """
    manager = make_user("manager")
    petrol = fuel_type_ids["PETROL"]
    make_fuel_price(petrol, "104.50", BEFORE, entered_by=manager)
    make_fuel_price(petrol, "106.00", SHIFT_START, entered_by=manager)
    make_fuel_margin(petrol, "1.85", BEFORE, entered_by=manager)

    nozzle = make_nozzle(petrol)
    shift = make_shift(
        manager,
        business_date=DAY,
        sequence=1,
        ended_at=datetime(2026, 3, 10, 16, 30, tzinfo=timezone.utc),
    )
    make_reading(shift, nozzle, opening_reading="1000.00", closing_reading="1500.00")

    with caplog.at_level(logging.WARNING, logger="app.services.readings"):
        response = await client.get(
            f"/api/v1/shifts/{shift}/sales", headers=auth_headers(manager)
        )

    assert response.json()["lines"][0]["rate_per_unit"] == "106.00"
    assert not [r for r in caplog.records if "price revised inside" in r.message]


async def test_a_missing_price_refuses_rather_than_valuing_at_zero(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_fuel_type: Callable[..., UUID],
    auth_headers,
) -> None:
    """A shift valued at zero is a plausible number and completely wrong, and nothing
    downstream would ever question it (§5.1)."""
    manager = make_user("manager")
    unpriced = make_fuel_type(code="XP95", unit_of_measure="litre")
    nozzle = make_nozzle(unpriced)
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, opening_reading="1000.00", closing_reading="1500.00")

    response = await client.get(
        f"/api/v1/shifts/{shift}/sales", headers=auth_headers(manager)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "NO_PRICE_FOR_DATE"


async def test_a_missing_margin_refuses_rather_than_reporting_zero_profit(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_fuel_type: Callable[..., UUID],
    make_fuel_price: Callable[..., UUID],
    auth_headers,
) -> None:
    """§14's standing open question: petrol and diesel commissions are still unentered, so
    this is the state the outlet is genuinely in today. It must refuse, not report zero."""
    manager = make_user("manager")
    priced_only = make_fuel_type(code="XP95B", unit_of_measure="litre")
    make_fuel_price(priced_only, "119.90", BEFORE, entered_by=manager)
    nozzle = make_nozzle(priced_only)
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, opening_reading="1000.00", closing_reading="1500.00")

    response = await client.get(
        f"/api/v1/shifts/{shift}/sales", headers=auth_headers(manager)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "NO_MARGIN_FOR_DATE"


# --- §4.5 units --------------------------------------------------------------


async def test_quantities_are_totalled_per_unit_never_summed_across_them(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_fuel_price: Callable[..., UUID],
    make_fuel_margin: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§4.5: "no report sums across units".

    500 litres of petrol plus 100 kg of CBG is not 600 of anything. A single "total
    quantity" figure would look like a total and mean nothing -- money is the only thing
    the two have in common, so money is the only thing totalled across them.
    """
    manager = make_user("manager")
    petrol, cbg = fuel_type_ids["PETROL"], fuel_type_ids["CBG"]
    make_fuel_price(petrol, "104.50", BEFORE, entered_by=manager)
    make_fuel_margin(petrol, "1.85", BEFORE, entered_by=manager)
    make_fuel_price(cbg, "90.00", BEFORE, entered_by=manager)
    make_fuel_margin(cbg, "2.28", BEFORE, entered_by=manager)

    petrol_nozzle = make_nozzle(petrol, label="DU-1/N-1")
    cbg_nozzle = make_nozzle(cbg, label="DU-3/N-1", dispenser_label="DU-3")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, petrol_nozzle, opening_reading="1000.00", closing_reading="1500.00")
    make_reading(shift, cbg_nozzle, opening_reading="200.00", closing_reading="300.00")

    body = (
        await client.get(f"/api/v1/shifts/{shift}/sales", headers=auth_headers(manager))
    ).json()

    assert body["quantity_by_unit"] == {"litre": "500.000", "kilogram": "100.000"}
    # 500 x 104.50 + 100 x 90.00 = 52,250 + 9,000
    assert body["total_sale_value"] == "61250.00"
    # 500 x 1.85 + 100 x 2.28 = 925 + 228
    assert body["total_gross_fuel_margin"] == "1153.00"


async def test_each_line_carries_its_own_unit(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_fuel_price: Callable[..., UUID],
    make_fuel_margin: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§4.5: CBG resolves as kilogram while petrol resolves as litre, from the fuel type."""
    manager = make_user("manager")
    petrol, cbg = fuel_type_ids["PETROL"], fuel_type_ids["CBG"]
    for fuel, rate, margin in ((petrol, "104.50", "1.85"), (cbg, "90.00", "2.28")):
        make_fuel_price(fuel, rate, BEFORE, entered_by=manager)
        make_fuel_margin(fuel, margin, BEFORE, entered_by=manager)

    p = make_nozzle(petrol, label="DU-1/N-1")
    c = make_nozzle(cbg, label="DU-3/N-1", dispenser_label="DU-3")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, p, closing_reading="1500.00")
    make_reading(shift, c, closing_reading="1100.00")

    body = (
        await client.get(f"/api/v1/shifts/{shift}/sales", headers=auth_headers(manager))
    ).json()

    units = {line["nozzle_label"]: line["unit_of_measure"] for line in body["lines"]}
    assert units == {"DU-1/N-1": "litre", "DU-3/N-1": "kilogram"}


# --- §13.7 labelling ---------------------------------------------------------


async def test_the_profit_figure_is_never_called_profit_and_is_always_qualified(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_fuel_price: Callable[..., UUID],
    make_fuel_margin: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§13.7: "an unlabelled 'profit' figure here is exactly the plausible-but-wrong number
    this document exists to prevent."

    Holding 12 kL of petrol when the rate rises Rs 1 is a real Rs 12,000 gain this system
    will never see, because nothing moved through a nozzle. The label travels in the
    payload rather than being left to the frontend, because §2 requires a mobile client
    hitting the same endpoint to be unable to render an unqualified number.
    """
    manager = make_user("manager")
    petrol = fuel_type_ids["PETROL"]
    make_fuel_price(petrol, "104.50", BEFORE, entered_by=manager)
    make_fuel_margin(petrol, "1.85", BEFORE, entered_by=manager)
    nozzle = make_nozzle(petrol)
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, closing_reading="1500.00")

    body = (
        await client.get(f"/api/v1/shifts/{shift}/sales", headers=auth_headers(manager))
    ).json()

    assert "profit" not in body
    assert "total_profit" not in body
    assert "gross_fuel_margin" in body["lines"][0]
    assert "stock revaluation" in body["margin_basis"]
    assert "not business profit" in body["margin_basis"]


async def test_an_unfinished_shift_is_marked_incomplete_rather_than_reported_as_zero(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_fuel_price: Callable[..., UUID],
    make_fuel_margin: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """"Not entered" and "sold nothing" must stay distinguishable all the way out."""
    manager = make_user("manager")
    petrol = fuel_type_ids["PETROL"]
    make_fuel_price(petrol, "104.50", BEFORE, entered_by=manager)
    make_fuel_margin(petrol, "1.85", BEFORE, entered_by=manager)

    done = make_nozzle(petrol, label="DU-1/N-1")
    pending = make_nozzle(petrol, label="DU-2/N-1", dispenser_label="DU-2")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, done, opening_reading="1000.00", closing_reading="1500.00")
    make_reading(shift, pending, opening_reading="1000.00", closing_reading=None)

    body = (
        await client.get(f"/api/v1/shifts/{shift}/sales", headers=auth_headers(manager))
    ).json()

    assert body["incomplete"] is True
    lines = {line["nozzle_label"]: line for line in body["lines"]}
    assert lines["DU-2/N-1"]["quantity_sold"] is None
    assert lines["DU-2/N-1"]["sale_value"] is None
    assert body["total_sale_value"] == "52250.00"


async def test_the_instant_the_shift_was_priced_at_is_stated(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_fuel_price: Callable[..., UUID],
    make_fuel_margin: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """So a reader can see which revision applied without having to infer it (§6.3)."""
    manager = make_user("manager")
    petrol = fuel_type_ids["PETROL"]
    make_fuel_price(petrol, "104.50", BEFORE, entered_by=manager)
    make_fuel_margin(petrol, "1.85", BEFORE, entered_by=manager)
    nozzle = make_nozzle(petrol)
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, closing_reading="1500.00")

    body = (
        await client.get(f"/api/v1/shifts/{shift}/sales", headers=auth_headers(manager))
    ).json()

    assert body["priced_at"].startswith("2026-03-10T00:30:00")
    assert body["business_date"] == "2026-03-10"


async def test_the_testing_quantity_reaches_the_money(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_fuel_price: Callable[..., UUID],
    make_fuel_margin: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§4.2, followed all the way through to rupees.

    The unit tests prove the subtraction; this proves nothing downstream quietly adds the
    5 litres back. Rs 522.50 a day is the "small, permanent, daily cash shortfall that is
    extremely hard to diagnose".
    """
    manager = make_user("manager")
    petrol = fuel_type_ids["PETROL"]
    make_fuel_price(petrol, "104.50", BEFORE, entered_by=manager)
    make_fuel_margin(petrol, "1.85", BEFORE, entered_by=manager)
    nozzle = make_nozzle(petrol)
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(
        shift, nozzle, opening_reading="1000.00", closing_reading="1500.00",
        testing_quantity="5.000",
    )

    body = (
        await client.get(f"/api/v1/shifts/{shift}/sales", headers=auth_headers(manager))
    ).json()

    assert body["lines"][0]["quantity_sold"] == "495.000"
    assert body["lines"][0]["sale_value"] == "51727.50"
    assert Decimal(body["total_sale_value"]) == Decimal("52250.00") - Decimal("522.50")
