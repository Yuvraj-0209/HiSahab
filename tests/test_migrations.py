"""The migration pipeline itself."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

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

    assert version == "0015"


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


# --- Phase 4: shifts, templates and the audit log (migration 0004) -----------


def test_phase_four_tables_exist(engine: Engine) -> None:
    with engine.connect() as connection:
        tables = set(
            connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_name "
                    "IN ('shifts', 'outlet_shift_templates', 'audit_logs')"
                )
            )
            .scalars()
            .all()
        )

    assert tables == {"shifts", "outlet_shift_templates", "audit_logs"}


def test_shifts_has_no_shift_type_column(engine: Engine) -> None:
    """§4.7: the morning|night enum described a station this outlet does not run.

    This is the assertion that would catch someone "restoring" it from the pre-§4.7 spec.
    """
    with engine.connect() as connection:
        columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'shifts'"
                )
            )
            .scalars()
            .all()
        )

    assert "shift_type" not in columns
    assert "sequence" in columns


@pytest.mark.parametrize(
    "table", ["shifts", "outlet_shift_templates", "audit_logs"]
)
def test_phase_four_tables_carry_their_own_outlet_id(engine: Engine, table: str) -> None:
    """§5.0: none of the three has a parent row to derive tenancy from."""
    with engine.connect() as connection:
        nullable = connection.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = :table AND column_name = 'outlet_id'"
            ).bindparams(table=table)
        ).scalar_one_or_none()

    assert nullable == "NO"


def test_shift_status_enum_has_exactly_three_values(engine: Engine) -> None:
    with engine.connect() as connection:
        labels = set(
            connection.execute(
                text(
                    "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'shift_status'"
                )
            )
            .scalars()
            .all()
        )

    assert labels == {"open", "closed", "locked"}


def test_audit_log_action_enum_matches_the_spec(engine: Engine) -> None:
    """§5.3 names all four, including `reversal`, which nothing emits until Phase 9.

    Enum labels are fixed at migration time, so the unused one has to be present now or
    adding it later is a schema change in the middle of live financial data.
    """
    with engine.connect() as connection:
        labels = set(
            connection.execute(
                text(
                    "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'audit_log_action'"
                )
            )
            .scalars()
            .all()
        )

    assert labels == {"insert", "update", "reversal", "status_change"}


def test_shift_sequence_is_unique_per_outlet_per_business_date(
    engine: Engine, make_user, make_shift
) -> None:
    """§5.2's duplicate guard, replacing the old (outlet, date, shift_type) constraint."""
    attendant = make_user("attendant")
    make_shift(attendant, business_date=date(2026, 3, 1), sequence=1)

    with pytest.raises(IntegrityError):
        make_shift(attendant, business_date=date(2026, 3, 1), sequence=1, status="closed")


def test_a_shift_cannot_end_before_it_starts(engine: Engine, make_user) -> None:
    """ck_shifts_ended_after_started, in the database and not only in the API.

    A negative shift duration would make §6.2's flow-rate ceiling compute against a
    negative window, which fails open -- every implausible quantity would pass.
    """
    attendant = make_user("attendant")
    start = datetime(2026, 3, 2, 0, 30, tzinfo=timezone.utc)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO shifts (outlet_id, business_date, sequence, started_at, "
                    "ended_at, attendant_id, status) VALUES "
                    "('00000000-0000-0000-0000-000000000001', :on, 1, :start, :end, "
                    ":attendant, CAST('open' AS shift_status))"
                ).bindparams(
                    on=date(2026, 3, 2),
                    start=start,
                    end=start - timedelta(hours=1),
                    attendant=attendant,
                )
            )


def test_the_outlets_trading_window_is_seeded(engine: Engine) -> None:
    """§4.7's 06:00-22:00, a confirmed fact rather than the kind of guess 0003 refused."""
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT sequence, starts_at_local, ends_at_local FROM "
                "outlet_shift_templates WHERE outlet_id = "
                "'00000000-0000-0000-0000-000000000001'"
            )
        ).one()

    assert row == (1, time(6, 0), time(22, 0))


def test_audit_logs_cannot_be_updated_or_deleted(engine: Engine, make_user) -> None:
    """§5.3: "append-only. No updates. No deletes. Ever."

    Enforced by trg_audit_logs_append_only, reusing 0003's reject_modification(). The API
    provides no route that would do either, but that is a promise in application code; this
    is the same promise somewhere a rogue script and a psql session also have to keep it.
    """
    actor = make_user("admin")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO audit_logs (outlet_id, table_name, record_id, action, "
                "changed_by, request_id) VALUES "
                "('00000000-0000-0000-0000-000000000001', 'shifts', gen_random_uuid(), "
                "CAST('insert' AS audit_log_action), :actor, 'test')"
            ).bindparams(actor=actor)
        )

    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as connection:
            connection.execute(text("UPDATE audit_logs SET table_name = 'x'"))

    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM audit_logs"))

    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
        )
        connection.execute(text("DELETE FROM audit_logs"))
        connection.execute(
            text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
        )


def test_zero_zero_zero_three_still_owns_reject_modification(engine: Engine) -> None:
    """0004 reuses the function; it must not redefine or drop it.

    If 0004's downgrade dropped it, rolling back one step would silently disarm the
    append-only guard on fuel_prices and fuel_margins -- the exact tables §5.1 says must
    never be updated.
    """
    with engine.connect() as connection:
        triggers = set(
            connection.execute(
                text(
                    "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
                    "AND tgfoid = 'reject_modification'::regproc"
                )
            )
            .scalars()
            .all()
        )

    assert triggers == {
        "trg_fuel_prices_append_only",
        "trg_fuel_margins_append_only",
        "trg_audit_logs_append_only",
    }


# --- Phase 5: nozzle readings (migration 0005) -------------------------------


def test_nozzle_readings_table_exists(engine: Engine) -> None:
    with engine.connect() as connection:
        exists = connection.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'nozzle_readings'"
            )
        ).scalar_one()
    assert exists == 1


def test_nozzle_readings_has_no_outlet_id(engine: Engine) -> None:
    """§5.0's rule: a tenancy column waits when it is derivable from a parent row.

    `nozzle_readings.shift_id -> shifts.outlet_id` answers the question, so adding the
    column here would denormalise a value that could drift out of step with its shift --
    the exact hazard §6.6 gives for not keeping running totals either.
    """
    with engine.connect() as connection:
        columns = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'nozzle_readings'"
                )
            )
        }
    assert "outlet_id" not in columns
    assert "shift_id" in columns


@pytest.mark.parametrize(
    "column,data_type,precision,scale",
    [
        ("opening_reading", "numeric", 12, 2),
        ("chained_opening_reading", "numeric", 12, 2),
        ("closing_reading", "numeric", 12, 2),
        ("testing_quantity", "numeric", 10, 3),
        ("manual_quantity_override", "numeric", 10, 3),
    ],
)
def test_reading_columns_have_the_precision_the_spec_requires(
    engine: Engine, column: str, data_type: str, precision: int, scale: int
) -> None:
    """§3 rules 1 and 2, asserted against the live database rather than the migration file.

    Reading the migration would prove what was *written*; this proves what PostgreSQL
    actually built. A `double precision` column here would silently corrupt every figure
    downstream and no arithmetic test would necessarily catch it.
    """
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT data_type, numeric_precision, numeric_scale "
                "FROM information_schema.columns "
                "WHERE table_name = 'nozzle_readings' AND column_name = :c"
            ).bindparams(c=column)
        ).one()
    assert row == (data_type, precision, scale)


def test_no_reading_column_is_a_float(engine: Engine) -> None:
    """§3 rule 1, swept across the whole table."""
    with engine.connect() as connection:
        types = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'nozzle_readings'"
                )
            )
        }
    assert not types & {"double precision", "real"}


def test_one_reading_per_nozzle_per_shift(
    engine: Engine, make_user, make_shift, make_nozzle, fuel_type_ids
) -> None:
    """The constraint that also gives §6.10 its answer for this table: a retried POST
    cannot create a second row, so readings need no idempotency-key store."""
    user = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(user, business_date=date(2026, 3, 10), sequence=1)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO nozzle_readings (shift_id, nozzle_id, opening_reading) "
                "VALUES (:s, :n, 1000.00)"
            ).bindparams(s=shift, n=nozzle)
        )
    with pytest.raises(IntegrityError) as caught:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO nozzle_readings (shift_id, nozzle_id, opening_reading) "
                    "VALUES (:s, :n, 2000.00)"
                ).bindparams(s=shift, n=nozzle)
            )
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM nozzle_readings WHERE shift_id = :s").bindparams(s=shift)
        )

    assert "uq_nozzle_readings_shift_nozzle" in str(caught.value)


def test_a_negative_reading_is_refused_by_the_database(
    engine: Engine, make_user, make_shift, make_nozzle, fuel_type_ids
) -> None:
    """A totalizer counts up from zero and cannot show a negative."""
    user = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(user, business_date=date(2026, 3, 10), sequence=1)

    with pytest.raises(IntegrityError) as caught:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO nozzle_readings (shift_id, nozzle_id, opening_reading) "
                    "VALUES (:s, :n, -1.00)"
                ).bindparams(s=shift, n=nozzle)
            )
    assert "ck_nozzle_readings_opening_non_negative" in str(caught.value)


def test_readings_are_not_append_only(engine: Engine) -> None:
    """The deliberate difference from `fuel_prices`, `fuel_margins` and `audit_logs`.

    A reading is corrected while its shift is open -- that is the normal workflow, not an
    exception -- so immutability is enforced by shift *status* (§6.9) rather than by a
    trigger. `reject_modification()` here would make typing a closing reading impossible.
    """
    with engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE c.relname = 'nozzle_readings' AND NOT t.tgisinternal"
                )
            )
        }
    assert triggers == set()


def test_the_chain_lookup_has_an_index(engine: Engine) -> None:
    """§4.7's lookup runs on every worksheet load and every reading entry."""
    with engine.connect() as connection:
        indexes = {
            row[0]
            for row in connection.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'nozzle_readings'")
            )
        }
    assert "ix_nozzle_readings_nozzle_closed" in indexes
    assert "ix_nozzle_readings_shift" in indexes
    assert "ix_nozzle_readings_review" in indexes


# --- Phase 6: collections and the idempotency store --------------------------


def test_collections_are_not_append_only(engine: Engine) -> None:
    """Same deliberate difference as `nozzle_readings`.

    A collection is corrected while its shift is open -- that is the normal workflow -- so
    immutability comes from shift *status* (§6.9) via `require_shift_access`, not from a
    trigger. `reject_modification()` here would make typing a corrected cash figure
    impossible on the very day it was typed wrong.
    """
    with engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE c.relname = 'collections' AND NOT t.tgisinternal"
                )
            )
        }
    assert triggers == set()


def test_collections_have_no_unique_mode_constraint(engine: Engine) -> None:
    """The absence is the design, so the absence is asserted.

    §5.2 does require one *live* row per mode, and `UNIQUE (shift_id, mode)` is the obvious
    way to say it -- which is exactly why this test exists. That constraint is incompatible
    with §6.9: a reversed row stays in the table forever, so its replacement collides with
    it, and corrections stop working on a table that by then holds real money.

    A later "consistency pass" adding the obvious constraint is a realistic thing to
    happen. This is what refuses it, with the reason attached.
    """
    with engine.connect() as connection:
        constraints = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT conname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "WHERE t.relname = 'collections' AND c.contype = 'u'"
                )
            )
        }
    # One unique constraint only, and it is the one that stops a row being reversed twice.
    assert constraints == {"uq_collections_reverses_id"}


def test_collections_carry_every_check_constraint(engine: Engine) -> None:
    with engine.connect() as connection:
        checks = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT conname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "WHERE t.relname = 'collections' AND c.contype = 'c' "
                    "AND conname LIKE 'ck_%'"
                )
            )
        }
    assert checks == {
        "ck_collections_reversal_has_reason",
        "ck_collections_amount_sign",
        "ck_collections_reversal_not_self",
    }


def test_collections_have_no_outlet_id(engine: Engine) -> None:
    """§5.0's rule: derivable via `shift_id -> shifts.outlet_id`, so it waits.

    Adding it here would denormalise a value that could drift out of step with its shift,
    and §5.0 is explicit that a derivable column can be backfilled later with a one-line
    UPDATE ... FROM.
    """
    with engine.connect() as connection:
        columns = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'collections'"
                )
            )
        }
    assert "outlet_id" not in columns


def test_money_on_collections_is_numeric_not_float(engine: Engine) -> None:
    """§3 rule 1, asserted against the live database rather than the model."""
    with engine.connect() as connection:
        data_type, precision, scale = connection.execute(
            text(
                "SELECT data_type, numeric_precision, numeric_scale "
                "FROM information_schema.columns "
                "WHERE table_name = 'collections' AND column_name = 'amount'"
            )
        ).one()
    assert (data_type, precision, scale) == ("numeric", 12, 2)


def test_the_idempotency_tuple_is_unique(engine: Engine) -> None:
    """§6.10's exact tuple: `(key, endpoint, user_id)`.

    This constraint is the concurrency mechanism, not merely a data rule: the reservation
    row is inserted before the handler runs, so two simultaneous retries race here and
    exactly one proceeds.
    """
    with engine.connect() as connection:
        definition = connection.execute(
            text(
                "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                "WHERE c.conname = 'uq_idempotency_keys_key_endpoint_user'"
            )
        ).scalar_one()
    assert "idempotency_key" in definition
    assert "endpoint" in definition
    assert "user_id" in definition


def test_the_override_column_finally_has_a_non_negative_check(engine: Engine) -> None:
    """Phase 5 gave every sibling numeric column a non-negative CHECK and missed this one.

    The API blocked it with `condecimal(ge=0)`, but the database did not -- so a fixture,
    an import or a future migration could write a negative, and it would surface as a 500
    deep in `sales.py` rather than a refusal at the boundary. Added in 0006.
    """
    with engine.connect() as connection:
        checks = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT conname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "WHERE t.relname = 'nozzle_readings' AND c.contype = 'c'"
                )
            )
        }
    assert "ck_nozzle_readings_override_non_negative" in checks


def test_the_collection_mode_enum_is_its_own_type(engine: Engine) -> None:
    """§5.2 gives `credit_repayments.mode` a different set -- `bank_transfer` in place of
    `wallet` -- because a customer settling an old bill can wire money and a customer
    buying diesel at the pump cannot. One shared `payment_mode` would force Phase 9 to
    either alter a live enum or carry a value that is meaningless for it."""
    with engine.connect() as connection:
        labels = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'collection_mode'"
                )
            )
        }
    assert labels == {"cash", "card", "upi", "wallet"}


# --- Phase 7: expenses ---------------------------------------------------------


def test_expenses_are_not_append_only(engine: Engine) -> None:
    """Same deliberate difference as `collections`.

    An expense is corrected while its shift is open -- that is the normal workflow -- so
    immutability comes from shift *status* (§6.9) via `require_shift_access`, not from a
    trigger.
    """
    with engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE c.relname = 'expenses' AND NOT t.tgisinternal"
                )
            )
        }
    assert triggers == set()


def test_expenses_have_no_unique_category_constraint(engine: Engine) -> None:
    """Unlike `collections`' one-live-row-per-mode rule, several expenses in one category
    on one shift are completely normal -- two maintenance call-outs in a day is not a
    mistake. The only unique constraint here is the reversal-once-ever rule."""
    with engine.connect() as connection:
        constraints = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT conname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "WHERE t.relname = 'expenses' AND c.contype = 'u'"
                )
            )
        }
    assert constraints == {"uq_expenses_reverses_id"}


def test_expenses_carry_every_check_constraint(engine: Engine) -> None:
    with engine.connect() as connection:
        checks = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT conname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "WHERE t.relname = 'expenses' AND c.contype = 'c' "
                    "AND conname LIKE 'ck_%'"
                )
            )
        }
    assert checks == {
        "ck_expenses_reversal_has_reason",
        "ck_expenses_amount_sign",
        "ck_expenses_reversal_not_self",
        "ck_expenses_description_length",
        "ck_expenses_receipt_required_has_attachment",
    }


def test_expenses_have_no_outlet_id(engine: Engine) -> None:
    """§5.0's rule: derivable via `shift_id -> shifts.outlet_id`, so it waits."""
    with engine.connect() as connection:
        columns = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'expenses'"
                )
            )
        }
    assert "outlet_id" not in columns


def test_expenses_has_a_nullable_attachment_id(engine: Engine) -> None:
    """§5.2, landed in 0011: nullable, unlike `credit_sales.attachment_id` (§6.6, Phase 9),
    because a receipt was never mandatory on an expense -- §6.11 decides per row."""
    with engine.connect() as connection:
        is_nullable = connection.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'expenses' AND column_name = 'attachment_id'"
            )
        ).scalar_one()
    assert is_nullable == "YES"


def test_expenses_receipt_required_is_not_null_with_a_false_default(
    engine: Engine,
) -> None:
    """0011. NOT NULL because §6.11's rule is an answer, never an omission -- the same
    posture §6.8 takes for an explicit ₹0 cash declaration. The false default exists only
    for rows written outside the API (fixtures, migrations); the API always computes and
    passes this explicitly, never reading it back."""
    with engine.connect() as connection:
        is_nullable, default = connection.execute(
            text(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = 'expenses' AND column_name = 'receipt_required'"
            )
        ).one()
    assert is_nullable == "NO"
    assert default is not None and "false" in default.lower()


def test_money_on_expenses_is_numeric_not_float(engine: Engine) -> None:
    """§3 rule 1, asserted against the live database rather than the model."""
    with engine.connect() as connection:
        data_type, precision, scale = connection.execute(
            text(
                "SELECT data_type, numeric_precision, numeric_scale "
                "FROM information_schema.columns "
                "WHERE table_name = 'expenses' AND column_name = 'amount'"
            )
        ).one()
    assert (data_type, precision, scale) == ("numeric", 12, 2)


