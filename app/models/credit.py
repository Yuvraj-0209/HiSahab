"""Udhaar -- customers, sales issued on credit, and repayments (CLAUDE.md §5.1, §5.2, §6.6).

Phase 9. Three tables in one module because they are one subject and are never reasoned about
apart: a sale and a repayment only mean anything against the customer whose balance they move.

## The number this phase exists to produce

§6.4's cash equation subtracts `credit_sales_amount` from metered sales to derive the cash a
salesman should be holding. Until these tables existed, the gap between ₹95,000 of metered
fuel and ₹40,000 of declared cash had no explanation at all -- which is exactly why §6.8
refuses to block a shift close on that gap. This module supplies the missing term.

## Outstanding is computed, never stored

§6.6: `SUM(credit_sales.amount) - SUM(credit_repayments.amount)`, over **every** row,
reversals included -- they carry negative amounts and net out on their own.

There is no `is_settled` column. §5.2 carried one until Phase 9 removed it: it was described
as a derived convenience flag while §6.6, two sections down, warns that a denormalised total
will drift. It also has no honest value. A customer with three open bills who pays a third of
the total has settled *which* rows? Any answer is invented, and every answer gets rewritten
the moment a reversal lands. "Fully settled" is `outstanding == 0`, derived.

## `credit_sales.attachment_id` is NOT NULL, and stays that way

The receipt control §6.6 calls belt and braces: enforced by the database, not by application
code and not by JavaScript. `expenses.attachment_id` is nullable and conditional (§6.11); this
one never is.

§6.9's reversal appears to collide with that -- a reversal is a new row, and demanding a
receipt for a cancellation is the exact thing §6.11 refuses to do. `expenses` escapes through
a CHECK that exempts reversals. **That escape is deliberately not copied here.** The reversal
(and its replacement, if any) **inherits the original's `attachment_id`** instead, exactly as
an expense's replacement already does. §5.3's one-attachment-one-*live*-row rule holds
throughout: the original is reversed and so not live, the reversal is itself a reversal and so
not live, and the replacement is the single live claimant.

## Sign rule, strict

`(reverses_id IS NULL AND amount > 0) OR (reverses_id IS NOT NULL AND amount < 0)` on both
transactional tables -- strict, matching `expenses` rather than `collections`' `>=` / `<=`. A
₹0 cash collection is a genuine declaration under §6.8; a ₹0 udhaar records nothing and has no
reason to exist.

## No `outlet_id` on the transactional tables (§5.0)

`credit_sales` and `credit_repayments` both hang off a shift, so their outlet is derivable via
`shift_id -> shifts.outlet_id`. `credit_customers` has no parent and carries its own.

No `relationship()` anywhere, consistent with the rest of app/models/: column-level foreign
keys only, and callers `flush()` between dependent inserts.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Literal labels and create_type=False, matching collection_mode in 0006 and expense_mode in
# 0008. sa.Enum(CreditRepaymentMode) would derive the labels from Python declaration order, so
# reordering the enum members would produce phantom autogenerate drift against a database that
# never changed.
_credit_repayment_mode_enum = postgresql.ENUM(
    "cash",
    "card",
    "upi",
    "bank_transfer",
    name="credit_repayment_mode",
    create_type=False,
)


class CreditCustomer(Base):
    """Who may take udhaar. Admin-managed reference data, modelled on `expense_categories`.

    **`UNIQUE (outlet_id, phone)`, and the phone rather than the name.** §6.7's argument about
    `Tea`/`tea`/`chai ` transfers intact: two rows for one person split one real balance
    across two ledgers, and §6.6's credit limit then never fires against either. Names
    genuinely collide -- a pump has three customers called Ramesh -- and phones do not.

    **`credit_limit IS NULL` means no limit.** Never read it as zero; that would refuse every
    sale to the customers who are trusted most.

    **Deactivation is asymmetric** (§5.1). A deactivated customer refuses a new *sale* but
    still accepts a *repayment*: you deactivate somebody precisely to stop the debt growing
    while they pay off what they owe, and refusing their money would leave a balance nothing
    could ever clear. Deactivate, never delete (§3 rule 6).
    """

    __tablename__ = "credit_customers"
    __table_args__ = (
        sa.UniqueConstraint("outlet_id", "phone", name="uq_credit_customers_outlet_phone"),
        sa.CheckConstraint(
            "name ~ '[^[:space:]]'", name="ck_credit_customers_name_not_blank"
        ),
        sa.CheckConstraint(
            "phone ~ '[^[:space:]]'", name="ck_credit_customers_phone_not_blank"
        ),
        # Null is "no limit" (§6.6). A *negative* limit is meaningless in either reading, and
        # zero is a real answer -- a customer allowed no new udhaar at all -- so this is >= 0,
        # not > 0.
        sa.CheckConstraint(
            "credit_limit IS NULL OR credit_limit >= 0",
            name="ck_credit_customers_limit_not_negative",
        ),
        sa.Index("ix_credit_customers_outlet", "outlet_id"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    outlet_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    phone: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    # text[] rather than a child table: §5.2 says so, and a vehicle number is a label for
    # recognising a customer at the pump, not an entity anything else points at. Normalised
    # upper-case in the API so `MH12AB1234` and `mh12 ab 1234` cannot become two vehicles.
    vehicle_numbers: Mapped[list[str] | None] = mapped_column(
        postgresql.ARRAY(sa.Text()), nullable=True
    )
    credit_limit: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 2), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )


class CreditSale(Base):
    """Udhaar issued during a shift. See the module docstring for the receipt rule."""

    __tablename__ = "credit_sales"
    __table_args__ = (
        sa.UniqueConstraint("reverses_id", name="uq_credit_sales_reverses_id"),
        sa.CheckConstraint(
            "reverses_id IS NULL "
            "OR (reversal_reason IS NOT NULL AND reversal_reason ~ '[^[:space:]]')",
            name="ck_credit_sales_reversal_has_reason",
        ),
        # Strict, like expenses and unlike collections' `>=` / `<=`. A ₹0 cash collection is a
        # genuine declaration (§6.8); a ₹0 udhaar records nothing.
        sa.CheckConstraint(
            "(reverses_id IS NULL AND amount > 0) "
            "OR (reverses_id IS NOT NULL AND amount < 0)",
            name="ck_credit_sales_amount_sign",
        ),
        sa.CheckConstraint(
            "reverses_id IS NULL OR reverses_id <> id",
            name="ck_credit_sales_reversal_not_self",
        ),
        # §4.5: a quantity is a *measure*, and its unit lives on the fuel type. One with
        # no fuel type therefore has no unit and cannot be interpreted at all -- is 12 litres
        # or kilograms? Not the biconditional: a fuel slip recording only rupees, with nobody
        # having written down the litres, is a real thing that must still be recordable.
        sa.CheckConstraint(
            "quantity IS NULL OR fuel_type_id IS NOT NULL",
            name="ck_credit_sales_quantity_needs_fuel_type",
        ),
        # Sign-aware, mirroring the amount rule above. A reversal negates the quantity
        # alongside the amount so a per-fuel udhaar report nets to zero the same way the
        # money does; a bare `quantity > 0` made that impossible.
        sa.CheckConstraint(
            "quantity IS NULL "
            "OR (reverses_id IS NULL AND quantity > 0) "
            "OR (reverses_id IS NOT NULL AND quantity < 0)",
            name="ck_credit_sales_quantity_sign",
        ),
        # §6.6's admin override. Mandatory *and non-blank* when present, for the reason §14
        # gives about `override_reason`: it can never be an unexplained number.
        sa.CheckConstraint(
            "limit_override_reason IS NULL OR limit_override_reason ~ '[^[:space:]]'",
            name="ck_credit_sales_override_reason_not_blank",
        ),
        sa.Index("ix_credit_sales_shift", "shift_id"),
        sa.Index("ix_credit_sales_customer", "credit_customer_id"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    shift_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("shifts.id"), nullable=False
    )
    credit_customer_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("credit_customers.id"), nullable=False
    )
    # Null = a non-fuel credit sale (a can of oil, a puncture repair). §5.2.
    fuel_type_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("fuel_types.id"), nullable=True
    )
    # Litres *or kilograms*, per the fuel's own `unit_of_measure` (§4.5). Never assume litres.
    quantity: Mapped[Decimal | None] = mapped_column(sa.Numeric(10, 3), nullable=True)
    amount: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False)
    vehicle_number: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    # §6.6's receipt control, at the database level. NOT NULL, with no CHECK-shaped exemption
    # for reversals -- see the module docstring on inheritance.
    attachment_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("attachments.id"), nullable=False
    )
    limit_override_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    reverses_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("credit_sales.id"), nullable=True
    )
    reversal_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )


class CreditRepayment(Base):
    """A customer settling an old bill (§4.4, §5.2, §6.6).

    **The cash here has no sale behind it on the day it arrives** -- that is §4.4's whole
    point, and why §6.4 carries `cash_credit_repayments` as its own term rather than folding
    it into sales. Only `mode = cash` enters that equation.

    `attachment_id` is *nullable*, unlike `credit_sales`'. A repayment is money coming in and
    the pump writes the receipt; there is no counterparty document to demand.
    """

    __tablename__ = "credit_repayments"
    __table_args__ = (
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
        sa.Index("ix_credit_repayments_shift", "shift_id"),
        sa.Index("ix_credit_repayments_customer", "credit_customer_id"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    credit_customer_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("credit_customers.id"), nullable=False
    )
    # The shift during which the money physically arrived (§5.2) -- not the shift the original
    # udhaar was issued on, which may be months earlier and is deliberately not referenced.
    shift_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("shifts.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False)
    mode: Mapped[str] = mapped_column(_credit_repayment_mode_enum, nullable=False)
    attachment_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("attachments.id"), nullable=True
    )
    reverses_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("credit_repayments.id"), nullable=True
    )
    reversal_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )
