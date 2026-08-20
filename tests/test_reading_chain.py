"""CLAUDE.md §4.7 -- the carried-forward opening, and the guard that it is confirmed.

§4.7 calls this "the rule that matters most", and the reason is not a technical one:

> Fuel siphoned through a nozzle between shifts still moves the totalizer -- but an
> assumed opening says it did not, so the missing quantity is absorbed into the next shift
> as sales that produced no cash. The shift then comes up short, and this outlet books a
> shortfall as udhaar against the salesman's own name.

So an assumed opening does not merely lose data. It converts theft into a debt owed by
somebody who did nothing wrong, and leaves nothing pointing at the gap. Every test in this
file exists to keep one specific part of that from happening.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

pytestmark = pytest.mark.anyio

from httpx import AsyncClient


DAY = date(2026, 3, 10)


def _post(client: AsyncClient, shift_id: UUID, headers: dict[str, str], **body: object):
    payload: dict[str, object] = {"opening_confirmed": True}
    payload.update(body)
    return client.post(
        f"/api/v1/shifts/{shift_id}/readings", json=payload, headers=headers
    )


def _stored(engine: Engine, shift_id: UUID, nozzle_id: UUID) -> dict[str, object]:
    """Read a reading straight out of the database.

    Several rules here are about what is *stored*, not what an endpoint returns -- §4.7 is
    explicit that the chained opening lives on the row rather than being computed on read.
    Asserting through the API would not distinguish the two.
    """
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT opening_reading, chained_opening_reading, closing_reading, "
                "opening_variance_reason, requires_review, review_note "
                "FROM nozzle_readings WHERE shift_id = :s AND nozzle_id = :n"
            ).bindparams(s=shift_id, n=nozzle_id)
        ).mappings().one()
    return dict(row)


# --- the chain itself --------------------------------------------------------


async def test_an_opening_is_carried_from_the_previous_shift(
    client: AsyncClient,
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§4.7: an attendant types a closing value and never an opening one."""
    attendant = make_user("attendant")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])

    yesterday = make_shift(attendant, business_date=DAY, sequence=1, status="closed")
    make_reading(yesterday, nozzle, opening_reading="1000.00", closing_reading="1500.00")
    today = make_shift(attendant, business_date=date(2026, 3, 11), sequence=1)

    # No opening_reading in the payload at all -- the normal day.
    response = await _post(
        client, today, auth_headers(attendant), nozzle_id=str(nozzle),
        closing_reading="1800.00",
    )

    assert response.status_code == 201
    assert response.json()["opening_reading"] == "1500.00"
    assert response.json()["quantity_sold"] == "300.000"


async def test_the_chained_value_is_stored_on_the_row_not_computed_on_read(
    client: AsyncClient,
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§4.7: "The chained opening is stored on the row, not computed on read."

    Computing it would mean correcting one shift silently rewrites the next shift's
    history -- so this asserts against the database column, not the response body.
    """
    attendant = make_user("attendant")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    previous = make_shift(attendant, business_date=DAY, sequence=1, status="closed")
    make_reading(previous, nozzle, closing_reading="1500.00")
    current = make_shift(attendant, business_date=date(2026, 3, 11), sequence=1)

    await _post(
        client, current, auth_headers(attendant), nozzle_id=str(nozzle),
        closing_reading="1800.00",
    )

    row = _stored(engine, current, nozzle)
    assert row["opening_reading"] == Decimal("1500.00")
    assert row["chained_opening_reading"] == Decimal("1500.00")


async def test_a_nozzle_out_of_service_for_one_shift_still_chains_to_its_own_last_reading(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§4.7: the lookup is "the most recent closing for **that nozzle**".

    A chain built on "the previous shift's reading" would snap here -- the intervening
    shift has no row for this nozzle at all, so there would be nothing to carry and the
    system would demand an anchor for a meter that has been running for years.
    """
    attendant = make_user("attendant")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])

    first = make_shift(attendant, business_date=DAY, sequence=1, status="closed")
    make_reading(first, nozzle, closing_reading="1500.00")
    # Shift two: this nozzle was out of order, so no reading exists for it.
    make_shift(attendant, business_date=date(2026, 3, 11), sequence=1, status="closed")
    third = make_shift(attendant, business_date=date(2026, 3, 12), sequence=1)

    response = await _post(
        client, third, auth_headers(attendant), nozzle_id=str(nozzle),
        closing_reading="1600.00",
    )

    assert response.status_code == 201
    assert response.json()["opening_reading"] == "1500.00"


