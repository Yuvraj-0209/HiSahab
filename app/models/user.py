"""Identity and per-outlet authorisation (CLAUDE.md §5.1).

These models mirror migration 0002 exactly. The migration is the source of truth for the
schema (§3 rule 9); these classes exist so application code can read and write those rows
without hand-writing SQL. `alembic revision --autogenerate` must produce an empty
migration -- if it does not, the two have drifted and the model is wrong.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Same definition as migration 0002: the identical label list in the identical order, and
# create_type=False so nothing here ever tries to emit CREATE TYPE. Spelled out as plain
# strings rather than sa.Enum(Role) on purpose -- sa.Enum() derives its labels from the
# Python enum's declaration order, which would silently disagree with the migration the
# moment someone reorders Role's members, and autogenerate would then report drift that
# is not really there.
_role_enum = postgresql.ENUM(
    "admin", "manager", "attendant", name="membership_role", create_type=False
)


class UserProfile(Base):
    """Application concerns for a user whose identity lives in Supabase `auth.users`.

    Note what is *absent*: no email, no password, no role. Email and password are
    Supabase's business and duplicating them here would create two sources of truth for a
    credential. Role is absent because it is per-outlet -- see OutletMembership.
    """

    __tablename__ = "user_profiles"

    # Equal to auth.users.id, i.e. the JWT's `sub` claim. Not generated here: see the
    # comment in migration 0002.
    id: Mapped[UUID] = mapped_column(sa.UUID(), primary_key=True)
    full_name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.text("true")
    )
    phone: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    # Nullable self-reference; NULL means "provisioned by the system". See migration 0002.
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )


class OutletMembership(Base):
    """Which user may act at which outlet, and in what capacity (§5.1).

    This is the table every permission check in the application resolves against. §8:
    "Never trust a client-supplied role claim without verification against
    `outlet_memberships`."
    """

    __tablename__ = "outlet_memberships"
    __table_args__ = (
        sa.UniqueConstraint(
            "user_id", "outlet_id", name="uq_outlet_memberships_user_outlet"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    user_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=False
    )
    outlet_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False
    )
    # Typed as str, not Role, because that is honestly what the driver hands back. Callers
    # convert with Role(...) at the point of use, which fails loudly on an unexpected
    # value instead of quietly propagating one.
    role: Mapped[str] = mapped_column(_role_enum, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
    )
