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
async def client(tmp_path) -> AsyncIterator[AsyncClient]:
    """httpx AsyncClient over the ASGI app, per CLAUDE.md §2.

    `get_storage` is overridden to a `LocalStorage` rooted in this test's own `tmp_path`,
    so every test -- not only the ones that know about Phase 8 -- gets an isolated,
    offline storage backend automatically. Without this, the app would fall back to
    `build_storage`'s dev default (a fixed, machine-wide temp directory), which works but
    is shared and unnecessary I/O for tests that never touch an attachment. `TEST_SUPABASE_
    URL` is set above but `SUPABASE_SERVICE_KEY` deliberately is not, so `build_storage`
    would already choose `LocalStorage` even without this override -- this makes that
    choice explicit and per-test instead of incidental.

    `get_auth` is overridden the same way and for the same reason (Phase 14). A **fresh**
    `LocalAuth` per test matters more than a fresh `LocalStorage` does: it holds the
    email -> id map that makes a duplicate a 409, so a session-wide instance would let one
    test's `ramesh@example.com` fail the next test that used the same address -- on a
    constraint rather than on its own assertion, pointing at the wrong test entirely. That
    is `clean_credit`'s lesson, arriving in a fixture instead of a table.
    """
    from app.api.deps import get_auth, get_storage
    from app.main import create_app
    from app.services.storage import LocalStorage
    from app.services.supabase_auth import LocalAuth

    app = create_app()
    app.dependency_overrides[get_storage] = lambda: LocalStorage(
        root=tmp_path / "storage"
    )
    auth = LocalAuth()
    app.dependency_overrides[get_auth] = lambda: auth
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
            # Phase 14: adopt anybody an API call created during this test, BEFORE any of
            # the cleanup below runs, so every delete that follows covers them too.
            #
            # `POST /api/v1/users` is the first thing in the codebase that inserts a
            # `user_profiles` row over HTTP, and it stamps `created_by` with the acting
            # admin -- who is one of `created`. So the profile delete at the bottom of this
            # block would hit that foreign key and fail. `created_by IS NOT NULL` is an
            # exact marker for "made through the API": this fixture leaves it NULL and so
            # does `app/jobs/provision_user.py`, both because a system action has nobody to
            # credit.
            #
            # Extending `created` rather than adding two deletes further down is what makes
            # this robust: a shift opened for an API-created attendant, an expense they
            # filed, an audit row naming them -- all of it is already handled by the
            # existing cascade, and stays handled when somebody adds a seventh level.
            #
            # A separate `clean_users` fixture was the obvious shape and does not work, for
            # the reason spelled out immediately below: declared with `usefixtures` it is
            # set up first, torn down last, and by then this block has already blown up.
            created.extend(
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT id FROM user_profiles WHERE created_by = ANY(:ids)"
                    ).bindparams(ids=created)
                )
            )

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
            # Phase 9: credit sales and repayments hang off a shift like expenses do, and
            # BOTH must go before `attachments` below -- credit_sales.attachment_id is NOT
            # NULL, so a surviving sale pins its receipt and the attachment delete fails.
            # Two passes each, reversals first, same shape as collections and expenses.
            for table in ("credit_sales", "credit_repayments"):
                for clause in (
                    "reverses_id IS NOT NULL AND (created_by = ANY(:ids) OR shift_id IN "
                    "(SELECT id FROM shifts WHERE attendant_id = ANY(:ids) "
                    "OR created_by = ANY(:ids)))",
                    "created_by = ANY(:ids) OR shift_id IN "
                    "(SELECT id FROM shifts WHERE attendant_id = ANY(:ids) "
                    "OR created_by = ANY(:ids))",
                ):
                    connection.execute(
                        text(f"DELETE FROM {table} WHERE {clause}").bindparams(ids=created)
                    )
            # Phase 10: four more shift-scoped money tables, same two-pass shape. Two
            # things about this block are load-bearing and neither is obvious:
            #
            #  - `bank_deposits.attachment_id` means deposits MUST be swept before the
            #    `attachments` delete below, exactly like credit_sales above. Nullable
            #    rather than NOT NULL, so a survivor blocks the attachment delete instead of
            #    erroring here -- a quieter version of the same failure.
            #  - the two shortfall tables carry `salesman_id` straight to `user_profiles`,
            #    a level no previous phase had. Every earlier child table reached a user
            #    only through `created_by` or through a shift, so "delete the shift first"
            #    was always enough. It is not enough here: a shortfall booked against a
            #    salesman on somebody else's shift still pins that salesman's row.
            for table in (
                "non_fuel_sales",
                "bank_deposits",
                "salesman_shortfalls",
                "salesman_shortfall_settlements",
            ):
                salesman = (
                    " OR salesman_id = ANY(:ids)"
                    if table.startswith("salesman_")
                    else ""
                )
                for clause in (
                    f"reverses_id IS NOT NULL AND (created_by = ANY(:ids){salesman} "
                    "OR shift_id IN (SELECT id FROM shifts WHERE attendant_id = ANY(:ids) "
                    "OR created_by = ANY(:ids)))",
                    f"created_by = ANY(:ids){salesman} OR shift_id IN "
                    "(SELECT id FROM shifts WHERE attendant_id = ANY(:ids) "
                    "OR created_by = ANY(:ids))",
                ):
                    connection.execute(
                        text(f"DELETE FROM {table} WHERE {clause}").bindparams(ids=created)
                    )
            # Phase 10: daily_cash_summaries has no shift to hang off -- it aggregates all
            # of them (§5.0) -- so it is swept on its two user columns alone.
            connection.execute(
                text(
                    "DELETE FROM daily_cash_summaries WHERE created_by = ANY(:ids) "
                    "OR finalised_by = ANY(:ids)"
                ).bindparams(ids=created)
            )
            # Phase 8: attachments point at the user who uploaded them (uploaded_by), and
            # expenses -- already deleted above -- were the only thing that could still
            # reference one via attachment_id. Must run after the expenses passes above and
            # before user_profiles below, or this hits the same class of gap the Phase 6
            # audit found when clean_shifts never swept collections: a foreign key blowing
            # up in a test that has nothing to do with attachments.
            connection.execute(
                text("DELETE FROM attachments WHERE uploaded_by = ANY(:ids)").bindparams(
                    ids=created
                )
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
            # Phase 9: credit_customers.created_by points at a user, and the sales and
            # repayments that pointed at the customer went two blocks up.
            connection.execute(
                text(
                    "DELETE FROM credit_customers WHERE created_by = ANY(:ids)"
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
            # Phase 9: and so do credit sales and repayments. Phase 10 adds four more.
            for _child_table in (
                "credit_sales",
                "credit_repayments",
                "non_fuel_sales",
                "bank_deposits",
                "salesman_shortfalls",
                "salesman_shortfall_settlements",
            ):
                connection.execute(
                    text(
                        "DELETE FROM audit_logs WHERE record_id IN "
                        f"(SELECT id FROM {_child_table} WHERE shift_id = ANY(:ids))"
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
            # Phase 9: credit sales and repayments, the same two-pass shape again. Left
            # before `expenses` only for readability -- they are siblings, not parents.
            # Phase 10 adds four more of the same shape.
            for _child_table in (
                "credit_sales",
                "credit_repayments",
                "non_fuel_sales",
                "bank_deposits",
                "salesman_shortfalls",
                "salesman_shortfall_settlements",
            ):
                connection.execute(
                    text(
                        f"DELETE FROM {_child_table} WHERE shift_id = ANY(:ids) "
                        "AND reverses_id IS NOT NULL"
                    ).bindparams(ids=created)
                )
                connection.execute(
                    text(
                        f"DELETE FROM {_child_table} WHERE shift_id = ANY(:ids)"
                    ).bindparams(ids=created)
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
                "('shifts', 'nozzle_readings', 'collections', 'expenses', "
                "'credit_sales', 'credit_repayments', 'non_fuel_sales', "
                "'bank_deposits', 'salesman_shortfalls', "
                "'salesman_shortfall_settlements', 'daily_cash_summaries')"
            )
        )
        connection.execute(
            text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
        )
        # Readings, then reversals (collections, expenses, the two credit tables and
        # Phase 10's four), before shifts -- all of them hold the foreign key that
        # DELETE FROM shifts needs clear. They all go before `attachments` is ever touched,
        # since credit_sales.attachment_id is NOT NULL and bank_deposits.attachment_id
        # would otherwise pin a receipt nothing can then delete.
        connection.execute(text("DELETE FROM nozzle_readings"))
        for _table in (
            "collections",
            "expenses",
            "credit_sales",
            "credit_repayments",
            "non_fuel_sales",
            "bank_deposits",
            "salesman_shortfalls",
            "salesman_shortfall_settlements",
        ):
            connection.execute(
                text(f"DELETE FROM {_table} WHERE reverses_id IS NOT NULL")
            )
            connection.execute(text(f"DELETE FROM {_table}"))
        # Phase 10: a summary has no shift FK, so DELETE FROM shifts would not fail on a
        # leaked one -- it would survive instead, and collide on
        # uq_daily_cash_summaries_outlet_date the next time any test reconciles the same
        # business date. That is the `clean_expense_categories` failure mode in a table
        # whose natural key is a *date*, which tests reuse far more readily than a code.
        connection.execute(text("DELETE FROM daily_cash_summaries"))
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
        attachment_id: UUID | None = None,
        # §6.11's answer, snapshotted at insert on a real row. Defaulting false here keeps
        # every existing fixture call satisfying `ck_expenses_receipt_required_has_attachment`
        # without having to name a receipt it doesn't have.
        receipt_required: bool = False,
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
                    "requires_review, created_by, attachment_id, receipt_required) "
                    "VALUES (:id, :shift_id, "
                    ":category_id, CAST(:mode AS expense_mode), "
                    "CAST(:amount AS numeric), :description, :paid_to, :reverses_id, "
                    ":reversal_reason, :requires_review, :created_by, :attachment_id, "
                    ":receipt_required)"
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
                    attachment_id=attachment_id,
                    receipt_required=receipt_required,
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


# --- Phase 9: credit ---------------------------------------------------------


@pytest.fixture
def make_attachment(engine: Engine) -> Iterator[Callable[..., UUID]]:
    """Create an `attachments` row directly, bypassing the upload endpoint.

    Phase 8 never needed this -- its tests uploaded through `POST /uploads/receipt`, which
    is what they were testing. Phase 9 needs a receipt on almost every credit sale
    (`attachment_id` is NOT NULL) while testing something else entirely, and driving a
    multipart upload to get one would make every credit test depend on the upload path
    working.

    `linked_at` defaults to NULL, matching a fresh upload. Tests exercising §6.8's
    `CREDIT_SALE_MISSING_RECEIPT` rely on that; anything going through the API gets it
    stamped by `attachment_service.link()`.
    """
    from app.core.config import get_settings

    created: list[UUID] = []

    def _make(
        uploaded_by: UUID,
        *,
        outlet_id: UUID | None = None,
        linked_at: datetime | None = None,
        mime_type: str = "image/jpeg",
    ) -> UUID:
        attachment_id = uuid4()
        outlet = outlet_id or get_settings().DEFAULT_OUTLET_ID
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO attachments (id, outlet_id, bucket, storage_path, "
                    "original_filename, mime_type, size_bytes, checksum_sha256, "
                    "uploaded_by, linked_at) VALUES (:id, :outlet, 'receipts', :path, "
                    "'slip.jpg', :mime, 1024, :checksum, :uploader, :linked)"
                ).bindparams(
                    id=attachment_id,
                    outlet=outlet,
                    path=f"{outlet}/2026/08/22/{attachment_id}.jpg",
                    mime=mime_type,
                    checksum="a" * 64,
                    uploader=uploaded_by,
                    linked=linked_at,
                )
            )
        created.append(attachment_id)
        return attachment_id

    yield _make

    if created:
        with engine.begin() as connection:
            # Credit sales pin their attachment with a NOT NULL FK, so they go first --
            # reversals before originals, as everywhere else.
            for clause in ("reverses_id IS NOT NULL", "TRUE"):
                connection.execute(
                    text(
                        f"DELETE FROM credit_sales WHERE {clause} "
                        "AND attachment_id = ANY(:ids)"
                    ).bindparams(ids=created)
                )
            connection.execute(
                text(
                    "DELETE FROM credit_repayments WHERE attachment_id = ANY(:ids)"
                ).bindparams(ids=created)
            )
            connection.execute(
                text("DELETE FROM expenses WHERE attachment_id = ANY(:ids)").bindparams(
                    ids=created
                )
            )
            connection.execute(
                text("DELETE FROM attachments WHERE id = ANY(:ids)").bindparams(
                    ids=created
                )
            )


@pytest.fixture
def make_credit_customer(engine: Engine) -> Iterator[Callable[..., UUID]]:
    """Create a `credit_customers` row directly, bypassing the API.

    `phone` defaults to a fresh random value rather than a fixed string, because
    `uq_credit_customers_outlet_phone` would otherwise make the *second* customer in any
    test fail on a constraint rather than on its own assertion -- the failure mode
    `clean_expense_categories` documents for category codes, in a table where duplicates are
    much more likely (every test wants two customers).

    Money arrives as a string and is cast in SQL, never as a Python float. §3 rule 1 is
    explicit that this applies "including in a quick test fixture".
    """
    from app.core.config import get_settings

    created: list[UUID] = []

    def _make(
        *,
        name: str = "Test Customer",
        phone: str | None = None,
        vehicle_numbers: list[str] | None = None,
        credit_limit: str | None = None,
        is_active: bool = True,
        outlet_id: UUID | None = None,
        created_by: UUID | None = None,
    ) -> UUID:
        customer_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO credit_customers (id, outlet_id, name, phone, "
                    "vehicle_numbers, credit_limit, is_active, created_by) VALUES "
                    "(:id, :outlet, :name, :phone, :vehicles, "
                    "CAST(:credit_limit AS numeric), :is_active, :created_by)"
                ).bindparams(
                    id=customer_id,
                    outlet=outlet_id or get_settings().DEFAULT_OUTLET_ID,
                    name=name,
                    phone=phone or f"9{uuid4().int % 10**9:09d}",
                    vehicles=vehicle_numbers,
                    credit_limit=credit_limit,
                    is_active=is_active,
                    created_by=created_by,
                )
            )
        created.append(customer_id)
        return customer_id

    yield _make

    if created:
        with engine.begin() as connection:
            # Children first -- nothing here has ON DELETE CASCADE, deliberately.
            for table in ("credit_sales", "credit_repayments"):
                for clause in ("reverses_id IS NOT NULL", "TRUE"):
                    connection.execute(
                        text(
                            f"DELETE FROM {table} WHERE {clause} "
                            "AND credit_customer_id = ANY(:ids)"
                        ).bindparams(ids=created)
                    )
            connection.execute(
                text("DELETE FROM credit_customers WHERE id = ANY(:ids)").bindparams(
                    ids=created
                )
            )


@pytest.fixture
def make_credit_sale(engine: Engine) -> Iterator[Callable[..., UUID]]:
    """Create a `credit_sales` row directly, bypassing the API.

    For tests that need a balance already on the books -- most often so an outstanding
    figure or a credit limit has something to be measured against.
    """
    created: list[UUID] = []

    def _make(
        shift_id: UUID,
        credit_customer_id: UUID,
        attachment_id: UUID,
        *,
        amount: str = "1000.00",
        fuel_type_id: UUID | None = None,
        quantity: str | None = None,
        vehicle_number: str | None = None,
        limit_override_reason: str | None = None,
        reverses_id: UUID | None = None,
        reversal_reason: str | None = None,
        created_by: UUID | None = None,
    ) -> UUID:
        sale_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO credit_sales (id, shift_id, credit_customer_id, "
                    "fuel_type_id, quantity, amount, vehicle_number, attachment_id, "
                    "limit_override_reason, reverses_id, reversal_reason, created_by) "
                    "VALUES (:id, :shift_id, :customer_id, :fuel_type_id, "
                    "CAST(:quantity AS numeric), CAST(:amount AS numeric), :vehicle, "
                    ":attachment_id, :override, :reverses_id, :reversal_reason, "
                    ":created_by)"
                ).bindparams(
                    id=sale_id,
                    shift_id=shift_id,
                    customer_id=credit_customer_id,
                    fuel_type_id=fuel_type_id,
                    quantity=quantity,
                    amount=amount,
                    vehicle=vehicle_number,
                    attachment_id=attachment_id,
                    override=limit_override_reason,
                    reverses_id=reverses_id,
                    reversal_reason=reversal_reason,
                    created_by=created_by,
                )
            )
        created.append(sale_id)
        return sale_id

    yield _make

    if created:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
            )
            connection.execute(
                text(
                    "DELETE FROM audit_logs WHERE table_name = 'credit_sales' "
                    "AND record_id = ANY(:ids)"
                ).bindparams(ids=created)
            )
            connection.execute(
                text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
            )
            # Reversals point at the rows they cancel, so children first.
            connection.execute(
                text("DELETE FROM credit_sales WHERE reverses_id = ANY(:ids)").bindparams(
                    ids=created
                )
            )
            connection.execute(
                text("DELETE FROM credit_sales WHERE id = ANY(:ids)").bindparams(
                    ids=created
                )
            )


