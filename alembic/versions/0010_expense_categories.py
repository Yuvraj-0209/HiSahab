"""expense_categories -- the category enum becomes admin-managed data

Revision ID: 0010
Revises: 0009
Create Date: Phase 8 -- adding a category you actually spend on is data entry

§5.2 originally said "keep it an enum, not free text", and 0008 shipped
`salary | maintenance | electricity | other` as a Postgres enum. §5.1 already rejected that
shape one table up, for `fuel_types`: *"Admins add fuel types through the API, not through a
migration... adding a product you sell is data entry, and requiring a schema change for it
would be wrong."* Every word transfers. An outlet buys tea, or DEF, or pays a borewell bill,
and needing a migration to say so is the same mistake in a different table.

**This is not a retreat to free text**, which is what the original rule guarded against.
Free text lets `Tea`, `tea` and `chai ` become three categories, and §6.7's aggregate then
never fires -- ₹600 under one label plus ₹600 under another never sums to ₹1,200, and the
control silently becomes theatre. `ck_expense_categories_code_format` is what keeps this a
controlled list rather than a text box: codes are `^[A-Z][A-Z0-9_]*$`, so a category cannot
be entered twice under two spellings.

**`code` is immutable** (enforced by the API, not here -- there is no UPDATE trigger on this
table because `display_name`, `requires_receipt` and `is_active` must stay editable). The
reasoning matches `fuel_types.code`: changing it retroactively relabels every expense ever
filed under it.

**Outlet-scoped, unlike `fuel_types`.** `PETROL` means the same thing at every outlet; "tea"
does not. One pump's category list is not another's, and `requires_receipt` is a control
decision an outlet's own admin makes. Not derivable from anything, so by §5.0's rule the
column exists from birth.

**`requires_receipt` ships with the table rather than waiting for 0011.** The plan had it
landing alongside its enforcement, on §11's no-scaffolding-ahead reasoning. §11 governs
*phases*, though, and both migrations are Phase 8 -- splitting it would only mean writing
`app/api/v1/expense_categories.py` twice within one afternoon. It does nothing until 0011
adds `expenses.receipt_required` and the CHECK that reads it; that gap is hours, not phases.

**The downgrade is lossy and says so.** A category an admin created has no enum label to go
back to, so anything outside the original four maps to `other` and prints a warning. That is
honest: downgrade is a development operation, and pretending a custom category survives a
round trip would be worse than losing it loudly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

# Mirrors 0008. Needed here to DROP it on upgrade and RECREATE it on downgrade.
expense_category_enum = postgresql.ENUM(
    "salary", "maintenance", "electricity", "other",
    name="expense_category", create_type=False,
)

# (code, display_name, requires_receipt)
#
# The four 0008 shipped, so the backfill below has somewhere to point. `OTHER` alone
# requires a receipt: it is the bucket for spending nobody anticipated, which is exactly the
# spending that most deserves a piece of paper behind it. The rest are recognisable, recurring
# costs where §6.11's amount threshold is the appropriate control instead.
#
# NOTHING ELSE IS SEEDED. The real list (tea, borewell, whatever this outlet actually buys)
# is on §14's open-questions list and arrives through the API -- which is the entire point of
# this migration.
_SEED = [
    ("SALARY", "Salary", False),
    ("MAINTENANCE", "Maintenance", False),
    ("ELECTRICITY", "Electricity", False),
    ("OTHER", "Other", True),
]


def upgrade() -> None:
    op.create_table(
        "expense_categories",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "outlet_id", sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False
        ),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        # §6.11. Editable -- this is the knob the admin turns. The answer is snapshotted
        # onto each expense at insert (0011's `expenses.receipt_required`), so flipping this
        # never retroactively rewrites whether history complied.
        sa.Column(
            "requires_receipt",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
        sa.UniqueConstraint("outlet_id", "code", name="uq_expense_categories_outlet_code"),
        # What keeps this a controlled list rather than a text box. See the module docstring.
        sa.CheckConstraint(
            "code ~ '^[A-Z][A-Z0-9_]*$'", name="ck_expense_categories_code_format"
        ),
        sa.CheckConstraint(
            "display_name ~ '[^[:space:]]'",
            name="ck_expense_categories_display_name_not_blank",
        ),
    )

    # --- seed, for every outlet that exists -----------------------------------
    # `SELECT ... FROM outlets` rather than a config lookup: V1 has one outlet, but a
    # migration that silently skipped a second one would leave its expenses unbackfillable
    # at the NOT NULL below, and the failure would point at the wrong line.
    #
    # created_by is NULL: system-seeded, the same documented exception as 0001's outlet.
    for code, display_name, requires_receipt in _SEED:
        op.execute(
            sa.text(
                "INSERT INTO expense_categories "
                "(outlet_id, code, display_name, requires_receipt) "
                "SELECT o.id, :code, :display_name, :requires_receipt FROM outlets o"
            ).bindparams(
                code=code,
                display_name=display_name,
                requires_receipt=requires_receipt,
            )
        )

    # --- swap expenses.category for expenses.category_id ----------------------
    op.add_column(
        "expenses",
        sa.Column(
            "category_id",
            sa.UUID(),
            sa.ForeignKey("expense_categories.id"),
            nullable=True,
        ),
    )

    # The outlet comes through `shift_id -> shifts.outlet_id`, per §5.0 -- `expenses` has no
    # `outlet_id` of its own and deliberately does not gain one here.
    op.execute(
        """
        UPDATE expenses AS e
           SET category_id = ec.id
          FROM shifts AS s, expense_categories AS ec
         WHERE e.shift_id = s.id
           AND ec.outlet_id = s.outlet_id
           AND ec.code = upper(e.category::text)
        """
    )

    # Refuse to proceed on a half-mapped table rather than letting SET NOT NULL fail with a
    # message that names a constraint instead of the cause.
    op.execute(
        """
        DO $$
        DECLARE unmapped bigint;
        BEGIN
            SELECT count(*) INTO unmapped FROM expenses WHERE category_id IS NULL;
            IF unmapped > 0 THEN
                RAISE EXCEPTION
                    'migration 0010: % expense row(s) have no matching expense_categories '
                    'row. Every (outlet, category) pair must have been seeded above.',
                    unmapped;
            END IF;
        END $$
        """
    )

    op.alter_column("expenses", "category_id", nullable=False)

    # The index has to go before the column it covers.
    op.drop_index("ix_expenses_shift_category", table_name="expenses")
    op.drop_column("expenses", "category")
    op.create_index(
        "ix_expenses_shift_category", "expenses", ["shift_id", "category_id"]
    )

    # `op.drop_column` does NOT drop the enum type -- the trap 0008's own downgrade
    # documents. Without this the type lingers with no column using it, and a later
    # `CREATE TYPE expense_category` (a downgrade, or a fresh branch) fails as a duplicate.
    expense_category_enum.drop(op.get_bind(), checkfirst=True)

    # Cheap to look up, and the sweep in 0011's cleanup job is not the only reader:
    # `GET /expense-categories` lists per outlet on every form load.
    op.create_index(
        "ix_expense_categories_outlet", "expense_categories", ["outlet_id"]
    )


def downgrade() -> None:
    expense_category_enum.create(op.get_bind(), checkfirst=True)

    op.drop_index("ix_expense_categories_outlet", table_name="expense_categories")
    op.add_column(
        "expenses", sa.Column("category", expense_category_enum, nullable=True)
    )

    # LOSSY, deliberately. A category the admin created has no enum label to return to, so
    # it collapses into `other`. Announced rather than silent -- see the module docstring.
    op.execute(
        """
        DO $$
        DECLARE lost bigint;
        BEGIN
            SELECT count(*) INTO lost
              FROM expenses e
              JOIN expense_categories ec ON ec.id = e.category_id
             WHERE lower(ec.code) NOT IN ('salary', 'maintenance', 'electricity', 'other');
            IF lost > 0 THEN
                RAISE WARNING
                    'migration 0010 downgrade: % expense row(s) filed under an '
                    'admin-created category collapse to ''other''. This is not reversible.',
                    lost;
            END IF;
        END $$
        """
    )

    op.execute(
        """
        UPDATE expenses AS e
           SET category = CASE
                   WHEN lower(ec.code) IN ('salary', 'maintenance', 'electricity', 'other')
                        THEN lower(ec.code)::expense_category
                   ELSE 'other'::expense_category
               END
          FROM expense_categories AS ec
         WHERE ec.id = e.category_id
        """
    )

    op.alter_column("expenses", "category", nullable=False)

    op.drop_index("ix_expenses_shift_category", table_name="expenses")
    op.drop_column("expenses", "category_id")
    op.create_index("ix_expenses_shift_category", "expenses", ["shift_id", "category"])

    op.drop_table("expense_categories")
