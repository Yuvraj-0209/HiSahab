"""Expenses -- money paid out during a shift (CLAUDE.md §5.2, §6.4, §6.7, §6.9).

A collection is money the pump received; an expense is money it paid out. Both are typed
figures, never derived, and both carry §6.9's reversal shape for the same reason: a
correction after close must append, not edit.

## `mode` decides whether an expense touches the drawer

§6.4 subtracts `cash_expenses` from the equation, and `cash_expenses` means rows with
`mode = cash` and only those (§6.4's amendment). A `card` / `upi` / `bank_transfer` expense
-- the electricity bill paid online, say -- is on the record for reporting but never
subtracted from the drawer, because it never came out of the drawer. Before this column
existed, every expense was implicitly cash, and a bank-paid bill would have invented a
phantom shortfall that this outlet books as udhaar against the salesman's own name (§14).

## §6.7's review flags mirror `nozzle_readings`, not a fresh design

`requires_review` / `reviewed_by` / `reviewed_at` / `review_note`, plus a partial index on
the flag, copied from `app/models/reading.py`. Two rules set it: a single row over
`EXPENSE_REVIEW_THRESHOLD`, or the sum of one category on one `business_date` crossing it --
and the second rule flags every live row in that group, not only the one that crossed the
line, or a manager reviewing a trivial-looking ₹500 row would never see the ₹1,300 pattern
behind it.

## Corrections are appended, never applied (§6.9)

Same shape as `collections`: a reversal is a new row with the negated amount, `reverses_id`
pointing back, and a mandatory, non-blank reason -- enforced at the database, not only by
Pydantic, per §6.6's belt-and-braces rule and the Phase 7 lesson from `collections`' own
`ck_collections_reversal_has_reason` (0007 had to strengthen NOT NULL into a real check
after a whitespace-only reason reached the database through the API).

No `UNIQUE (shift_id, category)` -- unlike `collections`' one-live-row-per-mode rule,
several expenses in one category on one shift are completely normal (two maintenance
call-outs in a day is not a mistake).

No `relationship()` anywhere, consistent with the rest of app/models/: column-level foreign
keys only, and callers `flush()` between dependent inserts.

**No `outlet_id`** (§5.0): derivable via `shift_id -> shifts.outlet_id`.

## Receipts are conditional, not mandatory (§6.11, Phase 8)

`attachment_id` is nullable -- only `credit_sales.attachment_id` is ever `NOT NULL` (§6.6).
`receipt_required` is evaluated once, at insert (`category.requires_receipt OR amount >
EXPENSE_RECEIPT_THRESHOLD`), and **snapshotted**, never read back off the category. Flipping
a category to receipt-required later must not retroactively declare every historical
expense filed under it non-compliant -- the same reasoning §5.2 gives for storing
`expected_closing` rather than recomputing it.

`attachment_id` is immutable once set -- may be supplied at create, or by a `PATCH` while
still `NULL`, never swapped. Enforced in `app/api/v1/expenses.py`, not here: a CHECK cannot
see a row's previous value, only its new one.

`ck_expenses_receipt_required_has_attachment` is the database's half of §6.11:
`reverses_id IS NOT NULL OR receipt_required = false OR attachment_id IS NOT NULL`. A
reversal is exempt -- it cancels a spend, it is not one, and there is nothing to photograph.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Literal labels and create_type=False, matching collection_mode in 0006. sa.Enum(PyEnum)
# would derive the labels from Python declaration order, so reordering the enum members
# would produce phantom autogenerate drift against a database that never changed.
_expense_mode_enum = postgresql.ENUM(
    "cash", "card", "upi", "bank_transfer", name="expense_mode", create_type=False
)


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        sa.UniqueConstraint("reverses_id", name="uq_expenses_reverses_id"),
        sa.CheckConstraint(
            "reverses_id IS NULL "
            "OR (reversal_reason IS NOT NULL AND reversal_reason ~ '[^[:space:]]')",
            name="ck_expenses_reversal_has_reason",
        ),
        # Strict, unlike collections' `>= 0` / `<= 0`. A cash collection of ₹0 is a genuine
        # answer (§6.8's explicit-zero declaration); a ₹0 expense records nothing and has
        # no reason to exist.
        sa.CheckConstraint(
            "(reverses_id IS NULL AND amount > 0) "
            "OR (reverses_id IS NOT NULL AND amount < 0)",
            name="ck_expenses_amount_sign",
        ),
        sa.CheckConstraint(
            "reverses_id IS NULL OR reverses_id <> id",
            name="ck_expenses_reversal_not_self",
        ),
        # Trims only the ENDS, matching the API's `StringConstraints(strip_whitespace=
        # True)` -- not `regexp_replace(..., '\s', '', 'g')` (every whitespace character,
        # anywhere), which 0009 replaced after it rejected a genuinely fine description
        # like "a b": collapsing the internal space left "ab", two characters, and the
        # database refused input the API had already accepted. `btrim` was never an
        # option either -- 0007 found it strips spaces only, not tabs or newlines.
        sa.CheckConstraint(
            "description IS NOT NULL "
            r"AND char_length(regexp_replace(description, '^\s+|\s+$', '', 'g')) >= 3",
            name="ck_expenses_description_length",
        ),
        sa.CheckConstraint(
            "reverses_id IS NOT NULL OR receipt_required = false "
            "OR attachment_id IS NOT NULL",
            name="ck_expenses_receipt_required_has_attachment",
        ),
        sa.Index("ix_expenses_shift", "shift_id"),
        sa.Index("ix_expenses_shift_category", "shift_id", "category_id"),
        sa.Index(
            "ix_expenses_review",
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
    # §5.1: an FK, not an enum, since Phase 8. Adding a category you actually spend on is
    # data entry, not a migration -- and `ck_expense_categories_code_format` is what keeps
    # that from becoming the free text §5.2 warns about.
    category_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("expense_categories.id"), nullable=False
    )
    mode: Mapped[str] = mapped_column(_expense_mode_enum, nullable=False)
    amount: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False)
    description: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    paid_to: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    # §6.11. Nullable -- a receipt was never mandatory on an expense, only on a credit sale
    # (§6.6). Immutable once set: enforced in the API, since a CHECK cannot see a row's
    # previous value.
    attachment_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("attachments.id"), nullable=True
    )
    # §6.11's answer, snapshotted at insert. Never read back off expense_categories when
    # validating or reporting on an existing row -- see the module docstring. The
    # server_default exists only for rows written outside the API (tests, migrations); the
    # API always computes and passes this explicitly.
    receipt_required: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.text("false")
    )
    reverses_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("expenses.id"), nullable=True
    )
    reversal_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
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
