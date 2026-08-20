"""expenses -- money paid out during a shift

Revision ID: 0008
Revises: 0007
Create Date: Phase 7 -- what the pump paid out, against Phase 6's what it received

`expenses` closes the second-to-last gap in §6.4's cash equation (only credit repayments,
Phase 9, remain) and lands §6.7's review-flagging control plus the third and final
shift-lifecycle precondition, `UNREVIEWED_EXPENSES_EXIST`.

Two enums, `expense_category` and `expense_mode`, both `create_type=False` with an explicit
`.create()` / `.drop()`, matching `collection_mode` in 0006. `expense_category` has no
`fuel_purchase` and no separate `misc` -- see `CLAUDE.md` §5.2's Phase 7 note for why a
tanker restock is not a cash expense at all, and why two synonymous categories would defeat
§6.7's per-category aggregate. `expense_mode` is its own type rather than sharing
`collection_mode`: an expense can be `bank_transfer`, a collection cannot, and the two
enums revise on independent schedules the same way `fuel_prices` and `fuel_margins` do.

**`ck_expenses_reversal_has_reason` ships with the strengthened check from day one** --
`reversal_reason ~ '[^[:space:]]'`, not NOT NULL alone. 0007 had to retrofit exactly this
onto `collections` after a whitespace-only reason reached the database through the API;
`expenses` does not get to make the same mistake once.

**`ck_expenses_amount_sign` is strict** (`> 0` / `< 0`), unlike `collections`' `>= 0` /
`<= 0`. A ₹0 cash collection is a genuine declaration (§6.8's explicit-zero rule); a ₹0
expense records nothing and has no reason to exist.

**No `UNIQUE (shift_id, category)`.** Unlike `collections`' one-live-row-per-mode rule,
several expenses in one category on one shift are completely normal -- two maintenance
call-outs in a day is not a mistake, and nothing in §5.2 or §6.7 asks for one row per
category.

**No append-only trigger**, same reasoning as `collections` in 0006: an expense is
corrected while its shift is open, which is the normal workflow, and immutability comes
from shift *status* via `require_shift_access(..., writable=True)`.

**No `outlet_id`**, per §5.0: derivable via `shift_id -> shifts.outlet_id`.

**No `attachment_id`.** §5.2 lists one, but `attachments` does not exist until Phase 8 and
§11 forbids scaffolding ahead of a table that isn't built. It lands as a nullable-FK
`ALTER TABLE` in that phase's migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

expense_category_enum = postgresql.ENUM(
    "salary", "maintenance", "electricity", "other",
    name="expense_category", create_type=False,
)
expense_mode_enum = postgresql.ENUM(
    "cash", "card", "upi", "bank_transfer", name="expense_mode", create_type=False
)


def upgrade() -> None:
    expense_category_enum.create(op.get_bind(), checkfirst=True)
    expense_mode_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "expenses",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("shift_id", sa.UUID(), sa.ForeignKey("shifts.id"), nullable=False),
        sa.Column("category", expense_category_enum, nullable=False),
        sa.Column("mode", expense_mode_enum, nullable=False),
        # §3 rule 1. NUMERIC(12,2) and Decimal end to end -- never float.
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("paid_to", sa.Text(), nullable=True),
        # §6.9's reversal pointer, identical shape to collections.reverses_id.
        sa.Column(
            "reverses_id", sa.UUID(), sa.ForeignKey("expenses.id"), nullable=True
        ),
        sa.Column("reversal_reason", sa.Text(), nullable=True),
        # §6.7's review flag, identical shape to nozzle_readings.
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
        sa.UniqueConstraint("reverses_id", name="uq_expenses_reverses_id"),
        sa.CheckConstraint(
            "reverses_id IS NULL "
            "OR (reversal_reason IS NOT NULL AND reversal_reason ~ '[^[:space:]]')",
            name="ck_expenses_reversal_has_reason",
        ),
        sa.CheckConstraint(
            "(reverses_id IS NULL AND amount > 0) "
            "OR (reverses_id IS NOT NULL AND amount < 0)",
            name="ck_expenses_amount_sign",
        ),
        sa.CheckConstraint(
            "reverses_id IS NULL OR reverses_id <> id",
            name="ck_expenses_reversal_not_self",
        ),
        sa.CheckConstraint(
            "description IS NOT NULL "
            r"AND char_length(regexp_replace(description, '\s', '', 'g')) >= 3",
            name="ck_expenses_description_length",
        ),
    )

    op.create_index("ix_expenses_shift", "expenses", ["shift_id"])
    op.create_index("ix_expenses_shift_category", "expenses", ["shift_id", "category"])
    op.create_index(
        "ix_expenses_review",
        "expenses",
        ["requires_review"],
        postgresql_where=sa.text("requires_review"),
    )


def downgrade() -> None:
    op.drop_index("ix_expenses_review", table_name="expenses")
    op.drop_index("ix_expenses_shift_category", table_name="expenses")
    op.drop_index("ix_expenses_shift", table_name="expenses")
    op.drop_table("expenses")
    expense_mode_enum.drop(op.get_bind(), checkfirst=True)
    expense_category_enum.drop(op.get_bind(), checkfirst=True)