async def test_a_whole_skipped_day_does_not_break_the_chain(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§4.7: "an overnight closure and a 24-hour handover are the same thing"."""
    attendant = make_user("attendant")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    old = make_shift(attendant, business_date=date(2026, 3, 1), sequence=1, status="closed")
    make_reading(old, nozzle, closing_reading="777.50")
    much_later = make_shift(attendant, business_date=date(2026, 6, 20), sequence=1)

    response = await _post(
        client, much_later, auth_headers(attendant), nozzle_id=str(nozzle),
        closing_reading="800.00",
    )

    assert response.status_code == 201
    assert response.json()["opening_reading"] == "777.50"


async def test_the_chain_follows_business_date_not_the_order_things_were_typed_in(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§4.7: the whole day is typed in **after the fact**, frequently out of order.

    Here the 12th is entered before the 11th, so `created_at` order is the reverse of chain
    order. A lookup ordered by insertion time would carry the 11th's closing forward into a
    shift that traded a day earlier -- silently, and with a plausible-looking number.
    """
    attendant = make_user("attendant")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])

    # Entered first, but it is the LATER business date.
    later = make_shift(attendant, business_date=date(2026, 3, 12), sequence=1, status="closed")
    make_reading(later, nozzle, closing_reading="9999.00")
    # Entered second, earlier business date.
    earlier = make_shift(attendant, business_date=date(2026, 3, 11), sequence=1, status="closed")
    make_reading(earlier, nozzle, closing_reading="2000.00")

    current = make_shift(attendant, business_date=date(2026, 3, 13), sequence=1)
    response = await _post(
        client, current, auth_headers(attendant), nozzle_id=str(nozzle),
        closing_reading="10000.00",
    )

    assert response.status_code == 201
    # The 12th's 9999.00, not the most recently inserted 2000.00.
    assert response.json()["opening_reading"] == "9999.00"


