"""Every admin write to reference data leaves a trail (CLAUDE.md §5.3, §11, §14).

§5.3 draws the line this file defends: `created_by` is **change tracking**, not an audit
trail -- it says who last touched a row, never what it was before or how many times it
changed. Seven reference tables had the first and not the second until Phase 11, and three of
them gate money rules directly (§6.11's `requires_receipt`, §6.6's `credit_limit`, §6.3's
valuation instant via a shift template).

The assertions here come in four shapes, and the last two are the ones worth reading:

* a successful write records **exactly one** row, with the right `table_name`;
* a `PATCH` records **both sides**, and they differ -- which fails if the snapshot is taken
  after the mutation instead of before;
* a **refused** write records **nothing**, which is what proves the audit row and the change
  it describes share one transaction (§5.3: "a separately committed audit row can describe a
  change that was subsequently rolled back... a log that lies");
* nothing here records `status_change`, because that label means a *shift* lifecycle move
  (§5.2) and a deactivation is an ordinary `update`.

Assertions are written **per endpoint rather than as a loop over a table of them**. A loop
that silently skips an entry passes, and this is precisely the gap that survived seven phases
because nothing failed when it was missing.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import text


# --- helpers ------------------------------------------------------------------
#
# Deliberately duplicated in each test module rather than hoisted into a shared
# tests/helpers.py -- the house idiom. A helper shared across files acquires parameters for
# each new caller until it is harder to read than the query it replaced.


def _audit_rows(engine, *, table_name: str, record_id) -> list[dict]:
    """Every audit row for one record, oldest first."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT action, changed_by, outlet_id, old_values, new_values, request_id "
                "FROM audit_logs WHERE table_name = :t AND record_id = :id "
                "ORDER BY changed_at, id"
            ).bindparams(t=table_name, id=UUID(str(record_id)))
        ).mappings()
        return [dict(row) for row in rows]


def _audit_count(engine, *, table_name: str) -> int:
    """How many rows the whole table has -- for the "a refusal writes nothing" case, where
    there is no record id to look up because no record was created."""
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT count(*) FROM audit_logs WHERE table_name = :t").bindparams(
                t=table_name
            )
        ).scalar_one()


# --- fuel_types (§5.1, §4.5) --------------------------------------------------


async def test_creating_a_fuel_type_records_one_insert(
    client, make_user, auth_headers, engine
) -> None:
    """The XP-95 case from §5.1, now with a record of who added it."""
    admin = make_user("admin")

    response = await client.post(
        "/api/v1/fuel-types",
        headers=auth_headers(admin),
        json={
            "code": "XP95AUDIT",
            "display_name": "XP-95",
            "unit_of_measure": "litre",
            "max_flow_rate_per_minute": "60.000",
        },
    )
    try:
        assert response.status_code == 201
        fuel_type_id = response.json()["id"]

        rows = _audit_rows(engine, table_name="fuel_types", record_id=fuel_type_id)
        assert len(rows) == 1
        assert rows[0]["action"] == "insert"
        assert rows[0]["changed_by"] == admin
        # An insert has no previous state. NULL rather than {} so "nothing was there" and
        # "everything was empty" stay distinguishable.
        assert rows[0]["old_values"] is None
        assert rows[0]["new_values"]["code"] == "XP95AUDIT"
        assert rows[0]["new_values"]["unit_of_measure"] == "litre"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM fuel_types WHERE code = 'XP95AUDIT'")
            )


async def test_a_fuel_types_audit_row_carries_the_acting_admins_outlet(
    client, make_user, auth_headers, engine
) -> None:
    """§5.3's global-reference-data note, as a test.

    `fuel_types` is the one audited table with no `outlet_id` of its own -- a litre is a litre
    at every outlet (§5.1) -- while `audit_logs.outlet_id` is NOT NULL. The audit row
    therefore records *the outlet whose admin made the change*, which is a different fact from
    the one every other table's audit row records, and is why §5.3 spells it out.
    """
    from app.core.config import get_settings

    admin = make_user("admin")

    response = await client.post(
        "/api/v1/fuel-types",
        headers=auth_headers(admin),
        json={
            "code": "XP95OUTLET",
            "display_name": "XP-95",
            "unit_of_measure": "litre",
            "max_flow_rate_per_minute": "60.000",
        },
    )
    try:
        assert response.status_code == 201
        rows = _audit_rows(
            engine, table_name="fuel_types", record_id=response.json()["id"]
        )
        assert rows[0]["outlet_id"] == get_settings().DEFAULT_OUTLET_ID
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM fuel_types WHERE code = 'XP95OUTLET'")
            )


async def test_editing_a_fuel_type_records_both_sides_of_the_change(
    client, make_user, auth_headers, engine, make_fuel_type
) -> None:
    """The assertion that fails if `_audit_snapshot` is called after the mutation.

    `old_values != new_values` is the whole point: a trail that records the new state twice
    tells you a change happened and refuses to say what it was.
    """
    admin = make_user("admin")
    fuel_type_id = make_fuel_type(
        "AUDITEDIT", display_name="Before", max_flow_rate_per_minute="60.000"
    )

    response = await client.patch(
        f"/api/v1/fuel-types/{fuel_type_id}",
        headers=auth_headers(admin),
        json={"display_name": "After", "max_flow_rate_per_minute": "45.500"},
    )

    assert response.status_code == 200
    rows = _audit_rows(engine, table_name="fuel_types", record_id=fuel_type_id)
    assert len(rows) == 1
    assert rows[0]["action"] == "update"
    assert rows[0]["old_values"]["display_name"] == "Before"
    assert rows[0]["new_values"]["display_name"] == "After"
    assert rows[0]["old_values"] != rows[0]["new_values"]
    # §6.2's sanity ceiling moved. §14 records that the seeded CBG figure is an unconfirmed
    # guess, so the day somebody widens one, this is the record of by how much.
    assert Decimal(rows[0]["old_values"]["max_flow_rate_per_minute"]) == Decimal("60.000")
    assert Decimal(rows[0]["new_values"]["max_flow_rate_per_minute"]) == Decimal("45.500")


async def test_deactivating_a_fuel_type_is_an_update_not_a_status_change(
    client, make_user, auth_headers, engine, make_fuel_type
) -> None:
    """§5.2 reserves `status_change` for a shift lifecycle move.

    Because §3 rule 6 forbids hard deletes, `is_active` is how *every* reference table retires
    a row. Admitting those as `status_change` would make the label mean "a shift moved, or
    anything at all was deactivated", and nobody could query for lifecycle events again.
    """
    admin = make_user("admin")
    fuel_type_id = make_fuel_type("AUDITDEACT")

    response = await client.patch(
        f"/api/v1/fuel-types/{fuel_type_id}",
        headers=auth_headers(admin),
        json={"is_active": False},
    )

    assert response.status_code == 200
    rows = _audit_rows(engine, table_name="fuel_types", record_id=fuel_type_id)
    assert rows[0]["action"] == "update"
    assert rows[0]["old_values"]["is_active"] is True
    assert rows[0]["new_values"]["is_active"] is False


