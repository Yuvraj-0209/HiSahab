"""Collections -- money received during a shift (CLAUDE.md §5.2, §6.4, §6.9).

A *sale* is what the meters say (Phase 5, never typed by a human). A *collection* is what
the salesman says arrived. They are different facts and the gap between them is the signal
this system exists to surface, so they are never derived from one another.

## `mode = cash` is a declaration, not a term in §6.4

Read §6.4's equation carefully:

    cash_sales = total_sales − card − upi − wallet − credit_sales_amount

The cash collection row **is not in it**. Cash is derived as the residual. So a `cash` row
is not an input -- it is the **independent observation the derived figure gets checked
against**, which is exactly how this outlet already works (§14: the salesman reconciles his
own shift and a shortfall is booked as udhaar against his own name):

* **derived cash** -- what the meters say he should be holding
* **declared cash** -- this row: what he says he counted into the locker

The gap is the shortfall. Same shape as §4.7's chain: the system predicts, a human
confirms, both values are stored, and a disagreement leaves a trace instead of being
absorbed into somebody's debt.

**Never sum a `cash` row together with derived `cash_sales`.** That double-counts the whole
day's cash, and the answer looks entirely reasonable -- the plausible-but-wrong number
CLAUDE.md was written to prevent.

## Corrections are appended, never applied (§6.9)

A row in a closed shift is never edited. A correction inserts a *new* row carrying the
negated amount, `reverses_id` pointing at the original, and a mandatory reason. Both stay
visible, so "what did the system say on the day" survives the fix.

There is no `UNIQUE (shift_id, mode)` -- §5.2 explains why it cannot exist alongside that
rule, and `app/services/collections.py` carries the one-live-row-per-mode check instead.

No `relationship()` anywhere, consistent with the rest of app/models/: column-level foreign
keys only, and callers `flush()` between dependent inserts.

**No `outlet_id`** (§5.0): derivable via `shift_id -> shifts.outlet_id`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Literal labels and create_type=False, matching 0004's shift_status. sa.Enum(CollectionMode)
# would derive the labels from Python declaration order, so reordering the enum members
# would produce phantom autogenerate drift against a database that never changed.
_collection_mode_enum = postgresql.ENUM(
    "cash", "card", "upi", "wallet", name="collection_mode", create_type=False
)


class Collection(Base):
    __tablename__ = "collections"
    __table_args__ = (
        sa.UniqueConstraint("reverses_id", name="uq_collections_reverses_id"),
        sa.CheckConstraint(
            "reverses_id IS NULL OR reversal_reason IS NOT NULL",
            name="ck_collections_reversal_has_reason",
        ),
        sa.CheckConstraint(
            "(reverses_id IS NULL AND amount >= 0) "
            "OR (reverses_id IS NOT NULL AND amount <= 0)",
            name="ck_collections_amount_sign",
        ),
        sa.CheckConstraint(
            "reverses_id IS NULL OR reverses_id <> id",
            name="ck_collections_reversal_not_self",
        ),
        sa.Index("ix_collections_shift", "shift_id"),
        sa.Index("ix_collections_shift_mode", "shift_id", "mode"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    shift_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("shifts.id"), nullable=False
    )
    # Mapped[str] rather than Mapped[CollectionMode]: the column round-trips the raw label,
    # and callers wrap it in CollectionMode where they need the enum. Same choice as
    # Shift.status, for the same reason -- one vocabulary, no implicit conversion layer.
    mode: Mapped[str] = mapped_column(_collection_mode_enum, nullable=False)
    amount: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False)
    reference: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    reverses_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("collections.id"), nullable=True
    )
    reversal_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )
