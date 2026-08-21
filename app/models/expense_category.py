"""Expense categories -- what an expense can be filed under (CLAUDE.md §5.1, §6.11).

Phase 8. Replaces the `expense_category` Postgres enum that 0008 shipped, for the reason
§5.1 already gives about `fuel_types`: adding a category you actually spend on is data
entry, not a schema change. An outlet buys tea, or DEF, or pays a borewell bill, and none of
that should need a migration.

## Why this is not free text

§5.2 originally said "keep it an enum, not free text", and the *free text* half of that is
still true. If `Tea`, `tea` and `chai ` can all be entered, §6.7's aggregate rule never
fires -- ₹600 under one spelling plus ₹600 under another never sums to ₹1,200, and a control
that exists to catch structuring silently stops working.

`ck_expense_categories_code_format` is what keeps this a list rather than a text box: codes
match `^[A-Z][A-Z0-9_]*$`, so one category cannot exist twice under two spellings.
`display_name` is where the human-readable form lives, and it is free to change.

## What is immutable, and what is not

`code` is immutable, enforced by the API refusing it on PATCH -- the same rule and the same
reason as `fuel_types.code`: changing it retroactively relabels every expense ever filed
under it, and history stops meaning what it said.

`display_name`, `requires_receipt` and `is_active` all stay editable. `requires_receipt` in
particular *must* -- it is the knob §6.11 exists to give the admin. Editing it is safe only
because the answer is snapshotted onto each expense at insert (`expenses.receipt_required`);
nothing ever reads this column back to judge an expense that already exists.

Retire a category with `is_active = false` rather than deleting it (§3 rule 6). A deactivated
category refuses *new* expenses while historical rows keep pointing at it and keep reporting.

## Outlet-scoped, unlike `fuel_types`

`PETROL` means the same thing at every outlet; "tea" does not. One pump's category list is
not another's, and `requires_receipt` is a control decision an outlet's own admin makes.
Not derivable from anything, so by §5.0's rule the column exists from birth.

No `relationship()`, consistent with the rest of app/models/: column-level foreign keys only.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Shared with app/api/v1/expense_categories.py so the API and the database cannot disagree
# about what a valid code looks like -- the failure mode §6.6 calls belt and braces, and the
# one migration 0009 had to fix when a CHECK and a Pydantic constraint drifted apart.
CODE_PATTERN = r"^[A-Z][A-Z0-9_]*$"


class ExpenseCategory(Base):
    __tablename__ = "expense_categories"
    __table_args__ = (
        sa.UniqueConstraint(
            "outlet_id", "code", name="uq_expense_categories_outlet_code"
        ),
        sa.CheckConstraint(
            "code ~ '^[A-Z][A-Z0-9_]*$'", name="ck_expense_categories_code_format"
        ),
        sa.CheckConstraint(
            "display_name ~ '[^[:space:]]'",
            name="ck_expense_categories_display_name_not_blank",
        ),
        sa.Index("ix_expense_categories_outlet", "outlet_id"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    outlet_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False
    )
    code: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    display_name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    requires_receipt: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.text("false")
    )
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )
