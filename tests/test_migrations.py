"""The migration pipeline itself."""

from __future__ import annotations

from datetime import datetime, timezone
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

    assert version == "0003"


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


# --- Phase 3: reference data (migration 0003) --------------------------------


def test_reference_tables_exist(engine: Engine) -> None:
    with engine.connect() as connection:
        tables = set(
            connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_name "
                    "IN ('fuel_types', 'nozzles', 'fuel_prices', 'fuel_margins')"
                )
            )
            .scalars()
            .all()
        )

    assert tables == {"fuel_types", "nozzles", "fuel_prices", "fuel_margins"}


def test_fuel_types_is_global_reference_data(engine: Engine) -> None:
    """§5.0's landing schedule: no outlet_id. A litre is a litre at every outlet."""
    with engine.connect() as connection:
        columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'fuel_types'"
                )
            )
            .scalars()
            .all()
        )

    assert "outlet_id" not in columns


@pytest.mark.parametrize("table", ["nozzles", "fuel_prices", "fuel_margins"])
def test_outlet_scoped_tables_carry_their_own_outlet_id(
    engine: Engine, table: str
) -> None:
    """§5.0: tenancy is not derivable from a parent row for any of these three."""
    with engine.connect() as connection:
        nullable = connection.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = :table AND column_name = 'outlet_id'"
            ).bindparams(table=table)
        ).scalar_one_or_none()

    assert nullable == "NO"


@pytest.mark.parametrize(
    ("table", "column", "precision", "scale"),
    [
        ("fuel_prices", "rate_per_unit", 12, 2),
        ("fuel_margins", "margin_per_unit", 12, 2),
        ("nozzles", "totalizer_max_value", 12, 2),
        ("fuel_types", "max_flow_rate_per_minute", 10, 3),
    ],
)
def test_numeric_precision_matches_the_spec(
    engine: Engine, table: str, column: str, precision: int, scale: int
) -> None:
    """§3 rules 1 and 2, asserted against the real column type rather than assumed."""
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT data_type, numeric_precision, numeric_scale "
                "FROM information_schema.columns "
                "WHERE table_name = :table AND column_name = :column"
            ).bindparams(table=table, column=column)
        ).one()

    assert row == ("numeric", precision, scale)


def test_unit_of_measure_enum_has_exactly_two_values(engine: Engine) -> None:
    with engine.connect() as connection:
        labels = set(
            connection.execute(
                text("SELECT unnest(enum_range(NULL::fuel_type_unit_of_measure))::text")
            )
            .scalars()
            .all()
        )

    assert labels == {"litre", "kilogram"}


def test_unit_of_measure_rejects_an_unknown_value(engine: Engine) -> None:
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                text("SELECT CAST('gallon' AS fuel_type_unit_of_measure)")
            )


def test_the_four_sold_fuels_are_seeded_with_correct_units(engine: Engine) -> None:
    """§4.5: CBG is metered in kilograms while the liquid fuels are in litres.

    This is the assertion that would have caught the whole CBG problem had it existed
    before the spec was amended.
    """
    with engine.connect() as connection:
        seeded = dict(
            connection.execute(
                text("SELECT code, unit_of_measure FROM fuel_types")
            ).all()
        )

    assert seeded == {
        "PETROL": "litre",
        "DIESEL": "litre",
        "PREMIUM_PETROL": "litre",
        "CBG": "kilogram",
    }


def test_cbg_has_its_own_flow_ceiling(engine: Engine) -> None:
    """§6.2's sanity ceiling is per fuel -- one global 60 L/min would never fire for CBG."""
    with engine.connect() as connection:
        rates = dict(
            connection.execute(
                text("SELECT code, max_flow_rate_per_minute FROM fuel_types")
            ).all()
        )

    assert rates["CBG"] != rates["PETROL"]
    assert rates["PETROL"] == rates["DIESEL"]


