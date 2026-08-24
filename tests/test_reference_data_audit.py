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

import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from httpx import ASGITransport, AsyncClient as _AsyncClient
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


# --- fuel_prices and fuel_margins (§4.1, §4.6, §5.1) --------------------------
#
# The sharpest case in this phase. Both tables are append-only -- no UPDATE, no DELETE,
# enforced by a trigger -- so `entered_by` on the row looks like it already answers "who".
# It does not answer the question that matters, because **backdating is permitted**: a row
# whose `effective_from` is in the past silently revalues shifts that are already closed
# (§6.3 recomputes valuation on read). §4.1 made these tables append-only precisely because
# a mutable price column "silently corrupts every historical report" -- and until Phase 11
# the one operation that can still do that left no trail at all.

# Well clear of "now" in both directions, so the backdating assertions do not depend on when
# the suite runs. The `datetime` pair is what `make_fuel_price` takes; the ISO strings are
# what a JSON body carries.
_FUTURE_AT = datetime(2030, 6, 1, 6, 0, tzinfo=timezone.utc)
_PAST_AT = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)
_FUTURE = _FUTURE_AT.isoformat()
_PAST = _PAST_AT.isoformat()


def _clear_effective_dated(engine, table: str) -> None:
    """Append-only means the trigger has to come off to clean up (the house pattern)."""
    with engine.begin() as connection:
        connection.execute(
            text(f"ALTER TABLE {table} DISABLE TRIGGER trg_{table}_append_only")
        )
        connection.execute(text(f"DELETE FROM {table}"))
        connection.execute(
            text(f"ALTER TABLE {table} ENABLE TRIGGER trg_{table}_append_only")
        )


async def test_entering_a_rate_records_one_insert_with_the_money_as_a_string(
    client, make_user, auth_headers, engine, fuel_type_ids
) -> None:
    """§3 rule 1 reaches into the audit trail too.

    `jsonable_encoder`'s default for `Decimal` is `float`, which is wrong twice over here: it
    is the forbidden type, and it drops the scale, so `Decimal("104.21")` would come back as
    `104.21` the float and the row could no longer show two decimal places. `services/audit.py`
    stringifies instead, and this asserts it end to end rather than at the helper.
    """
    admin = make_user("admin")

    response = await client.post(
        "/api/v1/fuel-prices",
        headers=auth_headers(admin),
        json={
            "fuel_type_id": str(fuel_type_ids["PETROL"]),
            "rate_per_unit": "104.21",
            "effective_from": _FUTURE,
        },
    )
    try:
        assert response.status_code == 201
        rows = _audit_rows(
            engine, table_name="fuel_prices", record_id=response.json()["id"]
        )
        assert len(rows) == 1
        assert rows[0]["action"] == "insert"
        assert rows[0]["old_values"] is None
        # A string, not a float -- and it round-trips exactly.
        assert rows[0]["new_values"]["rate_per_unit"] == "104.21"
        assert Decimal(rows[0]["new_values"]["rate_per_unit"]) == Decimal("104.21")
    finally:
        _clear_effective_dated(engine, "fuel_prices")


async def test_a_backdated_rate_is_recorded_as_backdated(
    client, make_user, auth_headers, engine, fuel_type_ids
) -> None:
    """The assertion this whole step exists for.

    Backdating is allowed on purpose: refusing it would leave Tuesday's unentered revision
    valuing Tuesday and Wednesday at a stale rate permanently, with no legal correction. The
    cost is that it revalues closed shifts, so the trail has to say it happened -- a log line
    alone is not the record §5.3 requires.
    """
    admin = make_user("admin")

    response = await client.post(
        "/api/v1/fuel-prices",
        headers=auth_headers(admin),
        json={
            "fuel_type_id": str(fuel_type_ids["PETROL"]),
            "rate_per_unit": "99.99",
            "effective_from": _PAST,
        },
    )
    try:
        assert response.status_code == 201
        assert response.json()["is_backdated"] is True

        rows = _audit_rows(
            engine, table_name="fuel_prices", record_id=response.json()["id"]
        )
        assert rows[0]["new_values"]["is_backdated"] is True
        assert rows[0]["changed_by"] == admin
    finally:
        _clear_effective_dated(engine, "fuel_prices")


