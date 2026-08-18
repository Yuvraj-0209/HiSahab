"""Fuel types, and the two effective-dated tables that value what they sell.

The shape of this module is driven by CLAUDE.md §4.1 and §4.6, which say the same thing
about two different numbers: **neither a price nor a margin is an attribute of a fuel; each
is a dated record.** Storing either as a mutable column on `fuel_types` would silently
corrupt every historical report the moment it changed, with no error and no way to
reconstruct what the figure had been.

`FuelPrice` and `FuelMargin` are therefore structurally identical and are read through the
same "greatest effective_from <= T" lookup in app/services/pricing.py. They are separate
tables because they revise on different schedules -- price often, margin almost never.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Mirrors migration 0003. create_type=False because the migration owns the type's
# lifecycle; see the comment there for why that has to be explicit.
_unit_of_measure_enum = postgresql.ENUM(
    "litre", "kilogram", name="fuel_type_unit_of_measure", create_type=False
)


class FuelType(Base):
    """A product the outlet sells. Global reference data -- no outlet_id (§5.0).

    Admin-managed rather than seeded-only: outlets sell products this codebase cannot
    anticipate (XP-95, Extra Green), and requiring a migration to add one you already sell
    would be wrong. Migration 0003 seeds the four sold today; the rest arrive via the API.
    """

    __tablename__ = "fuel_types"
    __table_args__ = (
        sa.UniqueConstraint("code", name="uq_fuel_types_code"),
        sa.CheckConstraint(
            "max_flow_rate_per_minute > 0", name="ck_fuel_types_max_flow_positive"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    code: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    display_name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    # §4.5. Immutable after creation -- the PATCH endpoint refuses it -- because changing
    # a unit reinterprets every quantity ever recorded against this fuel: litres read as
    # kilograms, and every historical sale value silently wrong.
    unit_of_measure: Mapped[str] = mapped_column(_unit_of_measure_enum, nullable=False)
    # §6.2's sanity ceiling for THIS fuel. Per-fuel rather than one global constant: a
    # petrol nozzle does ~60 L/min, a CBG dispenser single-digit kg/min.
    max_flow_rate_per_minute: Mapped[Decimal] = mapped_column(
        sa.Numeric(10, 3), nullable=False
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


class FuelPrice(Base):
    """What a fuel sold for, from a moment onward. Append-only (§5.1).

    Never updated and never deleted -- enforced by a database trigger as well as by the
    absence of any route that would do so. An UPDATE here would rewrite what a shift months
    ago was valued at, which is precisely what effective dating exists to prevent.
    """

    __tablename__ = "fuel_prices"
    __table_args__ = (
        sa.UniqueConstraint(
            "outlet_id",
            "fuel_type_id",
            "effective_from",
            name="uq_fuel_prices_outlet_fuel_effective",
        ),
        sa.CheckConstraint("rate_per_unit > 0", name="ck_fuel_prices_rate_positive"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    # §5.1: prices are outlet-scoped, not national. Dealers are supplied by different OMCs
    # and rates vary by state and district.
    outlet_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False
    )
    fuel_type_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("fuel_types.id"), nullable=False
    )
    # Rupees per litre or per kilogram, depending on the fuel's unit (§4.5).
    rate_per_unit: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False
    )
    # The domain fact -- who read the rate off the OMC's message and typed it in. Distinct
    # from created_by, which is change tracking (§5.3). Equal in V1; different questions.
    entered_by: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )


class FuelMargin(Base):
    """What the dealer keeps per unit, from a moment onward. Append-only (§5.1).

    §4.6: retail revisions pass straight through to the purchase invoice, so this figure --
    not the price gap -- is the constant. That is the entire reason V1 can report fuel
    profit without holding any purchase, tanker or stock data:

        dealer_profit = quantity_sold x margin_at(fuel_type, at)

    Structurally identical to FuelPrice on purpose, and read by the same helper. Kept a
    separate table because price revises often and margin almost never; sharing a row would
    force re-entry of an unchanged margin on every price change, and the first forgotten
    entry silently nulls that period's profit.
    """

    __tablename__ = "fuel_margins"
    __table_args__ = (
        sa.UniqueConstraint(
            "outlet_id",
            "fuel_type_id",
            "effective_from",
            name="uq_fuel_margins_outlet_fuel_effective",
        ),
        sa.CheckConstraint("margin_per_unit > 0", name="ck_fuel_margins_margin_positive"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    outlet_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False
    )
    fuel_type_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("fuel_types.id"), nullable=False
    )
    # Rupees per litre or per kilogram (§4.5). For CBG this is the ₹2.28 IOCL leaves behind
    # when it deducts (retail - margin) x kg from the ledger balance.
    margin_per_unit: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False
    )
    entered_by: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )
