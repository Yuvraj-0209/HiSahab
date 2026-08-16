"""Database engine and session management.

Sync SQLAlchemy over psycopg 3. Chosen over async deliberately: one petrol pump
will never need async throughput, Alembic stays simple, and clear code beats clever
code here (CLAUDE.md §2, secondary goal).
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()

# Note on money (CLAUDE.md §3 rule 1): no special type configuration is needed to
# get Decimal back. psycopg 3 maps PostgreSQL NUMERIC to Python Decimal natively,
# and SQLAlchemy's Numeric type keeps asdecimal=True by default. That is asserted
# by tests/test_decimal_roundtrip.py against a real database rather than assumed --
# this is the single rule whose violation produces silently wrong money figures.
engine = create_engine(
    str(_settings.DATABASE_URL),
    pool_pre_ping=True,  # a dropped connection surfaces as a retry, not a 500
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session that is always closed."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
