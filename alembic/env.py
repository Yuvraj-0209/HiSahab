"""Alembic environment.

The database URL comes from application settings, never from alembic.ini -- one
source of truth, and no credentials in a file that gets committed.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import app.models  # noqa: F401 -- registers every model on Base.metadata
from app.core.config import get_settings
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Injected by the test suite so migrations can be run against hisahab_test without
# mutating the developer's environment. Falls back to the configured DATABASE_URL.
_url = config.get_main_option("sqlalchemy.url") or str(get_settings().DATABASE_URL)
config.set_main_option("sqlalchemy.url", _url)

# Models become visible to `alembic revision --autogenerate` by virtue of the
# `import app.models` above, which is why that import exists despite looking unused.
target_metadata = Base.metadata

# compare_type / compare_server_default matter for this project specifically:
# without them autogenerate will not notice a NUMERIC(12,2) drifting to NUMERIC(10,2)
# or a default changing, which is exactly the class of silent money bug CLAUDE.md
# is written to prevent.
_COMPARE_OPTIONS = {"compare_type": True, "compare_server_default": True}


def run_migrations_offline() -> None:
    context.configure(
        url=_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_COMPARE_OPTIONS,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, **_COMPARE_OPTIONS
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
