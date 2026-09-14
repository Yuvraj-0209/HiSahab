"""bank statements: accounts, imports, transactions, sender aliases

Revision ID: 0016
Revises: 0015
Create Date: Phase 20 -- the first data in this schema that came from outside it

Every table before this one records something the outlet asserts about its own money: a
reading somebody took, a collection somebody declared, an expense somebody filed. These four
record what *the bank* says happened, and that difference drives almost every decision below.

## Why `bank_transactions` has no reversal shape (§5.3a, §6.9)

Every financial table added since 0006 carries `reverses_id` / `reversal_reason`, so the
absence here reads as an oversight unless it is written down. §6.9 governs **rows the business
asserts** -- a claim is corrected by appending its negation, so both remain legible and nobody
can quietly rewrite what was once said.

A statement line asserts nothing. It is a copy of what the bank did, and the bank does not
revise history. Correcting a *misclassification* is an ordinary `UPDATE` precisely because
**no money moves when it happens**: the amount, date and narration are untouched, and only our
opinion of what the line *was* changes. The money row a line may produce (`credit_repayments`)
already carries the reversal shape, and that is where a correction with financial meaning
belongs.

## `is_expense` is a tri-state, and `undecided` is the default for a reason

A debit is either money that left the business or money that moved between the owner's own
pockets -- an IOCL top-up is the second, and §12 forbids booking one as an expense because
§6.4 would invent a daily cash shortage that never happened.

**The statement cannot tell them apart.** `RTGS-BARBR5...-INDIAN OIL CORPORATION` is
recognisable, but a payment to a supplier is not. So a human decides, and the default is
`undecided` rather than `no` so that "nobody has looked at this yet" stays distinguishable
from "somebody decided it is not a cost". §6.8's *zero as an answer, never zero as an
omission*, one table further out: Phase 21's bank-expense total must report how many debits
are still undecided rather than silently treating them as nil.

## `fingerprint` is a unique constraint, not a service check

Re-uploading the same month must be safe -- the owner will do it, because an overlapping
export is the normal way to catch up. A service-level "have I seen this?" check loses the race
between two concurrent uploads and writes the month twice. A unique index loses it loudly, and
`_CONSTRAINT_ERRORS` turns that into a 409 the caller can act on.

SHA-256 over `(account_id, txn_date, amount, direction, normalised narration)` -- whitespace
collapsed and upper-cased, so a reformatted re-export of the same month still matches.

## `credit_repayments.bank_reference`

The column `bank_deposits` has carried since 0013, arriving on the credit table for the same
purpose: telling two otherwise identical rows apart. Verifying a typed repayment against a
statement can only match on `business_date` + `amount`, and two customers paying 10,000 on one
day are indistinguishable without it. Nullable, because a manager recording a repayment from a
phone call has no UTR to hand and refusing the entry would be worse than an ambiguous match.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


# Literal labels and create_type=False, matching credit_repayment_mode in 0012 and
# collection_mode in 0006. sa.Enum(PyEnum) would derive labels from Python declaration order,
# so reordering the enum members later would produce phantom autogenerate drift against a
# database that never changed.
bank_txn_direction_enum = postgresql.ENUM(
    "credit",
    "debit",
    name="bank_txn_direction",
    create_type=False,
)

bank_txn_classification_enum = postgresql.ENUM(
    "udhaar_repayment",
    "cash_deposit",
    "paytm_settlement",
    "iocl_ms_hsd",
    "iocl_cbg",
    "bank_charge",
    "loan",
    "self_transfer",
    "other",
    "unclassified",
    name="bank_txn_classification",
    create_type=False,
)

bank_txn_expense_flag_enum = postgresql.ENUM(
    "yes",
    "no",
    "undecided",
    name="bank_txn_expense_flag",
    create_type=False,
)


def upgrade() -> None:
    bank_txn_direction_enum.create(op.get_bind(), checkfirst=True)
    bank_txn_classification_enum.create(op.get_bind(), checkfirst=True)
    bank_txn_expense_flag_enum.create(op.get_bind(), checkfirst=True)

    # --- bank_accounts --------------------------------------------------------
    # Carries its own outlet_id: no parent row says which outlet an account belongs to, so
    # there is no correct backfill later, only a guess (§5.0).
    op.create_table(
        "bank_accounts",
        sa.Column(
            "id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("outlet_id", sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("bank_name", sa.Text(), nullable=False),
        # Deliberately the last four digits only. The full number is not needed to reconcile
        # anything, and storing it makes this table worth stealing.
        sa.Column("account_number_last4", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
        sa.CheckConstraint("label ~ '[^[:space:]]'", name="ck_bank_accounts_label_not_blank"),
        sa.CheckConstraint(
            "bank_name ~ '[^[:space:]]'", name="ck_bank_accounts_bank_name_not_blank"
        ),
        sa.CheckConstraint(
            "account_number_last4 IS NULL OR account_number_last4 ~ '^[0-9]{4}$'",
            name="ck_bank_accounts_last4_is_four_digits",
        ),
        sa.UniqueConstraint("outlet_id", "label", name="uq_bank_accounts_outlet_label"),
    )
    op.create_index("ix_bank_accounts_outlet", "bank_accounts", ["outlet_id"])

    # --- bank_statement_imports -----------------------------------------------
    # No outlet_id: derivable via bank_account_id -> bank_accounts.outlet_id (§5.0).
    op.create_table(
        "bank_statement_imports",
        sa.Column(
            "id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "bank_account_id", sa.UUID(), sa.ForeignKey("bank_accounts.id"), nullable=False
        ),
        sa.Column("period_from", sa.Date(), nullable=False),
        sa.Column("period_to", sa.Date(), nullable=False),
        # Display label only, exactly like attachments.original_filename. Never used to build
        # a path and never trusted (§7.2).
        sa.Column("original_filename", sa.Text(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("imported_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        # The two columns Phase 21 cannot proceed without: a bank *balance* exists nowhere
        # else in this schema, since bank_deposits records flows and never a position.
        # Nullable because a statement whose lines carry no running balance can still be
        # imported usefully -- the lines are the point, the endpoints are a bonus.
        sa.Column("opening_balance", sa.Numeric(12, 2), nullable=True),
        sa.Column("closing_balance", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
        sa.CheckConstraint(
            "period_to >= period_from", name="ck_bank_statement_imports_period_ordered"
        ),
        sa.CheckConstraint(
            "row_count >= 0 AND imported_count >= 0 AND skipped_count >= 0",
            name="ck_bank_statement_imports_counts_not_negative",
        ),
        sa.CheckConstraint(
            "imported_count + skipped_count <= row_count",
            name="ck_bank_statement_imports_counts_add_up",
        ),
    )
    op.create_index(
        "ix_bank_statement_imports_account", "bank_statement_imports", ["bank_account_id"]
    )

    # --- bank_transactions ----------------------------------------------------
    # No outlet_id: derivable via bank_account_id (§5.0). No reversal pair: see the module
    # docstring -- this table copies what the bank did rather than asserting anything.
    op.create_table(
        "bank_transactions",
        sa.Column(
            "id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "bank_account_id", sa.UUID(), sa.ForeignKey("bank_accounts.id"), nullable=False
        ),
        sa.Column(
            "import_id",
            sa.UUID(),
            sa.ForeignKey("bank_statement_imports.id"),
            nullable=False,
        ),
        sa.Column("txn_date", sa.Date(), nullable=False),
        # Stored verbatim, exactly as the bank wrote it. The matcher normalises a copy; this
        # column is evidence and must not be tidied.
        sa.Column("narration", sa.Text(), nullable=False),
        # Always positive. `direction` carries the sign -- see the CHECK below and §14's
        # guardrail against inferring direction from a signed amount.
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("direction", bank_txn_direction_enum, nullable=False),
        # The statement's own balance column, stored as read and never used in arithmetic.
        sa.Column("running_balance", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "classification",
            bank_txn_classification_enum,
            nullable=False,
            server_default="unclassified",
        ),
        # For a paytm_settlement, the trading day it settles (T-1). Stored so the rule is a
        # fact on the row rather than re-derived on every read.
        sa.Column("matched_business_date", sa.Date(), nullable=True),
        sa.Column(
            "is_expense",
            bank_txn_expense_flag_enum,
            nullable=False,
            server_default="undecided",
        ),
        sa.Column(
            "expense_decided_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
        sa.Column("expense_decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "classified_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
        sa.Column("classified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # The ONLY link from this table to a money row, and the only row this phase writes.
        sa.Column(
            "credit_repayment_id",
            sa.UUID(),
            sa.ForeignKey("credit_repayments.id"),
            nullable=True,
        ),
        # Reconciliation, never creation: a deposit needs a shift_id and creating one would
        # move §6.4's expected cash on the strength of an uploaded file (§14).
        sa.Column(
            "matched_bank_deposit_id",
            sa.UUID(),
            sa.ForeignKey("bank_deposits.id"),
            nullable=True,
        ),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
        # A zero-value statement line is not a thing a bank writes, and a negative one would
        # mean the sign lives in two places at once.
        sa.CheckConstraint("amount > 0", name="ck_bank_transactions_amount_positive"),
        sa.CheckConstraint(
            "narration ~ '[^[:space:]]'", name="ck_bank_transactions_narration_not_blank"
        ),
        # Only a debit can be an expense. A credit marked `yes` would be incoming money
        # counted as a cost -- wrong by twice its value in Phase 21's bridge.
        sa.CheckConstraint(
            "is_expense = 'undecided' OR direction = 'debit'",
            name="ck_bank_transactions_only_debits_are_expenses",
        ),
        # A settled trading day belongs to a settlement, and to nothing else.
        sa.CheckConstraint(
            "matched_business_date IS NULL OR classification = 'paytm_settlement'",
            name="ck_bank_transactions_settled_day_is_a_settlement",
        ),
        # A produced repayment belongs to a credit. Guards the one write path this phase has.
        sa.CheckConstraint(
            "credit_repayment_id IS NULL OR direction = 'credit'",
            name="ck_bank_transactions_repayment_is_a_credit",
        ),
        sa.UniqueConstraint(
            "bank_account_id", "fingerprint", name="uq_bank_transactions_account_fingerprint"
        ),
    )
    op.create_index(
        "ix_bank_transactions_account_date",
        "bank_transactions",
        ["bank_account_id", "txn_date"],
    )
    op.create_index(
        "ix_bank_transactions_classification", "bank_transactions", ["classification"]
    )
    op.create_index("ix_bank_transactions_import", "bank_transactions", ["import_id"])

    # --- bank_sender_aliases --------------------------------------------------
    # No outlet_id: derivable via credit_customer_id (§5.0). Written only when a human
    # confirms a match -- a hint generator, never a decision (§4.7).
    op.create_table(
        "bank_sender_aliases",
        sa.Column(
            "id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "credit_customer_id",
            sa.UUID(),
            sa.ForeignKey("credit_customers.id"),
            nullable=False,
        ),
        sa.Column("fragment", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
        sa.CheckConstraint(
            "fragment ~ '[^[:space:]]'", name="ck_bank_sender_aliases_fragment_not_blank"
        ),
        # Normalised upper-case at write time so the uniqueness means what it looks like.
        sa.CheckConstraint(
            "fragment = upper(fragment)", name="ck_bank_sender_aliases_fragment_upper"
        ),
        sa.UniqueConstraint(
            "credit_customer_id",
            "fragment",
            name="uq_bank_sender_aliases_customer_fragment",
        ),
    )
    op.create_index(
        "ix_bank_sender_aliases_customer", "bank_sender_aliases", ["credit_customer_id"]
    )

    # --- credit_repayments.bank_reference -------------------------------------
    # The tie-breaker for §13.37's ambiguous verification. Nullable and optional: a manager
    # recording a repayment from a phone call has no UTR, and refusing the entry would be
    # worse than an ambiguous match.
    op.add_column(
        "credit_repayments", sa.Column("bank_reference", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("credit_repayments", "bank_reference")

    op.drop_index("ix_bank_sender_aliases_customer", table_name="bank_sender_aliases")
    op.drop_table("bank_sender_aliases")

    op.drop_index("ix_bank_transactions_import", table_name="bank_transactions")
    op.drop_index("ix_bank_transactions_classification", table_name="bank_transactions")
    op.drop_index("ix_bank_transactions_account_date", table_name="bank_transactions")
    op.drop_table("bank_transactions")

    op.drop_index("ix_bank_statement_imports_account", table_name="bank_statement_imports")
    op.drop_table("bank_statement_imports")

    op.drop_index("ix_bank_accounts_outlet", table_name="bank_accounts")
    op.drop_table("bank_accounts")

    bank_txn_expense_flag_enum.drop(op.get_bind(), checkfirst=True)
    bank_txn_classification_enum.drop(op.get_bind(), checkfirst=True)
    bank_txn_direction_enum.drop(op.get_bind(), checkfirst=True)
