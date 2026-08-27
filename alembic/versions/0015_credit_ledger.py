"""the credit ledger: opening balances, dated repayments, and §6.4's twelfth term

Revision ID: 0015
Revises: 0014
Create Date: Phase 16 -- written after somebody sat down to enter what customers actually owe

Three changes, and they are three separate defects that happened to surface in one sitting.

## (a) `credit_opening_balances` -- outstanding began at zero for everyone

§6.6 computes outstanding from rows and rightly forbids a stored total because it drifts. The
consequence nobody had stated: the software's ledger begins the day the software does, and
this pump's began years earlier. A customer already owing 12,400 reads as square, his first
repayment drives the balance *negative*, and §6.6's credit limit is checked against a figure
wrong by his entire history.

**One live row per customer, and the uniqueness is NOT a constraint here.** §5.2 works this
through in full for `collections`: a reversed row stays in the table forever, so its
replacement collides on any natural key, and every partial-index variant fails identically
because the replacement also carries `reverses_id IS NULL`. The only escape is a marker
UPDATEd onto the original, which is exactly what §6.9 forbids. So the service refuses a second
live row with 409 OPENING_BALANCE_ALREADY_SET and this migration writes no unique constraint
beyond the reversal one.

**There is deliberately no amount sign CHECK, and this is the one table where §6.9's usual
shape does not apply.** Every other money table carries
`(reverses_id IS NULL AND amount > 0) OR (reverses_id IS NOT NULL AND amount < 0)`. It cannot
be written here: §6.6 already says an outstanding balance may legitimately be negative -- a
customer who paid in advance -- so an original may be negative and a reversal positive, and
the sign carries no information about which is which. `reverses_id` carries it alone. A CHECK
that has to be true and cannot be stated is worse than its absence.

Zero is permitted and is a real statement (§6.8): "I checked Vikram and he was square" is a
different fact from "nobody has entered Vikram yet", and an absent row means the latter.

No `outlet_id` -- derivable via `credit_customer_id -> credit_customers.outlet_id`, exactly as
`credit_sales` derives via `shift_id`. §5.0's rule.

## (b) `credit_repayments` learns to be dated

`shift_id` was NOT NULL, and §5.2 says nothing referencing a `locked` shift may be modified.
So a customer settling by bank transfer on a day already locked could not be recorded **at
all**, and a transfer arriving on a day the outlet was shut had no shift to attach to. §4.7
says the whole day is typed in after the fact, which makes reconstructing a ledger the normal
case here -- and it was blocked by the very shifts it described.

The rule that resolves it is one sentence (§5.2): **a repayment with a shift arrived at the
pump; a repayment with only a date arrived at the bank.** Every §6.4 sum is already
`WHERE shift_id = :shift_id`, so the presence of a shift carries the whole meaning and no
second column is needed. Cash is the one mode that cannot be shift-less, because cash can only
land in a drawer -- hence `ck_credit_repayments_cash_needs_shift`, at the database as well as
in the API, §6.6's belt-and-braces habit.

`business_date` is derivable from the shift and stored anyway, for the reason
`bank_deposits.business_date` is: it is the most-queried column on the table and reading it
through a join means it cannot be indexed directly. §3 rule 7 still holds -- when a shift is
supplied the server takes the date from `shifts.business_date` and refuses the client's.

## (c) `daily_cash_summaries.card_upi_credit_repayments` -- §6.4's twelfth term

Confirmed with the owner that customers settle udhaar on the card machine. That settlement
sits inside `card_collections`, which §6.4 subtracts, and nothing put it back: it is not a
metered sale and `cash_credit_repayments` filters to `mode = 'cash'`. So `accountable` came
out low by exactly the settlement, and since `gap = accountable - declared` the salesman read
as holding a **surplus** nobody gave him. §5.2 requires every term of the equation to be
stored, so the snapshot gains a twelfth column.

**Existing rows are flagged, never recomputed** (§13.32). This migration computes each
summary's real figure into the new column and, where it is non-zero, sets `requires_review`
with a note -- and leaves `expected_closing` and every other stored component byte-identical.
That is §13.16's rule applied to a bug fix rather than to a reopened shift, and the reason is
§5.2's: a reader has to be able to see what the manager was told on the day, including on the
days the system was wrong. §6.5 chains days, so recomputing one would silently move every
opening balance after it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------- (a) opening balances
    op.create_table(
        "credit_opening_balances",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "credit_customer_id",
            sa.UUID(),
            sa.ForeignKey("credit_customers.id"),
            nullable=False,
        ),
        # The ledger begins here. Everything before this date is inside `amount`, which is why
        # an entry dated earlier is refused (BEFORE_OPENING_BALANCE_DATE) -- it would be
        # counted twice.
        sa.Column("as_of_date", sa.Date(), nullable=False),
        # No sign CHECK. See the module docstring -- §6.6 permits a negative balance, so the
        # sign cannot distinguish an original from a reversal here.
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "reverses_id",
            sa.UUID(),
            sa.ForeignKey("credit_opening_balances.id"),
            nullable=True,
        ),
        sa.Column("reversal_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
        sa.UniqueConstraint("reverses_id", name="uq_credit_opening_balances_reverses_id"),
        sa.CheckConstraint(
            "reverses_id IS NULL "
            "OR (reversal_reason IS NOT NULL AND reversal_reason ~ '[^[:space:]]')",
            name="ck_credit_opening_balances_reversal_has_reason",
        ),
        sa.CheckConstraint(
            "reverses_id IS NULL OR reverses_id <> id",
            name="ck_credit_opening_balances_reversal_not_self",
        ),
    )
    op.create_index(
        "ix_credit_opening_balances_customer",
        "credit_opening_balances",
        ["credit_customer_id"],
    )

    # ---------------------------------------------------------------- (b) dated repayments
    op.add_column(
        "credit_repayments", sa.Column("business_date", sa.Date(), nullable=True)
    )
    # Every existing row has a shift -- the column was NOT NULL until this migration -- so the
    # backfill is total and the SET NOT NULL below cannot fail on real data.
    op.execute(
        """
        UPDATE credit_repayments r
           SET business_date = s.business_date
          FROM shifts s
         WHERE s.id = r.shift_id
        """
    )
    op.alter_column("credit_repayments", "business_date", nullable=False)
    op.alter_column("credit_repayments", "shift_id", nullable=True)
    op.create_check_constraint(
        "ck_credit_repayments_cash_needs_shift",
        "credit_repayments",
        "mode <> 'cash' OR shift_id IS NOT NULL",
    )
    op.create_index(
        "ix_credit_repayments_business_date", "credit_repayments", ["business_date"]
    )

    # ---------------------------------------------------------------- (c) the twelfth term
    # A server_default is required to add a NOT NULL column to a populated table, and is
    # dropped immediately after: §5.2's snapshot columns are answers, never omissions, and a
    # default would let a future insert forget one silently.
    op.add_column(
        "daily_cash_summaries",
        sa.Column(
            "card_upi_credit_repayments",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("'0.00'"),
        ),
    )
    op.alter_column(
        "daily_cash_summaries", "card_upi_credit_repayments", server_default=None
    )

    # Compute the real figure for existing rows -- but do NOT touch `expected_closing`.
    # §13.32: the fix corrects the arithmetic from here forward and flags what it cannot
    # safely change. §6.5 chains days, so a recomputed total would not stay local.
    op.execute(
        """
        UPDATE daily_cash_summaries d
           SET card_upi_credit_repayments = COALESCE(t.total, 0.00)
          FROM (
                SELECT s.outlet_id,
                       s.business_date,
                       SUM(r.amount) AS total
                  FROM credit_repayments r
                  JOIN shifts s ON s.id = r.shift_id
                 WHERE r.mode IN ('card', 'upi')
                 GROUP BY s.outlet_id, s.business_date
               ) t
         WHERE t.outlet_id = d.outlet_id
           AND t.business_date = d.business_date
        """
    )
    op.execute(
        """
        UPDATE daily_cash_summaries
           SET requires_review = true,
               review_note = COALESCE(review_note || E'\\n', '')
                          || 'Migration 0015: this day included card/UPI udhaar repayments '
                          || 'that the cash equation did not account for at the time. The '
                          || 'stored expected_closing is understated by '
                          || card_upi_credit_repayments::text
                          || ' and has deliberately not been recomputed (CLAUDE.md §13.32).'
         WHERE card_upi_credit_repayments <> 0.00
        """
    )


def downgrade() -> None:
    op.drop_column("daily_cash_summaries", "card_upi_credit_repayments")

    op.drop_index("ix_credit_repayments_business_date", table_name="credit_repayments")
    op.drop_constraint(
        "ck_credit_repayments_cash_needs_shift", "credit_repayments", type_="check"
    )
    # A shift-less repayment cannot survive the column becoming NOT NULL again, and there is
    # no honest shift to invent for one. Dropping them is correct on a downgrade -- they are
    # rows the older schema could not represent -- and is loud about it in the log.
    op.execute("DELETE FROM credit_repayments WHERE shift_id IS NULL")
    op.alter_column("credit_repayments", "shift_id", nullable=False)
    op.drop_column("credit_repayments", "business_date")

    op.drop_index(
        "ix_credit_opening_balances_customer", table_name="credit_opening_balances"
    )
    op.drop_table("credit_opening_balances")