@pytest.fixture
def make_credit_repayment(engine: Engine) -> Iterator[Callable[..., UUID]]:
    """Create a `credit_repayments` row directly, bypassing the API. Mirrors
    `make_credit_sale`."""
    created: list[UUID] = []

    def _make(
        shift_id: UUID,
        credit_customer_id: UUID,
        *,
        amount: str = "500.00",
        mode: str = "cash",
        attachment_id: UUID | None = None,
        reverses_id: UUID | None = None,
        reversal_reason: str | None = None,
        created_by: UUID | None = None,
    ) -> UUID:
        repayment_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO credit_repayments (id, credit_customer_id, shift_id, "
                    "amount, mode, attachment_id, reverses_id, reversal_reason, "
                    "created_by) VALUES (:id, :customer_id, :shift_id, "
                    "CAST(:amount AS numeric), CAST(:mode AS credit_repayment_mode), "
                    ":attachment_id, :reverses_id, :reversal_reason, :created_by)"
                ).bindparams(
                    id=repayment_id,
                    customer_id=credit_customer_id,
                    shift_id=shift_id,
                    amount=amount,
                    mode=mode,
                    attachment_id=attachment_id,
                    reverses_id=reverses_id,
                    reversal_reason=reversal_reason,
                    created_by=created_by,
                )
            )
        created.append(repayment_id)
        return repayment_id

    yield _make

    if created:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
            )
            connection.execute(
                text(
                    "DELETE FROM audit_logs WHERE table_name = 'credit_repayments' "
                    "AND record_id = ANY(:ids)"
                ).bindparams(ids=created)
            )
            connection.execute(
                text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
            )
            connection.execute(
                text(
                    "DELETE FROM credit_repayments WHERE reverses_id = ANY(:ids)"
                ).bindparams(ids=created)
            )
            connection.execute(
                text("DELETE FROM credit_repayments WHERE id = ANY(:ids)").bindparams(
                    ids=created
                )
            )


@pytest.fixture
def clean_credit(engine: Engine) -> Iterator[None]:
    """Remove every credit row created during a test, for tests that go through the API.

    The counterpart to `clean_expenses`. A customer created over HTTP has no id to hand
    back, and `uq_credit_customers_outlet_phone` means a leaked one fails the *next* test
    that uses the same phone number -- on a constraint rather than on its own assertion,
    pointing at the wrong test entirely.
    """
    yield
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
        )
        connection.execute(
            text(
                "DELETE FROM audit_logs WHERE table_name IN "
                "('credit_sales', 'credit_repayments', 'credit_customers')"
            )
        )
        connection.execute(
            text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
        )
        for table in ("credit_sales", "credit_repayments"):
            connection.execute(
                text(f"DELETE FROM {table} WHERE reverses_id IS NOT NULL")
            )
            connection.execute(text(f"DELETE FROM {table}"))
        connection.execute(text("DELETE FROM credit_customers"))
        connection.execute(text("DELETE FROM idempotency_keys"))


# --- Phase 10: the cash engine ------------------------------------------------------
#
# Same commit-and-clean contract as every fixture above: committed through `engine.begin()`,
# never held in an open transaction, because the ASGI app takes its own connection from
# SessionLocal and cannot see uncommitted work.
#
# Money arrives as a **string** and is cast in SQL. §3 rule 1 is explicit that "no float"
# applies "including in a quick test fixture" -- a float here would round-trip through binary
# floating point before it ever reached NUMERIC, and these are the fixtures feeding the
# arithmetic §6.4 is judged on.