async def test_a_duplicate_fuel_type_code_writes_no_audit_row(
    client, make_user, auth_headers, engine
) -> None:
    """A refusal records nothing -- the single-transaction contract (§5.3).

    Counted across the whole `fuel_types` table rather than by record id, because the point is
    that no record was created to have an id.
    """
    admin = make_user("admin")
    before = _audit_count(engine, table_name="fuel_types")

    response = await client.post(
        "/api/v1/fuel-types",
        headers=auth_headers(admin),
        # Seeded by migration 0003, so this collides.
        json={
            "code": "PETROL",
            "display_name": "Duplicate",
            "unit_of_measure": "litre",
            "max_flow_rate_per_minute": "60.000",
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "FUEL_TYPE_CODE_EXISTS"
    assert _audit_count(engine, table_name="fuel_types") == before


async def test_a_refused_immutable_field_writes_no_audit_row(
    client, make_user, auth_headers, engine, make_fuel_type
) -> None:
    """§5.1 refuses a `unit_of_measure` change rather than ignoring it, and the trail must not
    claim something happened. Changing a unit would reinterpret every quantity ever recorded
    against that fuel -- litres read as kilograms -- so a phantom audit row here would be a
    record of the most consequential edit in the schema, for an edit that did not occur."""
    admin = make_user("admin")
    fuel_type_id = make_fuel_type("AUDITIMMUT")

    response = await client.patch(
        f"/api/v1/fuel-types/{fuel_type_id}",
        headers=auth_headers(admin),
        json={"unit_of_measure": "kilogram"},
    )

    assert response.status_code == 422
    assert _audit_rows(engine, table_name="fuel_types", record_id=fuel_type_id) == []


async def test_a_non_admin_creating_a_fuel_type_writes_no_audit_row(
    client, make_user, auth_headers, engine
) -> None:
    """§8: managing fuel types is admin-only. A 403 is refused before any write."""
    manager = make_user("manager")
    before = _audit_count(engine, table_name="fuel_types")

    response = await client.post(
        "/api/v1/fuel-types",
        headers=auth_headers(manager),
        json={
            "code": "XP95FORBID",
            "display_name": "XP-95",
            "unit_of_measure": "litre",
            "max_flow_rate_per_minute": "60.000",
        },
    )

    assert response.status_code == 403
    assert _audit_count(engine, table_name="fuel_types") == before


async def test_the_editing_admin_is_recorded_not_the_creating_one(
    client, make_user, auth_headers, engine, make_fuel_type
) -> None:
    """`changed_by` is the **acting** admin, which is the fact `created_by` cannot give you.

    §5.3's whole argument in one assertion: the row's `created_by` still names whoever added
    the fuel, and only the audit trail knows somebody else changed it afterwards.
    """
    author = make_user("admin")
    editor = make_user("admin")
    fuel_type_id = make_fuel_type("AUDITWHO", display_name="Before")

    response = await client.patch(
        f"/api/v1/fuel-types/{fuel_type_id}",
        headers=auth_headers(editor),
        json={"display_name": "After"},
    )

    assert response.status_code == 200
    rows = _audit_rows(engine, table_name="fuel_types", record_id=fuel_type_id)
    assert rows[0]["changed_by"] == editor
    assert rows[0]["changed_by"] != author


async def test_the_request_id_ties_a_reference_data_change_to_its_response(
    client, make_user, auth_headers, engine, make_fuel_type
) -> None:
    """§5.3's `request_id` correlates the audit row with the application log lines for the
    same request. Honoured from an inbound header, so a client retrying a change can find
    both sides of it afterwards."""
    admin = make_user("admin")
    fuel_type_id = make_fuel_type("AUDITREQID")

    response = await client.patch(
        f"/api/v1/fuel-types/{fuel_type_id}",
        headers={**auth_headers(admin), "X-Request-ID": "phase-11-reference-data"},
        json={"display_name": "Renamed"},
    )

    assert response.status_code == 200
    rows = _audit_rows(engine, table_name="fuel_types", record_id=fuel_type_id)
    assert rows[0]["request_id"] == "phase-11-reference-data"


# --- nozzles (§5.1, §4.3) -----------------------------------------------------


async def test_registering_a_nozzle_records_one_insert(
    client, make_user, auth_headers, engine, fuel_type_ids
) -> None:
    """§4.3's meter facts are recorded at registration.

    `totalizer_max_value` is in the snapshot because §6.2 reads it to compute a rollover: get
    it wrong and a rolled-over meter yields the wrong quantity, silently. This is the record
    of what it was declared to be.
    """
    admin = make_user("admin")

    response = await client.post(
        "/api/v1/nozzles",
        headers=auth_headers(admin),
        json={
            "label": "DU-9/N-9",
            "dispenser_label": "DU-9",
            "fuel_type_id": str(fuel_type_ids["PETROL"]),
            "totalizer_max_value": "999999.99",
            "meter_installed_at": "2020-01-01T00:00:00+00:00",
        },
    )
    try:
        assert response.status_code == 201
        nozzle_id = response.json()["id"]

        rows = _audit_rows(engine, table_name="nozzles", record_id=nozzle_id)
        assert len(rows) == 1
        assert rows[0]["action"] == "insert"
        assert rows[0]["old_values"] is None
        assert rows[0]["new_values"]["label"] == "DU-9/N-9"
        assert Decimal(rows[0]["new_values"]["totalizer_max_value"]) == Decimal(
            "999999.99"
        )
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM nozzles WHERE label = 'DU-9/N-9'")
            )


async def test_relabelling_a_nozzle_records_both_sides(
    client, make_user, auth_headers, engine, make_nozzle, fuel_type_ids
) -> None:
    admin = make_user("admin")
    nozzle_id = make_nozzle(fuel_type_ids["PETROL"], label="DU-8/N-1")

    response = await client.patch(
        f"/api/v1/nozzles/{nozzle_id}",
        headers=auth_headers(admin),
        json={"label": "DU-8/N-2"},
    )

    assert response.status_code == 200
    rows = _audit_rows(engine, table_name="nozzles", record_id=nozzle_id)
    assert len(rows) == 1
    assert rows[0]["action"] == "update"
    assert rows[0]["old_values"]["label"] == "DU-8/N-1"
    assert rows[0]["new_values"]["label"] == "DU-8/N-2"


async def test_deactivating_a_nozzle_is_an_update(
    client, make_user, auth_headers, engine, make_nozzle, fuel_type_ids
) -> None:
    """No DELETE anywhere (§3 rule 6) -- removing a nozzle would orphan every reading taken
    through it -- so retirement is an `is_active` flip, and it is an ordinary `update`."""
    admin = make_user("admin")
    nozzle_id = make_nozzle(fuel_type_ids["PETROL"], label="DU-7/N-1")

    response = await client.patch(
        f"/api/v1/nozzles/{nozzle_id}",
        headers=auth_headers(admin),
        json={"is_active": False},
    )

    assert response.status_code == 200
    rows = _audit_rows(engine, table_name="nozzles", record_id=nozzle_id)
    assert rows[0]["action"] == "update"
    assert rows[0]["old_values"]["is_active"] is True
    assert rows[0]["new_values"]["is_active"] is False


async def test_a_duplicate_nozzle_label_writes_no_audit_row(
    client, make_user, auth_headers, engine, make_nozzle, fuel_type_ids
) -> None:
    admin = make_user("admin")
    make_nozzle(fuel_type_ids["PETROL"], label="DU-6/N-1")
    before = _audit_count(engine, table_name="nozzles")

    response = await client.post(
        "/api/v1/nozzles",
        headers=auth_headers(admin),
        json={
            "label": "DU-6/N-1",
            "dispenser_label": "DU-6",
            "fuel_type_id": str(fuel_type_ids["PETROL"]),
            "totalizer_max_value": "999999.99",
            "meter_installed_at": "2020-01-01T00:00:00+00:00",
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "NOZZLE_LABEL_EXISTS"
    assert _audit_count(engine, table_name="nozzles") == before


async def test_a_refused_nozzle_rewire_writes_no_audit_row(
    client, make_user, auth_headers, engine, make_nozzle, fuel_type_ids
) -> None:
    """§5.1 refuses `fuel_type_id` on a PATCH: repointing a nozzle at a different fuel would
    revalue every past shift at the wrong rate (§6.3 recomputes on read). A rewired meter is a
    new row. The trail must not record a change that was refused."""
    admin = make_user("admin")
    nozzle_id = make_nozzle(fuel_type_ids["PETROL"], label="DU-5/N-1")

    response = await client.patch(
        f"/api/v1/nozzles/{nozzle_id}",
        headers=auth_headers(admin),
        json={"fuel_type_id": str(fuel_type_ids["CBG"])},
    )

    assert response.status_code == 422
    assert _audit_rows(engine, table_name="nozzles", record_id=nozzle_id) == []


async def test_a_non_admin_registering_a_nozzle_writes_no_audit_row(
    client, make_user, auth_headers, engine, fuel_type_ids
) -> None:
    manager = make_user("manager")
    before = _audit_count(engine, table_name="nozzles")

    response = await client.post(
        "/api/v1/nozzles",
        headers=auth_headers(manager),
        json={
            "label": "DU-4/N-1",
            "dispenser_label": "DU-4",
            "fuel_type_id": str(fuel_type_ids["PETROL"]),
            "totalizer_max_value": "999999.99",
            "meter_installed_at": "2020-01-01T00:00:00+00:00",
        },
    )

    assert response.status_code == 403
    assert _audit_count(engine, table_name="nozzles") == before
