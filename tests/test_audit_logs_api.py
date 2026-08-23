"""Reading the audit trail over HTTP (CLAUDE.md §5.3, §8, §9).

The table has been written to since Phase 4 and could not be read back until now. What this
file defends, beyond the obvious filters:

* **admin only**, because it is the control record rather than a report, and because
  `credit_customers` snapshots carry `phone` and `credit_limit` -- fields §8 and §9 restrict
  elsewhere, which a manager floor here would route around;
* **outlet scoping**, asserted by inserting a row at another outlet rather than by inferring
  it from an empty page;
* **the keyset**, including the case §9 forbids `OFFSET` for -- a row arriving mid-walk. That
  is not hypothetical on this table: it gains a row on every financial write in the system,
  so it is paged underneath far more often than any other list endpoint here.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text


# --- helpers ------------------------------------------------------------------


def _insert_audit_row(
    engine,
    *,
    changed_by,
    table_name: str = "fuel_types",
    record_id=None,
    action: str = "insert",
    outlet_id=None,
    new_values: str = '{"code": "TEST"}',
) -> UUID:
    """Write one audit row directly.

    Bypassing the API on purpose: this file is about the *read* path, and driving it through
    real writes would couple every assertion here to whichever endpoint happened to produce
    the row. It also allows the other-outlet row, which no endpoint would ever create in a
    single-outlet V1.
    """
    from app.core.config import get_settings

    row_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO audit_logs (id, outlet_id, table_name, record_id, action, "
                "changed_by, new_values, request_id) VALUES (:id, :outlet, :t, :rec, "
                "CAST(:action AS audit_log_action), :who, CAST(:new AS jsonb), 'test')"
            ).bindparams(
                id=row_id,
                outlet=outlet_id or get_settings().DEFAULT_OUTLET_ID,
                t=table_name,
                rec=record_id or uuid4(),
                action=action,
                who=changed_by,
                new=new_values,
            )
        )
    return row_id


@pytest.fixture
def clean_audit_logs(engine):
    """Sweep audit rows this module wrote.

    The append-only trigger has to come off, which is the documented escape hatch every
    fixture touching this table already uses (`make_user`'s teardown does the same). Runs
    *after* the test so a leaked row cannot make a later page-walk assertion count wrong.
    """
    yield
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
        )
        connection.execute(
            text("DELETE FROM audit_logs WHERE request_id = 'test'")
        )
        connection.execute(
            text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
        )


# --- permissions (§8) ---------------------------------------------------------


async def test_an_admin_can_read_the_trail(
    client, make_user, auth_headers, engine, clean_audit_logs
) -> None:
    admin = make_user("admin")
    _insert_audit_row(engine, changed_by=admin)

    response = await client.get("/api/v1/audit-logs", headers=auth_headers(admin))

    assert response.status_code == 200
    assert len(response.json()["items"]) >= 1


async def test_a_manager_cannot_read_the_trail(
    client, make_user, auth_headers
) -> None:
    """§8 puts this above the manager floor, and the reason is not squeamishness.

    Every other manager-floor read is a report. This is the control record, and partly the
    record of what managers themselves did. It is also a leak boundary: a `credit_customers`
    snapshot carries `phone` and `credit_limit`, which §8 keeps out of the attendant-facing
    customer list and §9 restricts to the customer detail route. Allowing a manager here
    would expose those columns through a different endpoint.
    """
    response = await client.get(
        "/api/v1/audit-logs", headers=auth_headers(make_user("manager"))
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


async def test_an_attendant_cannot_read_the_trail(
    client, make_user, auth_headers
) -> None:
    response = await client.get(
        "/api/v1/audit-logs", headers=auth_headers(make_user("attendant"))
    )

    assert response.status_code == 403


async def test_the_trail_is_scoped_to_the_callers_outlet(
    client, make_user, auth_headers, engine, clean_audit_logs
) -> None:
    """Asserted by planting a row at another outlet, not by reading an empty page.

    An empty page proves nothing -- it is also what a broken query returns. This creates a
    row that a correct implementation must exclude and an incorrect one would return.
    """
    admin = make_user("admin")
    mine = _insert_audit_row(engine, changed_by=admin, table_name="scoping_mine")
    other_outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outlets (id, name, is_active) "
                "VALUES (:id, 'Other Outlet', true)"
            ).bindparams(id=other_outlet)
        )
    theirs = _insert_audit_row(
        engine,
        changed_by=admin,
        table_name="scoping_theirs",
        outlet_id=other_outlet,
    )

    try:
        response = await client.get(
            "/api/v1/audit-logs", headers=auth_headers(admin)
        )

        assert response.status_code == 200
        returned = {row["id"] for row in response.json()["items"]}
        assert str(mine) in returned
        assert str(theirs) not in returned
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
            )
            connection.execute(
                text("DELETE FROM audit_logs WHERE outlet_id = :id").bindparams(
                    id=other_outlet
                )
            )
            connection.execute(
                text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
            )
            connection.execute(
                text("DELETE FROM outlets WHERE id = :id").bindparams(id=other_outlet)
            )


# --- filters ------------------------------------------------------------------


async def test_the_trail_can_be_filtered_by_table(
    client, make_user, auth_headers, engine, clean_audit_logs
) -> None:
    admin = make_user("admin")
    _insert_audit_row(engine, changed_by=admin, table_name="filter_wanted")
    _insert_audit_row(engine, changed_by=admin, table_name="filter_unwanted")

    response = await client.get(
        "/api/v1/audit-logs",
        headers=auth_headers(admin),
        params={"table_name": "filter_wanted"},
    )

    assert response.status_code == 200
    tables = {row["table_name"] for row in response.json()["items"]}
    assert tables == {"filter_wanted"}


async def test_the_trail_can_be_filtered_to_one_record(
    client, make_user, auth_headers, engine, clean_audit_logs
) -> None:
    """"What happened to this row" is the question the trail is actually asked."""
    admin = make_user("admin")
    record = uuid4()
    _insert_audit_row(
        engine, changed_by=admin, table_name="one_record", record_id=record
    )
    _insert_audit_row(
        engine, changed_by=admin, table_name="one_record", action="update",
        record_id=record,
    )
    _insert_audit_row(engine, changed_by=admin, table_name="one_record")

    response = await client.get(
        "/api/v1/audit-logs",
        headers=auth_headers(admin),
        params={"record_id": str(record)},
    )

    assert response.status_code == 200
    assert len(response.json()["items"]) == 2
    assert {row["record_id"] for row in response.json()["items"]} == {str(record)}


async def test_filters_combine(
    client, make_user, auth_headers, engine, clean_audit_logs
) -> None:
    """Two filters must intersect, not merely apply the last one seen."""
    admin = make_user("admin")
    _insert_audit_row(engine, changed_by=admin, table_name="combo", action="insert")
    _insert_audit_row(engine, changed_by=admin, table_name="combo", action="update")
    _insert_audit_row(engine, changed_by=admin, table_name="other", action="update")

    response = await client.get(
        "/api/v1/audit-logs",
        headers=auth_headers(admin),
        params={"table_name": "combo", "action": "update"},
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["table_name"] == "combo"
    assert items[0]["action"] == "update"


async def test_the_trail_can_be_filtered_by_who_changed_it(
    client, make_user, auth_headers, engine, clean_audit_logs
) -> None:
    admin = make_user("admin")
    other_admin = make_user("admin")
    _insert_audit_row(engine, changed_by=admin, table_name="whodunnit")
    _insert_audit_row(engine, changed_by=other_admin, table_name="whodunnit")

    response = await client.get(
        "/api/v1/audit-logs",
        headers=auth_headers(admin),
        params={"changed_by": str(other_admin)},
    )

    assert response.status_code == 200
    assert {row["changed_by"] for row in response.json()["items"]} == {
        str(other_admin)
    }


async def test_an_unknown_record_id_is_an_empty_page_not_a_404(
    client, make_user, auth_headers
) -> None:
    """§5.3: `record_id` is deliberately not a foreign key -- it points at rows in many
    tables and has to survive its target being restructured.

    So nothing can distinguish "no such row" from "that row was never changed", and a 404
    would claim knowledge this table does not have. Recorded as a decision rather than left
    to look like an oversight the first time somebody queries a fresh id.
    """
    response = await client.get(
        "/api/v1/audit-logs",
        headers=auth_headers(make_user("admin")),
        params={"record_id": str(uuid4())},
    )

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["next_cursor"] is None


async def test_an_invalid_action_is_refused_not_silently_empty(
    client, make_user, auth_headers
) -> None:
    """The silent version is the dangerous one.

    `FuelTypeUpdate` makes the same argument about refusing an immutable field rather than
    ignoring it: an admin who filtered on a typo and got zero rows back would reasonably
    conclude that nothing had happened.
    """
    response = await client.get(
        "/api/v1/audit-logs",
        headers=auth_headers(make_user("admin")),
        params={"action": "deleted"},
    )

    assert response.status_code == 422


# --- pagination (§9) ----------------------------------------------------------


async def test_rows_come_back_newest_first(
    client, make_user, auth_headers, engine, clean_audit_logs
) -> None:
    admin = make_user("admin")
    for _ in range(3):
        _insert_audit_row(engine, changed_by=admin, table_name="ordering")

    response = await client.get(
        "/api/v1/audit-logs",
        headers=auth_headers(admin),
        params={"table_name": "ordering"},
    )

    assert response.status_code == 200
    stamps = [row["changed_at"] for row in response.json()["items"]]
    assert stamps == sorted(stamps, reverse=True)


async def test_a_page_walk_neither_skips_nor_repeats(
    client, make_user, auth_headers, engine, clean_audit_logs
) -> None:
    admin = make_user("admin")
    for _ in range(5):
        _insert_audit_row(engine, changed_by=admin, table_name="walk")

    seen: list[str] = []
    cursor = None
    for _ in range(5):
        params = {"table_name": "walk", "limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get(
            "/api/v1/audit-logs", headers=auth_headers(admin), params=params
        )
        assert response.status_code == 200
        seen.extend(row["id"] for row in response.json()["items"])
        cursor = response.json()["next_cursor"]
        if cursor is None:
            break

    assert cursor is None
    assert len(seen) == 5
    assert len(set(seen)) == 5


async def test_a_row_arriving_mid_walk_does_not_disturb_the_page_walk(
    client, make_user, auth_headers, engine, clean_audit_logs
) -> None:
    """The exact failure §9 forbids `OFFSET` for, and the only test that proves the keyset.

    With an offset, a row inserted between two page requests shifts every later row down by
    one, so the reader sees a duplicate and misses one entirely. A keyset asks "what sorts
    after *this exact row*", which is stable under a concurrent insert.

    This table is where that matters most: it gains a row on every financial write in the
    system, so a page walk during trading is genuinely being paged underneath.
    """
    admin = make_user("admin")
    for _ in range(4):
        _insert_audit_row(engine, changed_by=admin, table_name="concurrent")

    first = await client.get(
        "/api/v1/audit-logs",
        headers=auth_headers(admin),
        params={"table_name": "concurrent", "limit": 2},
    )
    assert first.status_code == 200
    page_one = [row["id"] for row in first.json()["items"]]
    cursor = first.json()["next_cursor"]
    assert cursor is not None

    # A change lands while the reader is between pages. It sorts newest-first, so it belongs
    # on a page the reader has already passed -- and must not push anything into view twice.
    _insert_audit_row(engine, changed_by=admin, table_name="concurrent")

    second = await client.get(
        "/api/v1/audit-logs",
        headers=auth_headers(admin),
        params={"table_name": "concurrent", "limit": 2, "cursor": cursor},
    )
    assert second.status_code == 200
    page_two = [row["id"] for row in second.json()["items"]]

    assert set(page_one) & set(page_two) == set()
    assert len(page_one + page_two) == len(set(page_one + page_two))


async def test_a_malformed_cursor_is_refused(
    client, make_user, auth_headers
) -> None:
    """400, not 422: a cursor is a token this API issued, not a field the caller can correct.

    And never a silent fall back to the first page -- restarting a walk is how a reader ends
    up seeing the same rows twice and believing they are different records.
    """
    response = await client.get(
        "/api/v1/audit-logs",
        headers=auth_headers(make_user("admin")),
        params={"cursor": "not-a-real-cursor"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_CURSOR"


async def test_a_limit_above_the_ceiling_is_refused(
    client, make_user, auth_headers
) -> None:
    response = await client.get(
        "/api/v1/audit-logs",
        headers=auth_headers(make_user("admin")),
        params={"limit": 201},
    )

    assert response.status_code == 422


async def test_the_ceiling_itself_is_accepted(
    client, make_user, auth_headers
) -> None:
    """The boundary convention this codebase uses everywhere: at the limit is fine, past it
    is not."""
    response = await client.get(
        "/api/v1/audit-logs",
        headers=auth_headers(make_user("admin")),
        params={"limit": 200},
    )

    assert response.status_code == 200


# --- the shape of what comes back ---------------------------------------------


async def test_money_in_a_snapshot_comes_back_as_a_string(
    client, make_user, auth_headers, engine, make_credit_customer, clean_credit
) -> None:
    """§3 rule 1 end to end, through a real write and out through the read endpoint.

    A float here would be wrong twice over: the forbidden type, and it drops the scale, so
    `Decimal("5000.00")` could no longer show two decimal places in the one record whose
    entire job is to say exactly what a value was.
    """
    from decimal import Decimal

    admin = make_user("admin")
    customer_id = make_credit_customer(name="Ledger", credit_limit="5000.00")

    patched = await client.patch(
        f"/api/v1/credit-customers/{customer_id}",
        headers=auth_headers(admin),
        json={"credit_limit": "12345.67"},
    )
    assert patched.status_code == 200

    response = await client.get(
        "/api/v1/audit-logs",
        headers=auth_headers(admin),
        params={"record_id": str(customer_id)},
    )

    assert response.status_code == 200
    row = response.json()["items"][0]
    assert row["old_values"]["credit_limit"] == "5000.00"
    assert row["new_values"]["credit_limit"] == "12345.67"
    assert isinstance(row["new_values"]["credit_limit"], str)
    assert Decimal(row["new_values"]["credit_limit"]) == Decimal("12345.67")


async def test_the_endpoint_exposes_no_way_to_write() -> None:
    """`audit_logs` is append-only (§5.3), enforced by a trigger *and* by there being no
    route. This pins the second half: a write route here would be a way to edit the record of
    what everybody else did.

    Enumerated through `app.openapi()`, **not** `app.routes`. tests/test_routes.py documents
    why: FastAPI does not flatten included routers into `app.routes`, so a check written that
    way sees only `/openapi.json` and `/docs` and passes vacuously -- which is exactly how the
    first draft of this test passed while proving nothing. The OpenAPI document carries the
    real, fully-prefixed URL of every route, which is also what a client sees.

    Asserted against the whole API surface rather than by reading this module, so a write
    route added under this path from some *other* file fails here too.
    """
    from app.main import create_app

    paths = create_app().openapi()["paths"]

    assert "/api/v1/audit-logs" in paths
    assert set(paths["/api/v1/audit-logs"]) == {"get"}
    # And no parameterised sibling appeared -- see the module docstring for why there is
    # deliberately no /audit-logs/{id}.
    assert not [p for p in paths if p.startswith("/api/v1/audit-logs/")]
