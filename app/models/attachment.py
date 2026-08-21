"""One row per uploaded file (CLAUDE.md §5.3, §7). All other tables reference *this*, never
a raw URL string -- the single authoritative representation of file knowledge (DRY).

## Carries its own `outlet_id`, unlike everything Phases 5-7 built

Not derivable via a parent shift (§5.0): at upload time nothing has linked the attachment to
anything yet (§7.2), so there is no `shift_id -> outlet_id` chain to walk. The outlet is
stamped on the row directly.

## `storage_path` excludes the bucket name

§7.2's path is `{outlet_id}/{YYYY}/{MM}/{DD}/{uuid4}.{ext}`; `bucket` is a separate column
holding `receipts`. Object storage takes the bucket as its own argument -- folding it into
`storage_path` too would put the object at `receipts/receipts/...` the first time anything
concatenates them.

## No `created_by`

A deliberate exception to §5's preamble, in the same documented shape as `outlets`'.
`uploaded_by` *is* the creator, under the name §7.2 already uses. Carrying both would mean
two columns holding one value until the day they disagree.

## One attachment, one *live* business row

Not expressible as a database constraint -- "live" means "not itself a reversal, and not
referenced by one" (§5.2's definition for collections), which needs a query across another
table's rows, not a CHECK on this one. Enforced in `app/services/attachments.py::link`.

No `relationship()`, consistent with the rest of app/models/: column-level foreign keys
only.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        sa.UniqueConstraint("storage_path", name="uq_attachments_storage_path"),
        sa.CheckConstraint("size_bytes > 0", name="ck_attachments_size_positive"),
        sa.CheckConstraint(
            "checksum_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_attachments_checksum_format",
        ),
        sa.CheckConstraint(
            "mime_type IN ('image/jpeg', 'image/png')",
            name="ck_attachments_mime_type_allowed",
        ),
        sa.Index("ix_attachments_outlet", "outlet_id"),
        sa.Index(
            "ix_attachments_unlinked",
            "created_at",
            postgresql_where=sa.text("linked_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    outlet_id: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False
    )
    bucket: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    storage_path: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    original_filename: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    mime_type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    uploaded_by: Mapped[UUID] = mapped_column(
        sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=False
    )
    linked_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
