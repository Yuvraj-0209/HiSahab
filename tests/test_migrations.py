"""The migration pipeline itself."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
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

    assert version == "0006"


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
