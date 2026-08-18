"""Nozzles -- the meters sales are actually computed from (CLAUDE.md §4.3).

A dispensing unit has several nozzles; each has its own totalizer and is wired to exactly
one fuel type, and two nozzles can dispense the same fuel. Dispensers are deliberately not
their own table in V1 (§5.1, YAGNI) -- `dispenser_label` groups them well enough to report
on, and a table would buy nothing.

`fuel_type_id` and `totalizer_max_value` are immutable after creation. Both feed §6.2 and
§6.3, which recompute historical sales on read, so changing either would rewrite the past:
a rewired or re-metered nozzle is a new row, not an edit.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Nozzle(Base):
    __tablename__ = "nozzles"
    __table_args__ = (
        # Outlet-scoped per §5.0 -- "DU-1/N-1" is a label the next outlet will also use.
        sa.UniqueConstraint("outlet_id", "label", name="uq_nozzles_outlet_label"),
        sa.CheckConstraint(
            "totalizer_max_value > 0", name="ck_nozzles_totalizer_max_positive"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    # §5.0: not derivable from anything else in the row, so it must exist from birth.
    outlet_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False
    )
    label: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    dispenser_label: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    fuel_type_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("fuel_types.id"), nullable=False
    )
    # The rollover ceiling for this specific meter (§4.3). Required: §6.2's rollover
    # formula cannot be evaluated without it, and a null would surface as a crash inside
    # the sales math rather than a refusal at data entry.
    totalizer_max_value: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 2), nullable=False
    )
    meter_installed_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False
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