def test_no_seeded_category_is_a_fuel_purchase_or_a_synonym_of_other(
    engine: Engine,
) -> None:
    """CLAUDE.md §5.2's Phase 7 note, carried onto 0010's table.

    `fuel_purchase` invited recording a bank/IOCL settlement as a drawer expense, and
    `misc`/`other` were synonyms that could split one real expense across both labels and
    defeat §6.7's per-category aggregate.

    **This test got weaker in Phase 8 and that is worth being honest about.** Until 0010,
    the absence of `fuel_purchase` was enforced by the enum -- there was no way to record
    one. Now an admin can create any category through the API, so the schema guarantees
    nothing and this only checks that the *seed* stays clean. The real control moved to
    §14's guardrail list and `app/core/expenses.py`'s docstring, where a human has to keep
    making the choice. Recorded here so nobody mistakes a passing test for a safety net.
    """
    with engine.connect() as connection:
        codes = {
            row[0]
            for row in connection.execute(text("SELECT code FROM expense_categories"))
        }
    assert codes == {"SALARY", "MAINTENANCE", "ELECTRICITY", "OTHER"}
    assert not {c for c in codes if "FUEL" in c or "IOCL" in c or "PAD" in c or c == "MISC"}


def test_the_expense_mode_enum_is_its_own_type(engine: Engine) -> None:
    """Not shared with `collection_mode`: an expense can be paid `bank_transfer`, a
    collection cannot, and `collections` has no `wallet`-equivalent for money going out."""
    with engine.connect() as connection:
        labels = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'expense_mode'"
                )
            )
        }
    assert labels == {"cash", "card", "upi", "bank_transfer"}


def test_expenses_review_index_is_partial(engine: Engine) -> None:
    """Copied from `nozzle_readings`' `ix_nozzle_readings_review` -- an index over every
    row that is *not* flagged is dead weight; the review queue only ever scans the flagged
    ones."""
    with engine.connect() as connection:
        definition = connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'expenses' AND indexname = 'ix_expenses_review'"
            )
        ).scalar_one()
    assert "WHERE" in definition
    assert "requires_review" in definition


# --- 0010: the category enum becomes a table (Phase 8) -----------------------


def test_expense_categories_are_seeded_and_outlet_scoped(engine: Engine) -> None:
    with engine.connect() as connection:
        rows = {
            row[0]: row[1]
            for row in connection.execute(
                text("SELECT code, requires_receipt FROM expense_categories")
            )
        }
    # The four 0008 shipped, so the backfill has somewhere to point. Everything else is
    # data entry now -- that is the whole purpose of the table.
    assert set(rows) == {"SALARY", "MAINTENANCE", "ELECTRICITY", "OTHER"}
    # §6.11: `OTHER` is the bucket for spending nobody anticipated, which is exactly the
    # spending that most deserves a piece of paper behind it.
    assert rows["OTHER"] is True
    assert not any(rows[code] for code in ("SALARY", "MAINTENANCE", "ELECTRICITY"))


def test_the_expense_category_enum_type_is_gone(engine: Engine) -> None:
    """`op.drop_column` does not drop an enum type -- 0008's own downgrade documents that
    trap, and 0010 is where forgetting it would bite: the orphaned type would collide with
    the `CREATE TYPE` in 0010's downgrade."""
    with engine.connect() as connection:
        remaining = connection.execute(
            text("SELECT count(*) FROM pg_type WHERE typname = 'expense_category'")
        ).scalar_one()
    assert remaining == 0


def test_expenses_no_longer_has_a_category_enum_column(engine: Engine) -> None:
    with engine.connect() as connection:
        columns = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'expenses'"
                )
            )
        }
    assert "category" not in columns
    assert "category_id" in columns


def test_a_lowercase_category_code_is_refused_at_the_database_level(
    engine: Engine,
) -> None:
    """Belt and braces (§6.6). The API validates the same pattern, but the constraint is
    what actually stops `TEA`/`tea`/`Tea` becoming three categories and quietly disabling
    §6.7's aggregate rule -- the exact failure §5.2's "not free text" warning names."""
    with pytest.raises(IntegrityError, match="ck_expense_categories_code_format"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO expense_categories (outlet_id, code, display_name) "
                    "SELECT id, 'tea', 'Tea' FROM outlets LIMIT 1"
                )
            )


def test_0010_backfills_existing_expenses_onto_the_new_category_table(
    engine: Engine, alembic_config: Config
) -> None:
    """The one path the rest of the suite can never reach.

    Every other migration test runs against a database migrated from empty, so 0010's
    `UPDATE ... FROM shifts, expense_categories` never touches a row and its correctness is
    taken on faith. A backfill that silently maps nothing still passes `alembic upgrade`,
    and the damage -- every historical expense pointing at the wrong category, or the
    migration aborting on real data during a deploy -- only shows up where it costs most.

    So: roll back to 0009, write an expense through the *old* enum column, migrate forward,
    and prove the row landed on the matching `expense_categories` id.
    """
    command.downgrade(alembic_config, "0009")

    user_id = uuid4()
    shift_id = uuid4()
    expense_id = uuid4()

    try:
        with engine.begin() as connection:
            outlet_id = connection.execute(
                text("SELECT id FROM outlets LIMIT 1")
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO user_profiles (id, full_name) "
                    "VALUES (:id, 'Backfill Tester')"
                ).bindparams(id=user_id)
            )
            connection.execute(
                text(
                    "INSERT INTO shifts "
                    "(id, outlet_id, business_date, sequence, started_at, attendant_id, "
                    " status) "
                    "VALUES (:id, :outlet, DATE '2026-03-01', 1, "
                    "        TIMESTAMPTZ '2026-03-01 06:00+05:30', :att, "
                    "        CAST('open' AS shift_status))"
                ).bindparams(id=shift_id, outlet=outlet_id, att=user_id)
            )
            connection.execute(
                text(
                    "INSERT INTO expenses "
                    "(id, shift_id, category, mode, amount, description) "
                    "VALUES (:id, :shift, CAST('maintenance' AS expense_category), "
                    "        CAST('cash' AS expense_mode), 500.00, 'pump servicing')"
                ).bindparams(id=expense_id, shift=shift_id)
            )

        command.upgrade(alembic_config, "0010")

        with engine.connect() as connection:
            code = connection.execute(
                text(
                    "SELECT ec.code FROM expenses e "
                    "JOIN expense_categories ec ON ec.id = e.category_id "
                    "WHERE e.id = :id"
                ).bindparams(id=expense_id)
            ).scalar_one()

        assert code == "MAINTENANCE"

        # And the round trip back, which is where the lossy branch lives.
        command.downgrade(alembic_config, "0009")
        with engine.connect() as connection:
            restored = connection.execute(
                text("SELECT category::text FROM expenses WHERE id = :id").bindparams(
                    id=expense_id
                )
            ).scalar_one()
        assert restored == "maintenance"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM expenses WHERE id = :id").bindparams(id=expense_id)
            )
            connection.execute(
                text("DELETE FROM shifts WHERE id = :id").bindparams(id=shift_id)
            )
            connection.execute(
                text("DELETE FROM user_profiles WHERE id = :id").bindparams(id=user_id)
            )
        command.upgrade(alembic_config, "head")


