"""Salesman cash shortfalls, and settling them (CLAUDE.md §5.2, §6.4, §13.14, §14).

Phase 10. Two tables in one module for the reason `credit.py` gives about its three: a
shortfall and a settlement only mean anything against the salesman whose balance they move.

## Why this is not a credit sale

§14 forbids booking a shortfall as a `credit_sale`, and the reasons compound:

* A shortfall is the **outcome of a reconciliation**, not a sale. Nothing was dispensed.
* `credit_sales.attachment_id` is `NOT NULL` and there is no receipt for a shortfall -- there
  is nothing to photograph. Satisfying that column would mean inventing evidence.
* It would mix staff debt into a real customer's outstanding balance, after which nobody can
  answer "what does this customer owe me" again. That figure is the one §14 says the owner
  checks first.

So `salesman_id` is a foreign key to **`user_profiles`**. A "credit customer" standing for a
salesman is the anti-pattern, not the shortcut.

## The shape is credit_sales / credit_repayments, on purpose

    outstanding(salesman) = SUM(salesman_shortfalls.amount)
                          - SUM(salesman_shortfall_settlements.amount)

Over **every** row, reversals included -- they carry negative amounts and net out. Computed,
never stored: §6.6's rule and the argument that deleted `credit_sales.is_settled` transfer
without modification, and §14's guardrail against a denormalised running total covers this
table too.

A settlement points at the **salesman**, not at a particular shortfall, for §5.2's reason: one
payment covering part of three debts has no honest per-row answer.

## The system computes the gap; a human books it

§4.7's argument carries more weight here than anywhere else in the document, because here the
debt is explicit and has a name on it: *"an assumed opening converts theft into a debt owed by
someone who did nothing wrong."* A ₹500 gap is more often a mistyped reading, a forgotten UPI
figure or an unrecorded udhaar slip than it is theft, and software must not be the thing that
decides which. So nothing here is written at shift close. A manager books it, with a reason.

`computed_gap` and `amount` are both stored and may differ -- the manager may know part of a
gap is a slip already corrected. The API warns on a divergence and writes anyway, for the
reason §6.8 gives about close preconditions: refusing a human's judgement sends the correction
somewhere the system cannot see it.

## No `mode` on settlements

Every settlement is cash and every one enters §6.4 (§5.2, the owner's answer). §13.15 records
the consequence: until a `mode` column exists there is no way to write off a ₹20 gap nobody
will ever chase, so a salesman's balance only ever grows.

No `outlet_id` on either table -- derivable via `shift_id -> shifts.outlet_id` (§5.0). No
`relationship()` anywhere, consistent with the rest of app/models/.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SalesmanShortfall(Base):
    """What a salesman owes because his shift's drawer came up short."""

    __tablename__ = "salesman_shortfalls"
    __table_args__ = (
        sa.UniqueConstraint("reverses_id", name="uq_salesman_shortfalls_reverses_id"),
        sa.CheckConstraint(
            "reverses_id IS NULL "
            "OR (reversal_reason IS NOT NULL AND reversal_reason ~ '[^[:space:]]')",
            name="ck_salesman_shortfalls_reversal_has_reason",
        ),
        sa.CheckConstraint(
            "(reverses_id IS NULL AND amount > 0) "
            "OR (reverses_id IS NOT NULL AND amount < 0)",
            name="ck_salesman_shortfalls_amount_sign",
        ),
        sa.CheckConstraint(
            "reverses_id IS NULL OR reverses_id <> id",
            name="ck_salesman_shortfalls_reversal_not_self",
        ),
        sa.CheckConstraint(
            "reason ~ '[^[:space:]]'", name="ck_salesman_shortfalls_reason_not_blank"
        ),
        sa.Index("ix_salesman_shortfalls_shift", "shift_id"),
        sa.Index("ix_salesman_shortfalls_salesman", "salesman_id"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    reverses_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("salesman_shortfalls.id"), nullable=True
    )
    reversal_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )
    # The shift whose reconciliation produced this. No outlet_id -- derivable (§5.0).
    shift_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("shifts.id"), nullable=False
    )
    # §13.14: user_profiles, never credit_customers. The API reads this from
    # shifts.attendant_id rather than accepting it from a client -- §5.2 says exactly one name
    # carries the drawer, and a client-supplied value lets a typo put a debt on the wrong
    # person with no second source of truth to catch it.
    salesman_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False)
    # What the system calculated at the moment of booking, kept beside what the human actually
    # booked. §4.7's predict-and-confirm shape: both values stored, so a disagreement is a
    # fact on the row rather than something nobody recorded.
    computed_gap: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False)
    reason: Mapped[str] = mapped_column(sa.Text(), nullable=False)


class SalesmanShortfallSettlement(Base):
    """A salesman paying back what he owed. Always cash -- see the module docstring."""

    __tablename__ = "salesman_shortfall_settlements"
    __table_args__ = (
        sa.UniqueConstraint(
            "reverses_id", name="uq_salesman_shortfall_settlements_reverses_id"
        ),
        sa.CheckConstraint(
            "reverses_id IS NULL "
            "OR (reversal_reason IS NOT NULL AND reversal_reason ~ '[^[:space:]]')",
            name="ck_salesman_shortfall_settlements_reversal_has_reason",
        ),
        sa.CheckConstraint(
            "(reverses_id IS NULL AND amount > 0) "
            "OR (reverses_id IS NOT NULL AND amount < 0)",
            name="ck_salesman_shortfall_settlements_amount_sign",
        ),
        sa.CheckConstraint(
            "reverses_id IS NULL OR reverses_id <> id",
            name="ck_salesman_shortfall_settlements_reversal_not_self",
        ),
        sa.Index("ix_shortfall_settlements_shift", "shift_id"),
        sa.Index("ix_shortfall_settlements_salesman", "salesman_id"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    reverses_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("salesman_shortfall_settlements.id"), nullable=True
    )
    reversal_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )
    # The shift during which the money physically arrived -- not the shift that produced the
    # shortfall, which may be weeks earlier. That is why §6.4 can add it to expected cash for
    # *this* day: it is cash that reached this day's locker.
    shift_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("shifts.id"), nullable=False
    )
    salesman_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False)
