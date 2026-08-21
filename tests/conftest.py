"""Shared test fixtures.

Tests run against a real PostgreSQL instance (docker compose up -d db), never
SQLite. CLAUDE.md §10: SQLite does not enforce the constraints this project depends
on, so a green SQLite suite would give false confidence about exactly the rules
that matter most.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Engine, create_engine, text

# The test database must be selected before app.core.config is imported anywhere,
# because the engine in app.db.session is built at import time.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://hisahab:hisahab@localhost:5433/hisahab_test",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["ENV"] = "test"
os.environ.setdefault(
    "CORS_ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
)

# Auth (Phase 2). The suite mints its own tokens with this secret rather than obtaining
# them from Supabase -- that is offline, deterministic, and the only way to produce the
# failure cases at all (Supabase will not issue you an expired or wrongly-signed token).
# At least 32 bytes, per RFC 7518 §3.2 for HS256.
TEST_JWT_SECRET = "test-secret-not-a-real-key-0123456789"
TEST_SUPABASE_URL = "http://localhost:54321"
os.environ.setdefault("SUPABASE_JWT_SECRET", TEST_JWT_SECRET)
os.environ.setdefault("SUPABASE_URL", TEST_SUPABASE_URL)


def _alembic_config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[None]:
    """Bring the test database to head once per session, and tear it down after.

    Starting from base guarantees the suite exercises the migrations themselves,
    not a schema that happened to be lying around.
    """
    config = _alembic_config(TEST_DATABASE_URL)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield
    command.downgrade(config, "base")


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    return _alembic_config(TEST_DATABASE_URL)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(TEST_DATABASE_URL, future=True)
    yield eng
    eng.dispose()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """httpx AsyncClient over the ASGI app, per CLAUDE.md §2."""
    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def make_token() -> Callable[..., str]:
    """Mint a Supabase-shaped access token.

    Every field is overridable so each failure mode in app/core/security.py is reachable
    from an HTTP test, not just a unit test.
    """
    import jwt

    def _make(
        sub: object,
        *,
        secret: str = TEST_JWT_SECRET,
        audience: str | None = "authenticated",
        issuer: str | None = f"{TEST_SUPABASE_URL}/auth/v1",
        expires_in: int = 3600,
        algorithm: str = "HS256",
    ) -> str:
        now = datetime.now(tz=timezone.utc)
        payload = {
            "sub": str(sub),
            "aud": audience,
            "iss": issuer,
            "iat": now,
            "exp": now + timedelta(seconds=expires_in),
        }
        return jwt.encode(payload, secret, algorithm=algorithm)

    return _make


@pytest.fixture
def make_user(engine: Engine) -> Iterator[Callable[..., UUID]]:
    """Create a user_profiles row and (optionally) an outlet_memberships row.

    Function-scoped and self-cleaning, for two reasons that both bite if ignored:

    * tests/test_migrations.py::test_migration_is_reversible downgrades to base and back
      mid-suite, so anything session-scoped would silently vanish part-way through a run.
    * The rows must be **committed**, not held in an open transaction. The ASGI app takes
      its own connection from SessionLocal and cannot see uncommitted work, so the usual
      transaction-rollback fixture would make every authenticated request 403. Committing
      and deleting afterwards is the house pattern (see tests/test_outlets_seed.py).
    """
    from app.core.config import get_settings

    created: list[UUID] = []

    def _make(
        role: str = "attendant",
        *,
        is_active: bool = True,
        membership_active: bool = True,
        with_membership: bool = True,
        outlet_id: UUID | None = None,
        full_name: str = "Test User",
        phone: str | None = None,
    ) -> UUID:
        user_id = uuid4()
        target_outlet = outlet_id or get_settings().DEFAULT_OUTLET_ID

        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO user_profiles (id, full_name, is_active, phone) "
                    "VALUES (:id, :full_name, :is_active, :phone)"
                ).bindparams(
                    id=user_id,
                    full_name=full_name,
                    is_active=is_active,
                    phone=phone,
                )
            )
            if with_membership:
                connection.execute(
                    text(
                        "INSERT INTO outlet_memberships "
                        "(user_id, outlet_id, role, is_active) "
                        "VALUES (:user_id, :outlet_id, CAST(:role AS membership_role), "
                        ":is_active)"
                    ).bindparams(
                        user_id=user_id,
                        outlet_id=target_outlet,
                        role=str(role),
                        is_active=membership_active,
                    )
                )

        created.append(user_id)
        return user_id

    yield _make

    if created:
        with engine.begin() as connection:
            # Children first -- nothing here has ON DELETE CASCADE, deliberately. Same
            # rule `make_fuel_type` follows.
            #
            # Shifts have to go before the profiles they point at, and their audit rows
            # before them, which needs the append-only trigger off (§5.3's documented
            # escape hatch). This cannot be left to `clean_shifts`: pytest tears fixtures
            # down in reverse setup order, and a usefixtures-declared fixture is set up
            # first, so it would run *after* this and the foreign key would already have
            # blown up.
            connection.execute(
                text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
            )
            # Phase 5: a reading's audit rows go before the reading, which goes before the
            # shift, which goes before the user. Four levels now, and getting the order
            # wrong shows up as a foreign-key error in an unrelated test.
            connection.execute(
                text(
                    "DELETE FROM audit_logs WHERE record_id IN "
                    "(SELECT r.id FROM nozzle_readings r JOIN shifts s ON s.id = r.shift_id"
                    " WHERE s.attendant_id = ANY(:ids) OR s.created_by = ANY(:ids))"
                ).bindparams(ids=created)
            )
            # Phase 6 added a fifth level: collections also hang off a shift and point at
            # a user. Their audit rows go with them.
            connection.execute(
                text(
                    "DELETE FROM audit_logs WHERE record_id IN "
                    "(SELECT c.id FROM collections c JOIN shifts s ON s.id = c.shift_id"
                    " WHERE s.attendant_id = ANY(:ids) OR s.created_by = ANY(:ids))"
                ).bindparams(ids=created)
            )
            # Phase 7 added a sixth: expenses also hang off a shift and, like
            # nozzle_readings, point at a user through BOTH created_by and reviewed_by.
            connection.execute(
                text(
                    "DELETE FROM audit_logs WHERE record_id IN "
                    "(SELECT e.id FROM expenses e JOIN shifts s ON s.id = e.shift_id "
                    "WHERE s.attendant_id = ANY(:ids) OR s.created_by = ANY(:ids) "
                    "OR e.created_by = ANY(:ids) OR e.reviewed_by = ANY(:ids))"
                ).bindparams(ids=created)
            )
            connection.execute(
                text(
                    "DELETE FROM audit_logs WHERE record_id IN "
                    "(SELECT id FROM shifts WHERE attendant_id = ANY(:ids)) "
                    "OR changed_by = ANY(:ids)"
                ).bindparams(ids=created)
            )
            connection.execute(
                text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
            )
            connection.execute(
                text(
                    "DELETE FROM nozzle_readings WHERE created_by = ANY(:ids) "
                    "OR reviewed_by = ANY(:ids) OR shift_id IN "
                    "(SELECT id FROM shifts WHERE attendant_id = ANY(:ids) "
                    "OR created_by = ANY(:ids))"
                ).bindparams(ids=created)
            )
            # Reversals reference the rows they cancel, so they go in their own pass first.
            for clause in (
                "reverses_id IS NOT NULL AND (created_by = ANY(:ids) OR shift_id IN "
                "(SELECT id FROM shifts WHERE attendant_id = ANY(:ids) "
                "OR created_by = ANY(:ids)))",
                "created_by = ANY(:ids) OR shift_id IN "
                "(SELECT id FROM shifts WHERE attendant_id = ANY(:ids) "
                "OR created_by = ANY(:ids))",
            ):
                connection.execute(
                    text(f"DELETE FROM collections WHERE {clause}").bindparams(
                        ids=created
                    )
                )
            # Phase 7: expenses, same two-pass shape, plus reviewed_by -- a user can
            # review an expense on a shift they neither own nor created.
            for clause in (
                "reverses_id IS NOT NULL AND (created_by = ANY(:ids) "
                "OR reviewed_by = ANY(:ids) OR shift_id IN "
                "(SELECT id FROM shifts WHERE attendant_id = ANY(:ids) "
                "OR created_by = ANY(:ids)))",
                "created_by = ANY(:ids) OR reviewed_by = ANY(:ids) OR shift_id IN "
                "(SELECT id FROM shifts WHERE attendant_id = ANY(:ids) "
                "OR created_by = ANY(:ids))",
            ):
                connection.execute(
                    text(f"DELETE FROM expenses WHERE {clause}").bindparams(
                        ids=created
                    )
                )
            connection.execute(
                text(
                    "DELETE FROM idempotency_keys WHERE user_id = ANY(:ids)"
                ).bindparams(ids=created)
            )
            connection.execute(
                text(
                    "DELETE FROM shifts WHERE attendant_id = ANY(:ids) "
                    "OR created_by = ANY(:ids) OR closed_by = ANY(:ids) "
                    "OR locked_by = ANY(:ids)"
                ).bindparams(ids=created)
            )
            connection.execute(
                text(
                    "DELETE FROM outlet_shift_templates WHERE created_by = ANY(:ids)"
                ).bindparams(ids=created)
            )
            # Memberships next: they hold the foreign key.
            connection.execute(
                text(
                    "DELETE FROM outlet_memberships WHERE user_id = ANY(:ids)"
                ).bindparams(ids=created)
            )
            connection.execute(
                text("DELETE FROM user_profiles WHERE id = ANY(:ids)").bindparams(
                    ids=created
                )
            )


@pytest.fixture
def fuel_type_ids(engine: Engine) -> dict[str, UUID]:
    """Map the codes seeded by migration 0003 to their ids.

    Read-only -- these four rows come from the migration, not from a test, so there is
    nothing to clean up. Looked up rather than hardcoded because the ids are
    gen_random_uuid(); only the *outlet* id is fixed (§5.0).

    **Function-scoped, and it must stay that way.** tests/test_migrations.py::
    test_migration_is_reversible downgrades to base and back part-way through the run,
    which drops fuel_types and re-seeds it with fresh random ids. A session-scoped cache
    would hold the pre-downgrade ids and every later test would fail on a foreign key
    against a row that no longer exists. Same hazard `make_user` documents above.
    """
    with engine.connect() as connection:
        return {
            code: row_id
            for code, row_id in connection.execute(
                text("SELECT code, id FROM fuel_types")
            )
        }


@pytest.fixture
def make_fuel_type(engine: Engine) -> Iterator[Callable[..., UUID]]:
    """Create an extra fuel type beyond the four seeded ones.

    For the XP-95 case -- proving an admin can add a product without a migration -- and for
    tests that need a fuel guaranteed to have no prices or margins attached.
    """
    created: list[UUID] = []

    def _make(
        code: str,
        *,
        display_name: str = "Test Fuel",
        unit_of_measure: str = "litre",
        max_flow_rate_per_minute: str = "60.000",
        is_active: bool = True,
    ) -> UUID:
        fuel_type_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO fuel_types (id, code, display_name, unit_of_measure, "
                    "max_flow_rate_per_minute, is_active) VALUES (:id, :code, :name, "
                    "CAST(:unit AS fuel_type_unit_of_measure), CAST(:flow AS numeric), "
                    ":is_active)"
                ).bindparams(
                    id=fuel_type_id,
                    code=code,
                    name=display_name,
                    unit=unit_of_measure,
                    flow=max_flow_rate_per_minute,
                    is_active=is_active,
                )
            )
        created.append(fuel_type_id)
        return fuel_type_id

    yield _make

    if created:
        with engine.begin() as connection:
            # Children first -- nothing here has ON DELETE CASCADE, deliberately.
            #
            # Phase 5 added a generation: readings hang off nozzles, so they must go before
            # the nozzles do. This fixture cannot rely on `make_reading` having cleaned up
            # first: pytest tears fixtures down in reverse *setup* order, and a test that
            # names make_fuel_type after make_reading tears this one down first.
            connection.execute(
                text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
            )
            connection.execute(
                text(
                    "DELETE FROM audit_logs WHERE record_id IN (SELECT r.id FROM "
                    "nozzle_readings r JOIN nozzles n ON n.id = r.nozzle_id "
                    "WHERE n.fuel_type_id = ANY(:ids))"
                ).bindparams(ids=created)
            )
            connection.execute(
                text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
            )
            connection.execute(
                text(
                    "DELETE FROM nozzle_readings WHERE nozzle_id IN "
                    "(SELECT id FROM nozzles WHERE fuel_type_id = ANY(:ids))"
                ).bindparams(ids=created)
            )
            for table in ("fuel_prices", "fuel_margins", "nozzles"):
                connection.execute(
                    text(
                        f"DELETE FROM {table} WHERE fuel_type_id = ANY(:ids)"
                    ).bindparams(ids=created)
                )
            connection.execute(
                text("DELETE FROM fuel_types WHERE id = ANY(:ids)").bindparams(
                    ids=created
                )
            )


@pytest.fixture
def make_nozzle(engine: Engine) -> Iterator[Callable[..., UUID]]:
    """Create a nozzle row directly, bypassing the API.

    Same commit-and-clean contract as `make_user`: the ASGI app takes its own connection
    from SessionLocal and cannot see uncommitted work, so a transaction-rollback fixture
    would make every one of these invisible to the endpoint under test.
    """
    from app.core.config import get_settings

    created: list[UUID] = []

    def _make(
        fuel_type_id: UUID,
        *,
        label: str = "DU-1/N-1",
        dispenser_label: str = "DU-1",
        totalizer_max_value: str = "999999.99",
        is_active: bool = True,
        outlet_id: UUID | None = None,
        meter_installed_at: datetime | None = None,
    ) -> UUID:
        # Phase 5 made this overridable and moved the default into the past. A nozzle
        # installed at now() is *out of scope* for any shift dated earlier (§6.8's close
        # precondition only counts nozzles that existed), so a now() default would make
        # every reading test silently see zero nozzles. The distant past is the safe
        # default; tests exercising NOZZLE_NOT_YET_INSTALLED pass a future value.
        installed = meter_installed_at or datetime(2020, 1, 1, tzinfo=timezone.utc)
        nozzle_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO nozzles (id, outlet_id, label, dispenser_label, "
                    "fuel_type_id, totalizer_max_value, meter_installed_at, is_active) "
                    "VALUES (:id, :outlet_id, :label, :dispenser_label, :fuel_type_id, "
                    "CAST(:max_value AS numeric), :installed, :is_active)"
                ).bindparams(
                    id=nozzle_id,
                    outlet_id=outlet_id or get_settings().DEFAULT_OUTLET_ID,
                    label=label,
                    dispenser_label=dispenser_label,
                    fuel_type_id=fuel_type_id,
                    max_value=totalizer_max_value,
                    installed=installed,
                    is_active=is_active,
                )
            )
        created.append(nozzle_id)
        return nozzle_id

    yield _make

    if created:
        with engine.begin() as connection:
            # Readings first -- they hold the foreign key. Their audit rows go before them,
            # which needs §5.3's documented escape hatch.
            connection.execute(
                text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
            )
            connection.execute(
                text(
                    "DELETE FROM audit_logs WHERE record_id IN "
                    "(SELECT id FROM nozzle_readings WHERE nozzle_id = ANY(:ids))"
                ).bindparams(ids=created)
            )
            connection.execute(
                text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
            )
            connection.execute(
                text(
                    "DELETE FROM nozzle_readings WHERE nozzle_id = ANY(:ids)"
                ).bindparams(ids=created)
            )
            connection.execute(
                text("DELETE FROM nozzles WHERE id = ANY(:ids)").bindparams(ids=created)
            )


def _make_effective_dated_fixture(
    engine: Engine, table: str, value_column: str
) -> Iterator[Callable[..., UUID]]:
    """Shared body for the fuel_prices and fuel_margins fixtures.

    The two tables are structurally identical (§4.6), so the insert differs only in a table
    name and a column name. Written once so a change to the cleanup order cannot be applied
    to one and forgotten on the other.

    Note the teardown uses `ALTER TABLE ... DISABLE TRIGGER`: both tables are append-only
    and a plain DELETE is refused by the trigger from migration 0003. That is the trigger
    doing its job -- these fixtures are the documented escape hatch, and disabling it here
    keeps the production rule strict rather than weakening it for the sake of tests.
    """
    from app.core.config import get_settings

    created: list[UUID] = []

    def _make(
        fuel_type_id: UUID,
        value: str,
        effective_from: datetime,
        *,
        entered_by: UUID,
        outlet_id: UUID | None = None,
    ) -> UUID:
        row_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {table} (id, outlet_id, fuel_type_id, {value_column}, "
                    "effective_from, entered_by) VALUES (:id, :outlet_id, :fuel_type_id, "
                    "CAST(:value AS numeric), :effective_from, :entered_by)"
                ).bindparams(
                    id=row_id,
                    outlet_id=outlet_id or get_settings().DEFAULT_OUTLET_ID,
                    fuel_type_id=fuel_type_id,
                    value=value,
                    effective_from=effective_from,
                    entered_by=entered_by,
                )
            )
        created.append(row_id)
        return row_id

    yield _make

    if created:
        with engine.begin() as connection:
            connection.execute(
                text(f"ALTER TABLE {table} DISABLE TRIGGER trg_{table}_append_only")
            )
            connection.execute(
                text(f"DELETE FROM {table} WHERE id = ANY(:ids)").bindparams(ids=created)
            )
            connection.execute(
                text(f"ALTER TABLE {table} ENABLE TRIGGER trg_{table}_append_only")
            )


@pytest.fixture
def make_fuel_price(engine: Engine) -> Iterator[Callable[..., UUID]]:
    yield from _make_effective_dated_fixture(engine, "fuel_prices", "rate_per_unit")


@pytest.fixture
def make_fuel_margin(engine: Engine) -> Iterator[Callable[..., UUID]]:
    yield from _make_effective_dated_fixture(engine, "fuel_margins", "margin_per_unit")


@pytest.fixture
def make_shift(engine: Engine) -> Iterator[Callable[..., UUID]]:
    """Create a shift row directly, bypassing the API.

    Same commit-and-clean contract as `make_user` and `make_nozzle`: committed, never held
    in an open transaction, because the ASGI app takes its own connection from
    SessionLocal and cannot see uncommitted work.

    Teardown must delete `audit_logs` first, and that needs the append-only trigger turned
    off -- exactly the documented escape hatch `_make_effective_dated_fixture` uses for the
    price tables. The trigger doing its job is the point; the production rule stays strict.
    """
    from app.core.config import get_settings

    created: list[UUID] = []

    def _make(
        attendant_id: UUID,
        *,
        business_date: date | None = None,
        sequence: int = 1,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        status: str = "open",
        outlet_id: UUID | None = None,
    ) -> UUID:
        shift_id = uuid4()
        on = business_date or date(2026, 8, 18)
        # 06:00 IST on the shift's own business date -- this outlet's trading window
        # (CLAUDE.md §4.7) -- expressed as the UTC instant it actually is. Derived from
        # `on` rather than hardcoded, so a test that passes a business_date does not end up
        # with a started_at months away from it and trip
        # ck_shifts_ended_after_started on close.
        start = started_at or datetime.combine(
            on, time(6, 0), tzinfo=ZoneInfo("Asia/Kolkata")
        ).astimezone(timezone.utc)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO shifts (id, outlet_id, business_date, sequence, "
                    "started_at, ended_at, attendant_id, status) VALUES (:id, :outlet_id, "
                    ":business_date, :sequence, :started_at, :ended_at, :attendant_id, "
                    "CAST(:status AS shift_status))"
                ).bindparams(
                    id=shift_id,
                    outlet_id=outlet_id or get_settings().DEFAULT_OUTLET_ID,
                    business_date=on,
                    sequence=sequence,
                    started_at=start,
                    ended_at=ended_at,
                    attendant_id=attendant_id,
                    status=status,
                )
            )
        created.append(shift_id)
        return shift_id

    yield _make

    if created:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
            )
            connection.execute(
                text(
                    "DELETE FROM audit_logs WHERE record_id IN "
                    "(SELECT id FROM nozzle_readings WHERE shift_id = ANY(:ids))"
                ).bindparams(ids=created)
            )
            connection.execute(
                text(
                    "DELETE FROM audit_logs WHERE record_id IN "
                    "(SELECT id FROM collections WHERE shift_id = ANY(:ids))"
                ).bindparams(ids=created)
            )
            # Phase 7: expenses hang off a shift too, same shape as collections.
            connection.execute(
                text(
                    "DELETE FROM audit_logs WHERE record_id IN "
                    "(SELECT id FROM expenses WHERE shift_id = ANY(:ids))"
                ).bindparams(ids=created)
            )
            connection.execute(
                text(
                    "DELETE FROM audit_logs WHERE table_name = 'shifts' "
                    "AND record_id = ANY(:ids)"
                ).bindparams(ids=created)
            )
            connection.execute(
                text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
            )
            connection.execute(
                text(
                    "DELETE FROM nozzle_readings WHERE shift_id = ANY(:ids)"
                ).bindparams(ids=created)
            )
            # Phase 6: collections hang off a shift too, and reversals hang off
            # collections, so this needs two passes before the shift can go.
            connection.execute(
                text(
                    "DELETE FROM collections WHERE shift_id = ANY(:ids) "
                    "AND reverses_id IS NOT NULL"
                ).bindparams(ids=created)
            )
            connection.execute(
                text("DELETE FROM collections WHERE shift_id = ANY(:ids)").bindparams(
                    ids=created
                )
            )
            # Phase 7: expenses, same two-pass shape as collections -- a reversal points
            # back at the row it cancels, so it must go first.
            connection.execute(
                text(
                    "DELETE FROM expenses WHERE shift_id = ANY(:ids) "
                    "AND reverses_id IS NOT NULL"
                ).bindparams(ids=created)
            )
            connection.execute(
                text("DELETE FROM expenses WHERE shift_id = ANY(:ids)").bindparams(
                    ids=created
                )
            )
            connection.execute(
                text("DELETE FROM shifts WHERE id = ANY(:ids)").bindparams(ids=created)
            )


@pytest.fixture
def clean_shifts(engine: Engine) -> Iterator[None]:
    """Remove every shift and shift-related audit row created during a test.

    `make_shift` cleans up only what it created. Tests that open shifts *through the API*
    have no id to hand back, so they use this instead. Both exist because the API-created
    rows are the ones that must be reachable by the endpoint under test, and the
    one-open-shift-per-outlet rule (§5.2) means a leaked open shift fails every later test
    in the run with SHIFT_ALREADY_OPEN.

    **Phase 7 audit finding, fixed here.** This fixture swept `nozzle_readings` but never
    `collections`, so a test that opened a shift through the API *and* created a collection
    through the API -- rather than `make_collection` -- would leave that collection behind,
    and `DELETE FROM shifts` below would then fail every later test in the run with a
    foreign-key violation, not just this one. `expenses` hangs off a shift the same way and
    would have failed identically the moment a test needed both fixtures together, so both
    are swept now rather than waiting for a second occurrence to notice the pattern.
    """
    yield
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
        )
        connection.execute(
            text(
                "DELETE FROM audit_logs WHERE table_name IN "
                "('shifts', 'nozzle_readings', 'collections', 'expenses')"
            )
        )
        connection.execute(
            text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
        )
        # Readings, then reversals (collections and expenses), before shifts -- all of
        # them hold the foreign key that DELETE FROM shifts needs clear.
        connection.execute(text("DELETE FROM nozzle_readings"))
        connection.execute(text("DELETE FROM collections WHERE reverses_id IS NOT NULL"))
        connection.execute(text("DELETE FROM collections"))
        connection.execute(text("DELETE FROM expenses WHERE reverses_id IS NOT NULL"))
        connection.execute(text("DELETE FROM expenses"))
        connection.execute(text("DELETE FROM idempotency_keys"))
        connection.execute(text("DELETE FROM shifts"))


@pytest.fixture
def make_reading(engine: Engine) -> Iterator[Callable[..., UUID]]:
    """Create a nozzle_readings row directly, bypassing the API.

    For tests that need a chain already in place -- a previous shift's closing reading to
    carry forward -- without driving six HTTP calls to build it. Same commit-and-clean
    contract as `make_shift`: committed, never held in an open transaction, because the
    ASGI app takes its own connection from SessionLocal and cannot see uncommitted work.

    Values arrive as strings and are cast in SQL, never passed as Python floats. §3 rule 1
    is explicit that this applies "including in a quick test fixture" -- a float here would
    round-trip through binary floating point before it ever reached NUMERIC.
    """
    created: list[UUID] = []

    def _make(
        shift_id: UUID,
        nozzle_id: UUID,
        *,
        opening_reading: str = "1000.00",
        chained_opening_reading: str | None = None,
        closing_reading: str | None = "1500.00",
        testing_quantity: str = "0",
        rollover_occurred: bool = False,
        meter_reset_occurred: bool = False,
        requires_review: bool = False,
        created_by: UUID | None = None,
    ) -> UUID:
        reading_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO nozzle_readings (id, shift_id, nozzle_id, "
                    "opening_reading, chained_opening_reading, closing_reading, "
                    "testing_quantity, rollover_occurred, meter_reset_occurred, "
                    "requires_review, created_by) VALUES (:id, :shift_id, :nozzle_id, "
                    "CAST(:opening AS numeric), CAST(:chained AS numeric), "
                    "CAST(:closing AS numeric), CAST(:testing AS numeric), "
                    ":rollover, :reset, :review, :created_by)"
                ).bindparams(
                    id=reading_id,
                    shift_id=shift_id,
                    nozzle_id=nozzle_id,
                    opening=opening_reading,
                    chained=chained_opening_reading,
                    closing=closing_reading,
                    testing=testing_quantity,
                    rollover=rollover_occurred,
                    reset=meter_reset_occurred,
                    review=requires_review,
                    created_by=created_by,
                )
            )
        created.append(reading_id)
        return reading_id

    yield _make

    if created:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
            )
            connection.execute(
                text(
                    "DELETE FROM audit_logs WHERE table_name = 'nozzle_readings' "
                    "AND record_id = ANY(:ids)"
                ).bindparams(ids=created)
            )
            connection.execute(
                text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
            )
            connection.execute(
                text("DELETE FROM nozzle_readings WHERE id = ANY(:ids)").bindparams(
                    ids=created
                )
            )


@pytest.fixture
def make_collection(engine: Engine) -> Iterator[Callable[..., UUID]]:
    """Create a collections row directly, bypassing the API.

    For tests that need money already recorded -- most often a cash declaration so that
    §6.8's MISSING_COLLECTIONS does not block a close that is not what the test is about.

    Amounts arrive as strings and are cast in SQL, never passed as Python floats. §3 rule 1
    is explicit that this applies "including in a quick test fixture": a float here would
    round-trip through binary floating point before it ever reached NUMERIC.
    """
    created: list[UUID] = []

    def _make(
        shift_id: UUID,
        *,
        mode: str = "cash",
        amount: str = "5000.00",
        reference: str | None = None,
        reverses_id: UUID | None = None,
        reversal_reason: str | None = None,
        created_by: UUID | None = None,
    ) -> UUID:
        collection_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO collections (id, shift_id, mode, amount, reference, "
                    "reverses_id, reversal_reason, created_by) VALUES (:id, :shift_id, "
                    "CAST(:mode AS collection_mode), CAST(:amount AS numeric), "
                    ":reference, :reverses_id, :reversal_reason, :created_by)"
                ).bindparams(
                    id=collection_id,
                    shift_id=shift_id,
                    mode=mode,
                    amount=amount,
                    reference=reference,
                    reverses_id=reverses_id,
                    reversal_reason=reversal_reason,
                    created_by=created_by,
                )
            )
        created.append(collection_id)
        return collection_id

    yield _make

    if created:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
            )
            connection.execute(
                text(
                    "DELETE FROM audit_logs WHERE table_name = 'collections' "
                    "AND record_id = ANY(:ids)"
                ).bindparams(ids=created)
            )
            connection.execute(
                text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
            )
            # Reversals point at the rows they cancel, so children first.
            connection.execute(
                text(
                    "DELETE FROM collections WHERE reverses_id = ANY(:ids)"
                ).bindparams(ids=created)
            )
            connection.execute(
                text("DELETE FROM collections WHERE id = ANY(:ids)").bindparams(
                    ids=created
                )
            )


@pytest.fixture
def clean_collections(engine: Engine) -> Iterator[None]:
    """Remove every collection created during a test, for tests that go through the API.

    The counterpart to `clean_readings`: a collection created over HTTP has no id to hand
    back, and §5.2's one-live-row-per-mode rule means a leaked row fails the next test that
    posts the same mode to the same shift with COLLECTION_ALREADY_EXISTS.

    Also clears `idempotency_keys`, because a leaked reservation makes the next test using
    the same key meet REQUEST_IN_PROGRESS rather than doing its work.
    """
    yield
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
        )
        connection.execute(
            text("DELETE FROM audit_logs WHERE table_name = 'collections'")
        )
        connection.execute(
            text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
        )
        connection.execute(text("DELETE FROM collections WHERE reverses_id IS NOT NULL"))
        connection.execute(text("DELETE FROM collections"))
        connection.execute(text("DELETE FROM idempotency_keys"))


@pytest.fixture
def expense_category_ids(engine: Engine) -> dict[str, UUID]:
    """Map the category codes seeded by migration 0010 to their ids, for one outlet.

    The `fuel_type_ids` pattern, and function-scoped for the same reason it documents:
    test_migrations.py downgrades to base and back part-way through the run, re-seeding
    these rows with fresh `gen_random_uuid()` ids. A session-scoped cache would hand every
    later test a foreign key to a row that no longer exists.

    Scoped to DEFAULT_OUTLET_ID because codes are unique *per outlet* (§5.1) -- unlike
    `fuel_types`, where a code is global.
    """
    from app.core.config import get_settings

    with engine.connect() as connection:
        return {
            code: row_id
            for code, row_id in connection.execute(
                text(
                    "SELECT code, id FROM expense_categories WHERE outlet_id = :outlet"
                ).bindparams(outlet=get_settings().DEFAULT_OUTLET_ID)
            )
        }


@pytest.fixture
def make_expense_category(engine: Engine) -> Iterator[Callable[..., UUID]]:
    """Create a category beyond the four seeded ones.

    For the case the whole table exists to serve -- proving an admin can add `TEA` without
    a migration -- and for tests needing a category with a specific `requires_receipt` or
    `is_active` state.
    """
    created: list[UUID] = []

    def _make(
        code: str,
        *,
        display_name: str = "Test Category",
        requires_receipt: bool = False,
        is_active: bool = True,
        outlet_id: UUID | None = None,
    ) -> UUID:
        from app.core.config import get_settings

        category_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO expense_categories (id, outlet_id, code, display_name, "
                    "requires_receipt, is_active) VALUES (:id, :outlet, :code, :name, "
                    ":requires_receipt, :is_active)"
                ).bindparams(
                    id=category_id,
                    outlet=outlet_id or get_settings().DEFAULT_OUTLET_ID,
                    code=code,
                    name=display_name,
                    requires_receipt=requires_receipt,
                    is_active=is_active,
                )
            )
        created.append(category_id)
        return category_id

    yield _make

    if created:
        with engine.begin() as connection:
            # Expenses point at categories, so children first -- the same generation rule
            # every other fixture here follows. Nothing has ON DELETE CASCADE, deliberately.
            connection.execute(
                text(
                    "DELETE FROM expenses WHERE category_id = ANY(:ids)"
                ).bindparams(ids=created)
            )
            connection.execute(
                text("DELETE FROM expense_categories WHERE id = ANY(:ids)").bindparams(
                    ids=created
                )
            )


@pytest.fixture
def clean_expense_categories(engine: Engine) -> Iterator[None]:
    """Remove every category created during a test, for tests that go through the API.

    The counterpart to `clean_expenses`: a category created over HTTP has no id to hand
    back, and `make_expense_category`'s own teardown only knows about rows it made itself.

    Sweeps by exclusion rather than by id -- anything that is not one of migration 0010's
    four seeded codes. A leaked category is worse here than a leaked expense: `(outlet_id,
    code)` is unique, so the *next* test to create `TEA` fails on a constraint rather than
    on its own assertion, and the error points at the wrong test entirely.
    """
    yield
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM expenses WHERE category_id IN ("
                "  SELECT id FROM expense_categories "
                "   WHERE code NOT IN ('SALARY', 'MAINTENANCE', 'ELECTRICITY', 'OTHER')"
                ")"
            )
        )
        connection.execute(
            text(
                "DELETE FROM expense_categories "
                "WHERE code NOT IN ('SALARY', 'MAINTENANCE', 'ELECTRICITY', 'OTHER')"
            )
        )


@pytest.fixture
def make_expense(engine: Engine) -> Iterator[Callable[..., UUID]]:
    """Create an expenses row directly, bypassing the API.

    Mirrors `make_collection` exactly. Amounts arrive as strings and are cast in SQL,
    never passed as Python floats -- §3 rule 1 applies "including in a quick test
    fixture".
    """
    created: list[UUID] = []

    def _make(
        shift_id: UUID,
        *,
        category: str = "maintenance",
        category_id: UUID | None = None,
        mode: str = "cash",
        amount: str = "500.00",
        description: str = "Test expense",
        paid_to: str | None = None,
        reverses_id: UUID | None = None,
        reversal_reason: str | None = None,
        requires_review: bool = False,
        created_by: UUID | None = None,
    ) -> UUID:
        expense_id = uuid4()
        with engine.begin() as connection:
            if category_id is None:
                # Phase 8 turned the category into an FK (§5.1), but callers still name a
                # category the way a human does. Resolving the code here rather than making
                # forty tests carry an id keeps them readable and keeps the change where it
                # belongs -- in the one place that knows the schema.
                #
                # Scoped through the shift's outlet, because codes are unique per outlet,
                # not globally.
                category_id = connection.execute(
                    text(
                        "SELECT ec.id FROM expense_categories ec "
                        "JOIN shifts s ON s.outlet_id = ec.outlet_id "
                        "WHERE s.id = :shift_id AND ec.code = :code"
                    ).bindparams(shift_id=shift_id, code=category.upper())
                ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO expenses (id, shift_id, category_id, mode, amount, "
                    "description, paid_to, reverses_id, reversal_reason, "
                    "requires_review, created_by) VALUES (:id, :shift_id, "
                    ":category_id, CAST(:mode AS expense_mode), "
                    "CAST(:amount AS numeric), :description, :paid_to, :reverses_id, "
                    ":reversal_reason, :requires_review, :created_by)"
                ).bindparams(
                    id=expense_id,
                    shift_id=shift_id,
                    category_id=category_id,
                    mode=mode,
                    amount=amount,
                    description=description,
                    paid_to=paid_to,
                    reverses_id=reverses_id,
                    reversal_reason=reversal_reason,
                    requires_review=requires_review,
                    created_by=created_by,
                )
            )
        created.append(expense_id)
        return expense_id

    yield _make

    if created:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
            )
            connection.execute(
                text(
                    "DELETE FROM audit_logs WHERE table_name = 'expenses' "
                    "AND record_id = ANY(:ids)"
                ).bindparams(ids=created)
            )
            connection.execute(
                text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
            )
            # Reversals point at the rows they cancel, so children first.
            connection.execute(
                text("DELETE FROM expenses WHERE reverses_id = ANY(:ids)").bindparams(
                    ids=created
                )
            )
            connection.execute(
                text("DELETE FROM expenses WHERE id = ANY(:ids)").bindparams(ids=created)
            )


@pytest.fixture
def clean_expenses(engine: Engine) -> Iterator[None]:
    """Remove every expense created during a test, for tests that go through the API.

    The counterpart to `clean_collections`: an expense created over HTTP has no id to hand
    back. Unlike collections, there is no natural key a leaked row could collide with, but
    a leaked flagged row would still pollute `GET /expenses/flagged` and the §6.7 aggregate
    for any later test sharing the same outlet and business date.
    """
    yield
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
        )
        connection.execute(text("DELETE FROM audit_logs WHERE table_name = 'expenses'"))
        connection.execute(
            text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
        )
        connection.execute(text("DELETE FROM expenses WHERE reverses_id IS NOT NULL"))
        connection.execute(text("DELETE FROM expenses"))
        connection.execute(text("DELETE FROM idempotency_keys"))


@pytest.fixture
def clean_readings(engine: Engine) -> Iterator[None]:
    """Remove every reading created during a test, for tests that go through the API.

    The counterpart to `clean_shifts`: a reading created over HTTP has no id to hand back,
    and `uq_nozzle_readings_shift_nozzle` means a leaked row fails the next test that
    touches the same nozzle with READING_ALREADY_EXISTS.
    """
    yield
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
        )
        connection.execute(
            text("DELETE FROM audit_logs WHERE table_name = 'nozzle_readings'")
        )
        connection.execute(
            text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
        )
        connection.execute(text("DELETE FROM nozzle_readings"))


@pytest.fixture
def auth_headers(make_token: Callable[..., str]) -> Callable[[UUID], dict[str, str]]:
    """Bearer header for a user id. Saves repeating the same two lines in every test."""

    def _headers(user_id: UUID) -> dict[str, str]:
        return {"Authorization": f"Bearer {make_token(user_id)}"}

    return _headers