# --- 0011: attachments (Phase 8) ----------------------------------------------


def test_attachments_carries_its_own_outlet_id(engine: Engine) -> None:
    """Unlike everything Phases 5-7 built, this is NOT derivable via a parent shift
    (§5.0) -- at upload time nothing has linked the attachment to anything yet."""
    with engine.connect() as connection:
        is_nullable = connection.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'attachments' AND column_name = 'outlet_id'"
            )
        ).scalar_one()
    assert is_nullable == "NO"


def test_attachments_has_no_created_by_column(engine: Engine) -> None:
    """A deliberate exception to §5's preamble, the same documented shape as `outlets`'.
    `uploaded_by` is the creator; carrying both would mean two columns holding one value
    until the day they disagree."""
    with engine.connect() as connection:
        columns = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'attachments'"
                )
            )
        }
    assert "created_by" not in columns
    assert "uploaded_by" in columns


def test_attachments_storage_path_is_unique(engine: Engine) -> None:
    with engine.connect() as connection:
        names = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT conname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "WHERE t.relname = 'attachments' AND c.contype = 'u'"
                )
            )
        }
    assert names == {"uq_attachments_storage_path"}


def test_attachments_unlinked_index_is_partial(engine: Engine) -> None:
    """The one query §7.4's sweep runs. Mirrors ix_expenses_review (0008) -- an index over
    every LINKED row would be dead weight, since nothing else scans by linked_at."""
    with engine.connect() as connection:
        definition = connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'attachments' AND indexname = 'ix_attachments_unlinked'"
            )
        ).scalar_one()
    assert "WHERE" in definition
    assert "linked_at" in definition


@pytest.fixture
def _attachment_uploader(engine: Engine) -> Iterator[UUID]:
    """A throwaway user_profiles row for the three CHECK tests below -- the migration test
    database seeds no users, so `attachments.uploaded_by` has nothing to point at."""
    from app.core.config import get_settings

    user_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO user_profiles (id, full_name) VALUES (:id, 'Upload Tester')"
            ).bindparams(id=user_id)
        )
    yield user_id
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM user_profiles WHERE id = :id").bindparams(id=user_id)
        )


def test_a_zero_size_attachment_is_refused_at_the_database_level(
    engine: Engine, _attachment_uploader: UUID
) -> None:
    """§6.6 belt and braces. Unreachable through the API -- upload validation refuses an
    empty file before storage or the database are ever touched -- so this proves the last
    line of defence works even though nothing should ever reach it."""
    from app.core.config import get_settings

    with pytest.raises(IntegrityError, match="ck_attachments_size_positive"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO attachments (outlet_id, bucket, storage_path, "
                    "original_filename, mime_type, size_bytes, checksum_sha256, "
                    "uploaded_by) VALUES (:outlet, 'receipts', 'x/y/z.jpg', 'r.jpg', "
                    "'image/jpeg', 0, repeat('a', 64), :uploader)"
                ).bindparams(
                    outlet=get_settings().DEFAULT_OUTLET_ID,
                    uploader=_attachment_uploader,
                )
            )


def test_a_malformed_checksum_is_refused_at_the_database_level(
    engine: Engine, _attachment_uploader: UUID
) -> None:
    from app.core.config import get_settings

    with pytest.raises(IntegrityError, match="ck_attachments_checksum_format"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO attachments (outlet_id, bucket, storage_path, "
                    "original_filename, mime_type, size_bytes, checksum_sha256, "
                    "uploaded_by) VALUES (:outlet, 'receipts', 'x/y/z2.jpg', 'r.jpg', "
                    "'image/jpeg', 100, 'not-a-real-checksum', :uploader)"
                ).bindparams(
                    outlet=get_settings().DEFAULT_OUTLET_ID,
                    uploader=_attachment_uploader,
                )
            )


def test_an_unsupported_mime_type_is_refused_at_the_database_level(
    engine: Engine, _attachment_uploader: UUID
) -> None:
    from app.core.config import get_settings

    with pytest.raises(IntegrityError, match="ck_attachments_mime_type_allowed"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO attachments (outlet_id, bucket, storage_path, "
                    "original_filename, mime_type, size_bytes, checksum_sha256, "
                    "uploaded_by) VALUES (:outlet, 'receipts', 'x/y/z3.pdf', 'r.pdf', "
                    "'application/pdf', 100, repeat('a', 64), :uploader)"
                ).bindparams(
                    outlet=get_settings().DEFAULT_OUTLET_ID,
                    uploader=_attachment_uploader,
                )
            )


def test_expenses_receipt_check_is_reachable_and_mapped(engine: Engine) -> None:
    """The one CHECK 0011 adds that a client CAN actually reach (see that migration's
    docstring) -- so unlike the three on `attachments` above, this one must be in
    `_CONSTRAINT_ERRORS` or a real refusal surfaces as an opaque 500."""
    from app.core.errors import _CONSTRAINT_ERRORS

    assert "ck_expenses_receipt_required_has_attachment" in _CONSTRAINT_ERRORS


# --- 0012: credit (Phase 9) ---------------------------------------------------


def test_credit_customers_carries_its_own_outlet_id(engine: Engine) -> None:
    """§5.0: a customer has no parent row to derive an outlet from, so the column exists
    from birth. Contrast the two tests below."""
    with engine.connect() as connection:
        column = connection.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'credit_customers' AND column_name = 'outlet_id'"
            )
        ).scalar_one()

    assert column == "NO"


@pytest.mark.parametrize("table", ["credit_sales", "credit_repayments"])
def test_the_transactional_credit_tables_have_no_outlet_id(
    engine: Engine, table: str
) -> None:
    """§5.0's other half: derivable via `shift_id -> shifts.outlet_id`, exactly like
    `collections` and `expenses`, so the column must NOT exist."""
    with engine.connect() as connection:
        found = connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = 'outlet_id'"
            ).bindparams(t=table)
        ).scalar_one_or_none()

    assert found is None


def test_credit_sales_attachment_id_is_not_nullable(engine: Engine) -> None:
    """§6.6's receipt control, asserted against the schema itself rather than inferred from
    a 4xx. This is the one column in the codebase that is unconditionally NOT NULL for a
    receipt -- `expenses.attachment_id` is nullable and conditional (§6.11)."""
    with engine.connect() as connection:
        is_nullable = connection.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'credit_sales' AND column_name = 'attachment_id'"
            )
        ).scalar_one()

    assert is_nullable == "NO"


def test_credit_sales_has_no_reversal_exemption_on_its_receipt(engine: Engine) -> None:
    """The counterpart to the test above, and the thing §14 forbids by name.

    `expenses` has `ck_expenses_receipt_required_has_attachment`, which begins
    `reverses_id IS NOT NULL OR ...` so a reversal needs no receipt. Copying that shape here
    would mean weakening `attachment_id` to nullable, taking the receipt control off genuine
    customer sales. Phase 9 uses inheritance instead -- the reversal carries the original's
    attachment -- so no such CHECK should exist.
    """
    with engine.connect() as connection:
        checks = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT c.conname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "WHERE t.relname = 'credit_sales' AND c.contype = 'c' "
                    "AND pg_get_constraintdef(c.oid) ILIKE '%attachment_id%'"
                )
            )
        }

    assert checks == set(), (
        "credit_sales.attachment_id is enforced by NOT NULL alone; a CHECK mentioning it "
        "suggests the reversal exemption §5.2 and §14 say not to add"
    )


def test_is_settled_does_not_exist_anywhere(engine: Engine) -> None:
    """Removed in Phase 9 (§5.2): it contradicted §6.6's compute-do-not-store rule, and had
    no honest value once one repayment covers part of three bills."""
    with engine.connect() as connection:
        found = connection.execute(
            text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE column_name = 'is_settled'"
            )
        ).scalar_one_or_none()

    assert found is None


def test_credit_repayment_mode_is_its_own_enum_type(engine: Engine) -> None:
    """Not `collection_mode` (has `wallet`, lacks `bank_transfer`) and not `expense_mode`
    (labels match today by coincidence, not contract). §5.2."""
    with engine.connect() as connection:
        labels = [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'credit_repayment_mode' ORDER BY e.enumsortorder"
                )
            )
        ]

    assert labels == ["cash", "card", "upi", "bank_transfer"]


@pytest.mark.parametrize(
    "table", ["credit_customers", "credit_sales", "credit_repayments"]
)
def test_every_credit_money_column_is_numeric_not_float(
    engine: Engine, table: str
) -> None:
    """§3 rule 1, asserted at the schema. A `double precision` here would be the exact
    silently-wrong-money failure this project exists to prevent."""
    with engine.connect() as connection:
        types = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name IN "
                    "('amount', 'credit_limit', 'quantity')"
                ).bindparams(t=table)
            )
        }

    assert types <= {"numeric"}, f"{table} has a non-numeric money/quantity column: {types}"


def test_a_quantity_without_a_fuel_type_is_refused(engine: Engine) -> None:
    """§4.5: a quantity's unit lives on the fuel type, so a quantity with no fuel type
    cannot be interpreted -- 12 of what?"""
    with engine.connect() as connection:
        definition = connection.execute(
            text(
                "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = 'credit_sales' "
                "AND c.conname = 'ck_credit_sales_quantity_needs_fuel_type'"
            )
        ).scalar_one()

    assert "fuel_type_id IS NOT NULL" in definition


@pytest.mark.parametrize("table", ["credit_sales", "credit_repayments"])
def test_the_credit_sign_rule_is_strict_like_expenses_not_collections(
    engine: Engine, table: str
) -> None:
    """A ₹0 cash collection is a genuine declaration under §6.8; a ₹0 udhaar records
    nothing. So `> 0` / `< 0`, never `>=` / `<=`."""
    with engine.connect() as connection:
        definition = connection.execute(
            text(
                "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = :t AND c.conname = :n"
            ).bindparams(t=table, n=f"ck_{table}_amount_sign")
        ).scalar_one()

    assert ">= (0)" not in definition
    assert "<= (0)" not in definition
    assert "> (0" in definition and "< (0" in definition


def test_a_live_credit_sale_cannot_carry_a_negative_quantity(engine: Engine) -> None:
    """The other half of `ck_credit_sales_quantity_sign`.

    Phase 9 first shipped this as a bare `quantity > 0`, which was wrong in a way no
    single-row test would have caught: §6.9's reversal negates the quantity alongside the
    amount, so every reversal of a *fuel* credit sale hit the constraint and returned a 500.
    Fixed to mirror `ck_credit_sales_amount_sign`. This asserts the direction that must still
    be refused, so the fix cannot be over-applied into "any sign, anywhere".
    """
    definition = None
    with engine.connect() as connection:
        definition = connection.execute(
            text(
                "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = 'credit_sales' "
                "AND c.conname = 'ck_credit_sales_quantity_sign'"
            )
        ).scalar_one()

    # A live row (reverses_id IS NULL) still requires a positive quantity; only a reversal
    # may carry a negative one.
    assert "reverses_id IS NULL" in definition
    assert "quantity > (0" in definition
    assert "quantity < (0" in definition


# --- Phase 10: the cash engine tables (0013) ---------------------------------------


def test_daily_cash_summaries_carries_its_own_outlet_id(engine: Engine) -> None:
    """§5.0: a summary has no parent shift -- it aggregates every shift on the date -- so
    there is no correct backfill and the column exists from birth."""
    with engine.connect() as connection:
        is_nullable = connection.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'daily_cash_summaries' AND column_name = 'outlet_id'"
            )
        ).scalar_one()

    assert is_nullable == "NO"


