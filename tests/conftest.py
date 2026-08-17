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