async def test_a_forward_dated_rate_is_recorded_as_not_backdated(
    client, make_user, auth_headers, engine, fuel_type_ids
) -> None:
    """The other half, so the flag is proven to discriminate rather than always being true."""
    response = await client.post(
        "/api/v1/fuel-prices",
        headers=auth_headers(make_user("admin")),
        json={
            "fuel_type_id": str(fuel_type_ids["PETROL"]),
            "rate_per_unit": "104.21",
            "effective_from": _FUTURE,
        },
    )
    try:
        assert response.status_code == 201
        rows = _audit_rows(
            engine, table_name="fuel_prices", record_id=response.json()["id"]
        )
        assert rows[0]["new_values"]["is_backdated"] is False
    finally:
        _clear_effective_dated(engine, "fuel_prices")


async def test_a_clashing_effective_from_writes_no_audit_row(
    client, make_user, auth_headers, engine, fuel_type_ids, make_fuel_price
) -> None:
    """Append-only means a clash cannot be resolved by overwriting, so it is a 409 -- and a
    refusal writes nothing."""
    admin = make_user("admin")
    make_fuel_price(fuel_type_ids["PETROL"], "104.21", _FUTURE_AT, entered_by=admin)
    before = _audit_count(engine, table_name="fuel_prices")

    response = await client.post(
        "/api/v1/fuel-prices",
        headers=auth_headers(admin),
        json={
            "fuel_type_id": str(fuel_type_ids["PETROL"]),
            "rate_per_unit": "105.00",
            "effective_from": _FUTURE,
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "PRICE_ALREADY_EFFECTIVE_AT"
    assert _audit_count(engine, table_name="fuel_prices") == before


async def test_entering_a_margin_records_one_insert(
    client, make_user, auth_headers, engine, fuel_type_ids
) -> None:
    """§4.6: margin is stored directly and effective-dated, never derived from a purchase
    price. CBG's ₹2.28/kg is the only one entered at this outlet (§14), so the record of who
    entered it -- and of anybody who later changes it -- is the whole history there is."""
    admin = make_user("admin")

    response = await client.post(
        "/api/v1/fuel-margins",
        headers=auth_headers(admin),
        json={
            "fuel_type_id": str(fuel_type_ids["CBG"]),
            "margin_per_unit": "2.28",
            "effective_from": _FUTURE,
        },
    )
    try:
        assert response.status_code == 201
        rows = _audit_rows(
            engine, table_name="fuel_margins", record_id=response.json()["id"]
        )
        assert len(rows) == 1
        assert rows[0]["action"] == "insert"
        assert rows[0]["new_values"]["margin_per_unit"] == "2.28"
        assert rows[0]["new_values"]["is_backdated"] is False
    finally:
        _clear_effective_dated(engine, "fuel_margins")


async def test_a_backdated_margin_is_recorded_as_backdated(
    client, make_user, auth_headers, engine, fuel_type_ids
) -> None:
    response = await client.post(
        "/api/v1/fuel-margins",
        headers=auth_headers(make_user("admin")),
        json={
            "fuel_type_id": str(fuel_type_ids["CBG"]),
            "margin_per_unit": "2.28",
            "effective_from": _PAST,
        },
    )
    try:
        assert response.status_code == 201
        rows = _audit_rows(
            engine, table_name="fuel_margins", record_id=response.json()["id"]
        )
        assert rows[0]["new_values"]["is_backdated"] is True
    finally:
        _clear_effective_dated(engine, "fuel_margins")


async def test_a_non_admin_entering_a_rate_writes_no_audit_row(
    client, make_user, auth_headers, engine, fuel_type_ids
) -> None:
    """§8 puts prices and margins above the manager floor."""
    before = _audit_count(engine, table_name="fuel_prices")

    response = await client.post(
        "/api/v1/fuel-prices",
        headers=auth_headers(make_user("manager")),
        json={
            "fuel_type_id": str(fuel_type_ids["PETROL"]),
            "rate_per_unit": "104.21",
            "effective_from": _FUTURE,
        },
    )

    assert response.status_code == 403
    assert _audit_count(engine, table_name="fuel_prices") == before


async def test_a_rate_for_a_deactivated_fuel_is_refused_and_writes_no_audit_row(
    client, make_user, auth_headers, engine, make_fuel_type
) -> None:
    """§5.1 retires a fuel with `is_active = false` rather than a DELETE, and a retired fuel
    must not accept new rates.

    Both halves matter. Accepting one would put a live rate on a product the outlet has
    stopped selling, and §6.3 would happily value a stray reading with it. And the refusal
    must leave no trail entry, or the log records a revision that never took effect.

    This is one of Step 0's uncovered branches: it was reachable and untested in both
    append-only routers, and it is exactly the "a refused write records nothing" case the
    verification checklist requires -- so it lands here rather than in a separate sweep.
    """
    admin = make_user("admin")
    retired = make_fuel_type("RETIRED_FUEL", is_active=False)
    before = _audit_count(engine, table_name="fuel_prices")

    response = await client.post(
        "/api/v1/fuel-prices",
        headers=auth_headers(admin),
        json={
            "fuel_type_id": str(retired),
            "rate_per_unit": "104.21",
            "effective_from": _FUTURE,
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "FUEL_TYPE_NOT_FOUND"
    assert _audit_count(engine, table_name="fuel_prices") == before


async def test_a_margin_for_a_deactivated_fuel_is_refused_and_writes_no_audit_row(
    client, make_user, auth_headers, engine, make_fuel_type
) -> None:
    """The `fuel_margins` half of the branch above. The two routers are structurally
    identical (§4.6), and a guard present in one and missing in the other is precisely what
    parallel code invites."""
    admin = make_user("admin")
    retired = make_fuel_type("RETIRED_MARGIN", is_active=False)
    before = _audit_count(engine, table_name="fuel_margins")

    response = await client.post(
        "/api/v1/fuel-margins",
        headers=auth_headers(admin),
        json={
            "fuel_type_id": str(retired),
            "margin_per_unit": "2.28",
            "effective_from": _FUTURE,
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "FUEL_TYPE_NOT_FOUND"
    assert _audit_count(engine, table_name="fuel_margins") == before


# --- expense_categories (§5.1, §6.11) -----------------------------------------
#
# The table §11's original wording did not know about, because Phase 8 had not happened yet.
# `requires_receipt` is §6.11's editable knob, and §6.11 snapshots its answer onto every
# expense at insert *because* it is editable -- so flipping it never rewrites whether history
# complied. That is right, and it left the flip itself invisible: the system recorded what the
# rule was on the day, and not who changed the rule.


async def test_creating_an_expense_category_records_one_insert(
    client, make_user, auth_headers, engine, clean_expense_categories
) -> None:
    """The `TEA` case from §5.1 -- adding a category you already spend on is data entry."""
    admin = make_user("admin")

    response = await client.post(
        "/api/v1/expense-categories",
        headers=auth_headers(admin),
        json={"code": "TEA", "display_name": "Tea", "requires_receipt": False},
    )

    assert response.status_code == 201
    rows = _audit_rows(
        engine, table_name="expense_categories", record_id=response.json()["id"]
    )
    assert len(rows) == 1
    assert rows[0]["action"] == "insert"
    assert rows[0]["old_values"] is None
    assert rows[0]["new_values"]["code"] == "TEA"
    assert rows[0]["new_values"]["requires_receipt"] is False


async def test_flipping_requires_receipt_records_both_sides(
    client, make_user, auth_headers, engine, make_expense_category
) -> None:
    """The assertion this table was added to the phase for.

    §6.11 is explicit that flipping this flag must not retroactively declare historical
    expenses non-compliant, and it does not -- `expenses.receipt_required` is the snapshot
    taken at insert. The consequence is that after the flip, nothing anywhere said who
    loosened or tightened the control. Now the trail does.
    """
    admin = make_user("admin")
    category_id = make_expense_category("AUDITRCPT", requires_receipt=False)

    response = await client.patch(
        f"/api/v1/expense-categories/{category_id}",
        headers=auth_headers(admin),
        json={"requires_receipt": True},
    )

    assert response.status_code == 200
    rows = _audit_rows(
        engine, table_name="expense_categories", record_id=category_id
    )
    assert len(rows) == 1
    assert rows[0]["action"] == "update"
    assert rows[0]["old_values"]["requires_receipt"] is False
    assert rows[0]["new_values"]["requires_receipt"] is True
    assert rows[0]["changed_by"] == admin


async def test_a_refused_category_code_change_writes_no_audit_row(
    client, make_user, auth_headers, engine, make_expense_category
) -> None:
    """§5.1 freezes `code` because changing it would retroactively relabel every expense ever
    filed under it, and §6.7's per-category aggregate would then be grouping on a label that
    means something different. A phantom audit row for that refusal would be a record of the
    single most misleading edit available in this table."""
    admin = make_user("admin")
    category_id = make_expense_category("AUDITFROZEN")

    response = await client.patch(
        f"/api/v1/expense-categories/{category_id}",
        headers=auth_headers(admin),
        json={"code": "SOMETHINGELSE"},
    )

    assert response.status_code == 422
    assert _audit_rows(engine, table_name="expense_categories", record_id=category_id) == []


async def test_a_non_admin_creating_a_category_writes_no_audit_row(
    client, make_user, auth_headers, engine, clean_expense_categories
) -> None:
    """§8: every role may *list* categories to fill a dropdown; only an admin may manage
    them."""
    before = _audit_count(engine, table_name="expense_categories")

    response = await client.post(
        "/api/v1/expense-categories",
        headers=auth_headers(make_user("manager")),
        json={"code": "SNEAKY", "display_name": "Sneaky", "requires_receipt": False},
    )

    assert response.status_code == 403
    assert _audit_count(engine, table_name="expense_categories") == before


# --- credit_customers (§5.1, §6.6) --------------------------------------------
#
# §6.6 insists that an admin *override* of a credit limit is stored on the row AND
# audit-logged, because letting a sale past the limit is a decision somebody answers for.
# Quietly **raising the limit** reaches the same outcome for every future sale, and recorded
# nothing at all until Phase 11.


async def test_creating_a_credit_customer_records_one_insert(
    client, make_user, auth_headers, engine, clean_credit
) -> None:
    admin = make_user("admin")

    response = await client.post(
        "/api/v1/credit-customers",
        headers=auth_headers(admin),
        json={
            "name": "Ramesh Transport",
            "phone": "9876500011",
            "credit_limit": "5000.00",
        },
    )

    assert response.status_code == 201
    rows = _audit_rows(
        engine, table_name="credit_customers", record_id=response.json()["id"]
    )
    assert len(rows) == 1
    assert rows[0]["action"] == "insert"
    # Money as a string, to the paisa (§3 rule 1).
    assert rows[0]["new_values"]["credit_limit"] == "5000.00"
    assert rows[0]["new_values"]["phone"] == "9876500011"


async def test_raising_a_credit_limit_records_both_figures(
    client, make_user, auth_headers, engine, make_credit_customer, clean_credit
) -> None:
    """The single most consequential edit in this table, and the reason it is in this phase.

    §6.6 refuses a sale when `outstanding + amount > credit_limit`. Raising the limit is how
    that refusal stops happening, permanently and for every future sale -- a quieter route to
    the same place as the override §6.6 already demands a stored reason for.
    """
    admin = make_user("admin")
    customer_id = make_credit_customer(name="Ramesh", credit_limit="5000.00")

    response = await client.patch(
        f"/api/v1/credit-customers/{customer_id}",
        headers=auth_headers(admin),
        json={"credit_limit": "50000.00"},
    )

    assert response.status_code == 200
    rows = _audit_rows(engine, table_name="credit_customers", record_id=customer_id)
    assert len(rows) == 1
    assert rows[0]["action"] == "update"
    assert rows[0]["old_values"]["credit_limit"] == "5000.00"
    assert rows[0]["new_values"]["credit_limit"] == "50000.00"
    assert Decimal(rows[0]["new_values"]["credit_limit"]) == Decimal("50000.00")


async def test_removing_a_credit_limit_records_null_not_zero(
    client, make_user, auth_headers, engine, make_credit_customer, clean_credit
) -> None:
    """§6.6 and §14: `credit_limit IS NULL` means **no limit**, and coercing it to `0` would
    refuse every sale to the customers who are trusted most.

    The two are opposite facts, and the trail has to keep them apart -- a `0` here would read
    as somebody having cut a customer off, when what happened is the reverse.
    """
    admin = make_user("admin")
    customer_id = make_credit_customer(name="Trusted", credit_limit="5000.00")

    response = await client.patch(
        f"/api/v1/credit-customers/{customer_id}",
        headers=auth_headers(admin),
        json={"credit_limit": None},
    )

    assert response.status_code == 200
    rows = _audit_rows(engine, table_name="credit_customers", record_id=customer_id)
    assert rows[0]["old_values"]["credit_limit"] == "5000.00"
    assert rows[0]["new_values"]["credit_limit"] is None
    assert rows[0]["new_values"]["credit_limit"] != 0


async def test_deactivating_a_customer_is_an_update(
    client, make_user, auth_headers, engine, make_credit_customer, clean_credit
) -> None:
    """§5.1's asymmetric retirement: a deactivated customer refuses new udhaar but still
    accepts repayments -- you retire somebody precisely to stop the debt growing while they
    pay it off. Squarely an ordinary field change, not a lifecycle move."""
    admin = make_user("admin")
    customer_id = make_credit_customer(name="Leaving")

    response = await client.patch(
        f"/api/v1/credit-customers/{customer_id}",
        headers=auth_headers(admin),
        json={"is_active": False},
    )

    assert response.status_code == 200
    rows = _audit_rows(engine, table_name="credit_customers", record_id=customer_id)
    assert rows[0]["action"] == "update"
    assert rows[0]["old_values"]["is_active"] is True
    assert rows[0]["new_values"]["is_active"] is False


async def test_a_duplicate_customer_phone_writes_no_audit_row(
    client, make_user, auth_headers, engine, make_credit_customer, clean_credit
) -> None:
    """§5.1 makes the phone the natural key: two rows for one person split one real balance
    across two ledgers, and §6.6's limit then never fires against either."""
    admin = make_user("admin")
    make_credit_customer(name="First", phone="9876500022")
    before = _audit_count(engine, table_name="credit_customers")

    response = await client.post(
        "/api/v1/credit-customers",
        headers=auth_headers(admin),
        json={"name": "Second", "phone": "9876500022"},
    )

    assert response.status_code == 409
    assert _audit_count(engine, table_name="credit_customers") == before


# --- outlet_shift_templates (§5.1, §6.3) --------------------------------------
#
# §5.1 is careful that editing a template must not revalue a shift that already traded, and it
# does not -- `started_at` is materialised onto the shift row at creation. But the edit still
# moves the valuation instant for every shift opened *afterwards*: §6.3 prices a whole shift
# at the rate effective at its `started_at`, and §4.1 puts a revision at 06:00 IST.


async def test_creating_a_shift_template_records_one_insert(
    client, make_user, auth_headers, engine
) -> None:
    admin = make_user("admin")

    response = await client.post(
        "/api/v1/shift-templates",
        headers=auth_headers(admin),
        json={
            "sequence": 7,
            "label": "Audit Night",
            "starts_at_local": "22:00:00",
            "ends_at_local": "06:00:00",
        },
    )
    try:
        assert response.status_code == 201
        rows = _audit_rows(
            engine,
            table_name="outlet_shift_templates",
            record_id=response.json()["id"],
        )
        assert len(rows) == 1
        assert rows[0]["action"] == "insert"
        assert rows[0]["new_values"]["sequence"] == 7
        assert rows[0]["new_values"]["starts_at_local"] == "22:00:00"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM outlet_shift_templates WHERE sequence = 7")
            )


async def test_moving_a_templates_start_time_records_both_sides(
    client, make_user, auth_headers, engine
) -> None:
    """The edit that quietly moves §6.3's valuation instant.

    This outlet's seeded template starts at 06:00 IST, which §6.3 notes is the revision moment
    itself -- so one rate covers the whole day with nothing to apportion. Moving the start to
    05:30 puts every future shift on the *previous* day's rate for its entire length, and
    §6.3's mid-shift approximation starts applying where it did not before. Nothing else in
    the schema records that somebody did this.
    """
    admin = make_user("admin")

    created = await client.post(
        "/api/v1/shift-templates",
        headers=auth_headers(admin),
        json={
            "sequence": 8,
            "label": "Audit Day",
            "starts_at_local": "06:00:00",
            "ends_at_local": "22:00:00",
        },
    )
    try:
        assert created.status_code == 201
        template_id = created.json()["id"]

        response = await client.patch(
            f"/api/v1/shift-templates/{template_id}",
            headers=auth_headers(admin),
            json={"starts_at_local": "05:30:00"},
        )

        assert response.status_code == 200
        rows = _audit_rows(
            engine, table_name="outlet_shift_templates", record_id=template_id
        )
        # The insert, then the update.
        assert [row["action"] for row in rows] == ["insert", "update"]
        assert rows[1]["old_values"]["starts_at_local"] == "06:00:00"
        assert rows[1]["new_values"]["starts_at_local"] == "05:30:00"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM outlet_shift_templates WHERE sequence = 8")
            )


async def test_a_zero_length_template_edit_writes_no_audit_row(
    client, make_user, auth_headers, engine
) -> None:
    """The ordering test for this router specifically.

    `update_shift_template` is the one endpoint in the retrofit whose validation runs *after*
    the mutation -- the zero-length check reads the already-updated object. Staging the audit
    row before that check would describe a change the request then refused: §5.3's "log that
    lies", reached by ordering rather than by a stray commit.
    """
    admin = make_user("admin")

    created = await client.post(
        "/api/v1/shift-templates",
        headers=auth_headers(admin),
        json={
            "sequence": 9,
            "label": "Audit Zero",
            "starts_at_local": "06:00:00",
            "ends_at_local": "22:00:00",
        },
    )
    try:
        assert created.status_code == 201
        template_id = created.json()["id"]

        response = await client.patch(
            f"/api/v1/shift-templates/{template_id}",
            headers=auth_headers(admin),
            # Collapses the template onto its own end time.
            json={"starts_at_local": "22:00:00"},
        )

        assert response.status_code == 422
        assert response.json()["code"] == "SHIFT_TEMPLATE_ZERO_LENGTH"

        rows = _audit_rows(
            engine, table_name="outlet_shift_templates", record_id=template_id
        )
        # The create's insert row, and nothing for the refused edit.
        assert [row["action"] for row in rows] == ["insert"]
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM outlet_shift_templates WHERE sequence = 9")
            )


# --- user_profiles and outlet_memberships (§5.1, §13.25-27) --------------------
#
# Phase 14, and the only section in this file where **one endpoint writes two tables**.
# That makes "did the audit row land on the right one" a real question rather than a
# formality: `audit_logs.record_id` points at a row in a *named* table, so "who made Ramesh
# a manager" is only ever answerable under `outlet_memberships`. An implementation that
# recorded both changes against `user_profiles` would pass every other assertion in this
# file and lose that question forever.
#
# The other thing worth pinning here is the negative: a `PATCH` of only `full_name` must
# record **nothing** on `outlet_memberships`. An audit log padded with no-op rows saying a
# role did not change is one nobody reads, which is the failure mode §5.2 names for a flag
# nobody can clear.


def _user_payload(**overrides) -> dict:
    body = {
        "email": "audit-subject@example.com",
        "password": "hunter22-long-enough",
        "full_name": "Ramesh Kumar",
        "role": "attendant",
    }
    body.update(overrides)
    return body


async def test_creating_a_user_records_one_row_per_table(client, make_user, auth_headers, engine) -> None:
    admin = make_user("admin")

    created = (
        await client.post(
            "/api/v1/users", json=_user_payload(phone="+919812345678"),
            headers=auth_headers(admin),
        )
    ).json()

    profile_rows = _audit_rows(
        engine, table_name="user_profiles", record_id=created["id"]
    )
    assert [row["action"] for row in profile_rows] == ["insert"]
    assert profile_rows[0]["old_values"] is None
    assert profile_rows[0]["new_values"]["full_name"] == "Ramesh Kumar"
    # `changed_by` is the ACTING admin, never the row's own id -- the case where the two
    # differ is every case here, which is exactly why it is worth asserting.
    assert profile_rows[0]["changed_by"] == admin

    with engine.connect() as connection:
        membership_id = connection.execute(
            text("SELECT id FROM outlet_memberships WHERE user_id = :id").bindparams(
                id=UUID(created["id"])
            )
        ).scalar_one()

    membership_rows = _audit_rows(
        engine, table_name="outlet_memberships", record_id=membership_id
    )
    assert [row["action"] for row in membership_rows] == ["insert"]
    assert membership_rows[0]["new_values"] == {"role": "attendant", "is_active": True}


async def test_no_password_or_email_reaches_the_audit_log(client, make_user, auth_headers, engine) -> None:
    """§14, and the reason `_profile_snapshot` is a plain column list rather than a
    `model_dump`. Neither value is a column here, so neither can be recorded -- but a
    snapshot built from the payload instead of the row would leak both, and would look
    entirely reasonable in review."""
    admin = make_user("admin")

    created = (
        await client.post(
            "/api/v1/users",
            json=_user_payload(password="a-very-secret-password"),
            headers=auth_headers(admin),
        )
    ).json()

    rows = _audit_rows(engine, table_name="user_profiles", record_id=created["id"])
    serialised = json.dumps(rows[0]["new_values"])

    assert "a-very-secret-password" not in serialised
    assert "audit-subject@example.com" not in serialised
    assert set(rows[0]["new_values"]) == {"full_name", "phone", "is_active"}


async def test_editing_a_name_records_only_the_profile(client, make_user, auth_headers, engine) -> None:
    admin = make_user("admin")
    subject = make_user("attendant", full_name="Old Name")

    with engine.connect() as connection:
        membership_id = connection.execute(
            text("SELECT id FROM outlet_memberships WHERE user_id = :id").bindparams(
                id=subject
            )
        ).scalar_one()

    response = await client.patch(
        f"/api/v1/users/{subject}",
        json={"full_name": "New Name"},
        headers=auth_headers(admin),
    )
    assert response.status_code == 200

    profile_rows = _audit_rows(engine, table_name="user_profiles", record_id=subject)
    assert [row["action"] for row in profile_rows] == ["update"]
    # Both sides populated and genuinely different -- which fails if the snapshot is taken
    # after the mutation instead of before.
    assert profile_rows[0]["old_values"]["full_name"] == "Old Name"
    assert profile_rows[0]["new_values"]["full_name"] == "New Name"

    assert _audit_rows(
        engine, table_name="outlet_memberships", record_id=membership_id
    ) == []


async def test_editing_a_role_records_only_the_membership(client, make_user, auth_headers, engine) -> None:
    admin = make_user("admin")
    subject = make_user("attendant")

    with engine.connect() as connection:
        membership_id = connection.execute(
            text("SELECT id FROM outlet_memberships WHERE user_id = :id").bindparams(
                id=subject
            )
        ).scalar_one()

    await client.patch(
        f"/api/v1/users/{subject}", json={"role": "manager"}, headers=auth_headers(admin)
    )

    rows = _audit_rows(engine, table_name="outlet_memberships", record_id=membership_id)
    assert [row["action"] for row in rows] == ["update"]
    assert rows[0]["old_values"]["role"] == "attendant"
    assert rows[0]["new_values"]["role"] == "manager"

    assert _audit_rows(engine, table_name="user_profiles", record_id=subject) == []


async def test_editing_both_records_both(client, make_user, auth_headers, engine) -> None:
    admin = make_user("admin")
    subject = make_user("attendant", full_name="Old Name")

    with engine.connect() as connection:
        membership_id = connection.execute(
            text("SELECT id FROM outlet_memberships WHERE user_id = :id").bindparams(
                id=subject
            )
        ).scalar_one()

    await client.patch(
        f"/api/v1/users/{subject}",
        json={"full_name": "New Name", "role": "manager"},
        headers=auth_headers(admin),
    )

    assert len(_audit_rows(engine, table_name="user_profiles", record_id=subject)) == 1
    assert (
        len(_audit_rows(engine, table_name="outlet_memberships", record_id=membership_id))
        == 1
    )


async def test_a_deactivation_is_an_update_not_a_status_change(client, make_user, auth_headers, engine) -> None:
    """§14: `status_change` means a *shift* lifecycle move. Because §3 rule 6 forbids hard
    deletes, `is_active` is how every reference table retires a row -- so admitting those
    would make the label mean "a shift moved, or anything at all was deactivated", and
    nobody could query for lifecycle events again."""
    admin = make_user("admin")
    subject = make_user("manager")

    with engine.connect() as connection:
        membership_id = connection.execute(
            text("SELECT id FROM outlet_memberships WHERE user_id = :id").bindparams(
                id=subject
            )
        ).scalar_one()

    await client.patch(
        f"/api/v1/users/{subject}", json={"is_active": False}, headers=auth_headers(admin)
    )

    rows = _audit_rows(engine, table_name="outlet_memberships", record_id=membership_id)
    assert [row["action"] for row in rows] == ["update"]
    assert rows[0]["old_values"]["is_active"] is True
    assert rows[0]["new_values"]["is_active"] is False


async def test_a_refused_last_admin_demotion_writes_no_audit_row(client, make_user, auth_headers, engine) -> None:
    """The assertion that proves the audit row and the change share one transaction."""
    admin = make_user("admin")

    with engine.connect() as connection:
        membership_id = connection.execute(
            text("SELECT id FROM outlet_memberships WHERE user_id = :id").bindparams(
                id=admin
            )
        ).scalar_one()

    response = await client.patch(
        f"/api/v1/users/{admin}", json={"role": "manager"}, headers=auth_headers(admin)
    )

    assert response.status_code == 409
    assert _audit_rows(
        engine, table_name="outlet_memberships", record_id=membership_id
    ) == []


async def test_a_refused_immutable_field_writes_no_audit_row(client, make_user, auth_headers, engine) -> None:
    admin = make_user("admin")
    subject = make_user("attendant")

    response = await client.patch(
        f"/api/v1/users/{subject}",
        json={"email": "new@example.com"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 422
    assert _audit_rows(engine, table_name="user_profiles", record_id=subject) == []


async def test_a_non_admin_create_writes_no_audit_row(client, make_user, auth_headers, engine) -> None:
    manager = make_user("manager")
    before = _audit_count(engine, table_name="user_profiles")

    response = await client.post(
        "/api/v1/users", json=_user_payload(), headers=auth_headers(manager)
    )

    assert response.status_code == 403
    assert _audit_count(engine, table_name="user_profiles") == before


async def test_a_failed_create_leaves_no_audit_row_behind(make_user, auth_headers, engine) -> None:
    """§13.25's rollback, from the audit log's side.

    `audit.record` only stages the row -- the caller commits -- so a rolled-back create must
    leave nothing. This is the assertion that would fail if somebody "helpfully" committed
    the audit row separately, which §14 forbids by name: a separately committed audit row
    can describe a change that was then rolled back, and that is worse than no log at all.
    """
    from app.api.deps import get_auth, get_storage
    from app.main import create_app
    from app.services.storage import LocalStorage

    admin = make_user("admin")
    occupied = make_user("attendant")

    class _CollidingAuth:
        def create_user(self, *, email: str, password: str) -> UUID:
            return occupied

        def delete_user(self, *, user_id: UUID) -> None:
            pass

    app = create_app()
    app.dependency_overrides[get_auth] = lambda: _CollidingAuth()
    app.dependency_overrides[get_storage] = lambda: LocalStorage(root="/tmp/hisahab-test")

    before = _audit_count(engine, table_name="user_profiles")

    async with _AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as local:
        await local.post(
            "/api/v1/users", json=_user_payload(), headers=auth_headers(admin)
        )

    assert _audit_count(engine, table_name="user_profiles") == before
