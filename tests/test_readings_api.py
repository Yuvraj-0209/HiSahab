"""The reading endpoints, their error codes, and §6.8's close precondition.

Covers the parts of §6.2 that are about *the row*, rather than the arithmetic --
tests/test_sales_math.py owns the arithmetic. The distinction matters most in
`test_a_rejected_reading_writes_no_row`: §10 requires that a refused reading leaves nothing
behind, which is a property of where the validation sits relative to the INSERT, not of the
formula.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.anyio

DAY = date(2026, 3, 10)


def _post(client: AsyncClient, shift_id: UUID, headers: dict[str, str], **body: object):
    payload: dict[str, object] = {"opening_confirmed": True}
    payload.update(body)
    return client.post(
        f"/api/v1/shifts/{shift_id}/readings", json=payload, headers=headers
    )


def _count_readings(engine: Engine, shift_id: UUID) -> int:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT count(*) FROM nozzle_readings WHERE shift_id = :s").bindparams(
                s=shift_id
            )
        ).scalar_one()


# --- the worksheet -----------------------------------------------------------


async def test_the_worksheet_prefills_every_opening(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """This route is what makes §4.7's "zero typing on a normal day" true."""
    attendant = make_user("attendant")
    petrol = make_nozzle(fuel_type_ids["PETROL"], label="DU-1/N-1")
    cbg = make_nozzle(fuel_type_ids["CBG"], label="DU-3/N-1", dispenser_label="DU-3")

    previous = make_shift(attendant, business_date=DAY, sequence=1, status="closed")
    make_reading(previous, petrol, closing_reading="1500.00")
    make_reading(previous, cbg, closing_reading="220.500")
    current = make_shift(attendant, business_date=date(2026, 3, 11), sequence=1)

    response = await client.get(
        f"/api/v1/shifts/{current}/readings", headers=auth_headers(attendant)
    )

    assert response.status_code == 200
    lines = {line["nozzle_label"]: line for line in response.json()["lines"]}
    assert lines["DU-1/N-1"]["chained_opening_reading"] == "1500.00"
    assert lines["DU-3/N-1"]["chained_opening_reading"] == "220.50"
    assert all(line["reading"] is None for line in lines.values())