def _simple_shift_child_fixture(
    table: str, columns: dict[str, str], *, money: tuple[str, ...] = ("amount",)
) -> Callable[[Engine], Iterator[Callable[..., UUID]]]:
    """Build a `make_X` fixture for a shift-scoped money table carrying §6.9's shape.

    Phase 10 adds four tables that differ only in their extra columns, and hand-copying the
    insert-plus-two-pass-teardown four times is how `clean_shifts` ended up never sweeping
    `collections` (the Phase 7 audit finding). Written once instead.

    Teardown is two passes -- reversals first, since a reversal points back at the row it
    cancels -- preceded by the audit sweep with the append-only trigger disabled. That is the
    documented escape hatch, and the trigger doing its job in production is the point.
    """

    def _fixture(engine: Engine) -> Iterator[Callable[..., UUID]]:
        created: list[UUID] = []
        names = ["id", "shift_id", *columns, "reverses_id", "reversal_reason", "created_by"]
        placeholders = ", ".join(
            f"CAST(:{n} AS numeric)" if n in money else f":{n}" for n in names
        )
        statement = text(
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})"
        )

        def _make(shift_id: UUID, **kwargs: object) -> UUID:
            row_id = uuid4()
            values: dict[str, object] = {"id": row_id, "shift_id": shift_id}
            values.update({name: default for name, default in columns.items()})
            values.update({"reverses_id": None, "reversal_reason": None, "created_by": None})
            unknown = set(kwargs) - set(values)
            assert not unknown, f"{table} fixture got unknown kwargs: {sorted(unknown)}"
            values.update(kwargs)
            with engine.begin() as connection:
                connection.execute(statement.bindparams(**values))
            created.append(row_id)
            return row_id

        yield _make

        if created:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only"
                    )
                )
                connection.execute(
                    text(
                        "DELETE FROM audit_logs WHERE table_name = :t "
                        "AND (record_id = ANY(:ids) OR record_id IN "
                        f"(SELECT id FROM {table} WHERE reverses_id = ANY(:ids)))"
                    ).bindparams(t=table, ids=created)
                )
                connection.execute(
                    text(
                        "ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only"
                    )
                )
                # Children first, and "child" means *a row pointing at one of ours* -- not
                # "one of ours that happens to be a reversal". A test that reverses through
                # the API creates a row this fixture never saw, and it holds the foreign key
                # that the delete below needs clear. `make_collection` has said this since
                # Phase 6; getting it backwards here cost four teardown errors.
                connection.execute(
                    text(
                        f"DELETE FROM {table} WHERE reverses_id = ANY(:ids)"
                    ).bindparams(ids=created)
                )
                connection.execute(
                    text(f"DELETE FROM {table} WHERE id = ANY(:ids)").bindparams(
                        ids=created
                    )
                )

    return _fixture


make_non_fuel_sale = pytest.fixture(
    _simple_shift_child_fixture(
        "non_fuel_sales", {"amount": "500.00", "description": "Engine oil"}
    )
)

make_bank_deposit = pytest.fixture(
    _simple_shift_child_fixture(
        "bank_deposits",
        {
            "business_date": None,
            "amount": "100000.00",
            "bank_reference": None,
            "attachment_id": None,
        },
    )
)

make_shortfall = pytest.fixture(
    _simple_shift_child_fixture(
        "salesman_shortfalls",
        {
            "salesman_id": None,
            "amount": "500.00",
            "computed_gap": "500.00",
            "reason": "Counted short at handover",
        },
        money=("amount", "computed_gap"),
    )
)

make_shortfall_settlement = pytest.fixture(
    _simple_shift_child_fixture(
        "salesman_shortfall_settlements", {"salesman_id": None, "amount": "500.00"}
    )
)


@pytest.fixture
def make_daily_summary(engine: Engine) -> Iterator[Callable[..., UUID]]:
    """Create a `daily_cash_summaries` row directly, bypassing the API.

    Not built from `_simple_shift_child_fixture`: this table has no `shift_id` and no
    reversal columns -- §6.9 corrects a *transaction*, and a summary is a derived record that
    an admin unfinalises and recomputes instead (§5.2).

    Every component defaults to ₹0.00 so a test can set only the two or three terms it is
    actually about, rather than restating eleven figures it does not care about.
    """
    from app.core.config import get_settings

    created: list[UUID] = []
    _components = (
        "metered_fuel_sales",
        "non_fuel_sales_total",
        "card_total",
        "upi_total",
        "wallet_total",
        "credit_sales_total",
        "cash_credit_repayments",
        "cash_shortfall_settlements",
        "cash_expenses",
        "bank_deposits_total",
        "shortfalls_booked",
    )

    def _make(
        *,
        business_date: date,
        outlet_id: UUID | None = None,
        opening_balance: str = "0.00",
        opening_balance_source: str = "seeded",
        expected_closing: str = "0.00",
        actual_counted: str | None = None,
        is_finalised: bool = False,
        finalised_by: UUID | None = None,
        finalised_at: datetime | None = None,
        notes: str | None = None,
        created_by: UUID | None = None,
        **components: str,
    ) -> UUID:
        unknown = set(components) - set(_components)
        assert not unknown, f"unknown component columns: {sorted(unknown)}"
        values: dict[str, object] = {name: "0.00" for name in _components}
        values.update(components)
        summary_id = uuid4()
        component_sql = ", ".join(f"CAST(:{name} AS numeric)" for name in _components)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO daily_cash_summaries (id, outlet_id, business_date, "
                    "opening_balance, opening_balance_source, expected_closing, "
                    f"actual_counted, {', '.join(_components)}, is_finalised, "
                    "finalised_by, finalised_at, notes, created_by) VALUES "
                    "(:id, :outlet_id, :business_date, CAST(:opening_balance AS numeric), "
                    "CAST(:opening_balance_source AS opening_balance_source), "
                    "CAST(:expected_closing AS numeric), "
                    f"CAST(:actual_counted AS numeric), {component_sql}, :is_finalised, "
                    ":finalised_by, :finalised_at, :notes, :created_by)"
                ).bindparams(
                    id=summary_id,
                    outlet_id=outlet_id or get_settings().DEFAULT_OUTLET_ID,
                    business_date=business_date,
                    opening_balance=opening_balance,
                    opening_balance_source=opening_balance_source,
                    expected_closing=expected_closing,
                    actual_counted=actual_counted,
                    is_finalised=is_finalised,
                    finalised_by=finalised_by,
                    finalised_at=finalised_at,
                    notes=notes,
                    created_by=created_by,
                    **values,
                )
            )
        created.append(summary_id)
        return summary_id

    yield _make

    if created:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
            )
            connection.execute(
                text(
                    "DELETE FROM audit_logs WHERE table_name = 'daily_cash_summaries' "
                    "AND record_id = ANY(:ids)"
                ).bindparams(ids=created)
            )
            connection.execute(
                text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
            )
            connection.execute(
                text(
                    "DELETE FROM daily_cash_summaries WHERE id = ANY(:ids)"
                ).bindparams(ids=created)
            )


