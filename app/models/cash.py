"""The cash engine's tables: non-fuel sales, deposits, and the daily summary
(CLAUDE.md §5.2, §6.4, §6.5).

Phase 10. Three tables in one module because §6.4's equation is the only reason any of them
exists: two supply terms to it and the third records its answer.

## `non_fuel_sales` -- §6.4's term that had no column

§6.4 named `other_cash_income` from the beginning; §5.2 listed a column for it nowhere. Two
decisions are baked into this table's shape and both are easy to get wrong.

**Per shift, not per day.** A ₹500 bottle of oil is in the salesman's hand and *not* in the
meter-derived figure, but it *is* inside the cash he counts into the locker. Compare derived
fuel cash against his declaration without it and he shows a ₹500 **surplus** -- every single
day he sells one, in his name. The figure has to sit beside the declaration it is checked
against, and that is the shift.

**Added to `total_sales`, never to the cash side, and therefore no `mode` column.** §6.4's
worked example: a card-paid oil sale is already inside the card collections total, so putting
it on the cash side understates derived cash by exactly its amount. On the sales side the
arithmetic is right however the customer paid -- which is why this table does not need to
know how they paid.

A table rather than a column on `shifts` because it is a money row, and §6.9 corrects a money
row by appending a reversal. You cannot reverse a column. It is still not the itemised sales
module §12 and §13.2 rule out: an amount and an optional note, no product, no stock, no rate.

## `bank_deposits.business_date` is derivable and stored anyway

Not a breach of §5.0, which governs *tenancy* columns that have no correct backfill. This one
backfills perfectly from `shifts.business_date`. It is stored because a deposit is filed
against a trading day and that is what reports group by. **§3 rule 7 still governs it**: the
server recomputes it from the shift and never accepts it from a client, so the copy cannot
drift from its source.

## `daily_cash_summaries` snapshots every term, not just the total

§5.2's reason for storing `expected_closing` -- *"you still need to know what the system told
the manager on that day"* -- applies term by term. A manager checking a ₹300 variance needs
the breakdown **as it stood**, not as recomputed after a reversal landed underneath it;
otherwise the total and its own explanation disagree, and the explanation is the half he can
actually verify.

`variance` is a generated column (§5.2) and is NULL whenever `actual_counted` is -- which is
most days under §6.5's locker model. That is a real state, "no variance known", and it must
not read as a variance of zero.

`expected_closing` subtracts `shortfalls_booked` (§6.4). Without that term the same ₹500 is an
asset twice: the salesman's debt *and* cash the locker does not hold.

No `relationship()` anywhere, consistent with the rest of app/models/.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Literal labels, create_type=False: the type is owned by migration 0013, not by the model.
# Same convention as collection_mode, expense_mode and credit_repayment_mode.
_opening_balance_source_enum = postgresql.ENUM(
    "seeded",
    "counted",
    "carried",
    name="opening_balance_source",
    create_type=False,
)


def _money(nullable: bool = False) -> Mapped[Decimal]:
    """§3 rule 1: NUMERIC(12,2), never FLOAT. One definition, so no column can drift."""
    return mapped_column(sa.Numeric(12, 2), nullable=nullable)


class NonFuelSale(Base):
    """Lubricants, coolant, a puncture repair -- anything sold that no meter counted."""

    __tablename__ = "non_fuel_sales"
    __table_args__ = (
        sa.UniqueConstraint("reverses_id", name="uq_non_fuel_sales_reverses_id"),
        sa.CheckConstraint(
            "reverses_id IS NULL "
            "OR (reversal_reason IS NOT NULL AND reversal_reason ~ '[^[:space:]]')",
            name="ck_non_fuel_sales_reversal_has_reason",
        ),
        sa.CheckConstraint(
            "(reverses_id IS NULL AND amount > 0) "
            "OR (reverses_id IS NOT NULL AND amount < 0)",
            name="ck_non_fuel_sales_amount_sign",
        ),
        sa.CheckConstraint(
            "reverses_id IS NULL OR reverses_id <> id",
            name="ck_non_fuel_sales_reversal_not_self",
        ),
        sa.Index("ix_non_fuel_sales_shift", "shift_id"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    reverses_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("non_fuel_sales.id"), nullable=True
    )
    reversal_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )
    # No outlet_id -- derivable via shift_id -> shifts.outlet_id (§5.0).
    shift_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("shifts.id"), nullable=False
    )
    amount: Mapped[Decimal] = _money()
    description: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)


class BankDeposit(Base):
    """Cash taken out of the locker and paid into the bank (§6.4)."""

    __tablename__ = "bank_deposits"
    __table_args__ = (
        sa.UniqueConstraint("reverses_id", name="uq_bank_deposits_reverses_id"),
        sa.CheckConstraint(
            "reverses_id IS NULL "
            "OR (reversal_reason IS NOT NULL AND reversal_reason ~ '[^[:space:]]')",
            name="ck_bank_deposits_reversal_has_reason",
        ),
        sa.CheckConstraint(
            "(reverses_id IS NULL AND amount > 0) "
            "OR (reverses_id IS NOT NULL AND amount < 0)",
            name="ck_bank_deposits_amount_sign",
        ),
        sa.CheckConstraint(
            "reverses_id IS NULL OR reverses_id <> id",
            name="ck_bank_deposits_reversal_not_self",
        ),
        sa.Index("ix_bank_deposits_shift", "shift_id"),
        sa.Index("ix_bank_deposits_date", "business_date"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    reverses_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("bank_deposits.id"), nullable=True
    )
    reversal_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )
    shift_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("shifts.id"), nullable=False
    )
    # Set server-side from the shift, never from a client -- see the module docstring.
    business_date: Mapped[date] = mapped_column(sa.Date(), nullable=False)
    amount: Mapped[Decimal] = _money()
    bank_reference: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    # The deposit slip. Nullable -- only credit_sales.attachment_id is ever NOT NULL (§6.6).
    attachment_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("attachments.id"), nullable=True
    )


class DailyCashSummary(Base):
    """One row per outlet per business date: §6.4's answer, as it stood that day."""

    __tablename__ = "daily_cash_summaries"
    __table_args__ = (
        sa.UniqueConstraint(
            "outlet_id", "business_date", name="uq_daily_cash_summaries_outlet_date"
        ),
        sa.CheckConstraint(
            "is_finalised = false "
            "OR (finalised_by IS NOT NULL AND finalised_at IS NOT NULL)",
            name="ck_daily_cash_summaries_finalised_has_actor",
        ),
        sa.CheckConstraint(
            "requires_review = false OR review_note IS NOT NULL",
            name="ck_daily_cash_summaries_review_has_note",
        ),
        sa.Index("ix_daily_cash_summaries_outlet_date", "outlet_id", "business_date"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    # §5.0: carries its own. A summary has no parent shift -- it aggregates all of them.
    outlet_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False
    )
    business_date: Mapped[date] = mapped_column(sa.Date(), nullable=False)
    opening_balance: Mapped[Decimal] = _money()
    # Mapped[str], not Mapped[OpeningBalanceSource]: the driver hands back the raw label and
    # callers convert explicitly, so an unexpected value fails loudly rather than silently
    # comparing unequal to every member. Same choice as Shift.status.
    opening_balance_source: Mapped[str] = mapped_column(
        _opening_balance_source_enum, nullable=False
    )
    expected_closing: Mapped[Decimal] = _money()
    actual_counted: Mapped[Decimal | None] = _money(nullable=True)
    # Generated by the database (§5.2), and NULL whenever actual_counted is. An uncounted day
    # must read as "no variance known", never as a variance of zero.
    variance: Mapped[Decimal | None] = mapped_column(
        sa.Numeric(12, 2),
        sa.Computed("actual_counted - expected_closing", persisted=True),
        nullable=True,
    )

    # --- the component snapshot (§5.2) ---
    # Every term of §6.4, frozen at the moment the summary was computed. Recomputing these on
    # read would let a later reversal change the explanation of a variance without changing
    # the variance, and the explanation is the half a manager can check by hand.
    metered_fuel_sales: Mapped[Decimal] = _money()
    non_fuel_sales_total: Mapped[Decimal] = _money()
    card_total: Mapped[Decimal] = _money()
    upi_total: Mapped[Decimal] = _money()
    wallet_total: Mapped[Decimal] = _money()
    credit_sales_total: Mapped[Decimal] = _money()
    cash_credit_repayments: Mapped[Decimal] = _money()
    # §6.4's twelfth term, Phase 16 -- udhaar settled on the card machine or the UPI QR.
    # Stored like every other component so a manager can see which term moved (§5.2).
    card_upi_credit_repayments: Mapped[Decimal] = _money()
    cash_shortfall_settlements: Mapped[Decimal] = _money()
    cash_expenses: Mapped[Decimal] = _money()
    bank_deposits_total: Mapped[Decimal] = _money()
    # §6.4's newest term. Subtracted, so that a debt booked against a salesman is not also
    # counted as cash sitting in the locker.
    shortfalls_booked: Mapped[Decimal] = _money()

    is_finalised: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.text("false")
    )
    finalised_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )
    finalised_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    # §13.16: a shift reopened beneath a finalised day flags this row. Nothing is recomputed.
    requires_review: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.text("false")
    )
    review_note: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    notes: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )
