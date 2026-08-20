"""The audit log (CLAUDE.md §5.3, §5.2).

§5.3 is explicit that `created_by` / `updated_at` are change *tracking*, not an audit
trail: they say who last touched a row, never what it was before or how many times it
changed. These tests are about the difference.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.usefixtures("clean_shifts")


def _rows(engine: Engine, shift_id: str) -> list[dict]:
    """Audit rows for one shift, oldest first.

    `record_id` is a real uuid column, and an id read back out of a JSON response is a
    string -- psycopg will not compare the two, so it is converted rather than cast in SQL.
    """
    with engine.connect() as connection:
        return [
            dict(row._mapping)
            for row in connection.execute(
                text(
                    "SELECT action, changed_by, old_values, new_values, request_id "
                    "FROM audit_logs WHERE table_name = 'shifts' AND record_id = :id "
                    "ORDER BY changed_at"
                ).bindparams(id=UUID(shift_id))
            )
        ]


async def test_the_whole_lifecycle_is_recorded_in_order(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """§5.2: status transitions are the thing that must be traceable."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    admin = make_user("admin")

    opened = await client.post(
        "/api/v1/shifts",
        json={"business_date": "2026-05-01"},
        headers=auth_headers(attendant),
    )
    shift_id = opened.json()["id"]
    await client.patch(
        f"/api/v1/shifts/{shift_id}/close", json={}, headers=auth_headers(manager)
    )
    await client.patch(
        f"/api/v1/shifts/{shift_id}/reopen",
        json={"reason": "closing reading was mistyped"},
        headers=auth_headers(admin),
    )
    await client.patch(
        f"/api/v1/shifts/{shift_id}/close", json={}, headers=auth_headers(manager)
    )
    await client.patch(f"/api/v1/shifts/{shift_id}/lock", headers=auth_headers(admin))

    rows = _rows(engine, shift_id)

    assert [row["action"] for row in rows] == [
        "insert",
        "status_change",
        "status_change",
        "status_change",
        "status_change",
    ]
    assert [row["changed_by"] for row in rows] == [
        attendant,
        manager,
        admin,
        manager,
        admin,
    ]


async def test_a_reopen_records_the_reason_and_both_sides_of_the_change(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """The reason lives here rather than on the shift row on purpose: a column holds only
    the most recent one, and a shift reopened three times is exactly the case somebody
    will need to reconstruct."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    admin = make_user("admin")

    shift_id = (
        await client.post(
            "/api/v1/shifts",
            json={"business_date": "2026-05-02"},
            headers=auth_headers(attendant),
        )
    ).json()["id"]
    await client.patch(
        f"/api/v1/shifts/{shift_id}/close", json={}, headers=auth_headers(manager)
    )
    await client.patch(
        f"/api/v1/shifts/{shift_id}/reopen",
        json={"reason": "manager closed before the night collections were in"},
        headers=auth_headers(admin),
    )

    reopen = _rows(engine, shift_id)[-1]

    assert reopen["old_values"]["status"] == "closed"
    assert reopen["new_values"]["status"] == "open"
    assert (
        reopen["new_values"]["reason"]
        == "manager closed before the night collections were in"
    )
    # The close stamps are cleared, and the audit row shows they used to be set -- which is
    # the whole difference between an audit trail and a `closed_by` column.
    assert reopen["old_values"]["closed_by"] is not None
    assert reopen["new_values"]["closed_by"] is None


async def test_a_refused_transition_writes_no_audit_row(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """The audit row and the change it describes share one transaction (see
    app/services/audit.py). A log that records changes which never happened is worse than
    no log: it is a log that lies."""
    attendant = make_user("attendant")
    admin = make_user("admin")

    shift_id = (
        await client.post(
            "/api/v1/shifts",
            json={"business_date": "2026-05-03"},
            headers=auth_headers(attendant),
        )
    ).json()["id"]

    # Refused: an open shift cannot be locked (§6.8).
    refused = await client.patch(
        f"/api/v1/shifts/{shift_id}/lock", headers=auth_headers(admin)
    )

    assert refused.status_code == 409
    assert [row["action"] for row in _rows(engine, shift_id)] == ["insert"]


async def test_the_request_id_ties_the_audit_row_to_the_response(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """§5.3: correlate with application logs. Honours an inbound X-Request-ID, which is why
    the column is text rather than uuid."""
    attendant = make_user("attendant")

    response = await client.post(
        "/api/v1/shifts",
        json={"business_date": "2026-05-04"},
        headers=auth_headers(attendant) | {"X-Request-ID": "trace-me-12345"},
    )

    row = _rows(engine, response.json()["id"])[0]

    assert response.headers["X-Request-ID"] == "trace-me-12345"
    assert row["request_id"] == "trace-me-12345"


def test_decimal_values_survive_serialisation(engine: Engine, make_user) -> None:
    """The trap Phase 3 already fell into once.

    `json.dumps` refuses `Decimal`, and money fields are precisely what most needs
    auditing. Without `jsonable_encoder` in app/services/audit.py, the first Phase 6
    collection or Phase 7 expense written through this helper would raise *inside* the
    audit write and take the money transaction down with it.
    """
    from app.core.audit import AuditAction
    from app.db.session import SessionLocal
    from app.services import audit

    actor = make_user("admin")
    record_id = uuid4()

    with SessionLocal() as session:
        audit.record(
            session,
            outlet_id=UUID("00000000-0000-0000-0000-000000000001"),
            table_name="collections",
            record_id=record_id,
            action=AuditAction.insert,
            changed_by=actor,
            new_values={"amount": Decimal("5000.00"), "mode": "cash"},
        )
        session.commit()

    with engine.begin() as connection:
        stored = connection.execute(
            text(
                "SELECT new_values FROM audit_logs WHERE record_id = :id"
            ).bindparams(id=record_id)
        ).scalar_one()
        connection.execute(
            text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
        )
        connection.execute(
            text("DELETE FROM audit_logs WHERE record_id = :id").bindparams(id=record_id)
        )
        connection.execute(
            text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
        )

    # A string, not a JSON number. §3 rule 1 forbids float end-to-end, and a float would
    # also drop the scale -- 5000.00 would come back as 5000.0, so the audit row could no
    # longer show what was actually stored. This round-trips exactly:
    assert stored == {"amount": "5000.00", "mode": "cash"}
    assert Decimal(stored["amount"]) == Decimal("5000.00")


def test_sub_rupee_amounts_are_not_rounded_through_a_float(
    engine: Engine, make_user
) -> None:
    """The case that makes the string encoding more than pedantry.

    0.1 is not exactly representable in binary floating point -- the exact problem §3 rule 1
    opens with. Through a float encoder these paise values come back as 0.1 and 20.15 with
    the scale gone; as strings they survive byte for byte.
    """
    from app.core.audit import AuditAction
    from app.db.session import SessionLocal
    from app.services import audit

    actor = make_user("admin")
    record_id = uuid4()

    with SessionLocal() as session:
        audit.record(
            session,
            outlet_id=UUID("00000000-0000-0000-0000-000000000001"),
            table_name="expenses",
            record_id=record_id,
            action=AuditAction.insert,
            changed_by=actor,
            old_values={"amount": Decimal("0.10")},
            new_values={"amount": Decimal("20.15"), "quantity": Decimal("12.345")},
        )
        session.commit()

    with engine.begin() as connection:
        old_values, new_values = connection.execute(
            text(
                "SELECT old_values, new_values FROM audit_logs WHERE record_id = :id"
            ).bindparams(id=record_id)
        ).one()
        connection.execute(
            text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
        )
        connection.execute(
            text("DELETE FROM audit_logs WHERE record_id = :id").bindparams(id=record_id)
        )
        connection.execute(
            text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
        )

    assert old_values == {"amount": "0.10"}
    # NUMERIC(10,3) quantities keep their three places too (§3 rule 2).
    assert new_values == {"amount": "20.15", "quantity": "12.345"}


def test_audit_rows_outside_a_request_still_record_something(
    engine: Engine, make_user
) -> None:
    """app/services/__init__.py: services must be callable without FastAPI.

    Outside a request the ContextVar reads its default, so a management command writing an
    audit row gets "-" rather than a crash.
    """
    from app.core.audit import AuditAction
    from app.db.session import SessionLocal
    from app.services import audit

    actor = make_user("admin")
    record_id = uuid4()

    with SessionLocal() as session:
        audit.record(
            session,
            outlet_id=UUID("00000000-0000-0000-0000-000000000001"),
            table_name="shifts",
            record_id=record_id,
            action=AuditAction.status_change,
            changed_by=actor,
        )
        session.commit()

    with engine.begin() as connection:
        request_id = connection.execute(
            text(
                "SELECT request_id FROM audit_logs WHERE record_id = :id"
            ).bindparams(id=record_id)
        ).scalar_one()
        connection.execute(
            text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
        )
        connection.execute(
            text("DELETE FROM audit_logs WHERE record_id = :id").bindparams(id=record_id)
        )
        connection.execute(
            text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
        )

    assert request_id == "-"