@pytest.mark.parametrize(
    "table",
    [
        "non_fuel_sales",
        "bank_deposits",
        "salesman_shortfalls",
        "salesman_shortfall_settlements",
    ],
)
def test_the_shift_scoped_cash_tables_have_no_outlet_id(
    engine: Engine, table: str
) -> None:
    """§5.0's other half: all four hang off a shift, so the outlet is one join away and the
    column must NOT exist -- exactly like `collections`, `expenses` and the credit tables."""
    with engine.connect() as connection:
        found = connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = 'outlet_id'"
            ).bindparams(t=table)
        ).scalar_one_or_none()

    assert found is None


@pytest.mark.parametrize(
    "table", ["salesman_shortfalls", "salesman_shortfall_settlements"]
)
def test_a_shortfall_points_at_a_user_profile_never_a_credit_customer(
    engine: Engine, table: str
) -> None:
    """§13.14 and §14, asserted against the schema rather than inferred from behaviour.

    A shortfall filed against a credit customer would mix staff debt into a real customer's
    outstanding balance, after which "what does this customer owe me" -- the figure §14 says
    the owner checks first -- stops having an answer. Making that structurally impossible is
    worth a test that no value test replaces.
    """
    with engine.connect() as connection:
        target = connection.execute(
            text(
                "SELECT ccu.table_name FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON kcu.constraint_name = tc.constraint_name "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON ccu.constraint_name = tc.constraint_name "
                "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_name = :t "
                "  AND kcu.column_name = 'salesman_id'"
            ).bindparams(t=table)
        ).scalar_one()

    assert target == "user_profiles"


def test_no_cash_engine_table_references_credit_customers(engine: Engine) -> None:
    """The same guardrail stated negatively, across all five tables at once, so that a
    future column cannot reintroduce the link under a different name."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT tc.table_name, kcu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON kcu.constraint_name = tc.constraint_name "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON ccu.constraint_name = tc.constraint_name "
                "WHERE tc.constraint_type = 'FOREIGN KEY' "
                "  AND ccu.table_name = 'credit_customers' "
                "  AND tc.table_name IN ('non_fuel_sales', 'bank_deposits', "
                "      'salesman_shortfalls', 'salesman_shortfall_settlements', "
                "      'daily_cash_summaries')"
            )
        ).all()

    assert rows == []


def test_the_variance_column_is_generated_by_the_database(engine: Engine) -> None:
    """§5.2 says generated, and it matters that it is not merely computed in Python: an
    application-computed variance can be written to disagree with its own inputs."""
    with engine.connect() as connection:
        is_generated, expression = connection.execute(
            text(
                "SELECT is_generated, generation_expression "
                "FROM information_schema.columns "
                "WHERE table_name = 'daily_cash_summaries' AND column_name = 'variance'"
            )
        ).one()

    assert is_generated == "ALWAYS"
    assert "actual_counted" in expression
    assert "expected_closing" in expression


def test_variance_is_null_when_nothing_was_counted(
    engine: Engine, make_user, make_daily_summary
) -> None:
    """§6.5's locker model means most days have no count at all. NULL must propagate, so an
    uncounted day reads as "no variance known" rather than as a variance of zero -- which
    would look like a perfectly reconciled day nobody ever checked."""
    admin = make_user("admin")
    summary_id = make_daily_summary(
        business_date=date(2027, 3, 1), created_by=admin, actual_counted=None
    )

    with engine.connect() as connection:
        variance = connection.execute(
            text(
                "SELECT variance FROM daily_cash_summaries WHERE id = :id"
            ).bindparams(id=summary_id)
        ).scalar_one()

    assert variance is None


def test_variance_is_computed_when_a_count_lands(
    engine: Engine, make_user, make_daily_summary
) -> None:
    admin = make_user("admin")
    summary_id = make_daily_summary(
        business_date=date(2027, 3, 2),
        created_by=admin,
        expected_closing="56000.00",
        actual_counted="55700.00",
    )

    with engine.connect() as connection:
        variance = connection.execute(
            text(
                "SELECT variance FROM daily_cash_summaries WHERE id = :id"
            ).bindparams(id=summary_id)
        ).scalar_one()

    assert variance == Decimal("-300.00")


def test_the_opening_balance_source_enum_has_exactly_three_labels(
    engine: Engine,
) -> None:
    """§6.5's three branches. A fourth would mean the rule grew a case nobody wrote down."""
    with engine.connect() as connection:
        labels = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'opening_balance_source'"
                )
            )
        }

    assert labels == {"seeded", "counted", "carried"}


def test_every_cash_engine_money_column_is_numeric_not_float(engine: Engine) -> None:
    """§3 rule 1, asserted across all five tables at once rather than column by column.

    A `double precision` here would not crash anything -- it would quietly make
    `expected_closing` disagree with its own components by fractions of a paisa that
    compound, which is the failure mode §3 rule 1 exists to prevent.
    """
    with engine.connect() as connection:
        wrong = connection.execute(
            text(
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_name IN ('non_fuel_sales', 'bank_deposits', "
                "    'salesman_shortfalls', 'salesman_shortfall_settlements', "
                "    'daily_cash_summaries') "
                "  AND data_type NOT IN ('numeric', 'uuid', 'text', 'boolean', "
                "    'date', 'timestamp with time zone', 'USER-DEFINED')"
            )
        ).all()

    assert wrong == []


@pytest.mark.parametrize(
    "table",
    [
        "non_fuel_sales",
        "bank_deposits",
        "salesman_shortfalls",
        "salesman_shortfall_settlements",
    ],
)
def test_every_new_cash_table_carries_the_full_reversal_quartet(
    engine: Engine, table: str
) -> None:
    """§6.9's shape, all four parts. Phase 9 shipped a quantity CHECK that was written four
    lines below an already sign-aware rule and still ignored reversals, so "it looks like
    the others" is not evidence -- the names are checked directly."""
    with engine.connect() as connection:
        names = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT c.conname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "WHERE t.relname = :t"
                ).bindparams(t=table)
            )
        }

    assert f"uq_{table}_reverses_id" in names
    assert f"ck_{table}_reversal_has_reason" in names
    assert f"ck_{table}_amount_sign" in names
    assert f"ck_{table}_reversal_not_self" in names


