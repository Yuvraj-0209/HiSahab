"""Bank statements -- accounts, imports, transactions, sender aliases (CLAUDE.md §5.3a).

Phase 20. Four tables in one module because they are one subject: an account holds imports,
an import holds transactions, and an alias only means anything against a transaction's
narration.

## What makes these tables different from every other table in this schema

Everything above records something **the outlet asserts** about its own money -- a reading
somebody took, a collection somebody declared, an expense somebody filed. These record what
**the bank says** happened. That one difference drives the three decisions below, and each is
the sort that reads as an oversight unless the reasoning is written down next to it.

## No reversal shape on `bank_transactions` (§6.9)

Every financial table since 0006 carries `reverses_id` / `reversal_reason`. §6.9 governs rows
the business *asserts*: a claim is corrected by appending its negation, so both stay legible
and nobody can quietly rewrite what was once said.

A statement line asserts nothing. It is a copy, and the bank does not revise history.
Correcting a *misclassification* is an ordinary `UPDATE` precisely because **no money moves
when it happens** -- amount, date and narration are untouched, and only our opinion of what
the line *was* changes. The money row a line may produce (`credit_repayments`) already carries
the reversal shape, and that is where a correction with financial meaning belongs.

## `is_expense` is a tri-state and defaults to `undecided`

A debit is either money that left the business or money that moved between the owner's own
pockets. An IOCL top-up is the second -- §12 forbids booking one as an expense because §6.4
would invent a daily cash shortage that never happened, and Phase 21's profit bridge would
understate profit by the whole advance.

**The statement cannot tell them apart**, so a human does. The default is `undecided` rather
than `no` so that "nobody has looked at this yet" stays distinguishable from "somebody decided
it is not a cost" -- §6.8's *zero as an answer, never zero as an omission*, one table further
out. A period total must report how many debits are still undecided rather than treating them
as nil.

The database refuses the nonsensical case: `ck_bank_transactions_only_debits_are_expenses`
means a *credit* can never be flagged an expense, which would be incoming money counted as a
cost and wrong by twice its value.

## `amount` is always positive; `direction` carries the sign

The export has separate `WITHDRAWAL(DR)` and `DEPOSIT(CR)` columns and exactly one is filled,
so the direction is a fact read off the file rather than inferred from a sign. Keeping the
sign in one place means a query can never disagree with itself about what a row meant --
and §14 makes inferring direction from a signed amount a named guardrail, because getting it
backwards puts every repayment in the wrong way round while the ledger still balances.

No `relationship()` anywhere, consistent with the rest of app/models/: column-level foreign
keys only, and callers `flush()` between dependent inserts.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Literal labels and create_type=False, matching credit_repayment_mode in app/models/credit.py
# and collection_mode. sa.Enum(PyEnum) derives labels from Python declaration order, so
# reordering members would produce phantom autogenerate drift against an unchanged database.
_bank_txn_direction_enum = postgresql.ENUM(
    "credit",
    "debit",
    name="bank_txn_direction",
    create_type=False,
)

_bank_txn_classification_enum = postgresql.ENUM(
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

_bank_txn_expense_flag_enum = postgresql.ENUM(
    "yes",
    "no",
    "undecided",
    name="bank_txn_expense_flag",
    create_type=False,
)


def _money(nullable: bool = False) -> Mapped[Decimal]:
    """§3 rule 1: NUMERIC(12,2), never FLOAT. One definition, so no column can drift."""
    return mapped_column(sa.Numeric(12, 2), nullable=nullable)


class BankAccount(Base):
    """An account this outlet holds (§5.3a).

    One row in V1. A table rather than config because a second account is a fact about the
    business, not a deployment setting -- and §5.0's argument is that the cheap moment to get
    a key right is before there is data behind it.
    """

    __tablename__ = "bank_accounts"
    __table_args__ = (
        sa.CheckConstraint("label ~ '[^[:space:]]'", name="ck_bank_accounts_label_not_blank"),
        sa.CheckConstraint(
            "bank_name ~ '[^[:space:]]'", name="ck_bank_accounts_bank_name_not_blank"
        ),
        sa.CheckConstraint(
            "account_number_last4 IS NULL OR account_number_last4 ~ '^[0-9]{4}$'",
            name="ck_bank_accounts_last4_is_four_digits",
        ),
        sa.UniqueConstraint("outlet_id", "label", name="uq_bank_accounts_outlet_label"),
        sa.Index("ix_bank_accounts_outlet", "outlet_id"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    # Carries its own outlet_id: nothing else says which outlet an account belongs to, so
    # there is no correct backfill later, only a guess (§5.0).
    outlet_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False
    )
    label: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    bank_name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    # The last four digits only. The full number is not needed to reconcile anything, and
    # storing it would make this table worth stealing.
    account_number_last4: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )


class BankStatementImport(Base):
    """One uploaded statement file (§5.3a).

    **The file itself is not stored**, and the two balance columns are part of why that is
    acceptable: a statement's information content is its lines plus its endpoints, and both
    are here. Storing the file would mean either widening `ck_attachments_mime_type_allowed`
    -- which exists so the API can never accept what the database refuses -- or building a
    second storage path for one file type. The bank keeps the original anyway.
    """

    __tablename__ = "bank_statement_imports"
    __table_args__ = (
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
        sa.Index("ix_bank_statement_imports_account", "bank_account_id"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    # No outlet_id -- derivable via bank_account_id -> bank_accounts.outlet_id (§5.0).
    bank_account_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("bank_accounts.id"), nullable=False
    )
    period_from: Mapped[date] = mapped_column(sa.Date(), nullable=False)
    period_to: Mapped[date] = mapped_column(sa.Date(), nullable=False)
    # Display label only, exactly like attachments.original_filename: never used to build a
    # path, never trusted (§7.2).
    original_filename: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    row_count: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    imported_count: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    skipped_count: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    # The columns Phase 21's profit bridge cannot proceed without: a bank *balance* exists
    # nowhere else in this schema, since bank_deposits records flows and never a position.
    # Nullable because a statement whose lines carry no running balance is still worth
    # importing -- the lines are the point, the endpoints are a bonus.
    opening_balance: Mapped[Decimal | None] = _money(nullable=True)
    closing_balance: Mapped[Decimal | None] = _money(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )


class BankTransaction(Base):
    """One line of a bank statement -- what the bank says happened (§5.3a).

    No reversal pair, and no `outlet_id`. See the module docstring for both.
    """

    __tablename__ = "bank_transactions"
    __table_args__ = (
        sa.CheckConstraint("amount > 0", name="ck_bank_transactions_amount_positive"),
        sa.CheckConstraint(
            "narration ~ '[^[:space:]]'", name="ck_bank_transactions_narration_not_blank"
        ),
        # A credit flagged as an expense would be incoming money counted as a cost -- wrong
        # by twice its value in Phase 21's bridge.
        sa.CheckConstraint(
            "is_expense = 'undecided' OR direction = 'debit'",
            name="ck_bank_transactions_only_debits_are_expenses",
        ),
        sa.CheckConstraint(
            "matched_business_date IS NULL OR classification = 'paytm_settlement'",
            name="ck_bank_transactions_settled_day_is_a_settlement",
        ),
        sa.CheckConstraint(
            "credit_repayment_id IS NULL OR direction = 'credit'",
            name="ck_bank_transactions_repayment_is_a_credit",
        ),
        sa.UniqueConstraint(
            "bank_account_id", "fingerprint", name="uq_bank_transactions_account_fingerprint"
        ),
        sa.Index("ix_bank_transactions_account_date", "bank_account_id", "txn_date"),
        sa.Index("ix_bank_transactions_classification", "classification"),
        sa.Index("ix_bank_transactions_import", "import_id"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    bank_account_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("bank_accounts.id"), nullable=False
    )
    import_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("bank_statement_imports.id"), nullable=False
    )
    txn_date: Mapped[date] = mapped_column(sa.Date(), nullable=False)
    # Stored verbatim, as the bank wrote it. The matcher normalises a copy; this column is
    # evidence and must not be tidied.
    narration: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    # Always positive -- `direction` carries the sign. See the module docstring.
    amount: Mapped[Decimal] = _money()
    direction: Mapped[str] = mapped_column(_bank_txn_direction_enum, nullable=False)
    # The statement's own balance column, stored as read and never used in arithmetic.
    running_balance: Mapped[Decimal | None] = _money(nullable=True)
    classification: Mapped[str] = mapped_column(
        _bank_txn_classification_enum, nullable=False, server_default="unclassified"
    )
    # For a paytm_settlement, the trading day it settles (T-1). Stored so the rule is a fact
    # on the row rather than re-derived on every read.
    matched_business_date: Mapped[date | None] = mapped_column(sa.Date(), nullable=True)
    is_expense: Mapped[str] = mapped_column(
        _bank_txn_expense_flag_enum, nullable=False, server_default="undecided"
    )
    expense_decided_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )
    expense_decided_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    classified_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )
    classified_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    # The ONLY link from this table to a money row, and the only row this phase writes.
    credit_repayment_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("credit_repayments.id"), nullable=True
    )
    # Reconciliation, never creation: a deposit needs a shift_id, and creating one would move
    # §6.4's expected cash on the strength of an uploaded file (§14).
    matched_bank_deposit_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("bank_deposits.id"), nullable=True
    )
    fingerprint: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )


class BankSenderAlias(Base):
    """A narration fragment a human has confirmed belongs to a credit customer (§5.3a).

    Written **only** when somebody confirms a match, never by the matcher itself. It is a hint
    generator: next month the same fragment pre-selects that customer, and the line still sits
    in the review list waiting for a tick.

    §4.7's rule -- the system predicts, a human confirms -- and the reason a remembered sender
    must never auto-write is the one §4.7 gives: an assumed answer erases the second
    independent observation that made the first one worth having. Here the cost is a payment
    landing on the wrong customer's ledger with a tick beside it saying somebody checked.
    """

    __tablename__ = "bank_sender_aliases"
    __table_args__ = (
        sa.CheckConstraint(
            "fragment ~ '[^[:space:]]'", name="ck_bank_sender_aliases_fragment_not_blank"
        ),
        # Normalised at write time, so the uniqueness below means what it looks like.
        sa.CheckConstraint(
            "fragment = upper(fragment)", name="ck_bank_sender_aliases_fragment_upper"
        ),
        sa.UniqueConstraint(
            "credit_customer_id",
            "fragment",
            name="uq_bank_sender_aliases_customer_fragment",
        ),
        sa.Index("ix_bank_sender_aliases_customer", "credit_customer_id"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    # No outlet_id -- derivable via credit_customer_id (§5.0).
    credit_customer_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("credit_customers.id"), nullable=False
    )
    fragment: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )
