"""reference data: fuel types, nozzles, prices and margins

Revision ID: 0003
Revises: 0002
Create Date: Phase 3 -- reference data

The tables that tell the rest of the system *what* is sold, *through which meter*, at
*what rate*, and at *what margin*. Everything in Phases 5, 10 and 13 reads from here.

Two things in this migration exist because of CLAUDE.md §4.5 and §4.6, and both would be
expensive to retrofit:

* **`fuel_types.unit_of_measure`.** This outlet sells CBG by the kilogram alongside
  petrol and diesel by the litre. A quantity in this system is therefore a *measure*, not
  a volume, and the unit has to be an attribute of the fuel. Without it, every historical
  kilogram would eventually be read as a litre by some future reporting query.
* **`fuel_margins`.** Retail price revisions pass straight through to the purchase
  invoice, so the dealer margin is the constant, not the price gap. That single fact is
  what makes profit computable from the totalizer alone -- no purchase data, no tanker
  intake, no stock. See §4.6.

`fuel_prices` and `fuel_margins` are append-only (§5.1). That is enforced here by a
trigger as well as by the absence of a PATCH endpoint -- the §6.6 "belt and braces"
principle: a client can bypass JavaScript, but not a database constraint.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.config import get_settings

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


# create_type=False for the same reason as 0002's membership_role: op.create_table()
# would otherwise emit CREATE TYPE implicitly on the way up while op.drop_table() does
# NOT drop it on the way down. The leaked type makes the second downgrade fail forever
# with "type already exists", which breaks tests/conftest.py's teardown.
#
# Name follows 0002's convention, <table_singular>_<column>.
unit_of_measure_enum = postgresql.ENUM(
    "litre", "kilogram", name="fuel_type_unit_of_measure", create_type=False
)


def upgrade() -> None:
    unit_of_measure_enum.create(op.get_bind(), checkfirst=True)

    # --- fuel_types --------------------------------------------------------
    # No outlet_id: §5.0's landing schedule marks this as global reference data. A litre
    # is a litre at every outlet.
    op.create_table(
        "fuel_types",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        # §4.5. Immutable once set -- the API refuses to PATCH it -- because changing a
        # unit reinterprets every quantity ever recorded against this fuel.
        sa.Column("unit_of_measure", unit_of_measure_enum, nullable=False),
        # §6.2's sanity ceiling, per fuel rather than one global constant. A petrol
        # nozzle does ~60 L/min; a CBG dispenser does single-digit kg/min. One shared
        # number would either never fire or reject every real petrol sale.
        sa.Column("max_flow_rate_per_minute", sa.Numeric(10, 3), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
        sa.UniqueConstraint("code", name="uq_fuel_types_code"),
        sa.CheckConstraint(
            "max_flow_rate_per_minute > 0", name="ck_fuel_types_max_flow_positive"
        ),
    )

    # --- nozzles -----------------------------------------------------------
    # Dispensers are deliberately not their own table in V1 (§5.1, YAGNI) -- the label
    # groups them well enough for reporting.
    op.create_table(
        "nozzles",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("outlet_id", sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("dispenser_label", sa.Text(), nullable=False),
        sa.Column(
            "fuel_type_id", sa.UUID(), sa.ForeignKey("fuel_types.id"), nullable=False
        ),
        # The rollover ceiling for THIS meter (§4.3). Required, not nullable: §6.2's
        # rollover formula is uncomputable without it, and a null would surface as a
        # crash inside the sales math rather than a refusal at data entry.
        sa.Column("totalizer_max_value", sa.Numeric(12, 2), nullable=False),
        sa.Column("meter_installed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
        # Outlet-scoped, per §5.0: "DU-1/N-1" is a label the next outlet will also use.
        sa.UniqueConstraint("outlet_id", "label", name="uq_nozzles_outlet_label"),
        sa.CheckConstraint(
            "totalizer_max_value > 0", name="ck_nozzles_totalizer_max_positive"
        ),
    )

    # --- fuel_prices and fuel_margins --------------------------------------
    # Structurally identical on purpose. Both are effective-dated, append-only, and read
    # through the same "greatest effective_from <= T" lookup (app/services/pricing.py).
    #
    # They are separate tables rather than two columns on one row because they revise on
    # different schedules: price often (§4.1), margin almost never (§4.6). Sharing a row
    # would force re-entry of an unchanged margin on every price revision, and the first
    # forgotten entry silently nulls that period's profit.
    #
    # Index note: each UniqueConstraint's backing index on
    # (outlet_id, fuel_type_id, effective_from) is exactly the access path the lookup
    # uses -- WHERE outlet_id=? AND fuel_type_id=? AND effective_from <= ?
    # ORDER BY effective_from DESC LIMIT 1. No additional index is needed here.
    for table_name, value_column, check_name in (
        ("fuel_prices", "rate_per_unit", "ck_fuel_prices_rate_positive"),
        ("fuel_margins", "margin_per_unit", "ck_fuel_margins_margin_positive"),
    ):
        op.create_table(
            table_name,
            sa.Column(
                "id",
                sa.UUID(),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            # §5.1: prices are outlet-scoped, not national -- dealers are supplied by
            # different OMCs and rates vary by state and district.
            sa.Column(
                "outlet_id", sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False
            ),
            sa.Column(
                "fuel_type_id", sa.UUID(), sa.ForeignKey("fuel_types.id"), nullable=False
            ),
            # Rupees per litre or per kilogram, depending on the fuel's unit (§4.5).
            # NUMERIC(12,2) and never a float -- §3 rule 1.
            sa.Column(value_column, sa.Numeric(12, 2), nullable=False),
            sa.Column("effective_from", sa.TIMESTAMP(timezone=True), nullable=False),
            # The domain fact: who read the rate off the OMC's message and typed it in.
            # Distinct from created_by, which is change tracking (§5.3). In V1 they hold
            # the same value; they are not the same question.
            sa.Column(
                "entered_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=False
            ),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "created_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
            ),
            sa.UniqueConstraint(
                "outlet_id",
                "fuel_type_id",
                "effective_from",
                name=f"uq_{table_name}_outlet_fuel_effective",
            ),
            sa.CheckConstraint(f"{value_column} > 0", name=check_name),
        )

    # §5.1 says these two tables are never updated and never deleted. The API provides no
    # route that would do either, but that is a promise in application code; this is the
    # same promise in the database, where a rogue script or a psql session also has to
    # keep it. An append-only price history is the only reason §4.1's effective dating
    # means anything -- an UPDATE would rewrite what a shift was valued at months ago.
    #
    # Escape hatch for a future migration that genuinely must touch these rows:
    #   ALTER TABLE fuel_prices DISABLE TRIGGER trg_fuel_prices_append_only;
    op.execute(
        """
        CREATE FUNCTION reject_modification() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'table % is append-only (CLAUDE.md 5.1); insert a new '
                'effective-dated row instead of modifying %',
                TG_TABLE_NAME, TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table_name in ("fuel_prices", "fuel_margins"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_append_only
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION reject_modification();
            """
        )

    # --- seed the fuel types ----------------------------------------------
    # Seeded rather than left to the API because these four are what the outlet sells
    # today, and Phase 5 needs something to attach a nozzle to. Admins can add more
    # (XP-95, Extra Green) through POST /api/v1/fuel-types -- adding a product you sell
    # is data entry, not a schema change.
    #
    # created_by is NULL: system-seeded, the same documented exception as 0001's outlet
    # and 0002's bootstrap admin.
    #
    # NOTHING ELSE IS SEEDED. Nozzles, prices and margins are all real-world figures
    # nobody has supplied yet, and a plausible guess in a money table is worse than an
    # empty one -- it looks like data.
    settings = get_settings()
    litre_flow_rate = settings.MAX_FLOW_RATE_LPM

    fuel_types = [
        ("PETROL", "Petrol", "litre", litre_flow_rate),
        ("DIESEL", "Diesel", "litre", litre_flow_rate),
        ("PREMIUM_PETROL", "Premium Petrol", "litre", litre_flow_rate),
        # 15 kg/min is a DELIBERATELY GENEROUS GUESS, not a measured figure. A typical
        # CBG fast-fill dispenser does 3-8 kg/min. It is set high on purpose: this
        # ceiling exists only to catch a mistyped extra digit (§6.2), so being too
        # generous merely misses a typo, while being too tight rejects real sales. On
        # §14's open-questions list -- confirm the real rate before Phase 5 consumes it.
        ("CBG", "Compressed Bio Gas", "kilogram", 15),
    ]
    for code, display_name, unit, flow_rate in fuel_types:
        op.execute(
            sa.text(
                "INSERT INTO fuel_types "
                "(code, display_name, unit_of_measure, max_flow_rate_per_minute) "
                "VALUES (:code, :display_name, "
                "CAST(:unit AS fuel_type_unit_of_measure), :flow_rate)"
            ).bindparams(
                code=code,
                display_name=display_name,
                unit=unit,
                flow_rate=flow_rate,
            )
        )


def downgrade() -> None:
    # Triggers go with their tables, but the function does not -- drop it explicitly.
    op.drop_table("fuel_margins")
    op.drop_table("fuel_prices")
    op.execute("DROP FUNCTION IF EXISTS reject_modification()")
    op.drop_table("nozzles")
    op.drop_table("fuel_types")
    # Dropped last, and explicitly: fuel_types.unit_of_measure depended on it, and
    # op.drop_table() leaves the type behind. See the comment on unit_of_measure_enum.
    unit_of_measure_enum.drop(op.get_bind(), checkfirst=True)
