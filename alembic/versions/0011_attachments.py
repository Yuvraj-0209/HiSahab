"""attachments -- one row per uploaded file, and the receipt rule that consumes it

Revision ID: 0011
Revises: 0010
Create Date: Phase 8 -- what §6.11 needed to become enforceable

0010 gave categories a `requires_receipt` flag with nothing to enforce it. This migration
is what makes that flag mean something: `attachments` (§5.3), `expenses.attachment_id` and
`expenses.receipt_required` (§5.2), and the CHECK that ties them together (§6.11).

## `attachments` carries its own `outlet_id`

Unlike every table Phases 5-7 built, this one is **not** derivable via `shift_id ->
shifts.outlet_id` -- see §5.0's landing schedule. An attachment is not owned by a shift; it
is owned by whichever business row eventually links it, and at upload time nothing has
linked it yet (§7.2). The outlet has to be stamped on the row itself.

## `storage_path` does not include the bucket

§7.2 writes the example path as `receipts/{outlet_id}/...`, where `receipts` is the bucket
-- which object storage takes as a separate argument, not a path prefix. Storing it in both
places would put the object at `receipts/receipts/...` the first time anything concatenates
them. So `bucket` holds `receipts` and `storage_path` holds
`{outlet_id}/{YYYY}/{MM}/{DD}/{uuid4}.{ext}`.

## No `created_by`

A deliberate exception to §5's preamble, in the same documented shape as `outlets`'.
`uploaded_by` *is* the creator, under the name §7.2 already uses -- carrying both would mean
two columns holding one value until the day they disagree.

## The three CHECKs on `attachments`

`size_bytes > 0`, `checksum_sha256` is exactly 64 lowercase hex characters, and `mime_type`
is one of the two allowed values. All three are unreachable through the API -- upload
validation refuses each case before a row is ever inserted (`app/core/uploads.py`) -- so
none needs a `_CONSTRAINT_ERRORS` entry. Belt and braces (§6.6): the database is the last
line of defence, not the first, and these three prove that a bug in the application layer
cannot silently write a corrupt attachment.

## The `expenses.receipt_required` CHECK is the only one that IS reachable

```sql
CHECK (reverses_id IS NOT NULL OR receipt_required = false OR attachment_id IS NOT NULL)
```

A reversal is exempt (`reverses_id IS NOT NULL`) -- it is a cancellation, not a spend, and
there is nothing to photograph (§6.11). This one genuinely can be reached by a client that
bypasses the API's own check, so it earns a `_CONSTRAINT_ERRORS` entry:
`ck_expenses_receipt_required_has_attachment` -> 422 `EXPENSE_REQUIRES_RECEIPT`.

## `attachment_id` is immutable once set -- enforced in the API, not here

There is no CHECK that stops `attachment_id` from being changed once non-NULL, because a
CHECK constraint cannot see the row's *previous* value, only its new one. §5.3's
one-attachment-one-*live*-row rule and the "may only be set while NULL" rule both live in
`app/services/attachments.py` and `app/api/v1/expenses.py`.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # §5.0: not derivable. See the module docstring.
        sa.Column(
            "outlet_id", sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False
        ),
        sa.Column("bucket", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        # Display label only. Never appears in storage_path (§7.2) -- path traversal and
        # collision risk if it did.
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("checksum_sha256", sa.Text(), nullable=False),
        sa.Column(
            "uploaded_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=False
        ),
        # NULL means "not yet claimed by a business row" -- an abandoned upload after 24h
        # is garbage (§7.4). Set once, by whichever row links it; never cleared.
        sa.Column("linked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
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
    )

    op.create_index("ix_attachments_outlet", "attachments", ["outlet_id"])
    # The one query the §7.4 sweep runs. Partial, mirroring ix_expenses_review (0008) --
    # an index over every row that IS linked would be dead weight, since the sweep and
    # nothing else ever scans by linked_at.
    op.create_index(
        "ix_attachments_unlinked",
        "attachments",
        ["created_at"],
        postgresql_where=sa.text("linked_at IS NULL"),
    )

    # --- expenses: attachment_id + receipt_required ---------------------------
    op.add_column(
        "expenses",
        sa.Column(
            "attachment_id", sa.UUID(), sa.ForeignKey("attachments.id"), nullable=True
        ),
    )
    op.add_column(
        "expenses",
        # server_default=false, matching requires_review's shape -- but the API always
        # computes and passes this explicitly (§6.11); the default exists only so a row
        # written outside that path (a fixture, a migration) does not need to know the
        # rule. Existing rows backfill to false: they predate §6.11 and were, by
        # construction, never checked against it -- retroactively marking them true would
        # invent a compliance failure that never happened, the same reasoning §6.11 gives
        # for never recomputing this column after insert.
        sa.Column(
            "receipt_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    op.create_check_constraint(
        "ck_expenses_receipt_required_has_attachment",
        "expenses",
        "reverses_id IS NOT NULL OR receipt_required = false OR attachment_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_expenses_receipt_required_has_attachment", "expenses", type_="check"
    )
    op.drop_column("expenses", "receipt_required")
    op.drop_column("expenses", "attachment_id")

    op.drop_index("ix_attachments_unlinked", table_name="attachments")
    op.drop_index("ix_attachments_outlet", table_name="attachments")
    op.drop_table("attachments")