async def test_the_worksheet_states_each_nozzles_unit(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§4.5: a client rendering this sheet must never have to guess litres or kilograms."""
    attendant = make_user("attendant")
    make_nozzle(fuel_type_ids["PETROL"], label="DU-1/N-1")
    make_nozzle(fuel_type_ids["CBG"], label="DU-3/N-1", dispenser_label="DU-3")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await client.get(
        f"/api/v1/shifts/{shift}/readings", headers=auth_headers(attendant)
    )

    units = {
        line["nozzle_label"]: line["unit_of_measure"] for line in response.json()["lines"]
    }
    assert units == {"DU-1/N-1": "litre", "DU-3/N-1": "kilogram"}


async def test_the_worksheet_marks_a_nozzle_that_needs_an_anchor(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """So a frontend can ask an admin for the starting value instead of letting an
    attendant walk into a 403 (§4.7)."""
    attendant = make_user("attendant")
    make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await client.get(
        f"/api/v1/shifts/{shift}/readings", headers=auth_headers(attendant)
    )

    line = response.json()["lines"][0]
    assert line["requires_anchor"] is True
    assert line["chained_opening_reading"] is None


async def test_an_inactive_nozzle_is_absent_from_the_worksheet(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    make_nozzle(fuel_type_ids["PETROL"], label="LIVE")
    make_nozzle(fuel_type_ids["PETROL"], label="RETIRED", is_active=False)
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await client.get(
        f"/api/v1/shifts/{shift}/readings", headers=auth_headers(attendant)
    )

    assert [line["nozzle_label"] for line in response.json()["lines"]] == ["LIVE"]


async def test_a_nozzle_installed_after_the_shift_is_absent_from_the_worksheet(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """A meter fitted next week cannot have a reading for today, and demanding one would
    make the shift impossible to close (§6.8)."""
    attendant = make_user("attendant")
    make_nozzle(fuel_type_ids["PETROL"], label="ALREADY-THERE")
    make_nozzle(
        fuel_type_ids["PETROL"],
        label="FITTED-LATER",
        meter_installed_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await client.get(
        f"/api/v1/shifts/{shift}/readings", headers=auth_headers(attendant)
    )

    assert [line["nozzle_label"] for line in response.json()["lines"]] == [
        "ALREADY-THERE"
    ]


# --- creating a reading ------------------------------------------------------


async def test_a_rejected_reading_writes_no_row(
    client: AsyncClient,
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§10 requires this explicitly for TOTALIZER_DECREASED.

    Asserted by counting rows, not by trusting the status code: a 422 returned *after* a
    flush would leave a half-written reading that the next request would then refuse as a
    duplicate, and the status code alone cannot tell the two apart.
    """
    attendant = make_user("attendant")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    previous = make_shift(attendant, business_date=DAY, sequence=1, status="closed")
    make_reading(previous, nozzle, closing_reading="1500.00")
    current = make_shift(attendant, business_date=date(2026, 3, 11), sequence=1)

    response = await _post(
        client, current, auth_headers(attendant), nozzle_id=str(nozzle),
        closing_reading="1200.00",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "TOTALIZER_DECREASED"
    assert _count_readings(engine, current) == 0


async def test_a_reading_over_the_flow_ceiling_writes_no_row(
    client: AsyncClient,
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """A mistyped extra digit, caught at entry (§6.2)."""
    attendant = make_user("attendant")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    previous = make_shift(attendant, business_date=DAY, sequence=1, status="closed")
    make_reading(previous, nozzle, closing_reading="1500.00")
    current = make_shift(
        attendant,
        business_date=date(2026, 3, 11),
        sequence=1,
        ended_at=datetime(2026, 3, 11, 16, 30, tzinfo=timezone.utc),
    )

    response = await _post(
        client, current, auth_headers(attendant), nozzle_id=str(nozzle),
        closing_reading="584320.00",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "IMPLIED_FLOW_RATE_TOO_HIGH"
    assert _count_readings(engine, current) == 0


async def test_a_second_reading_for_the_same_nozzle_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§6.10: this is also what makes readings idempotent without a key store.

    A retry after a timeout on patchy rural connectivity cannot create a second row.
    """
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(admin, business_date=DAY, sequence=1)

    first = await _post(
        client, shift, auth_headers(admin), nozzle_id=str(nozzle),
        opening_reading="1000.00", closing_reading="1500.00",
    )
    retry = await _post(
        client, shift, auth_headers(admin), nozzle_id=str(nozzle),
        opening_reading="1000.00", closing_reading="1500.00",
    )

    assert first.status_code == 201
    assert retry.status_code == 409
    assert retry.json()["code"] == "READING_ALREADY_EXISTS"


async def test_a_nozzle_from_another_outlet_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """V1 has one outlet so this can never fire in production -- which is exactly why it is
    written now (§5.0). The day a second outlet exists, a reading pointed at the wrong
    outlet's meter would value a shift off a stranger's totalizer."""
    admin = make_user("admin")
    other_outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outlets (id, name, is_active) VALUES (:id, 'Other', true)"
            ).bindparams(id=other_outlet)
        )
    stranger = make_nozzle(fuel_type_ids["PETROL"], outlet_id=other_outlet)
    shift = make_shift(admin, business_date=DAY, sequence=1)

    response = await _post(
        client, shift, auth_headers(admin), nozzle_id=str(stranger),
        opening_reading="1000.00",
    )

    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM nozzles WHERE id = :n").bindparams(n=stranger)
        )
        connection.execute(
            text("DELETE FROM outlets WHERE id = :o").bindparams(o=other_outlet)
        )

    assert response.status_code == 409
    assert response.json()["code"] == "NOZZLE_NOT_AT_OUTLET"


async def test_a_decommissioned_nozzle_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    admin = make_user("admin")
    retired = make_nozzle(fuel_type_ids["PETROL"], is_active=False)
    shift = make_shift(admin, business_date=DAY, sequence=1)

    response = await _post(
        client, shift, auth_headers(admin), nozzle_id=str(retired),
        opening_reading="1000.00",
    )

    assert response.status_code == 409
    assert response.json()["code"] == "NOZZLE_INACTIVE"


async def test_a_nozzle_installed_after_the_shift_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    admin = make_user("admin")
    future_nozzle = make_nozzle(
        fuel_type_ids["PETROL"],
        meter_installed_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    shift = make_shift(admin, business_date=DAY, sequence=1)

    response = await _post(
        client, shift, auth_headers(admin), nozzle_id=str(future_nozzle),
        opening_reading="1000.00",
    )

    assert response.status_code == 409
    assert response.json()["code"] == "NOZZLE_NOT_YET_INSTALLED"


async def test_the_client_cannot_send_a_computed_quantity(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§3 rule 7 / rule 8: never trust a client-supplied derived figure.

    `extra="forbid"` turns an attempt into a 422 rather than a silently ignored field --
    which matters, because a client that thinks it set the quantity and was ignored is a
    client whose author will not find out until the money is wrong.
    """
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(admin, business_date=DAY, sequence=1)

    response = await _post(
        client, shift, auth_headers(admin), nozzle_id=str(nozzle),
        opening_reading="1000.00", closing_reading="1500.00", quantity_sold="99999.000",
    )

    assert response.status_code == 422


# --- correcting a reading ----------------------------------------------------


async def test_a_closing_reading_can_be_added_later(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """Opening at the start of the day, closing at the end -- the two-visit workflow."""
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(admin, business_date=DAY, sequence=1)
    await _post(
        client, shift, auth_headers(admin), nozzle_id=str(nozzle),
        opening_reading="1000.00",
    )

    response = await client.patch(
        f"/api/v1/shifts/{shift}/readings/{nozzle}",
        json={"closing_reading": "1500.00", "testing_quantity": "5.000"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    assert response.json()["quantity_sold"] == "495.000"


async def test_a_patch_cannot_change_the_opening_reading(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """The opening is §4.7's chained-and-confirmed value. Editing it after the fact would
    rewrite what somebody confirmed against a physical meter."""
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(admin, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, opening_reading="1000.00", closing_reading="1500.00")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/readings/{nozzle}",
        json={"opening_reading": "900.00"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 422


async def test_patching_a_nozzle_with_no_reading_is_a_404(
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

    response = await client.patch(
        f"/api/v1/shifts/{shift}/readings/{nozzle}",
        json={"closing_reading": "1500.00"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "READING_NOT_FOUND"


async def test_an_empty_patch_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(admin, business_date=DAY, sequence=1)
    make_reading(shift, nozzle)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/readings/{nozzle}",
        json={},
        headers=auth_headers(admin),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "NO_FIELDS_TO_UPDATE"


# --- §6.2's meter reset override --------------------------------------------


async def test_a_meter_reset_blocks_the_quantity_until_an_admin_states_it(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§6.2: after a reset the reading pair is meaningless. Do not infer the split.

    The worksheet must stay readable while the row is in this state -- it is where somebody
    goes to see the problem -- so the quantity comes back as null rather than a 422 that
    would hide the whole sheet.
    """
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(admin, business_date=DAY, sequence=1)
    make_reading(
        shift, nozzle, opening_reading="99000.00", closing_reading="40.00",
        meter_reset_occurred=True,
    )

    sheet = await client.get(
        f"/api/v1/shifts/{shift}/readings", headers=auth_headers(admin)
    )

    line = sheet.json()["lines"][0]
    assert line["reading"]["meter_reset_occurred"] is True
    assert line["reading"]["quantity_sold"] is None


async def test_an_admin_override_resolves_a_meter_reset(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(admin, business_date=DAY, sequence=1)
    make_reading(
        shift, nozzle, opening_reading="99000.00", closing_reading="40.00",
        meter_reset_occurred=True,
    )

    response = await client.post(
        f"/api/v1/shifts/{shift}/readings/{nozzle}/override",
        json={
            "manual_quantity_override": "312.500",
            "override_reason": "Meter replaced 14:00, W&M re-sealed. Paper log: 312.5 L.",
        },
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    assert response.json()["quantity_sold"] == "312.500"
    assert response.json()["override_reason"].startswith("Meter replaced")


async def test_an_override_without_a_reason_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§5.2: "required if override is set". An unexplained manual quantity is unauditable."""
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(admin, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, meter_reset_occurred=True)

    response = await client.post(
        f"/api/v1/shifts/{shift}/readings/{nozzle}/override",
        json={"manual_quantity_override": "312.500", "override_reason": ""},
        headers=auth_headers(admin),
    )

    assert response.status_code == 422


async def test_an_override_cannot_be_written_without_a_reason_at_the_database_level(
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
) -> None:
    """Belt and braces (§6.6)."""
    from sqlalchemy.exc import IntegrityError

    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(admin, business_date=DAY, sequence=1)

    with pytest.raises(IntegrityError) as caught:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO nozzle_readings (shift_id, nozzle_id, opening_reading, "
                    "manual_quantity_override) VALUES (:s, :n, 1000.00, 50.000)"
                ).bindparams(s=shift, n=nozzle)
            )
    assert "ck_nozzle_readings_override_has_reason" in str(caught.value)


async def test_rollover_and_reset_cannot_both_be_set_at_the_database_level(
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
) -> None:
    """§6.2 has one formula for a rollover and a different, manual path for a reset. Both
    flags together has no defined meaning, and silently picking a branch would produce a
    plausible wrong number."""
    from sqlalchemy.exc import IntegrityError

    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(admin, business_date=DAY, sequence=1)

    with pytest.raises(IntegrityError) as caught:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO nozzle_readings (shift_id, nozzle_id, opening_reading, "
                    "rollover_occurred, meter_reset_occurred) "
                    "VALUES (:s, :n, 1000.00, true, true)"
                ).bindparams(s=shift, n=nozzle)
            )
    assert "ck_nozzle_readings_flags_exclusive" in str(caught.value)


# --- review (§4.7, §13.10) ---------------------------------------------------


async def test_a_manager_clears_a_review_flag(
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
    make_reading(shift, nozzle, requires_review=True)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/readings/{nozzle}/review",
        json={"review_note": "Checked against the paper register; meter is correct."},
        headers=auth_headers(manager),
    )

    assert response.status_code == 200
    assert response.json()["requires_review"] is False
    assert response.json()["reviewed_by"] == str(manager)
    assert response.json()["reviewed_at"] is not None


async def test_reviewing_an_unflagged_reading_is_refused(
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
    make_reading(shift, nozzle)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/readings/{nozzle}/review",
        json={"review_note": "Looks fine"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "READING_NOT_FLAGGED"


async def test_a_flag_can_be_reviewed_on_a_closed_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """The one write that must still work after close.

    A mismatch is usually noticed *while reconciling*, which happens after the shift is
    closed. Gating review on `writable=True` would make the flag permanently unclearable
    on exactly the shifts that raise one.
    """
    manager = make_user("manager")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    make_reading(shift, nozzle, requires_review=True)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/readings/{nozzle}/review",
        json={"review_note": "Reconciled after close."},
        headers=auth_headers(manager),
    )

    assert response.status_code == 200


async def test_a_flag_cannot_be_reviewed_on_a_locked_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§5.2: "Nothing referencing a `locked` shift may be modified." No exceptions."""
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(admin, business_date=DAY, sequence=1, status="locked")
    make_reading(shift, nozzle, requires_review=True)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/readings/{nozzle}/review",
        json={"review_note": "Too late."},
        headers=auth_headers(admin),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_LOCKED"


async def test_the_review_note_keeps_the_original_question(
    client: AsyncClient,
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """The sign-off is appended, never substituted.

    The note explaining *why* a reading was flagged is the context a future reader needs;
    overwriting it with the answer deletes the question.
    """
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])

    first = make_shift(admin, business_date=DAY, sequence=1)
    make_reading(first, nozzle, opening_reading="1000.00", closing_reading="1500.00")
    second = make_shift(admin, business_date=date(2026, 3, 11), sequence=1)
    make_reading(
        second, nozzle, opening_reading="1500.00",
        chained_opening_reading="1500.00", closing_reading="1900.00",
    )
    await client.patch(
        f"/api/v1/shifts/{first}/readings/{nozzle}",
        json={"closing_reading": "1450.00"},
        headers=auth_headers(admin),
    )

    await client.patch(
        f"/api/v1/shifts/{second}/readings/{nozzle}/review",
        json={"review_note": "Original 1500 confirmed against paper; upstream typo."},
        headers=auth_headers(admin),
    )

    with engine.connect() as connection:
        note = connection.execute(
            text(
                "SELECT review_note FROM nozzle_readings "
                "WHERE shift_id = :s AND nozzle_id = :n"
            ).bindparams(s=second, n=nozzle)
        ).scalar_one()

    assert "deliberately NOT been changed" in note  # the question
    assert "upstream typo" in note  # the answer


# --- §6.8's close precondition ----------------------------------------------


async def test_a_shift_cannot_close_with_an_unread_nozzle(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§6.8's MISSING_NOZZLE_READINGS. An unread nozzle is fuel that left the tank with no
    sale against it."""
    manager = make_user("manager")
    read = make_nozzle(fuel_type_ids["PETROL"], label="DU-1/N-1")
    make_nozzle(fuel_type_ids["PETROL"], label="DU-2/N-1", dispenser_label="DU-2")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, read, closing_reading="1500.00")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/close",
        json={"ended_at": "2026-03-10T22:00:00+05:30"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MISSING_NOZZLE_READINGS"
    # Named, not counted -- "two nozzles are missing" sends somebody hunting.
    assert "DU-2/N-1" in response.json()["detail"]
    assert "DU-1/N-1" not in response.json()["detail"]


async def test_a_shift_cannot_close_with_a_reading_that_has_no_closing_value(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    nozzle = make_nozzle(fuel_type_ids["PETROL"], label="DU-1/N-1")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, closing_reading=None)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/close",
        json={"ended_at": "2026-03-10T22:00:00+05:30"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MISSING_NOZZLE_READINGS"


async def test_a_meter_reset_awaiting_an_override_blocks_the_close(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """Easy to miss: the row HAS a closing reading, but §6.2 says the pair is meaningless
    after a reset. Treating the number as an answer would value the shift off a reading the
    spec explicitly calls unusable."""
    manager = make_user("manager")
    nozzle = make_nozzle(fuel_type_ids["PETROL"], label="DU-1/N-1")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(
        shift, nozzle, opening_reading="99000.00", closing_reading="40.00",
        meter_reset_occurred=True,
    )

    response = await client.patch(
        f"/api/v1/shifts/{shift}/close",
        json={"ended_at": "2026-03-10T22:00:00+05:30"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MISSING_NOZZLE_READINGS"


async def test_a_fully_read_shift_closes(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    one = make_nozzle(fuel_type_ids["PETROL"], label="DU-1/N-1")
    two = make_nozzle(fuel_type_ids["DIESEL"], label="DU-2/N-1", dispenser_label="DU-2")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, one, closing_reading="1500.00")
    make_reading(shift, two, closing_reading="2500.00")
    # Phase 6: fuel moved, so §6.8 now also wants a declared cash figure before close.
    make_collection(shift, mode="cash", amount="42000.00")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/close",
        json={"ended_at": "2026-03-10T22:00:00+05:30"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "closed"


async def test_a_shift_with_no_nozzles_at_all_still_closes(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """A brand-new outlet with no meters registered yet must not be trapped open."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/close",
        json={"ended_at": "2026-03-10T22:00:00+05:30"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 200


async def test_the_flow_ceiling_is_re_run_at_close(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """The second half of the deferred-ceiling arrangement (§6.2).

    A reading entered while the shift had no `ended_at` skipped the ceiling. Close supplies
    the end time, so this is where that gap shuts -- here, a 20-minute shift that would
    have had to pump 584,320 litres.
    """
    manager = make_user("manager")
    nozzle = make_nozzle(fuel_type_ids["PETROL"], label="DU-1/N-1")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="584320.00")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/close",
        json={"ended_at": "2026-03-10T06:20:00+05:30"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "IMPLIED_FLOW_RATE_TOO_HIGH"


# --- remaining branches ------------------------------------------------------


async def test_a_reading_against_an_unknown_nozzle_is_a_404(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """404 rather than a permission error: the nozzle is missing, not forbidden."""
    admin = make_user("admin")
    shift = make_shift(admin, business_date=DAY, sequence=1)

    response = await _post(
        client, shift, auth_headers(admin), nozzle_id=str(uuid4()),
        opening_reading="1000.00",
    )

    assert response.status_code == 404
    assert response.json()["code"] == "NOZZLE_NOT_FOUND"


async def test_a_reading_within_the_ceiling_is_accepted_when_the_window_is_known(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """The positive side of §6.2's ceiling.

    Every other ceiling test asserts a refusal, which would all still pass if the guard
    rejected *everything*. This is the one that pins down that a real day gets through:
    5,000 litres over a 16-hour shift, against a 60 L/min nozzle.
    """
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(
        admin,
        business_date=DAY,
        sequence=1,
        ended_at=datetime(2026, 3, 10, 16, 30, tzinfo=timezone.utc),
    )

    response = await _post(
        client, shift, auth_headers(admin), nozzle_id=str(nozzle),
        opening_reading="1000.00", closing_reading="6000.00",
    )

    assert response.status_code == 201
    assert response.json()["quantity_sold"] == "5000.000"


async def test_an_explicit_null_in_a_patch_is_ignored_rather_than_clearing_a_field(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """Matches the PATCH semantics the Phase 3 and Phase 4 routers already use.

    A client that serialises its whole form, nulls included, must not silently wipe a
    closing reading it never meant to touch.
    """
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(admin, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, opening_reading="1000.00", closing_reading="1500.00")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/readings/{nozzle}",
        json={"closing_reading": None, "testing_quantity": "5.000"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    assert response.json()["closing_reading"] == "1500.00"
    assert response.json()["quantity_sold"] == "495.000"


async def test_a_shift_with_an_overridden_reading_closes(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """An admin's signed quantity is not held against a mechanical flow rate at close.

    Doing so would refuse the one path that exists for when the meter itself lied (§6.2) --
    the reading pair after a reset is meaningless, so a ceiling derived from it is too.
    """
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"], label="DU-1/N-1")
    shift = make_shift(admin, business_date=DAY, sequence=1)
    make_reading(
        shift, nozzle, opening_reading="99000.00", closing_reading="40.00",
        meter_reset_occurred=True,
    )
    make_collection(shift, mode="cash", amount="29687.50")
    await client.post(
        f"/api/v1/shifts/{shift}/readings/{nozzle}/override",
        json={
            "manual_quantity_override": "312.500",
            "override_reason": "Meter replaced 14:00, W&M re-sealed.",
        },
        headers=auth_headers(admin),
    )

    response = await client.patch(
        f"/api/v1/shifts/{shift}/close",
        json={"ended_at": "2026-03-10T06:20:00+05:30"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "closed"


async def test_an_unread_nozzle_appears_on_the_sales_report_as_unknown(
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
    """A nozzle with no reading row at all is still a line, with nulls.

    Dropping it would make the report silently describe fewer meters than the outlet has,
    which reads as a complete day and is not one.
    """
    manager = make_user("manager")
    petrol = fuel_type_ids["PETROL"]
    make_fuel_price(petrol, "104.50", datetime(2026, 1, 1, tzinfo=timezone.utc),
                    entered_by=manager)
    make_fuel_margin(petrol, "1.85", datetime(2026, 1, 1, tzinfo=timezone.utc),
                     entered_by=manager)
    read = make_nozzle(petrol, label="DU-1/N-1")
    make_nozzle(petrol, label="DU-2/N-1", dispenser_label="DU-2")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, read, opening_reading="1000.00", closing_reading="1500.00")

    body = (
        await client.get(f"/api/v1/shifts/{shift}/sales", headers=auth_headers(manager))
    ).json()

    lines = {line["nozzle_label"]: line for line in body["lines"]}
    assert set(lines) == {"DU-1/N-1", "DU-2/N-1"}
    assert lines["DU-2/N-1"]["quantity_sold"] is None
    assert lines["DU-2/N-1"]["rate_per_unit"] is None
    assert body["incomplete"] is True


async def test_a_next_shift_with_no_reading_for_that_nozzle_has_nothing_to_flag(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§13.10's flag has to cope with the nozzle being out of order next shift.

    There is no stale opening to warn about, because no opening was carried -- so the
    correction proceeds quietly rather than failing on a missing row.
    """
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])

    first = make_shift(admin, business_date=DAY, sequence=1)
    make_reading(first, nozzle, opening_reading="1000.00", closing_reading="1500.00")
    # The next shift exists but never used this nozzle.
    make_shift(admin, business_date=date(2026, 3, 11), sequence=1)

    response = await client.patch(
        f"/api/v1/shifts/{first}/readings/{nozzle}",
        json={"closing_reading": "1450.00"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    assert response.json()["quantity_sold"] == "450.000"


def test_revalidating_flow_rates_skips_rows_it_cannot_measure(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
) -> None:
    """A service-level test, because the close route can no longer reach this branch.

    `missing_closing_readings` rejects an unread or half-read nozzle before
    `revalidate_flow_rates` ever runs, so through HTTP these `continue`s are dead. They are
    kept anyway, and tested here, because this is a public service function: Phase 10's
    cash engine and any later management command may call it on a shift that is still being
    entered, and a flow-rate check against a NULL closing reading would be a TypeError deep
    inside the money path rather than a skipped row.
    """
    from app.db.session import SessionLocal
    from app.models.shift import Shift
    from app.services import readings as reading_service

    admin = make_user("admin")
    unread = make_nozzle(fuel_type_ids["PETROL"], label="DU-9/N-1", dispenser_label="DU-9")
    half_read = make_nozzle(fuel_type_ids["PETROL"], label="DU-9/N-2", dispenser_label="DU-9")
    shift_id = make_shift(
        admin,
        business_date=DAY,
        sequence=1,
        ended_at=datetime(2026, 3, 10, 16, 30, tzinfo=timezone.utc),
    )
    make_reading(shift_id, half_read, opening_reading="1000.00", closing_reading=None)
    # `unread` deliberately has no row at all.

    with SessionLocal() as session:
        shift = session.get(Shift, shift_id)
        # The assertion is that this returns rather than raising.
        reading_service.revalidate_flow_rates(session, shift=shift)

    assert unread is not None


async def test_a_meter_reset_can_be_recorded_before_an_admin_states_the_quantity(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """The two-step workflow §6.2 actually describes.

    "The meter was replaced" is an **observation**, made by whoever is entering the day.
    "312.5 litres went through it" is a **judgement**, and §8 reserves that for an admin.
    They are different acts by different people at different times, so recording the first
    must not require the second to have happened already -- otherwise a reset can only be
    reported by an admin, and the attendant who saw the engineer swap the meter has no way
    to say so.
    """
    attendant = make_user("attendant")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    previous = make_shift(attendant, business_date=DAY, sequence=1, status="closed")
    make_reading(previous, nozzle, closing_reading="99000.00")
    shift = make_shift(attendant, business_date=date(2026, 3, 11), sequence=1)

    response = await _post(
        client, shift, auth_headers(attendant), nozzle_id=str(nozzle),
        closing_reading="40.00", meter_reset_occurred=True,
    )

    assert response.status_code == 201, response.json()
    # Recorded, but pointedly without a quantity: the reading pair is meaningless (§6.2)
    # and nothing may guess. An admin supplies the figure through the override route.
    assert response.json()["meter_reset_occurred"] is True
    assert response.json()["quantity_sold"] is None


async def test_the_worksheet_survives_a_row_whose_arithmetic_is_invalid(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """The worksheet is where somebody goes to FIX a bad row, so it must render one.

    A decreased totalizer with no rollover flag cannot be created through the API -- the
    §6.2 guards run before every write. It can arrive from a fixture, a data import, or a
    future migration, and if the sheet 422'd on it the only page that would let anyone
    correct the row would be the one page they could not open.

    This is the single place in the module where a §6.2 failure is swallowed. Every write
    path still validates and still refuses.
    """
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"], label="DU-1/N-1")
    shift = make_shift(admin, business_date=DAY, sequence=1)
    # Written straight to the database, bypassing the guards, as an import would.
    make_reading(shift, nozzle, opening_reading="1500.00", closing_reading="1000.00")

    response = await client.get(
        f"/api/v1/shifts/{shift}/readings", headers=auth_headers(admin)
    )

    assert response.status_code == 200
    line = response.json()["lines"][0]
    assert line["reading"]["closing_reading"] == "1000.00"
    assert line["reading"]["quantity_sold"] is None


# --- Phase 6 Step 0: three defects found auditing Phase 5 ---------------------
#
# All three are written here rather than in a separate file because they are properties of
# the reading routes, and a reader looking for "what does POST /readings refuse" should not
# have to know which phase found the gap.


async def test_both_meter_flags_through_the_api_is_a_422_not_a_500(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
    clean_readings,
) -> None:
    """The route must refuse what the CHECK constraint refuses, in the §3 rule 10 envelope.

    `test_rollover_and_reset_cannot_both_be_set_at_the_database_level` above proves the
    constraint exists, but it inserts raw SQL and so never exercised the route. Through
    HTTP the two flags together reached `db.flush()` -- `quantity_if_known` returns None
    for a row awaiting an override, so `_validate_math` returned without checking anything
    -- and the resulting IntegrityError had no handler, giving the caller a 500.

    A 500 tells an attendant nothing and tells a client nothing to branch on. §3 rule 10
    is not satisfied by a constraint that fires; it is satisfied by an error the caller
    can read.
    """
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(admin, business_date=DAY, sequence=1)

    response = await _post(
        client,
        shift,
        auth_headers(admin),
        nozzle_id=str(nozzle),
        opening_reading="1000.00",
        closing_reading="1500.00",
        rollover_occurred=True,
        meter_reset_occurred=True,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "METER_FLAGS_MUTUALLY_EXCLUSIVE"


async def test_an_admin_override_is_exempt_from_the_flow_ceiling_at_entry(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """One rule about overrides, applied at both sites that check the ceiling.

    `revalidate_flow_rates` skips any row carrying an override, for the reason stated in
    its own docstring: holding a human's signed figure against a mechanical flow rate would
    refuse the one path that exists for when the meter itself lied. `_validate_math` did
    not skip it, so the same row was refused at entry and never re-checked at close --
    the two sites disagreeing about the same fact.

    The scenario is the one the override exists for: a meter that ran away, a gross
    throughput far above anything the nozzle can physically deliver, and an admin stating
    what actually went out.
    """
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(
        admin,
        business_date=DAY,
        sequence=1,
        ended_at=datetime(2026, 3, 10, 16, 30, tzinfo=timezone.utc),
    )
    # 100,000 L across a 16-hour window is ~104 L/min against a 60 L/min ceiling.
    # Inserted directly because entry would rightly refuse it -- there is no override yet.
    make_reading(shift, nozzle, opening_reading="0.00", closing_reading="100000.00")

    response = await client.post(
        f"/api/v1/shifts/{shift}/readings/{nozzle}/override",
        json={
            "manual_quantity_override": "312.500",
            "override_reason": "meter ran away after a power surge; forecourt log says 312.5 L",
        },
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    assert response.json()["quantity_sold"] == "312.500"


async def test_the_downstream_flag_follows_the_chain_not_merely_the_next_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """§13.10 must flag the row that actually carried the value, wherever it is.

    `chained_opening` deliberately skips shifts with no closing reading for the nozzle
    (§4.7: "the most recent closing reading *for that nozzle*", not "the previous shift's"),
    so with the nozzle out of order for shift 2, shift 3's opening was carried from
    shift 1. The flag looked only at shift 2, found nothing, and returned -- leaving
    shift 3 silently stale, which is the exact failure §13.10 exists to prevent.
    """
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    other = make_nozzle(fuel_type_ids["PETROL"], label="DU-2/N-1", dispenser_label="DU-2")

    first = make_shift(admin, business_date=DAY, sequence=1)
    make_reading(first, nozzle, opening_reading="1000.00", closing_reading="1500.00")

    # Shift 2: this nozzle was out of order, so it has a reading for `other` only.
    second = make_shift(admin, business_date=date(2026, 3, 11), sequence=1, status="closed")
    make_reading(second, other, opening_reading="0.00", closing_reading="90.00")

    # Shift 3 carried its opening from shift 1, across the gap.
    third = make_shift(admin, business_date=date(2026, 3, 12), sequence=1, status="closed")
    downstream = make_reading(
        third,
        nozzle,
        opening_reading="1500.00",
        chained_opening_reading="1500.00",
        closing_reading="1600.00",
    )

    response = await client.patch(
        f"/api/v1/shifts/{first}/readings/{nozzle}",
        json={"closing_reading": "1450.00"},
        headers=auth_headers(admin),
    )
    assert response.status_code == 200

    with engine.connect() as connection:
        flagged, note = connection.execute(
            text(
                "SELECT requires_review, review_note FROM nozzle_readings WHERE id = :id"
            ).bindparams(id=downstream)
        ).one()

    assert flagged is True
    # The note names the shift that moved: two days back, across the gap.
    assert "2026-03-10" in note
    assert "DU-1/N-1" in note
