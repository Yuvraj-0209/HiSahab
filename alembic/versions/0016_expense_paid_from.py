"""where a cash expense's money came from

Revision ID: 0016
Revises: 0015
Create Date: Phase 17 -- written after a real trading day reported a salesman in surplus

## The defect

`expenses` recorded *how* money left (`mode`, since 0008) and never *whose pile it left
from*. §6.4's per-shift `accountable_cash` therefore had to assume, and the assumption --
stated plainly in `cash.py`'s own docstring -- was that a cash expense "physically passes
through the salesman's hands during the shift and is therefore already inside the figure he
declares." That is true of an ordinary day and false of a bill paid out of the locker.

It was found on real money. On 30 July this outlet took 302,827 of metered fuel, 265,617 of
it on card and Paytm, and issued 19,610 of udhaar -- leaving 17,600 of cash, which the
salesman declared correctly. The day's 60,170 of bills were then paid from the locker's
79,790 opening balance, because the day's own cash could not cover them. Charging all of that
to a shift that had taken 17,600 drove `accountable_cash` to -42,569 and reported a **60,169
surplus** -- a salesman holding money nobody gave him. Same phantom §6.4 already documents
for card-settled udhaar, one term over.

## The column

`paid_from`, its own type `expense_paid_from`, not shared with `expense_mode` -- §5.2 makes
this argument for `credit_repayment_mode` and it holds here: two enums whose labels have
nothing to do with each other must not be welded together because one is convenient.

**NOT NULL with a server default of `shift_cash`**, which is what makes this migration safe
on a live table: every existing row backfills to it *correctly*, because `shift_cash` is
precisely the assumption the old code encoded. By §5.0's derivability rule this column could
legitimately wait, and did.

The default is deliberately **not** §6.8's "an answer, never an omission". That rule governs
figures nothing else can check; here the common case is genuinely `shift_cash`, and forcing
the choice on every 20-rupee chai entry is the friction §6.11 warns teaches staff to fake
input. §13.33 records the residual risk that a mis-defaulted bill still distorts one shift.

**No CHECK ties it to `mode`.** It is meaningful only when `mode = 'cash'`; a non-cash
expense carries the default and nothing reads it. A CHECK asserting otherwise would be the
kind §5.2 rejects on `credit_opening_balances` -- one that has to be true and cannot usefully
be stated.

## What this migration does NOT do

**It backfills nothing beyond the default and flags nothing.** Unlike 0015, no stored
`daily_cash_summaries` component reads this column -- `expected_closing` goes on subtracting
every cash expense whoever paid it, because the locker really is lighter by all of it. So no
reconciled day's figures move and there is nothing to review (§13.32's situation, absent).
The 30 July rows are re-tagged by hand through the API, because only the owner knows which
bills came from where and a migration that guessed would be inventing history.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


# `create_type=False` with an explicit create/drop below, matching 0008 and 0012 -- it keeps
# the type's lifecycle visible in the migration rather than hidden in column reflection.
expense_paid_from_enum = postgresql.ENUM(
    "shift_cash",
    "locker_cash",
    name="expense_paid_from",
    create_type=False,
)


def upgrade() -> None:
    expense_paid_from_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "expenses",
        sa.Column(
            "paid_from",
            expense_paid_from_enum,
            nullable=False,
            # The server default does the backfill in one pass and then stays, because the
            # API leaves it unset on the overwhelmingly common case (§5.2).
            server_default=sa.text("'shift_cash'::expense_paid_from"),
        ),
    )

    # §6.4 reads this column only for `mode = 'cash'` rows, and only on the per-shift
    # `accountable_cash` path. Partial, because that is the only query shape that exists.
    op.create_index(
        "ix_expenses_shift_paid_from",
        "expenses",
        ["shift_id", "paid_from"],
        postgresql_where=sa.text("mode = 'cash'"),
    )


def downgrade() -> None:
    op.drop_index("ix_expenses_shift_paid_from", table_name="expenses")
    op.drop_column("expenses", "paid_from")
    expense_paid_from_enum.drop(op.get_bind(), checkfirst=True)
