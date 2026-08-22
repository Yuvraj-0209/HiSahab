"""the cash engine: non-fuel sales, deposits, shortfalls and the daily summary

Revision ID: 0013
Revises: 0012
Create Date: Phase 10 -- the equation every phase since 5 has been assembling terms for

0005 gave §6.4 its `total_sales`, 0006 its collection modes, 0008 its `cash_expenses`, and
0012 its `credit_sales_amount`. This migration adds the four things still missing and the
row that records the answer.

## `non_fuel_sales` -- the term §5.2 never gave a column

§6.4 has named `other_cash_income` since the beginning and §5.2 listed no column for it
anywhere. This is it, and two decisions are baked into the shape.

**Per shift, not per day.** A ₹500 bottle of oil is in the salesman's hand and *not* in the
meter-derived figure, but it *is* in the cash he counts into the locker. Compare derived fuel
cash against his declaration without it and he shows a ₹500 **surplus** every day he sells
one -- a phantom in his name. The figure has to sit beside the declaration it is checked
against, and that is the shift.

**No `mode` column, because it is added to `total_sales`, not to the cash side.** §6.4's
amendment works this through: a card-paid oil sale is already inside the card collections
total, so putting it on the cash side understates derived cash by exactly its amount. On the
sales side the arithmetic is correct however the customer paid, which is why this table does
not need to know.

A table rather than a column on `shifts` because it is a money row, §6.9 corrects a money row
by appending a reversal, and you cannot reverse a column. Still not an itemised sales module
(§12, §13.2): an amount and an optional note, no product, no stock, no unit price.

## `bank_deposits` gains §6.9's reversal pair

§5.2 listed the table without one. §6.9 governs *financial rows in a closed or locked shift*
and a deposit is squarely that -- and §4.7 says the whole day is typed in after the fact, so
a mistyped deposit found after close is the normal case here, not an edge one.

`business_date` is derivable from the shift and is stored anyway. That is not a breach of
§5.0, which is about *tenancy* columns with no correct backfill; this one backfills correctly
from `shifts.business_date`. It is kept because a deposit is filed against a trading day and
that is the column reports group by. **§3 rule 7 still applies**: the server recomputes it
from the shift and never takes the client's word, so the two cannot drift.

## `salesman_shortfalls` points at `user_profiles`, never `credit_customers`

§13.14 and §14. A shortfall is the *outcome of a reconciliation*, not a sale: it has no
receipt to satisfy `credit_sales.attachment_id`, and filing it against a credit customer
would mix staff debt into a real customer's outstanding balance -- after which nobody can
answer "what does this customer owe me" again.

`salesman_id` is a real FK to `user_profiles`. The API reads it from `shifts.attendant_id`
(§5.2's "exactly one name carries the drawer") rather than accepting it from a client, but
the column is a plain FK because a settlement arriving on a *later* shift belongs to the
salesman, not to that shift's attendant.

**`computed_gap` is stored beside `amount`.** §4.7's predict-and-confirm shape, applied to a
debt: the system's figure and the human's, side by side, so a disagreement is legible instead
of absorbed. They may differ -- a manager may know part of a gap is a slip already corrected
-- and the API warns rather than refusing, for the reason §6.8 gives about close
preconditions.

## `salesman_shortfall_settlements` has no `mode`, deliberately

The owner's answer: a shortfall is repaid in cash. Not written off, not deducted from wages.
So every row reaches §6.4's drawer and there is nothing to filter on. A `mode` column added
later backfills to `'cash'` for every existing row *correctly*, because every existing row
genuinely is cash -- so by §5.0's own derivability rule it can wait, rather than being a
one-label enum built for a future nobody has committed to (§11). §13.15 records the
consequence: until it exists, a ₹20 gap nobody will chase never closes.

## `daily_cash_summaries` stores every term, not just the total

§5.2's reason for storing `expected_closing` -- *"you still need to know what the system told
the manager on that day"* -- applies term by term. A manager checking a ₹300 variance needs
the breakdown **as it stood**, not as recomputed six months later after a reversal landed
underneath it; otherwise the total and its own explanation disagree, and the explanation is
the half he can verify.

`variance` is a **generated** column, per §5.2. It is NULL whenever `actual_counted` is --
which is most days under §6.5's locker model, and a real state rather than a missing value.

`opening_balance_source` records which of §6.5's three branches produced the opening figure.
Derivable from the previous row in principle, but not for the seeded first day, and it is the
fact a reader most wants sitting next to the number.

## Every new unique constraint has a `_CONSTRAINT_ERRORS` entry, in this same commit

Five of them. `tests/test_errors.py` reads `pg_constraint` and fails if one is missing --
which is how Phase 8 discovered it had shipped an unmapped constraint in the very phase that
was paying attention to the problem.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

opening_balance_source_enum = postgresql.ENUM(
    "seeded",
    "counted",
    "carried",
    name="opening_balance_source",
    create_type=False,
)


def _money(name: str, *, nullable: bool = False) -> sa.Column:
    """§3 rule 1: NUMERIC(12,2), never FLOAT. Written once so no column can drift."""
    return sa.Column(name, sa.Numeric(12, 2), nullable=nullable)


def _reversal_constraints(table: str) -> list[sa.schema.SchemaItem]:
    """§6.9's quartet, identical on every financial table that carries it.

    Written once rather than copied four times, because the parts that matter -- the
    **strict** sign rule, the non-blank reason, the not-self guard, and the unique index that
    loses a concurrent double-reversal loudly -- are exactly the parts a copy would get subtly
    wrong. 0012 learned this the hard way with a quantity CHECK written four lines below an
    already sign-aware amount rule and still not sign-aware itself.

    Strict `> 0` / `< 0`, matching `expenses` and `credit_sales` rather than `collections`: a
    ₹0 cash collection is a genuine §6.8 declaration, but a ₹0 deposit, non-fuel sale,
    shortfall or settlement records nothing and has no reason to exist.
    """
    return [
        sa.UniqueConstraint("reverses_id", name=f"uq_{table}_reverses_id"),
        sa.CheckConstraint(
            "reverses_id IS NULL "
            "OR (reversal_reason IS NOT NULL AND reversal_reason ~ '[^[:space:]]')",
            name=f"ck_{table}_reversal_has_reason",
        ),
        sa.CheckConstraint(
            "(reverses_id IS NULL AND amount > 0) "
            "OR (reverses_id IS NOT NULL AND amount < 0)",
            name=f"ck_{table}_amount_sign",
        ),
        sa.CheckConstraint(
            "reverses_id IS NULL OR reverses_id <> id",
            name=f"ck_{table}_reversal_not_self",
        ),
    ]


def _audit_columns(table: str) -> list[sa.Column]:
    """`id`, `reverses_id`, `reversal_reason`, `created_at`, `created_by` -- the five every
    financial child table in this schema carries, in the same order and the same types."""
    return [
        sa.Column(
            "id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("reverses_id", sa.UUID(), sa.ForeignKey(f"{table}.id"), nullable=True),
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
    ]


def upgrade() -> None:
    opening_balance_source_enum.create(op.get_bind(), checkfirst=True)

    # --- non_fuel_sales -------------------------------------------------------
    op.create_table(
        "non_fuel_sales",
        *_audit_columns("non_fuel_sales"),
        # No outlet_id: derivable via shift_id -> shifts.outlet_id (§5.0).
        sa.Column("shift_id", sa.UUID(), sa.ForeignKey("shifts.id"), nullable=False),
        _money("amount"),
        sa.Column("description", sa.Text(), nullable=True),
        *_reversal_constraints("non_fuel_sales"),
    )
    op.create_index("ix_non_fuel_sales_shift", "non_fuel_sales", ["shift_id"])

    # --- bank_deposits --------------------------------------------------------
    op.create_table(
        "bank_deposits",
        *_audit_columns("bank_deposits"),
        sa.Column("shift_id", sa.UUID(), sa.ForeignKey("shifts.id"), nullable=False),
        # Derivable from the shift, and written anyway -- see the module docstring. Set
        # server-side from shifts.business_date, never accepted from a client (§3 rule 7).
        sa.Column("business_date", sa.Date(), nullable=False),
        _money("amount"),
        sa.Column("bank_reference", sa.Text(), nullable=True),
        # The deposit slip. Nullable: only credit_sales.attachment_id is ever NOT NULL (§6.6).
        sa.Column(
            "attachment_id", sa.UUID(), sa.ForeignKey("attachments.id"), nullable=True
        ),
        *_reversal_constraints("bank_deposits"),
    )
    op.create_index("ix_bank_deposits_shift", "bank_deposits", ["shift_id"])
    op.create_index("ix_bank_deposits_date", "bank_deposits", ["business_date"])

    # --- salesman_shortfalls --------------------------------------------------
    op.create_table(
        "salesman_shortfalls",
        *_audit_columns("salesman_shortfalls"),
        sa.Column("shift_id", sa.UUID(), sa.ForeignKey("shifts.id"), nullable=False),
        # §13.14, §14: user_profiles, NEVER credit_customers. Staff debt and customer debt
        # are different questions and mixing them destroys the answer to both.
        sa.Column(
            "salesman_id", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=False
        ),
        _money("amount"),
        # What the system calculated when this was booked, kept beside what the human
        # actually booked. §4.7's predict-and-confirm shape applied to a debt.
        _money("computed_gap"),
        sa.Column("reason", sa.Text(), nullable=False),
        *_reversal_constraints("salesman_shortfalls"),
        sa.CheckConstraint(
            "reason ~ '[^[:space:]]'", name="ck_salesman_shortfalls_reason_not_blank"
        ),
    )
    op.create_index("ix_salesman_shortfalls_shift", "salesman_shortfalls", ["shift_id"])
    op.create_index(
        "ix_salesman_shortfalls_salesman", "salesman_shortfalls", ["salesman_id"]
    )

    # --- salesman_shortfall_settlements ---------------------------------------
    op.create_table(
        "salesman_shortfall_settlements",
        *_audit_columns("salesman_shortfall_settlements"),
        # The shift during which the money physically arrived -- not the shift that produced
        # the shortfall, which may be weeks earlier.
        sa.Column("shift_id", sa.UUID(), sa.ForeignKey("shifts.id"), nullable=False),
        # Points at the salesman, not at a particular shortfall. §5.2's reason for
        # credit_repayments: one payment covering part of three debts has no honest per-row
        # answer, which is the same argument that deleted credit_sales.is_settled.
        sa.Column(
            "salesman_id", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=False
        ),
        _money("amount"),
        *_reversal_constraints("salesman_shortfall_settlements"),
    )
    op.create_index(
        "ix_shortfall_settlements_shift", "salesman_shortfall_settlements", ["shift_id"]
    )
    op.create_index(
        "ix_shortfall_settlements_salesman",
        "salesman_shortfall_settlements",
        ["salesman_id"],
    )

    # --- daily_cash_summaries -------------------------------------------------
    op.create_table(
        "daily_cash_summaries",
        sa.Column(
            "id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        # §5.0: carries its own. A summary has no parent shift -- it aggregates all of them.
        sa.Column("outlet_id", sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        _money("opening_balance"),
        sa.Column("opening_balance_source", opening_balance_source_enum, nullable=False),
        # §5.2: the figure the system computed and showed, stored so that fixing a
        # calculation bug six months from now does not destroy the record of what the manager
        # was told on the day.
        _money("expected_closing"),
        # NULL on every day nobody counted the locker, which is most of them (§6.5).
        _money("actual_counted", nullable=True),
        # §5.2 says generated. NULL propagates correctly when nothing was counted, so an
        # uncounted day reads as "no variance known" rather than as a variance of zero.
        sa.Column(
            "variance",
            sa.Numeric(12, 2),
            sa.Computed("actual_counted - expected_closing", persisted=True),
            nullable=True,
        ),
        # --- the component snapshot (§5.2) ---
        _money("metered_fuel_sales"),
        _money("non_fuel_sales_total"),
        _money("card_total"),
        _money("upi_total"),
        _money("wallet_total"),
        _money("credit_sales_total"),
        _money("cash_credit_repayments"),
        _money("cash_shortfall_settlements"),
        _money("cash_expenses"),
        _money("bank_deposits_total"),
        _money("shortfalls_booked"),
        sa.Column(
            "is_finalised", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "finalised_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
        sa.Column("finalised_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # §13.10/§13.16: a shift reopened beneath a finalised day flags it. Nothing is
        # recomputed -- that would be the silent rewrite §5.2 stores these figures to prevent.
        sa.Column(
            "requires_review",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
        sa.UniqueConstraint(
            "outlet_id", "business_date", name="uq_daily_cash_summaries_outlet_date"
        ),
        # A finalised row must say who and when. Without this a day could read as finalised
        # with nobody accountable for having finalised it, which is exactly the gap
        # audit_logs exists to close and a column can close more cheaply.
        sa.CheckConstraint(
            "is_finalised = false "
            "OR (finalised_by IS NOT NULL AND finalised_at IS NOT NULL)",
            name="ck_daily_cash_summaries_finalised_has_actor",
        ),
        # Mirrors nozzle_readings' review columns: a flag with no note is a flag nobody can
        # act on, because the question it was raising is not recorded anywhere.
        sa.CheckConstraint(
            "requires_review = false OR review_note IS NOT NULL",
            name="ck_daily_cash_summaries_review_has_note",
        ),
    )
    op.create_index(
        "ix_daily_cash_summaries_outlet_date",
        "daily_cash_summaries",
        ["outlet_id", "business_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_daily_cash_summaries_outlet_date", table_name="daily_cash_summaries"
    )
    op.drop_table("daily_cash_summaries")

    op.drop_index(
        "ix_shortfall_settlements_salesman",
        table_name="salesman_shortfall_settlements",
    )
    op.drop_index(
        "ix_shortfall_settlements_shift", table_name="salesman_shortfall_settlements"
    )
    op.drop_table("salesman_shortfall_settlements")

    op.drop_index(
        "ix_salesman_shortfalls_salesman", table_name="salesman_shortfalls"
    )
    op.drop_index("ix_salesman_shortfalls_shift", table_name="salesman_shortfalls")
    op.drop_table("salesman_shortfalls")

    op.drop_index("ix_bank_deposits_date", table_name="bank_deposits")
    op.drop_index("ix_bank_deposits_shift", table_name="bank_deposits")
    op.drop_table("bank_deposits")

    op.drop_index("ix_non_fuel_sales_shift", table_name="non_fuel_sales")
    op.drop_table("non_fuel_sales")

    # DROP TABLE does not drop the enum type -- 0008's downgrade comment warned about exactly
    # this and 0010 was caught by it. 0012 repeated the lesson; this is the third table set
    # to need the line, so it is not an accident of one migration.
    opening_balance_source_enum.drop(op.get_bind(), checkfirst=True)
