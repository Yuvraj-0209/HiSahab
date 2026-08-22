"""credit customers, sales issued on credit, and repayments

Revision ID: 0012
Revises: 0011
Create Date: Phase 9 -- the term §6.4's cash equation has been missing since Phase 6

`collections` (0006) records money that arrived. `expenses` (0008) records money that left.
Neither explains the largest gap on a real day at this pump: fuel that went through a nozzle
against a customer's name with no cash behind it. §6.4 subtracts `credit_sales_amount` to
derive expected cash, and until this migration there was nothing to subtract.

## `credit_customers` carries its own `outlet_id`; the other two do not

§5.0's rule: a tenancy column exists from birth only if it is *not derivable*. A customer has
no parent row, so nothing else would say which pump they belong to and there is no correct
backfill -- only a guess. `credit_sales` and `credit_repayments` both hang off a shift, so
their outlet is one join away (`shift_id -> shifts.outlet_id`), exactly like `collections` and
`expenses`.

## `UNIQUE (outlet_id, phone)` on customers

§6.7's structuring argument, applied to people rather than categories. Two rows for one man
split one real balance across two ledgers, and §6.6's credit limit then never fires against
either. Names collide (a pump has three customers called Ramesh); phone numbers do not.

Outlet-scoped, not global: the same fleet operator may hold accounts at two pumps.

## `credit_sales.attachment_id` is NOT NULL, with no exemption for reversals

§6.6 states this control twice, as the belt to the API's braces. 0011 gave `expenses` a CHECK
that exempts reversals (`reverses_id IS NOT NULL OR receipt_required = false OR ...`) because
a cancellation is not a spend and there is nothing to photograph. **That shape is deliberately
not copied here.** A `credit_sales` reversal *inherits* the original's `attachment_id` -- the
same photograph, the same piece of evidence -- so the column can stay unconditionally NOT
NULL and the receipt control is never weakened for genuine customer sales. §5.2, §5.3.

## `credit_repayment_mode` is a new type, not a reuse

`cash | card | upi | bank_transfer`. Not `collection_mode` (0006), which carries `wallet` and
lacks `bank_transfer`. Not `expense_mode` (0008), whose labels match today by coincidence
rather than contract -- sharing it would mean the first phase needing a new mode on either
table has to alter a live enum shared by two unrelated ones.

**`downgrade()` drops the type explicitly.** `DROP TABLE` does not drop an enum, the trap
0008's own downgrade comment warned about and 0010 was actually caught by.

## Both transactional tables carry §6.9's reversal quartet

`uq_<table>_reverses_id`, a non-blank reason CHECK, the sign rule, and a not-self CHECK. The
sign rule is **strict** (`> 0` / `< 0`), matching `expenses` rather than `collections`: a ₹0
cash collection is a genuine declaration under §6.8, a ₹0 udhaar records nothing.

Both unique constraints get `_CONSTRAINT_ERRORS` entries in the same commit as this file --
see `tests/test_errors.py`, which reads `pg_constraint` and fails if they do not.

## What is NOT here

No `is_settled` (§5.2, removed in Phase 9 -- outstanding is computed, and a per-row settled
flag has no honest value once one repayment covers part of three bills). No shortfall table:
that is Phase 10's, where §6.4's reconciliation actually produces one (§13.14).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

credit_repayment_mode_enum = postgresql.ENUM(
    "cash",
    "card",
    "upi",
    "bank_transfer",
    name="credit_repayment_mode",
    create_type=False,
)


def upgrade() -> None:
    credit_repayment_mode_enum.create(op.get_bind(), checkfirst=True)

    # --- credit_customers -----------------------------------------------------
    op.create_table(
        "credit_customers",
        sa.Column(
            "id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        # §5.0: not derivable -- a customer has no parent row to read an outlet off.
        sa.Column("outlet_id", sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        # NOT NULL because it is half of the unique key below, and because a credit customer
        # you cannot telephone is not one this pump can chase for payment.
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("vehicle_numbers", postgresql.ARRAY(sa.Text()), nullable=True),
        # NULL = no limit (§6.6). Never coerced to zero -- that would refuse every sale to the
        # customers who are trusted most.
        sa.Column("credit_limit", sa.Numeric(12, 2), nullable=True),
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
        sa.UniqueConstraint(
            "outlet_id", "phone", name="uq_credit_customers_outlet_phone"
        ),
        sa.CheckConstraint(
            "name ~ '[^[:space:]]'", name="ck_credit_customers_name_not_blank"
        ),
        sa.CheckConstraint(
            "phone ~ '[^[:space:]]'", name="ck_credit_customers_phone_not_blank"
        ),
        # `>= 0`, not `> 0`: zero is a real answer -- a customer allowed no further udhaar at
        # all -- while a negative limit is meaningless under either reading of the column.
        sa.CheckConstraint(
            "credit_limit IS NULL OR credit_limit >= 0",
            name="ck_credit_customers_limit_not_negative",
        ),
    )
    op.create_index("ix_credit_customers_outlet", "credit_customers", ["outlet_id"])

    # --- credit_sales ---------------------------------------------------------
    op.create_table(
        "credit_sales",
        sa.Column(
            "id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        # No outlet_id: derivable via shift_id -> shifts.outlet_id (§5.0).
        sa.Column("shift_id", sa.UUID(), sa.ForeignKey("shifts.id"), nullable=False),
        sa.Column(
            "credit_customer_id",
            sa.UUID(),
            sa.ForeignKey("credit_customers.id"),
            nullable=False,
        ),
        # NULL = a non-fuel credit sale -- a can of oil, a puncture repair (§5.2).
        sa.Column(
            "fuel_type_id", sa.UUID(), sa.ForeignKey("fuel_types.id"), nullable=True
        ),
        # Litres OR kilograms, per the fuel's own unit_of_measure (§4.5). Never assume litres.
        sa.Column("quantity", sa.Numeric(10, 3), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("vehicle_number", sa.Text(), nullable=True),
        # The receipt control, at the database level (§6.6). See the module docstring for why
        # this has no reversal exemption.
        sa.Column(
            "attachment_id", sa.UUID(), sa.ForeignKey("attachments.id"), nullable=False
        ),
        sa.Column("limit_override_reason", sa.Text(), nullable=True),
        sa.Column(
            "reverses_id", sa.UUID(), sa.ForeignKey("credit_sales.id"), nullable=True
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
        sa.UniqueConstraint("reverses_id", name="uq_credit_sales_reverses_id"),
        sa.CheckConstraint(
            "reverses_id IS NULL "
            "OR (reversal_reason IS NOT NULL AND reversal_reason ~ '[^[:space:]]')",
            name="ck_credit_sales_reversal_has_reason",
        ),
        sa.CheckConstraint(
            "(reverses_id IS NULL AND amount > 0) "
            "OR (reverses_id IS NOT NULL AND amount < 0)",
            name="ck_credit_sales_amount_sign",
        ),
        sa.CheckConstraint(
            "reverses_id IS NULL OR reverses_id <> id",
            name="ck_credit_sales_reversal_not_self",
        ),
        # §4.5: a quantity's unit lives on the fuel type, so a quantity with no fuel type
        # cannot be interpreted at all. Not the biconditional -- a slip recording only rupees,
        # with nobody having written the litres down, is real and must stay recordable.
        sa.CheckConstraint(
            "quantity IS NULL OR fuel_type_id IS NOT NULL",
            name="ck_credit_sales_quantity_needs_fuel_type",
        ),
        # Sign-aware, mirroring ck_credit_sales_amount_sign rather than a bare `> 0`.
        # A reversal negates the quantity alongside the amount, so that a per-fuel udhaar
        # report nets to zero the same way the money does -- leaving it positive would show
        # 40 litres sold on credit with no money owed against them. A bare `quantity > 0`
        # made that impossible and turned every reversal of a fuel sale into a 500.
        sa.CheckConstraint(
            "quantity IS NULL "
            "OR (reverses_id IS NULL AND quantity > 0) "
            "OR (reverses_id IS NOT NULL AND quantity < 0)",
            name="ck_credit_sales_quantity_sign",
        ),
        sa.CheckConstraint(
            "limit_override_reason IS NULL OR limit_override_reason ~ '[^[:space:]]'",
            name="ck_credit_sales_override_reason_not_blank",
        ),
    )
    op.create_index("ix_credit_sales_shift", "credit_sales", ["shift_id"])
    op.create_index("ix_credit_sales_customer", "credit_sales", ["credit_customer_id"])

    # --- credit_repayments ----------------------------------------------------
    op.create_table(
        "credit_repayments",
        sa.Column(
            "id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "credit_customer_id",
            sa.UUID(),
            sa.ForeignKey("credit_customers.id"),
            nullable=False,
        ),
        # The shift the money physically arrived on (§5.2) -- not the shift the original
        # udhaar was issued on, which may be months earlier and is not referenced at all.
        sa.Column("shift_id", sa.UUID(), sa.ForeignKey("shifts.id"), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("mode", credit_repayment_mode_enum, nullable=False),
        # Nullable, unlike credit_sales'. A repayment is money coming in and the pump writes
        # the receipt; there is no counterparty document to demand.
        sa.Column(
            "attachment_id", sa.UUID(), sa.ForeignKey("attachments.id"), nullable=True
        ),
        sa.Column(
            "reverses_id", sa.UUID(), sa.ForeignKey("credit_repayments.id"), nullable=True
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
        sa.UniqueConstraint("reverses_id", name="uq_credit_repayments_reverses_id"),
        sa.CheckConstraint(
            "reverses_id IS NULL "
            "OR (reversal_reason IS NOT NULL AND reversal_reason ~ '[^[:space:]]')",
            name="ck_credit_repayments_reversal_has_reason",
        ),
        sa.CheckConstraint(
            "(reverses_id IS NULL AND amount > 0) "
            "OR (reverses_id IS NOT NULL AND amount < 0)",
            name="ck_credit_repayments_amount_sign",
        ),
        sa.CheckConstraint(
            "reverses_id IS NULL OR reverses_id <> id",
            name="ck_credit_repayments_reversal_not_self",
        ),
    )
    op.create_index("ix_credit_repayments_shift", "credit_repayments", ["shift_id"])
    op.create_index(
        "ix_credit_repayments_customer", "credit_repayments", ["credit_customer_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_credit_repayments_customer", table_name="credit_repayments")
    op.drop_index("ix_credit_repayments_shift", table_name="credit_repayments")
    op.drop_table("credit_repayments")

    op.drop_index("ix_credit_sales_customer", table_name="credit_sales")
    op.drop_index("ix_credit_sales_shift", table_name="credit_sales")
    op.drop_table("credit_sales")

    op.drop_index("ix_credit_customers_outlet", table_name="credit_customers")
    op.drop_table("credit_customers")

    # DROP TABLE does not drop the enum type -- 0008's downgrade comment warned about exactly
    # this and 0010 was caught by it. Without this line, re-running upgrade() fails with
    # "type credit_repayment_mode already exists".
    credit_repayment_mode_enum.drop(op.get_bind(), checkfirst=True)