@pytest.fixture
def clean_cash(engine: Engine) -> Iterator[None]:
    """Remove every cash-engine row created during a test, for tests that go through the API
    and have no id to hand back.

    The counterpart to `clean_shifts`, and it exists for the same reason: a leaked
    `daily_cash_summaries` row does not fail `DELETE FROM shifts` -- it has no shift FK -- it
    survives and collides on `uq_daily_cash_summaries_outlet_date` the next time any test
    reconciles that business date. Tests reuse dates far more readily than they reuse a
    category code, so this is the `clean_expense_categories` failure mode with a wider blast
    radius.
    """
    yield
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
        )
        connection.execute(
            text(
                "DELETE FROM audit_logs WHERE table_name IN "
                "('non_fuel_sales', 'bank_deposits', 'salesman_shortfalls', "
                "'salesman_shortfall_settlements', 'daily_cash_summaries')"
            )
        )
        connection.execute(
            text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
        )
        for _table in (
            "non_fuel_sales",
            "bank_deposits",
            "salesman_shortfalls",
            "salesman_shortfall_settlements",
        ):
            connection.execute(
                text(f"DELETE FROM {_table} WHERE reverses_id IS NOT NULL")
            )
            connection.execute(text(f"DELETE FROM {_table}"))
        connection.execute(text("DELETE FROM daily_cash_summaries"))