def test_nothing_unverified_is_seeded(engine: Engine) -> None:
    """Nozzles, prices and margins are real-world figures nobody has supplied yet.

    A plausible guess in a money table looks exactly like data, which is worse than an
    empty table -- an attendant could enter readings against a nozzle that does not exist.
    """
    with engine.connect() as connection:
        counts = {
            table: connection.execute(
                text(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed literals
            ).scalar_one()
            for table in ("nozzles", "fuel_prices", "fuel_margins")
        }

    assert counts == {"nozzles": 0, "fuel_prices": 0, "fuel_margins": 0}


def test_nozzle_labels_are_unique_per_outlet_not_globally(engine: Engine) -> None:
    """§5.0: "DU-1/N-1" is a label the next outlet will also use."""
    from app.core.config import get_settings

    outlet_id = get_settings().DEFAULT_OUTLET_ID
    other_outlet = uuid4()
    with engine.connect() as connection:
        fuel_type_id = connection.execute(
            text("SELECT id FROM fuel_types WHERE code = 'PETROL'")
        ).scalar_one()

    insert = text(
        "INSERT INTO nozzles (outlet_id, label, dispenser_label, fuel_type_id, "
        "totalizer_max_value, meter_installed_at) "
        "VALUES (:outlet_id, 'DUP/N-1', 'DUP', :fuel_type_id, 999999.99, now())"
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO outlets (id, name) VALUES (:id, 'Second')").bindparams(
                    id=other_outlet
                )
            )
            connection.execute(
                insert.bindparams(outlet_id=outlet_id, fuel_type_id=fuel_type_id)
            )

        # Same label, different outlet -- must be allowed.
        with engine.begin() as connection:
            connection.execute(
                insert.bindparams(outlet_id=other_outlet, fuel_type_id=fuel_type_id)
            )

        # Same label, same outlet -- must not.
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    insert.bindparams(outlet_id=outlet_id, fuel_type_id=fuel_type_id)
                )
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM nozzles WHERE label = 'DUP/N-1'"))
            connection.execute(
                text("DELETE FROM outlets WHERE id = :id").bindparams(id=other_outlet)
            )


@pytest.mark.parametrize(
    ("table", "value_column"),
    [("fuel_prices", "rate_per_unit"), ("fuel_margins", "margin_per_unit")],
)
def test_one_value_per_fuel_per_outlet_per_instant(
    engine: Engine, table: str, value_column: str
) -> None:
    from app.core.config import get_settings

    outlet_id = get_settings().DEFAULT_OUTLET_ID
    user_id = uuid4()
    moment = datetime(2027, 1, 1, 6, 0, tzinfo=timezone.utc)
    with engine.connect() as connection:
        fuel_type_id = connection.execute(
            text("SELECT id FROM fuel_types WHERE code = 'DIESEL'")
        ).scalar_one()

    insert = text(
        f"INSERT INTO {table} (outlet_id, fuel_type_id, {value_column}, "  # noqa: S608
        "effective_from, entered_by) "
        "VALUES (:outlet_id, :fuel_type_id, 50.00, :moment, :user_id)"
    ).bindparams(
        outlet_id=outlet_id, fuel_type_id=fuel_type_id, moment=moment, user_id=user_id
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO user_profiles (id, full_name) VALUES (:id, 'Dup')"
                ).bindparams(id=user_id)
            )
            connection.execute(insert)

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(insert)
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(f"ALTER TABLE {table} DISABLE TRIGGER trg_{table}_append_only")
            )
            connection.execute(
                text(
                    f"DELETE FROM {table} WHERE entered_by = :id"  # noqa: S608
                ).bindparams(id=user_id)
            )
            connection.execute(
                text(f"ALTER TABLE {table} ENABLE TRIGGER trg_{table}_append_only")
            )
            connection.execute(
                text("DELETE FROM user_profiles WHERE id = :id").bindparams(id=user_id)
            )
