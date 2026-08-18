"""Shared test fixtures.

Tests run against a real PostgreSQL instance (docker compose up -d db), never
SQLite. CLAUDE.md §10: SQLite does not enforce the constraints this project depends
on, so a green SQLite suite would give false confidence about exactly the rules
that matter most.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

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
            # Memberships first: they hold the foreign key.
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
    ) -> UUID:
        nozzle_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO nozzles (id, outlet_id, label, dispenser_label, "
                    "fuel_type_id, totalizer_max_value, meter_installed_at, is_active) "
                    "VALUES (:id, :outlet_id, :label, :dispenser_label, :fuel_type_id, "
                    "CAST(:max_value AS numeric), now(), :is_active)"
                ).bindparams(
                    id=nozzle_id,
                    outlet_id=outlet_id or get_settings().DEFAULT_OUTLET_ID,
                    label=label,
                    dispenser_label=dispenser_label,
                    fuel_type_id=fuel_type_id,
                    max_value=totalizer_max_value,
                    is_active=is_active,
                )
            )
        created.append(nozzle_id)
        return nozzle_id

    yield _make

    if created:
        with engine.begin() as connection:
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
def auth_headers(make_token: Callable[..., str]) -> Callable[[UUID], dict[str, str]]:
    """Bearer header for a user id. Saves repeating the same two lines in every test."""

    def _headers(user_id: UUID) -> dict[str, str]:
        return {"Authorization": f"Bearer {make_token(user_id)}"}

    return _headers
