"""Nozzle readings -- the source of truth for sales (CLAUDE.md §5.2, §6.2, §4.7).

One row per nozzle per shift. Three numbers go in -- an opening, a closing and a testing
quantity -- and §6.2 turns them into a quantity sold that §6.3 turns into money. Nothing in
this project has a shorter path from a typo to a wrong rupee figure.

No `relationship()` anywhere, consistent with the rest of app/models/: column-level foreign
keys only, and callers `flush()` between dependent inserts because SQLAlchemy has no
dependency edge to work from.

**No `outlet_id`** (§5.0): derivable via `shift_id -> shifts.outlet_id`. Adding it here
would denormalise a value that could drift out of step with its shift.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class NozzleReading(Base):
    __tablename__ = "nozzle_readings"
    __table_args__ = (
        sa.UniqueConstraint(
            "shift_id", "nozzle_id", name="uq_nozzle_readings_shift_nozzle"
        ),
        sa.CheckConstraint(
            "opening_reading >= 0", name="ck_nozzle_readings_opening_non_negative"
        ),
        sa.CheckConstraint(
            "closing_reading IS NULL OR closing_reading >= 0",
            name="ck_nozzle_readings_closing_non_negative",
        ),
        sa.CheckConstraint(
            "testing_quantity >= 0", name="ck_nozzle_readings_testing_non_negative"
        ),
        sa.CheckConstraint(
            "manual_quantity_override IS NULL OR override_reason IS NOT NULL",
            name="ck_nozzle_readings_override_has_reason",
        ),
        # Added in 0006. The API blocks a negative with condecimal(ge=0); the database did
        # not, so a fixture or an import could write one and it would surface as a 500 deep
        # in sales.py rather than a refusal at the boundary -- unlike every sibling column,
        # which has carried a non-negative CHECK since 0005.
        sa.CheckConstraint(
            "manual_quantity_override IS NULL OR manual_quantity_override >= 0",
            name="ck_nozzle_readings_override_non_negative",
        ),
        sa.CheckConstraint(
            "NOT (rollover_occurred AND meter_reset_occurred)",
            name="ck_nozzle_readings_flags_exclusive",
        ),
        sa.CheckConstraint(
            "chained_opening_reading IS NULL "
            "OR opening_reading = chained_opening_reading "
            "OR opening_variance_reason IS NOT NULL",
            name="ck_nozzle_readings_variance_has_reason",
        ),
        sa.Index(
            "ix_nozzle_readings_nozzle_closed",
            "nozzle_id",
            postgresql_where=sa.text("closing_reading IS NOT NULL"),
        ),
        sa.Index("ix_nozzle_readings_shift", "shift_id"),
        sa.Index(
            "ix_nozzle_readings_review",
            "requires_review",
            postgresql_where=sa.text("requires_review"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    shift_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("shifts.id"), nullable=False
    )
    nozzle_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("nozzles.id"), nullable=False
    )
    # The confirmed physical value (§4.7). Pre-filled from the chain, but what is stored
    # here is what a human said the meter actually reads.
    opening_reading: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False)
    # What the chain predicted. NULL = this row anchored the chain (admin-only).
    # See migration 0005 for why the two values are kept side by side rather than one
    # overwriting the other.
    chained_opening_reading: Mapped[Decimal | None] = mapped_column(
        sa.Numeric(12, 2), nullable=True
    )
    opening_variance_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    closing_reading: Mapped[Decimal | None] = mapped_column(
        sa.Numeric(12, 2), nullable=True
    )
    # §4.2. NOT NULL DEFAULT 0, never skipped -- see the migration.
    testing_quantity: Mapped[Decimal] = mapped_column(
        sa.Numeric(10, 3), nullable=False, server_default=sa.text("0")
    )
    rollover_occurred: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.text("false")
    )
    meter_reset_occurred: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.text("false")
    )
    manual_quantity_override: Mapped[Decimal | None] = mapped_column(
        sa.Numeric(10, 3), nullable=True
    )
    override_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    requires_review: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.text("false")
    )
    reviewed_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    review_note: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )
