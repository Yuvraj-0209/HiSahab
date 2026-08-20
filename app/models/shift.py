"""Shifts and shift templates -- the spine (CLAUDE.md §5.2, §4.7).

Every financial row in later phases carries a `shift_id`, so this is the table the rest of
the system is built on.

Two things here differ from the spec as originally written, both because §4.7 replaced the
`morning | night` model after the real operation was described:

* **There is no `shift_type`.** A day has as many shifts as it has; they are numbered.
  This outlet runs one (06:00-22:00) and a 24-hour outlet runs three, through one mechanism.
* **`started_at` is NOT NULL.** §6.3 values a whole shift's fuel at the rate effective at
  `started_at`. A shift without one cannot be priced, so a null would surface as a crash
  inside the sales math instead of a refusal at data entry.

No `relationship()` anywhere, consistent with the rest of app/models/: column-level foreign
keys only, and callers `flush()` between dependent inserts because SQLAlchemy has no
dependency edge to work from.
"""

from __future__ import annotations

from datetime import date, datetime, time
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Literal labels and create_type=False, matching 0002's membership_role and 0003's
# fuel_type_unit_of_measure. sa.Enum(ShiftStatus) would derive the labels from Python
# declaration order, so reordering the enum members would produce phantom autogenerate
# drift against a database that never changed.
_shift_status_enum = postgresql.ENUM(
    "open", "closed", "locked", name="shift_status", create_type=False
)


class OutletShiftTemplate(Base):
    """The shifts an outlet *usually* runs, supplying default times when one is opened.

    Days are typed in after the fact (§4.7), so without this somebody retypes 06:00 and
    22:00 every morning -- and that is a field which decides *which day's fuel rate* a
    whole shift is valued at (§6.3), so a rushed retype is expensive.

    The defaults are materialised onto the shift row at creation and this table is never
    read back afterwards. Editing a template must not revalue a shift that already happened.
    """

    __tablename__ = "outlet_shift_templates"
    __table_args__ = (
        sa.UniqueConstraint(
            "outlet_id", "sequence", name="uq_outlet_shift_templates_outlet_sequence"
        ),
        sa.CheckConstraint(
            "sequence >= 1", name="ck_outlet_shift_templates_sequence_positive"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    outlet_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(sa.SmallInteger(), nullable=False)
    # Display only. `sequence` is the identity; nothing keys off this string.
    label: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    # TIME, not TIMESTAMPTZ, and not a breach of §3 rule 4 -- that rule governs *instants*.
    # "06:00 local, every day" is a recurring wall-clock time, a genuinely different type
    # that cannot be stored as an instant without inventing a date to attach it to.
    starts_at_local: Mapped[time] = mapped_column(sa.Time(), nullable=False)
    ends_at_local: Mapped[time] = mapped_column(sa.Time(), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )


class Shift(Base):
    __tablename__ = "shifts"
    __table_args__ = (
        sa.UniqueConstraint(
            "outlet_id",
            "business_date",
            "sequence",
            name="uq_shifts_outlet_date_sequence",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_shifts_sequence_positive"),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at > started_at",
            name="ck_shifts_ended_after_started",
        ),
        sa.Index(
            "ix_shifts_chain",
            "outlet_id",
            sa.text("business_date DESC"),
            sa.text("sequence DESC"),
        ),
        sa.Index("ix_shifts_attendant", "attendant_id"),
        sa.Index("ix_shifts_outlet_status", "outlet_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    # §5.0: not derivable from anything else in the row -- nothing in a shift says which
    # pump it belonged to -- so it must exist from birth. There is no correct backfill.
    outlet_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False
    )
    # §6.1: explicit, never date(created_at). Two independent reasons -- a 24-hour outlet's
    # night shift crosses midnight, and this outlet types the whole day in after the fact,
    # so created_at is frequently the *following* calendar day.
    business_date: Mapped[date] = mapped_column(sa.Date(), nullable=False)
    # §4.7. Server-assigned from a read of this table; never accepted from a client.
    sequence: Mapped[int] = mapped_column(sa.SmallInteger(), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    # The one person accountable for this shift's cash. Other staff may be on duty -- this
    # outlet runs a crew of three -- but exactly one name carries the drawer, and a
    # shortfall is booked against it.
    attendant_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=False
    )
    # Mapped[str], not Mapped[ShiftStatus]: the driver hands back the raw label and callers
    # convert explicitly with ShiftStatus(shift.status), so an unexpected value fails
    # loudly rather than silently comparing unequal to every member. Same choice as
    # OutletMembership.role.
    status: Mapped[str] = mapped_column(
        _shift_status_enum, nullable=False, server_default=sa.text("'open'")
    )
    closed_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    locked_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )
