"""Voiding an empty shift (CLAUDE.md §3 rule 6, §6.8, Phase 27).

On 5 October a manager opened that day's shift before 2 October had been entered, and every
lifecycle rule then refused the way back: 2 October could not be opened (SHIFT_ALREADY_OPEN,
then SHIFT_OUT_OF_SEQUENCE) and 5 October could not be closed (MISSING_NOZZLE_READINGS). Each
refusal was correct. What was missing was a way to remove a shift that holds nothing.

The tests below are the §10 list for that route. Two of them carry the design:

* `test_a_table_added_later_blocks_the_void_without_touching_the_route` proves "empty" is
  read from `pg_constraint` rather than from a list someone has to remember to extend.
* `test_a_row_that_slips_past_the_check_is_refused_by_the_foreign_key` proves the second
  lock: every foreign key to `shifts` is NO ACTION, so a race loses at the database.

Every test drives the real HTTP API with a real token, per tests/test_permissions.py.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.usefixtures("clean_shifts")

ON = "2026-03-05"
REASON = "Opened 5 March by mistake before 2 March was entered"

# Every table that held a foreign key to `shifts` when Phase 27 was written. The route does
# not read this -- it asks pg_constraint -- and the cascade test below uses it only to prove
# its own catalogue query is not vacuous.
_KNOWN_CHILDREN = {
    "nozzle_readings",
    "collections",
    "expenses",
    "credit_sales",
    "credit_repayments",
    "non_fuel_sales",
    "bank_deposits",
    "salesman_shortfalls",
    "salesman_shortfall_settlements",
}


async def _open(
    client: AsyncClient, headers: dict[str, str], business_date: str = ON
) -> str:
    response = await client.post(
        "/api/v1/shifts", json={"business_date": business_date}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _void(
    client: AsyncClient, shift_id: object, headers: dict[str, str], reason: str = REASON
):
    return await client.patch(
        f"/api/v1/shifts/{shift_id}/void", json={"reason": reason}, headers=headers
    )


def _shift_exists(engine: Engine, shift_id: object) -> bool:
    with engine.connect() as connection:
        found = connection.execute(
            text("SELECT 1 FROM shifts WHERE id = :id").bindparams(
                id=UUID(str(shift_id))
            )
        ).first()
    return found is not None


def _void_audit_rows(engine: Engine, shift_id: object) -> list[tuple]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT changed_by, old_values, new_values FROM audit_logs "
                    "WHERE table_name = 'shifts' AND record_id = :id "
                    "AND action = 'status_change'"
                ).bindparams(id=UUID(str(shift_id)))
            )
        )


# --- the happy path ------------------------------------------------------------


async def test_an_admin_voids_an_empty_open_shift(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers, engine: Engine
) -> None:
    """The row is gone, and exactly one status_change audit row is the whole record of it."""
    manager = make_user("manager")
    admin = make_user("admin")
    shift_id = await _open(client, auth_headers(manager))

    response = await _void(client, shift_id, auth_headers(admin))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == shift_id
    assert body["business_date"] == ON
    assert body["sequence"] == 1
    assert not _shift_exists(engine, shift_id)

    rows = _void_audit_rows(engine, shift_id)
    assert len(rows) == 1
    changed_by, old_values, new_values = rows[0]
    # The admin who pressed the button, not the manager who opened the shift.
    assert changed_by == admin
    # The audit row alone reconstructs the shift: it is all that is left of it (§13.45).
    assert old_values["status"] == "open"
    assert old_values["business_date"] == ON
    assert old_values["sequence"] == 1
    assert old_values["attendant_id"] == str(manager)
    assert old_values["started_at"] is not None
    assert new_values == {"status": "voided", "reason": REASON}


async def test_voiding_the_tip_lets_an_earlier_date_be_opened(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """The 2 October case, end to end, on March dates so it never depends on today.

    The earlier date opens as sequence 1 and takes the template's 06:00 IST start -- which is
    the reason the row is deleted rather than marked void (§6.8): a kept row would have pushed
    the real day to sequence 2, past the only template, onto the wrong start time.
    """
    manager = make_user("manager")
    admin = make_user("admin")
    first = await _open(client, auth_headers(manager), "2026-03-01")
    await client.patch(
        f"/api/v1/shifts/{first}/close", json={}, headers=auth_headers(manager)
    )
    mistake = await _open(client, auth_headers(manager), ON)

    blocked = await client.post(
        "/api/v1/shifts",
        json={"business_date": "2026-03-02"},
        headers=auth_headers(manager),
    )
    assert blocked.json()["code"] == "SHIFT_ALREADY_OPEN"

    assert (await _void(client, mistake, auth_headers(admin))).status_code == 200

    opened = await client.post(
        "/api/v1/shifts",
        json={"business_date": "2026-03-02"},
        headers=auth_headers(manager),
    )
    assert opened.status_code == 201, opened.text
    assert opened.json()["sequence"] == 1
    assert opened.json()["started_at"] == "2026-03-02T00:30:00Z"


# --- who, and with what --------------------------------------------------------


async def test_a_manager_cannot_void_a_shift(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers, engine: Engine
) -> None:
    manager = make_user("manager")
    shift_id = await _open(client, auth_headers(manager))

    response = await _void(client, shift_id, auth_headers(manager))

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"
    assert _shift_exists(engine, shift_id)


async def test_an_attendant_cannot_void_their_own_shift(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers, engine: Engine
) -> None:
    attendant = make_user("attendant")
    shift_id = await _open(client, auth_headers(attendant))

    response = await _void(client, shift_id, auth_headers(attendant))

    assert response.status_code == 403
    assert _shift_exists(engine, shift_id)


async def test_a_reason_is_mandatory(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers, engine: Engine
) -> None:
    """§5.2: a lifecycle move with no stated reason defeats the audit row that records it."""
    admin = make_user("admin")
    shift_id = await _open(client, auth_headers(admin))

    blank = await _void(client, shift_id, auth_headers(admin), reason="")
    # Stripped before it is measured, so spaces are not a reason (collections.py's rule).
    spaces = await _void(client, shift_id, auth_headers(admin), reason="      ")
    missing = await client.patch(
        f"/api/v1/shifts/{shift_id}/void", json={}, headers=auth_headers(admin)
    )

    assert blank.status_code == 422
    assert spaces.status_code == 422
    assert missing.status_code == 422
    assert _shift_exists(engine, shift_id)


# --- which shifts ---------------------------------------------------------------


async def test_a_closed_shift_cannot_be_voided(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers, engine: Engine
) -> None:
    """A close is a manager's statement about a day; reopen is the audited way back."""
    manager = make_user("manager")
    admin = make_user("admin")
    shift_id = await _open(client, auth_headers(manager))
    await client.patch(
        f"/api/v1/shifts/{shift_id}/close", json={}, headers=auth_headers(manager)
    )

    response = await _void(client, shift_id, auth_headers(admin))

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_NOT_OPEN"
    assert _shift_exists(engine, shift_id)


async def test_a_locked_shift_cannot_be_voided(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers, engine: Engine
) -> None:
    manager = make_user("manager")
    admin = make_user("admin")
    shift_id = await _open(client, auth_headers(manager))
    await client.patch(
        f"/api/v1/shifts/{shift_id}/close", json={}, headers=auth_headers(manager)
    )
    await client.patch(f"/api/v1/shifts/{shift_id}/lock", headers=auth_headers(admin))

    response = await _void(client, shift_id, auth_headers(admin))

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_LOCKED"
    assert _shift_exists(engine, shift_id)


# --- a shift that holds something ------------------------------------------------
#
# One test per kind of row rather than a loop: a loop that silently skips a case is the
# failure mode §10 names for the audit tests, and it applies here for the same reason.
# These use `make_shift` rather than the API, because its teardown clears every child table
# before the shift -- and before `make_nozzle` removes the nozzle a reading points at.


def _assert_refused_as_not_empty(
    response, engine: Engine, shift_id: UUID, table: str
) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "SHIFT_NOT_EMPTY"
    assert table in response.json()["detail"]
    assert _shift_exists(engine, shift_id)
    # Refused means refused: no audit row describing a void that did not happen.
    assert _void_audit_rows(engine, shift_id) == []


async def test_a_shift_with_a_reading_cannot_be_voided(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    fuel_type_ids: dict[str, UUID],
    make_nozzle: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_reading: Callable[..., UUID],
) -> None:
    admin = make_user("admin")
    nozzle_id = make_nozzle(fuel_type_ids["PETROL"])
    shift_id = make_shift(admin)
    make_reading(shift_id, nozzle_id, closing_reading=None)

    response = await _void(client, shift_id, auth_headers(admin))

    _assert_refused_as_not_empty(response, engine, shift_id, "nozzle_readings")


async def test_a_shift_with_a_collection_cannot_be_voided(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
) -> None:
    admin = make_user("admin")
    shift_id = make_shift(admin)
    make_collection(shift_id, amount="0.00")

    response = await _void(client, shift_id, auth_headers(admin))

    _assert_refused_as_not_empty(response, engine, shift_id, "collections")


async def test_a_shift_with_an_expense_cannot_be_voided(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
) -> None:
    admin = make_user("admin")
    shift_id = make_shift(admin)
    make_expense(shift_id)

    response = await _void(client, shift_id, auth_headers(admin))

    _assert_refused_as_not_empty(response, engine, shift_id, "expenses")


async def test_a_shift_with_a_credit_repayment_cannot_be_voided(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
) -> None:
    """A cash repayment is the one money row with a nullable shift_id; with one, it counts."""
    admin = make_user("admin")
    shift_id = make_shift(admin)
    make_credit_repayment(shift_id, make_credit_customer())

    response = await _void(client, shift_id, auth_headers(admin))

    _assert_refused_as_not_empty(response, engine, shift_id, "credit_repayments")


# --- the catalogue, not a list ---------------------------------------------------


@pytest.fixture
def probe_table(engine: Engine) -> Iterator[Callable[[UUID], None]]:
    """A table no phase ever wrote, with a foreign key to `shifts`.

    Created and dropped in committed transactions of their own. Phase 10 found a test leaving
    a connection idle in transaction, which blocked test_migration_is_reversible's DROP TABLE
    and hung the suite; nothing here holds a connection between statements.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE zz_void_probe ("
                "id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
                "shift_id uuid NOT NULL REFERENCES shifts(id))"
            )
        )

    def _insert(shift_id: UUID) -> None:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO zz_void_probe (shift_id) VALUES (:id)").bindparams(
                    id=shift_id
                )
            )

    yield _insert

    with engine.begin() as connection:
        connection.execute(text("DROP TABLE zz_void_probe"))


async def test_a_table_added_later_blocks_the_void_without_touching_the_route(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    make_shift: Callable[..., UUID],
    probe_table: Callable[[UUID], None],
) -> None:
    """The point of reading pg_constraint (§6.8, §14): the tenth table is covered the day its
    migration lands, without anybody remembering to extend a list."""
    admin = make_user("admin")
    shift_id = make_shift(admin)
    probe_table(shift_id)

    response = await _void(client, shift_id, auth_headers(admin))

    _assert_refused_as_not_empty(response, engine, shift_id, "zz_void_probe")


def test_no_foreign_key_to_shifts_cascades(engine: Engine) -> None:
    """A cascade would turn the race below into a deleted day's money (§14)."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT conrelid::regclass::text, confdeltype FROM pg_constraint "
                "WHERE contype = 'f' AND confrelid = 'shifts'::regclass"
            )
        ).all()

    children = {table for table, _ in rows}
    # Not vacuous: the query sees at least every table Phase 27 knew about.
    assert _KNOWN_CHILDREN <= children
    # 'a' is NO ACTION and 'r' is RESTRICT; both refuse. 'c' cascade, 'n' set null and
    # 'd' set default would each let the delete through.
    assert {table: action for table, action in rows if action not in ("a", "r")} == {}


async def test_a_row_that_slips_past_the_check_is_refused_by_the_foreign_key(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race, made deterministic: the check says "empty", a row exists anyway.

    That is what an attendant saving a reading between the check and the delete looks like
    from inside the route. The NO ACTION key refuses the DELETE, the caller gets the same 409,
    and the audit row written in the same transaction rolls back with it.
    """
    from app.services import shifts as shift_service

    admin = make_user("admin")
    shift_id = make_shift(admin)
    make_collection(shift_id, amount="0.00")
    monkeypatch.setattr(shift_service, "tables_holding", lambda db, shift_id: [])

    response = await _void(client, shift_id, auth_headers(admin))

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "SHIFT_NOT_EMPTY"
    # Named by the database error itself, since the check had nothing to say.
    assert "collections" in response.json()["detail"]
    assert _shift_exists(engine, shift_id)
    assert _void_audit_rows(engine, shift_id) == []
