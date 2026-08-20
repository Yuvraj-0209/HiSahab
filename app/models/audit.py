"""The audit log (CLAUDE.md §5.3).

Append-only. No updates, no deletes, ever -- enforced by a database trigger in migration
0004 as well as by the absence of any route that would do either. §6.6's belt-and-braces
principle: a client can bypass JavaScript, but not a database constraint.

§5.3 is explicit that this is *not* the same thing as `created_by` / `updated_at` columns.
Those are change tracking: they say who last touched a row, never what it was before or how
many times it changed. For a cash system both are required.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_audit_action_enum = postgresql.ENUM(
    "insert",
    "update",
    "reversal",
    "status_change",
    name="audit_log_action",
    create_type=False,
)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        sa.Index("ix_audit_logs_record", "table_name", "record_id"),
        sa.Index("ix_audit_logs_changed_at", "changed_at"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    # §5.0: no parent row to derive tenancy from, so it carries its own.
    outlet_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False
    )
    table_name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    # Deliberately NOT a foreign key. It points at rows in many different tables and a real
    # FK cannot express that. It also has to survive its target being restructured -- an
    # audit trail that breaks when the schema moves is useless exactly when it is needed.
    record_id: Mapped[UUID] = mapped_column(sa.UUID(), nullable=False)
    action: Mapped[str] = mapped_column(_audit_action_enum, nullable=False)
    changed_by: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    old_values: Mapped[dict[str, Any] | None] = mapped_column(
        postgresql.JSONB(), nullable=True
    )
    new_values: Mapped[dict[str, Any] | None] = mapped_column(
        postgresql.JSONB(), nullable=True
    )
    # §5.3: ties an audit entry to the application log lines for the same request. Text
    # rather than UUID because RequestIdMiddleware honours an inbound X-Request-ID header,
    # and a caller can send anything at all in it.
    request_id: Mapped[str] = mapped_column(sa.Text(), nullable=False)

    # No created_at / created_by: changed_at and changed_by are the same facts under their
    # domain names, and carrying both invites the two to disagree.
