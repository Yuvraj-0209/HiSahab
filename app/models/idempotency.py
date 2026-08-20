"""The §6.10 replay store.

`POST`s that create money records carry an `Idempotency-Key`. The first request stores its
response against `(key, endpoint, user_id)`; a repeat within 24 hours replays that response
and creates nothing.

**Not optional, per §6.10.** Attendants use phones on patchy rural connectivity, and a
retry after a timeout must not create a second ₹5,000 collection.

This table is infrastructure, not a business record: no `outlet_id` (§5.3), no audit trail,
and rows are swept after 24 hours by `app/jobs/cleanup_idempotency_keys.py`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        sa.UniqueConstraint(
            "idempotency_key",
            "endpoint",
            "user_id",
            name="uq_idempotency_keys_key_endpoint_user",
        ),
        sa.Index("ix_idempotency_keys_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    idempotency_key: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    endpoint: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    user_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=False
    )
    request_fingerprint: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    # NULL while the handler is still running. The reservation row is written first so that
    # two simultaneous retries race on the unique constraint rather than both writing money.
    response_status: Mapped[int | None] = mapped_column(sa.SmallInteger(), nullable=True)
    response_body: Mapped[Any | None] = mapped_column(postgresql.JSONB(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
