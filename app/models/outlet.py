"""The tenancy root (CLAUDE.md §5.0).

The table itself was created in migration 0001; this is only its ORM mapping. It is added
in Phase 2 rather than Phase 1 because nothing needed it until `outlet_memberships` grew a
foreign key to it -- SQLAlchemy cannot resolve `ForeignKey("outlets.id")` unless the target
table is present in the same MetaData.

Mirrors 0001 exactly, including the deliberate absence of `created_by`: the first outlet is
seeded by the system before any user exists.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Outlet(Base):
    __tablename__ = "outlets"

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