async def test_an_unclosed_predecessor_is_not_carried_forward(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """A reading with no closing value has not produced anything to carry.

    It must be skipped rather than carried as NULL, which would look like an anchor.
    """
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    settled = make_shift(admin, business_date=date(2026, 3, 10), sequence=1, status="closed")
    make_reading(settled, nozzle, closing_reading="500.00")
    half_entered = make_shift(admin, business_date=date(2026, 3, 11), sequence=1, status="closed")
    make_reading(half_entered, nozzle, opening_reading="500.00", closing_reading=None)

    current = make_shift(admin, business_date=date(2026, 3, 12), sequence=1)
    response = await _post(
        client, current, auth_headers(admin), nozzle_id=str(nozzle),
        closing_reading="600.00",
    )

    assert response.status_code == 201
    assert response.json()["opening_reading"] == "500.00"


# --- anchoring (§4.7) --------------------------------------------------------


async def test_a_nozzle_with_no_history_needs_an_anchor(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(admin, business_date=DAY, sequence=1)

    response = await _post(
        client, shift, auth_headers(admin), nozzle_id=str(nozzle),
        closing_reading="1500.00",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "ANCHOR_READING_REQUIRED"


async def test_only_an_admin_may_anchor_a_meter(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§4.7: anchoring is admin-only because every later reading is measured from it.

    A manager is refused too, which is the case worth pinning: the role floor for entering
    a reading is `attendant`, so without this check anyone who can enter a reading could
    also invent where a meter started.
    """
    manager = make_user("manager")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(manager, business_date=DAY, sequence=1)

    response = await _post(
        client, shift, auth_headers(manager), nozzle_id=str(nozzle),
        opening_reading="1000.00", closing_reading="1500.00",
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ANCHOR_REQUIRES_ADMIN"


async def test_an_admin_anchors_and_the_row_records_that_it_was_anchored(
    client: AsyncClient,
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """A NULL `chained_opening_reading` is what marks an anchor for good.

    Without it, an anchored opening and a confirmed carried one look identical a year
    later, and there is no way to tell which numbers rest on an unverifiable starting
    value.
    """
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(admin, business_date=DAY, sequence=1)

    response = await _post(
        client, shift, auth_headers(admin), nozzle_id=str(nozzle),
        opening_reading="1000.00", closing_reading="1500.00",
    )

    assert response.status_code == 201
    row = _stored(engine, shift, nozzle)
    assert row["opening_reading"] == Decimal("1000.00")
    assert row["chained_opening_reading"] is None


async def test_the_anchor_then_chains_normally_to_the_next_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """One admin entry, then the chain runs itself. That is the whole design."""
    admin = make_user("admin")
    attendant = make_user("attendant")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])

    first = make_shift(admin, business_date=DAY, sequence=1)
    await _post(
        client, first, auth_headers(admin), nozzle_id=str(nozzle),
        opening_reading="1000.00", closing_reading="1500.00",
    )
    second = make_shift(attendant, business_date=date(2026, 3, 11), sequence=1)

    response = await _post(
        client, second, auth_headers(attendant), nozzle_id=str(nozzle),
        closing_reading="1900.00",
    )

    assert response.status_code == 201
    assert response.json()["opening_reading"] == "1500.00"
    assert response.json()["quantity_sold"] == "400.000"


async def test_a_newly_installed_nozzle_anchors_while_its_neighbours_chain(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§4.7 names "a nozzle installed mid-life" as a case a naive chain would snap on.

    The old nozzle carries forward; the new one asks for an anchor. Both on the same shift.
    """
    admin = make_user("admin")
    old_nozzle = make_nozzle(fuel_type_ids["PETROL"], label="DU-1/N-1")
    new_nozzle = make_nozzle(fuel_type_ids["PETROL"], label="DU-2/N-1")

    history = make_shift(admin, business_date=DAY, sequence=1, status="closed")
    make_reading(history, old_nozzle, closing_reading="4000.00")
    current = make_shift(admin, business_date=date(2026, 3, 11), sequence=1)

    carried = await _post(
        client, current, auth_headers(admin), nozzle_id=str(old_nozzle),
        closing_reading="4200.00",
    )
    fresh = await _post(
        client, current, auth_headers(admin), nozzle_id=str(new_nozzle),
        closing_reading="60.00",
    )

    assert carried.status_code == 201
    assert carried.json()["opening_reading"] == "4000.00"
    assert fresh.status_code == 422
    assert fresh.json()["code"] == "ANCHOR_READING_REQUIRED"


# --- confirm, don't assume (§4.7) -------------------------------------------


async def test_an_unconfirmed_opening_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§4.7: pre-filled, but **confirmed against the physical meter**.

    Auto-filling without confirmation deletes one of the two independent meter
    observations an attendant makes each shift.
    """
    attendant = make_user("attendant")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    previous = make_shift(attendant, business_date=DAY, sequence=1, status="closed")
    make_reading(previous, nozzle, closing_reading="1500.00")
    current = make_shift(attendant, business_date=date(2026, 3, 11), sequence=1)

    response = await client.post(
        f"/api/v1/shifts/{current}/readings",
        json={
            "nozzle_id": str(nozzle),
            "opening_confirmed": False,
            "closing_reading": "1800.00",
        },
        headers=auth_headers(attendant),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "OPENING_NOT_CONFIRMED"


async def test_a_mismatch_without_a_reason_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    previous = make_shift(attendant, business_date=DAY, sequence=1, status="closed")
    make_reading(previous, nozzle, closing_reading="1500.00")
    current = make_shift(attendant, business_date=date(2026, 3, 11), sequence=1)

    response = await _post(
        client, current, auth_headers(attendant), nozzle_id=str(nozzle),
        opening_reading="1520.00", closing_reading="1800.00",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "OPENING_VARIANCE_REASON_REQUIRED"
    # The message must not read as an accusation -- §4.7's whole concern is that the
    # innocent case looks identical to the guilty one at this moment.
    assert "not charged to anyone" in response.json()["detail"]


async def test_a_mismatch_keeps_both_numbers_and_raises_a_flag(
    client: AsyncClient,
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """The heart of §4.7. 20 litres left this nozzle between shifts.

    What must happen: the **observed** reading is what the shift is measured from, the
    predicted one is kept beside it, and a human is asked to look -- before anyone is
    blamed. What must NOT happen: the 20 litres silently becoming this shift's sales with
    no cash against them, and then the salesman's own udhaar.
    """
    attendant = make_user("attendant")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    previous = make_shift(attendant, business_date=DAY, sequence=1, status="closed")
    make_reading(previous, nozzle, closing_reading="1500.00")
    current = make_shift(attendant, business_date=date(2026, 3, 11), sequence=1)

    response = await _post(
        client, current, auth_headers(attendant), nozzle_id=str(nozzle),
        opening_reading="1520.00", closing_reading="1800.00",
        opening_variance_reason="Meter reads 20 higher than last night's close.",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["opening_reading"] == "1520.00"
    assert body["chained_opening_reading"] == "1500.00"
    assert body["requires_review"] is True
    # Measured from what the meter actually shows: 1800 - 1520 = 280, not 300.
    assert body["quantity_sold"] == "280.000"

    row = _stored(engine, current, nozzle)
    assert row["opening_variance_reason"].startswith("Meter reads 20 higher")


async def test_confirming_the_same_value_explicitly_raises_no_flag(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """Sending the matching number is confirmation, not a variance."""
    attendant = make_user("attendant")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    previous = make_shift(attendant, business_date=DAY, sequence=1, status="closed")
    make_reading(previous, nozzle, closing_reading="1500.00")
    current = make_shift(attendant, business_date=date(2026, 3, 11), sequence=1)

    response = await _post(
        client, current, auth_headers(attendant), nozzle_id=str(nozzle),
        opening_reading="1500.00", closing_reading="1800.00",
    )

    assert response.status_code == 201
    assert response.json()["requires_review"] is False


async def test_a_mismatch_cannot_be_written_without_a_reason_at_the_database_level(
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
) -> None:
    """§6.6's belt and braces: a client can bypass JavaScript, but not a CHECK constraint."""
    from sqlalchemy.exc import IntegrityError

    attendant = make_user("attendant")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    with pytest.raises(IntegrityError) as caught:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO nozzle_readings (shift_id, nozzle_id, opening_reading, "
                    "chained_opening_reading) VALUES (:s, :n, 1520.00, 1500.00)"
                ).bindparams(s=shift, n=nozzle)
            )
    assert "ck_nozzle_readings_variance_has_reason" in str(caught.value)


# --- §13.10: a mid-chain change flags, it does not rewrite -------------------


async def test_changing_a_closing_reading_flags_the_next_shift(
    client: AsyncClient,
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§13.10 as resolved in Phase 5, and the assertion that matters is the negative one.

    The next shift's stored `opening_reading` must be **unchanged**. Recomputing it would
    be the silent rewrite §4.7 stores the value on the row to prevent -- and the rewritten
    number would look exactly like a reading somebody had confirmed against a meter.
    """
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])

    first = make_shift(admin, business_date=DAY, sequence=1)
    make_reading(first, nozzle, opening_reading="1000.00", closing_reading="1500.00")
    second = make_shift(admin, business_date=date(2026, 3, 11), sequence=1)
    make_reading(
        second, nozzle,
        opening_reading="1500.00", chained_opening_reading="1500.00",
        closing_reading="1900.00",
    )

    response = await client.patch(
        f"/api/v1/shifts/{first}/readings/{nozzle}",
        json={"closing_reading": "1450.00"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    downstream = _stored(engine, second, nozzle)
    # Untouched. This is the point of the whole approach.
    assert downstream["opening_reading"] == Decimal("1500.00")
    assert downstream["requires_review"] is True
    assert "deliberately NOT been changed" in downstream["review_note"]
    assert "1450.00" in downstream["review_note"]


async def test_changing_only_the_testing_quantity_does_not_flag_downstream(
    client: AsyncClient,
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """The chain carries a *closing reading*. Testing quantity changes what was sold, not
    what the meter read, so the next shift's opening is unaffected and flagging it would
    be noise -- and a review flag nobody needs is a review flag nobody reads."""
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])

    first = make_shift(admin, business_date=DAY, sequence=1)
    make_reading(first, nozzle, opening_reading="1000.00", closing_reading="1500.00")
    second = make_shift(admin, business_date=date(2026, 3, 11), sequence=1)
    make_reading(
        second, nozzle,
        opening_reading="1500.00", chained_opening_reading="1500.00",
        closing_reading="1900.00",
    )

    response = await client.patch(
        f"/api/v1/shifts/{first}/readings/{nozzle}",
        json={"testing_quantity": "5.000"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    assert response.json()["quantity_sold"] == "495.000"
    assert _stored(engine, second, nozzle)["requires_review"] is False


async def test_the_last_shift_in_the_chain_has_nothing_to_flag(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """Correcting the tip of the chain is the ordinary case and must stay uneventful."""
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    only = make_shift(admin, business_date=DAY, sequence=1)
    make_reading(only, nozzle, opening_reading="1000.00", closing_reading="1500.00")

    response = await client.patch(
        f"/api/v1/shifts/{only}/readings/{nozzle}",
        json={"closing_reading": "1600.00"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    assert response.json()["quantity_sold"] == "600.000"
