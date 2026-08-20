"""§6.8's `MISSING_COLLECTIONS` precondition.

The rule, and the one line in this file that matters more than the rest:

    A shift whose collections total ₹40,000 against ₹95,000 of metered sales **closes**.

Refusing to close until the two agree would leave the salesman in front of a form with
exactly one freely adjustable field, and he would type whatever balanced it. The system
would then be perfectly reconciled and worthless. §6.4 is explicit: variance is recorded,
never auto-corrected, because the variance *is* the signal.

So the check fires on **absence**, and an explicit ₹0 satisfies it -- the same
confirm-don't-assume discipline §4.7 applies to a carried opening reading.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from uuid import UUID

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.anyio

DAY = date(2026, 4, 9)
END = {"ended_at": "2026-04-09T22:00:00+05:30"}


async def test_a_shift_that_sold_fuel_cannot_close_without_a_cash_figure(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, opening_reading="1000.00", closing_reading="2000.00")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/close", json=END, headers=auth_headers(manager)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MISSING_COLLECTIONS"
    # The message has to say what to do, including the part nobody would guess.
    assert "0" in response.json()["detail"]


async def test_an_explicit_zero_cash_declaration_lets_the_shift_close(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """A genuinely cashless day. Rare, but it must be sayable.

    Zero as an answer, never zero as an omission: without a way to declare it, a cashless
    day would be indistinguishable from one where somebody forgot -- and that is precisely
    where a shortfall hides.
    """
    manager = make_user("manager")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, opening_reading="1000.00", closing_reading="2000.00")
    make_collection(shift, mode="cash", amount="0.00")
    make_collection(shift, mode="upi", amount="95000.00")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/close", json=END, headers=auth_headers(manager)
    )

    assert response.status_code == 200
    assert response.json()["status"] == "closed"


async def test_collections_far_below_sales_do_not_block_the_close(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """**The most important test in Phase 6.**

    1,000 litres through the meter against ₹40,000 declared. The gap is udhaar issued
    during the shift plus whatever the salesman is short, and both of those are things the
    system exists to *show*, not to prevent. If this test ever starts failing, somebody has
    turned §6.4's variance into a validation error and the system has stopped reporting the
    one number it was built for.
    """
    manager = make_user("manager")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, opening_reading="1000.00", closing_reading="2000.00")
    make_collection(shift, mode="cash", amount="40000.00")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/close", json=END, headers=auth_headers(manager)
    )

    assert response.status_code == 200


async def test_collections_far_above_sales_do_not_block_the_close_either(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """The other direction is a real day too -- an udhaar repayment arriving in cash (§4.4)
    puts money in the drawer that no sale on that day accounts for."""
    manager = make_user("manager")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, opening_reading="1000.00", closing_reading="1010.00")
    make_collection(shift, mode="cash", amount="250000.00")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/close", json=END, headers=auth_headers(manager)
    )

    assert response.status_code == 200


async def test_a_upi_only_declaration_does_not_satisfy_the_check(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """The check is specifically about *cash*, because cash is the declared figure.

    Card and UPI are inputs to §6.4's equation and are reconciled against a bank statement.
    Cash is the one figure only the salesman can state, so it is the one the close asks for.
    """
    manager = make_user("manager")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, opening_reading="1000.00", closing_reading="2000.00")
    make_collection(shift, mode="upi", amount="95000.00")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/close", json=END, headers=auth_headers(manager)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MISSING_COLLECTIONS"


async def test_a_shift_where_nothing_moved_closes_without_a_cash_figure(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """A shift that dispensed nothing has nothing to declare, and asking for a figure would
    be asking for a number that does not exist."""
    manager = make_user("manager")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, opening_reading="1000.00", closing_reading="1000.00")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/close", json=END, headers=auth_headers(manager)
    )

    assert response.status_code == 200


async def test_missing_readings_are_reported_before_missing_collections(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """An unread meter is the more fundamental failure.

    Until the meters are in there is no figure for the cash to be reconciled against, so
    reporting the cash first would send somebody to fix the second-order problem.
    """
    manager = make_user("manager")
    make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(manager, business_date=DAY, sequence=1)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/close", json=END, headers=auth_headers(manager)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MISSING_NOZZLE_READINGS"


async def test_a_reversed_cash_declaration_no_longer_satisfies_the_check(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
    clean_collections,
) -> None:
    """Reverse the cash figure on a reopened shift and the close asks for it again.

    The alternative -- treating a cancelled declaration as still standing -- would let a
    shift close on a figure somebody had explicitly withdrawn.
    """
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(admin, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, opening_reading="1000.00", closing_reading="2000.00")
    cash = make_collection(shift, mode="cash", amount="60000.00")
    headers = auth_headers(admin)

    await client.post(
        f"/api/v1/shifts/{shift}/collections/{cash}/reversals",
        json={"reason": "figure belonged to the previous day"},
        headers={**headers, "Idempotency-Key": "undo"},
    )

    response = await client.patch(
        f"/api/v1/shifts/{shift}/close", json=END, headers=headers
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MISSING_COLLECTIONS"


def test_the_quantity_probe_skips_a_nozzle_with_no_reading(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
) -> None:
    """A service-level test, because the close route cannot reach this branch.

    `missing_closing_readings` refuses an unread nozzle before `missing_cash_declaration`
    ever runs, so through HTTP the `continue` is dead. It is kept, and tested here, for the
    same reason Phase 5 kept the skips in `revalidate_flow_rates`: this is a public service
    function, and Phase 10's cash engine may well call it on a shift that is still being
    entered. An unread nozzle there must be "nothing moved through it yet", not an
    AttributeError deep in the money path.
    """
    from app.db.session import SessionLocal
    from app.models.shift import Shift
    from app.services import collections as collection_service

    manager = make_user("manager")
    make_nozzle(fuel_type_ids["PETROL"], label="DU-7/N-1", dispenser_label="DU-7")
    shift_id = make_shift(manager, business_date=DAY, sequence=1)

    with SessionLocal() as session:
        shift = session.get(Shift, shift_id)
        assert collection_service.shift_moved_any_quantity(session, shift=shift) is False
