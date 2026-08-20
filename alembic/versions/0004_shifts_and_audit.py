"""shifts, shift templates and the audit log

Revision ID: 0004
Revises: 0003
Create Date: Phase 4 -- the spine

`shifts` is the row every later phase hangs off: readings, collections, expenses, credit
sales and deposits all carry a `shift_id`. Its shape is therefore the most expensive
decision left in the project, which is why CLAUDE.md gained §4.7 before this file existed.

Two departures from the spec as originally written, both explained in §4.7:

* **No `shift_type`.** The original `morning | night` enum described a station this outlet
  does not run -- it trades 06:00-22:00 and has one accounting period a day -- and was not
  general enough for the 24-hour outlets this software will also serve. Shifts are
  sequence-numbered per business date instead, any number of them.
* **`audit_logs` arrives here, not in Phase 11.** §5.2 requires a backwards status
  transition to be audit-logged and §6.8 lets an admin reopen a shift, so Phase 4 is the
  first phase that cannot be correct without it. CLAUDE.md §11 explicitly invited this.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.config import get_settings

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


# create_type=False, then an explicit .create()/.drop(), for the same reason as 0002 and
# 0003: op.create_table() emits CREATE TYPE implicitly on the way up but op.drop_table()
# does NOT drop it on the way down, and the leaked type makes the second downgrade fail
# forever with "type already exists" -- which breaks tests/conftest.py's session teardown.
#
# Names follow the established <table_singular>_<column> convention.
shift_status_enum = postgresql.ENUM(
    "open", "closed", "locked", name="shift_status", create_type=False
)
audit_action_enum = postgresql.ENUM(
    "insert", "update", "reversal", "status_change",
    name="audit_log_action",
    create_type=False,
)


def upgrade() -> None:
    shift_status_enum.create(op.get_bind(), checkfirst=True)
    audit_action_enum.create(op.get_bind(), checkfirst=True)

    # --- outlet_shift_templates -------------------------------------------
    # The shifts an outlet *usually* runs, supplying default start/end times when one is
    # opened. Days are typed in after the fact (§4.7), so without this somebody retypes
    # 06:00 and 22:00 every single morning -- and a rushed retype of a field that decides
    # which day's fuel rate applies (§6.3) is exactly where a wrong hour comes from.
    #
    # The defaults are materialised onto the shift row at creation and never read back
    # through this table. Editing a template must not revalue a shift that already
    # happened.
    op.create_table(
        "outlet_shift_templates",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("outlet_id", sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False),
        sa.Column("sequence", sa.SmallInteger(), nullable=False),
        # Display only. Nothing keys off this -- `sequence` is the identity.
        sa.Column("label", sa.Text(), nullable=False),
        # TIME, not TIMESTAMPTZ, and this is NOT a breach of §3 rule 4. That rule governs
        # *instants*, which must be TIMESTAMPTZ in UTC. "06:00 local, every day" is a
        # recurring wall-clock time -- a genuinely different type that cannot be stored as
        # an instant without inventing a date to attach it to.
        sa.Column("starts_at_local", sa.Time(), nullable=False),
        sa.Column("ends_at_local", sa.Time(), nullable=False),
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
        sa.UniqueConstraint(
            "outlet_id", "sequence", name="uq_outlet_shift_templates_outlet_sequence"
        ),
        sa.CheckConstraint(
            "sequence >= 1", name="ck_outlet_shift_templates_sequence_positive"
        ),
    )

    # --- shifts ------------------------------------------------------------
    op.create_table(
        "shifts",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # §5.0: not derivable from anything else in the row -- nothing in a shift says
        # which pump it belonged to -- so it must exist from birth. There is no correct
        # backfill later, only a guess.
        sa.Column("outlet_id", sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False),
        # §6.1: explicit, never date(created_at). Two independent reasons here -- a
        # 24-hour outlet's night shift crosses midnight, and this outlet types the whole
        # day in after the fact, so created_at is frequently the *following* day.
        sa.Column("business_date", sa.Date(), nullable=False),
        # §4.7: replaces the old morning|night enum. Server-assigned, never client-supplied.
        sa.Column("sequence", sa.SmallInteger(), nullable=False),
        # NOT NULL: §6.3 values the whole shift's fuel at the rate effective at
        # started_at, so a shift with no start cannot be priced at all. A nullable column
        # would surface as a crash inside the sales math rather than a refusal at entry.
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # The one person accountable for this shift's cash. Other staff may be on duty --
        # this outlet runs a crew of three -- but exactly one name carries the drawer, and
        # a shortfall is booked against it.
        sa.Column(
            "attendant_id", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=False
        ),
        sa.Column(
            "status", shift_status_enum, nullable=False, server_default=sa.text("'open'")
        ),
        sa.Column(
            "closed_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "locked_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
        sa.Column("locked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
        # Outlet-scoped per §5.0. This is the guard that stops the same shift being
        # entered twice, which matters more here than under the old shift_type constraint
        # because the sequence is assigned by the server from a read of this same table.
        sa.UniqueConstraint(
            "outlet_id", "business_date", "sequence", name="uq_shifts_outlet_date_sequence"
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_shifts_sequence_positive"),
        # A shift that ended before it started is nonsense, and it would make every
        # duration-based guard (notably §6.2's flow-rate ceiling) compute a negative
        # window. Enforced in the database as well as the API: belt and braces (§6.6).
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at > started_at",
            name="ck_shifts_ended_after_started",
        ),
    )

    # The chain lookup (§4.7): "the latest shift at this outlet", run on every shift open
    # and, from Phase 5, on every reading entry. DESC to match the query's ORDER BY so the
    # planner can walk the index backwards-free.
    op.create_index(
        "ix_shifts_chain",
        "shifts",
        ["outlet_id", sa.text("business_date DESC"), sa.text("sequence DESC")],
    )
    # "Which shifts are mine" (§8's attendant read scope) and "is anything open here".
    op.create_index("ix_shifts_attendant", "shifts", ["attendant_id"])
    op.create_index("ix_shifts_outlet_status", "shifts", ["outlet_id", "status"])

    # --- audit_logs --------------------------------------------------------
    # §5.3. created_by/updated_at columns are change *tracking*: they say who last touched
    # a row, not what it was before or how many times it changed. For a cash system that
    # is not an audit trail, and this table is required in addition to them.
    op.create_table(
        "audit_logs",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # §5.0: no parent row to derive tenancy from, so it carries its own.
        sa.Column("outlet_id", sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False),
        sa.Column("table_name", sa.Text(), nullable=False),
        # Deliberately NOT a foreign key. It points at rows in many different tables, and
        # a real FK cannot express that. It also has to survive its target being renamed
        # or restructured -- an audit trail that breaks when the schema moves is useless
        # exactly when you need it.
        sa.Column("record_id", sa.UUID(), nullable=False),
        sa.Column("action", audit_action_enum, nullable=False),
        sa.Column(
            "changed_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=False
        ),
        sa.Column(
            "changed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("old_values", postgresql.JSONB(), nullable=True),
        sa.Column("new_values", postgresql.JSONB(), nullable=True),
        # §5.3: correlates an audit entry with the application log lines for the same
        # request. Text rather than UUID because RequestIdMiddleware honours an inbound
        # X-Request-ID header, and a caller can send anything.
        sa.Column("request_id", sa.Text(), nullable=False),
    )
    # No created_at/created_by on this table: changed_at and changed_by are the same
    # facts under their domain names, and duplicating them invites the two to disagree.

    # "What happened to this row" -- the question this table exists to answer.
    op.create_index("ix_audit_logs_record", "audit_logs", ["table_name", "record_id"])
    op.create_index("ix_audit_logs_changed_at", "audit_logs", ["changed_at"])

    # §5.3: "append-only. No updates. No deletes. Ever." Reuses reject_modification(),
    # created by migration 0003 -- do not redefine it here, and do not drop it in this
    # file's downgrade, because fuel_prices and fuel_margins still depend on it.
    #
    # Escape hatch, same as the price tables:
    #   ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only;
    op.execute(
        """
        CREATE TRIGGER trg_audit_logs_append_only
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION reject_modification();
        """
    )

    # --- seed this outlet's shift template ---------------------------------
    # 0003 deliberately seeded no nozzles, prices or margins, because a plausible guess in
    # a money table looks exactly like data. This row is different in kind: 06:00-22:00 is
    # a confirmed fact about how the outlet trades (§4.7), not an invented figure, and it
    # is editable through the API. Getting it wrong is visible immediately on the first
    # shift opened; getting a seeded price wrong is invisible for months.
    settings = get_settings()
    op.execute(
        sa.text(
            "INSERT INTO outlet_shift_templates "
            "(outlet_id, sequence, label, starts_at_local, ends_at_local) "
            "VALUES (:outlet_id, 1, 'Day', "
            "CAST(:starts AS time), CAST(:ends AS time))"
        ).bindparams(
            outlet_id=settings.DEFAULT_OUTLET_ID,
            starts="06:00:00",
            ends="22:00:00",
        )
    )


def downgrade() -> None:
    # The trigger goes with its table. reject_modification() does NOT -- it belongs to
    # 0003 and fuel_prices/fuel_margins still use it.
    op.drop_table("audit_logs")
    op.drop_table("shifts")
    op.drop_table("outlet_shift_templates")
    # Dropped last and explicitly: op.drop_table() leaves the types behind. See the
    # comment on shift_status_enum.
    audit_action_enum.drop(op.get_bind(), checkfirst=True)
    shift_status_enum.drop(op.get_bind(), checkfirst=True)