@pytest.mark.parametrize(
    "table",
    [
        "non_fuel_sales",
        "bank_deposits",
        "salesman_shortfalls",
        "salesman_shortfall_settlements",
    ],
)
def test_the_cash_engine_sign_rule_is_strict(engine: Engine, table: str) -> None:
    """Strict `> 0`, matching `expenses` and `credit_sales` rather than `collections`' `>=`.

    §6.8 makes a ₹0 *cash collection* a genuine declaration -- zero as an answer, not as an
    omission. None of these four tables has that property: a ₹0 deposit, non-fuel sale,
    shortfall or settlement records nothing at all.
    """
    with engine.connect() as connection:
        definition = connection.execute(
            text(
                "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = :t AND c.conname = :n"
            ).bindparams(t=table, n=f"ck_{table}_amount_sign")
        ).scalar_one()

    assert ">= (0)" not in definition
    assert "<= (0)" not in definition
    assert "> (0)" in definition
    assert "< (0)" in definition


def test_a_finalised_summary_must_name_who_finalised_it(
    engine: Engine, make_user, make_daily_summary
) -> None:
    """A day cannot read as finalised with nobody accountable for having finalised it."""
    admin = make_user("admin")
    summary_id = make_daily_summary(business_date=date(2027, 3, 3), created_by=admin)

    with engine.begin() as connection, pytest.raises(IntegrityError) as exc:
        connection.execute(
            text(
                "UPDATE daily_cash_summaries SET is_finalised = true WHERE id = :id"
            ).bindparams(id=summary_id)
        )

    assert "ck_daily_cash_summaries_finalised_has_actor" in str(exc.value)


def test_a_review_flag_must_carry_a_note(
    engine: Engine, make_user, make_daily_summary
) -> None:
    """§13.16's flag mirrors `nozzle_readings`': a flag with no note is one nobody can act
    on, because the question it was raising was never written down."""
    admin = make_user("admin")
    summary_id = make_daily_summary(business_date=date(2027, 3, 4), created_by=admin)

    with engine.begin() as connection, pytest.raises(IntegrityError) as exc:
        connection.execute(
            text(
                "UPDATE daily_cash_summaries SET requires_review = true WHERE id = :id"
            ).bindparams(id=summary_id)
        )

    assert "ck_daily_cash_summaries_review_has_note" in str(exc.value)


# --- Phase 11: the audit log's read path (0014) --------------------------------


def test_the_audit_read_path_has_an_index(engine: Engine) -> None:
    """`GET /audit-logs` is outlet-scoped and sorted `(changed_at, id)` DESC (§5.3, §9).

    Neither index from 0004 serves that query: `ix_audit_logs_record` is keyed on
    `(table_name, record_id)` and cannot filter by outlet, and `ix_audit_logs_changed_at`
    cannot either. Without 0014 the default read is a sequential scan plus a sort over the
    fastest-growing table in the schema -- §13.17 records that nothing ever removes a row.

    The two older indexes are asserted alongside it, so dropping one to "tidy up" fails here
    rather than in production six months later.
    """
    with engine.connect() as connection:
        indexes = {
            row[0]
            for row in connection.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'audit_logs'")
            )
        }

    assert "ix_audit_logs_outlet_changed_at" in indexes
    assert "ix_audit_logs_record" in indexes
    assert "ix_audit_logs_changed_at" in indexes


def test_the_audit_read_index_is_ascending_and_not_an_expression(
    engine: Engine,
) -> None:
    """Ascending on purpose, and this pins the reasoning in 0014's docstring.

    A btree scans backwards as cheaply as forwards, so a uniformly-DESC sort needs no DESC
    index -- that only matters for a *mixed* ordering. Expressing DESC would require
    `sa.text("changed_at DESC")` in the model's `__table_args__`, turning a plain column index
    into an expression index that autogenerate cannot reliably compare, so `alembic check`
    would report drift that is not real on every future phase.

    `id` is the third column because the keyset predicate is a row comparison
    `(changed_at, id) < (:last, :last_id)`; including the tiebreaker keeps the seek in the
    index instead of filtering after it.
    """
    with engine.connect() as connection:
        definition = connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'audit_logs' "
                "AND indexname = 'ix_audit_logs_outlet_changed_at'"
            )
        ).scalar_one()

    assert "(outlet_id, changed_at, id)" in definition
    assert "DESC" not in definition


# --- Phase 18: a password containing '%' must survive alembic.Config ------------------
#
# Found on a real deployment, not in review. The pre-deploy migration crashed with
#
#     ValueError: invalid interpolation syntax in
#     'postgresql+psycopg://postgres:...%25HA%40M...' at position 32
#
# alembic.Config wraps configparser, where '%' is the interpolation escape character, so
# set_main_option() treats a percent-encoded password as a broken substitution and raises
# before a single migration runs. The outlet's real Supabase password contains '%'.
#
# The fix is set_section_option's raw sibling: write into the underlying ConfigParser with
# the value escaped as '%%'. Both callers -- alembic/env.py and tests/conftest.py -- go
# through app.db.alembic_url.set_alembic_url so the escaping cannot be applied in one place
# and forgotten in the other, which is how this class of bug survives (CLAUDE.md §6.9's
# argument about _CONSTRAINT_ERRORS being forgotten twice).


def test_set_alembic_url_accepts_a_password_containing_percent() -> None:
    """The regression. A '%' in the password must not raise, and must round-trip."""
    from app.db.alembic_url import set_alembic_url

    # The exact shape that failed in production: percent-encoded reserved characters.
    url = "postgresql+psycopg://postgres:Y2%5C9%5C05%24D%25HA%40M%26I%24JA@db.example.co:5432/postgres"

    config = Config("alembic.ini")
    set_alembic_url(config, url)

    # Round-trips byte for byte -- the escaping is an artefact of configparser's storage,
    # never something a caller has to know about or undo.
    assert config.get_main_option("sqlalchemy.url") == url


def test_set_alembic_url_handles_a_password_with_no_percent() -> None:
    """The ordinary case still works -- the escape must not corrupt a plain password."""
    from app.db.alembic_url import set_alembic_url

    url = "postgresql+psycopg://user:plainpassword@localhost:5432/db"

    config = Config("alembic.ini")
    set_alembic_url(config, url)

    assert config.get_main_option("sqlalchemy.url") == url
