"""nozzle readings -- the source of truth for sales

Revision ID: 0005
Revises: 0004
Create Date: Phase 5 -- where the system starts producing money figures

Everything before this migration was scaffolding: who you are (Phase 2), what you sell and
for how much (Phase 3), which day and shift you are recording (Phase 4). This table holds
the meter numbers, and §6.2 turns them into quantities that §6.3 turns into rupees. Every
later phase -- collections, expenses, credit, the cash engine -- compares itself against
the figure computed from these rows.

**No append-only trigger here, deliberately**, unlike `fuel_prices`, `fuel_margins` and
`audit_logs`. A reading is corrected while its shift is still open -- that is the normal
workflow, not an exception -- and immutability is enforced by shift *status* instead:
`require_shift_access(..., writable=True)` refuses any write to a closed or locked shift
(§6.9). Attaching `reject_modification()` here would make the ordinary act of typing a
closing reading impossible.

**No `outlet_id`**, per §5.0's rule: it is derivable via `shift_id -> shifts.outlet_id`, so
it waits rather than being denormalised into a value that could drift.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nozzle_readings",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("shift_id", sa.UUID(), sa.ForeignKey("shifts.id"), nullable=False),
        sa.Column("nozzle_id", sa.UUID(), sa.ForeignKey("nozzles.id"), nullable=False),
        # §4.7: the **confirmed** physical value. Pre-filled from the chain, but what lands
        # here is what a human said the meter actually reads.
        sa.Column("opening_reading", sa.Numeric(12, 2), nullable=False),
        # What the chain predicted (§4.7). NULL means this row anchored the chain -- there
        # was no predecessor for this nozzle -- and anchoring is admin-only.
        #
        # This column is the whole of §4.7's confirm-don't-assume rule. Without it a
        # confirmed reading and an assumed one are indistinguishable the moment they are
        # written, and a mismatch -- the signal that fuel moved through a nozzle between
        # shifts -- leaves no trace. Fuel siphoned overnight still turns the totalizer; an
        # assumed opening says it did not, so the missing quantity is absorbed into the
        # next shift as sales that produced no cash, the shift comes up short, and this
        # outlet books a shortfall as udhaar against the salesman's own name. The column
        # exists so that theft cannot quietly become an innocent person's debt.
        sa.Column("chained_opening_reading", sa.Numeric(12, 2), nullable=True),
        sa.Column("opening_variance_reason", sa.Text(), nullable=True),
        sa.Column("closing_reading", sa.Numeric(12, 2), nullable=True),
        # §4.2: fuel dispensed into the 5-litre standard measure during calibration testing
        # and poured back into the tank. The totalizer counted it; nobody bought it.
        # Omitting it produces a small, permanent, daily cash shortfall that is extremely
        # hard to diagnose -- CLAUDE.md calls this the single most common bug in home-grown
        # pump software. NUMERIC(10,3) because §3 rule 2 measures to the millilitre / gram.
        sa.Column(
            "testing_quantity",
            sa.Numeric(10, 3),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # §4.3: totalizers roll over like an odometer, and reset to zero when a meter is
        # repaired or replaced. A closing reading below the opening is therefore a genuine
        # real-world event, and these two flags say which of the two it was.
        sa.Column(
            "rollover_occurred",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "meter_reset_occurred",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        # §6.2: after a meter reset the reading pair is meaningless, so an admin states the
        # quantity directly. Never inferred -- there is no honest way to split a reset.
        sa.Column("manual_quantity_override", sa.Numeric(10, 3), nullable=True),
        sa.Column("override_reason", sa.Text(), nullable=True),
        # §4.7's mismatch path and §13.10's downstream flag both land here. Same shape as
        # `expenses.requires_review` (§5.2), on purpose: one review vocabulary, not two.
        sa.Column(
            "requires_review",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "reviewed_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
        sa.Column("reviewed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
        # One reading per nozzle per shift. This constraint is also what gives §6.10 its
        # answer for this table: a retried POST cannot create a second row, so readings are
        # idempotent by construction and need no Idempotency-Key store. Collections in
        # Phase 6 have no such natural key and do.
        sa.UniqueConstraint("shift_id", "nozzle_id", name="uq_nozzle_readings_shift_nozzle"),
        # A totalizer counts up from zero and cannot show a negative. Cheap guard against a
        # sign that arrived from a mistyped minus.
        sa.CheckConstraint("opening_reading >= 0", name="ck_nozzle_readings_opening_non_negative"),
        sa.CheckConstraint(
            "closing_reading IS NULL OR closing_reading >= 0",
            name="ck_nozzle_readings_closing_non_negative",
        ),
        sa.CheckConstraint(
            "testing_quantity >= 0", name="ck_nozzle_readings_testing_non_negative"
        ),
        # §5.2: "required if override is set". In the database as well as the API, because
        # an unexplained manual quantity is an unauditable one.
        sa.CheckConstraint(
            "manual_quantity_override IS NULL OR override_reason IS NOT NULL",
            name="ck_nozzle_readings_override_has_reason",
        ),
        # Mutually exclusive: §6.2 has one formula for a rollover and a different, manual
        # path for a reset. Both flags together has no defined meaning, and silently
        # picking one branch would produce a plausible wrong number.
        sa.CheckConstraint(
            "NOT (rollover_occurred AND meter_reset_occurred)",
            name="ck_nozzle_readings_flags_exclusive",
        ),
        # §4.7's mismatch path, enforced at the database level: if the confirmed opening
        # differs from what the chain predicted, somebody has to say why. Belt and braces
        # (§6.6) -- a client can bypass JavaScript, but not a CHECK constraint.
        sa.CheckConstraint(
            "chained_opening_reading IS NULL "
            "OR opening_reading = chained_opening_reading "
            "OR opening_variance_reason IS NOT NULL",
            name="ck_nozzle_readings_variance_has_reason",
        ),
    )

    # The chain lookup (§4.7): "the most recent closing reading for THIS nozzle", which
    # joins to shifts and orders by (business_date DESC, sequence DESC) using
    # ix_shifts_chain from 0004. This index is the nozzle-side half of that join.
    # Partial on closing_reading IS NOT NULL because an open shift's row has no closing
    # value yet and is never a chain predecessor.
    op.create_index(
        "ix_nozzle_readings_nozzle_closed",
        "nozzle_readings",
        ["nozzle_id"],
        postgresql_where=sa.text("closing_reading IS NOT NULL"),
    )
    # "Give me this shift's worksheet" -- run on every reading page load and at close.
    op.create_index("ix_nozzle_readings_shift", "nozzle_readings", ["shift_id"])
    # "What is waiting for a manager to look at" (§4.7, §13.10). Partial, because the
    # answer is almost always a handful of rows out of the whole table.
    op.create_index(
        "ix_nozzle_readings_review",
        "nozzle_readings",
        ["requires_review"],
        postgresql_where=sa.text("requires_review"),
    )


def downgrade() -> None:
    # No enum and no trigger to unwind -- see the module docstring. The indexes go with
    # the table.
    op.drop_table("nozzle_readings")
