"""The migration pipeline itself."""

from __future__ import annotations

from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError


def test_schema_is_at_head(engine: Engine) -> None:
    with engine.connect() as connection:
        version = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()

    assert version == "0002"


def test_pgcrypto_extension_is_installed(engine: Engine) -> None:
    with engine.connect() as connection:
        extension = connection.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'pgcrypto'")
        ).scalar_one_or_none()

    assert extension == "pgcrypto"


def test_gen_random_uuid_is_callable(engine: Engine) -> None:
    """§5 makes this the default for every table's primary key."""
    with engine.connect() as connection:
        generated = connection.execute(text("SELECT gen_random_uuid()")).scalar_one()

    assert generated is not None


def test_migration_is_reversible(engine: Engine, alembic_config: Config) -> None:
    """A migration that cannot be rolled back is a one-way door.

    Runs last-ish and restores head, so ordering with other tests does not matter.
    """
    command.downgrade(alembic_config, "base")

    with engine.connect() as connection:
        remaining = connection.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name = 'outlets'"
            )
        ).scalar_one()
    assert remaining == 0

    command.upgrade(alembic_config, "head")

    with engine.connect() as connection:
        restored = connection.execute(
            text("SELECT count(*) FROM outlets")
        ).scalar_one()
    assert restored == 1


def test_auth_tables_exist(engine: Engine) -> None:
    with engine.connect() as connection:
        tables = set(
            connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_name IN ('user_profiles', 'outlet_memberships')"
                )
            )
            .scalars()
            .all()
        )

    assert tables == {"user_profiles", "outlet_memberships"}


def test_user_profiles_has_no_role_or_outlet_column(engine: Engine) -> None:
    """CLAUDE.md §5.1 is explicit: a role is per-outlet, held in outlet_memberships.

    A role column here would hardcode one-role-per-user, which is exactly the retrofit
    §5.0 exists to avoid.
    """
    with engine.connect() as connection:
        columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'user_profiles'"
                )
            )
            .scalars()
            .all()
        )

    assert "role" not in columns
    assert "outlet_id" not in columns


def test_outlet_memberships_carries_its_own_outlet_id(engine: Engine) -> None:
    """§5.0's landing schedule: this table's tenancy is not derivable from a parent."""
    with engine.connect() as connection:
        columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'outlet_memberships'"
                )
            )
            .scalars()
            .all()
        )

    assert "outlet_id" in columns


def test_user_profiles_id_has_no_generated_default(engine: Engine) -> None:
    """It must equal auth.users.id -- a generated id could never match a token's `sub`."""
    with engine.connect() as connection:
        default = connection.execute(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = 'user_profiles' AND column_name = 'id'"
            )
        ).scalar_one()

    assert default is None


def test_membership_role_enum_has_exactly_the_three_roles(engine: Engine) -> None:
    with engine.connect() as connection:
        labels = set(
            connection.execute(
                text("SELECT unnest(enum_range(NULL::membership_role))::text")
            )
            .scalars()
            .all()
        )

    assert labels == {"admin", "manager", "attendant"}


def test_membership_role_rejects_an_unknown_value(engine: Engine) -> None:
    """The database is the last line of defence, not the API layer (§6.6 "belt and braces")."""
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                text("SELECT CAST('superuser' AS membership_role)")
            )


def test_one_membership_per_user_per_outlet(engine: Engine) -> None:
    """§5.1's unique constraint, outlet-scoped."""
    from app.core.config import get_settings

    user_id = uuid4()
    outlet_id = get_settings().DEFAULT_OUTLET_ID

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO user_profiles (id, full_name) VALUES (:id, 'Dup Test')"
            ).bindparams(id=user_id)
        )
        connection.execute(
            text(
                "INSERT INTO outlet_memberships (user_id, outlet_id, role) "
                "VALUES (:user_id, :outlet_id, 'attendant')"
            ).bindparams(user_id=user_id, outlet_id=outlet_id)
        )

    try:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO outlet_memberships (user_id, outlet_id, role) "
                        "VALUES (:user_id, :outlet_id, 'admin')"
                    ).bindparams(user_id=user_id, outlet_id=outlet_id)
                )
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM outlet_memberships WHERE user_id = :id"
                ).bindparams(id=user_id)
            )
            connection.execute(
                text("DELETE FROM user_profiles WHERE id = :id").bindparams(id=user_id)
            )


def test_user_profiles_created_by_accepts_null(engine: Engine) -> None:
    """The bootstrap admin has no creator -- see migration 0002's comment."""
    user_id = uuid4()

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO user_profiles (id, full_name, created_by) "
                "VALUES (:id, 'Bootstrap', NULL)"
            ).bindparams(id=user_id)
        )
        stored = connection.execute(
            text("SELECT created_by FROM user_profiles WHERE id = :id").bindparams(
                id=user_id
            )
        ).scalar_one()
        connection.execute(
            text("DELETE FROM user_profiles WHERE id = :id").bindparams(id=user_id)
        )

    assert stored is None


def test_downgrade_drops_the_membership_role_enum(
    engine: Engine, alembic_config: Config
) -> None:
    """Regression test for the trap this migration was written around.

    op.drop_table() does NOT drop a PostgreSQL ENUM type. If 0002's downgrade leaks it,
    the migration succeeds once and then fails forever after with "type already exists" --
    including in this suite's own session teardown. Restores head afterwards.
    """
    command.downgrade(alembic_config, "base")

    with engine.connect() as connection:
        leaked = connection.execute(
            text("SELECT count(*) FROM pg_type WHERE typname = 'membership_role'")
        ).scalar_one()

    command.upgrade(alembic_config, "head")

    assert leaked == 0
