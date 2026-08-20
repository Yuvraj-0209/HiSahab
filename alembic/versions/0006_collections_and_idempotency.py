"""collections, the idempotency store, and §6.9's reversal pattern

Revision ID: 0006
Revises: 0005
Create Date: Phase 6 -- what the pump received, against what Phase 5 says it sold

Phase 5 made the meters produce rupees. Nothing yet says what actually arrived, and the gap
between those two figures is the whole reason this system exists.

Three things land together because they are one idea:

* **`collections`** -- money received during a shift, tagged by how it arrived.
* **`idempotency_keys`** (§6.10) -- deferred from Phase 5 with a written reason. A reading
  is idempotent by construction (`uq_nozzle_readings_shift_nozzle`); a collection is not,
  because §6.9 means a mode can legitimately hold several rows over its lifetime.
* **`reverses_id` / `reversal_reason`** -- §6.9's correction path. `collections` is the
  first table the rule can apply to; `nozzle_readings` carried no rupee amount.

**No `UNIQUE (shift_id, mode)`**, and this absence is deliberate -- see the constraint block
below, and `test_collections_have_no_unique_mode_constraint`, which asserts it stays absent.

**No append-only trigger**, same reasoning as 0005: a collection is corrected while its
shift is open, which is the normal workflow, and immutability comes from shift *status* via
`require_shift_access(..., writable=True)`.

**No `outlet_id` on `collections`**, per §5.0: derivable via `shift_id -> shifts.outlet_id`.
**No `outlet_id` on `idempotency_keys`** either, for a different reason -- §5.3: those rows
are infrastructure, carry no business meaning and expire after 24 hours, so there is never
a backfill to get wrong.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# create_type=False plus an explicit .create()/.drop(), matching 0002, 0003 and 0004.
collection_mode_enum = postgresql.ENUM(
    "cash", "card", "upi", "wallet", name="collection_mode", create_type=False
)


def upgrade() -> None:
    collection_mode_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "collections",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("shift_id", sa.UUID(), sa.ForeignKey("shifts.id"), nullable=False),
        sa.Column("mode", collection_mode_enum, nullable=False),
        # §3 rule 1. NUMERIC(12,2) and Decimal end to end -- never float, not even in a
        # test fixture. A collection is the figure the drawer is reconciled against, so a
        # binary-floating-point rounding error here compounds into variance nobody can
        # explain.
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("reference", sa.Text(), nullable=True),
        # §6.9's reversal pointer. A correction after close never edits the original row:
        # it adds a negated row pointing back at it, and both stay visible. Six hundred
        # years of double-entry bookkeeping, and the only way to answer "who changed this,
        # when, and what was it before".
        sa.Column(
            "reverses_id", sa.UUID(), sa.ForeignKey("collections.id"), nullable=True
        ),
        sa.Column("reversal_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
        # A row can be reversed once, ever. Without this, two concurrent corrections each
        # negate the same ₹60,000 and the mode's balance goes ₹60,000 the wrong way.
        sa.UniqueConstraint("reverses_id", name="uq_collections_reverses_id"),
        # §5.2: required when reverses_id is set. In the database as well as the API,
        # because an unexplained negation is an unauditable one. Mirrors
        # ck_nozzle_readings_override_has_reason.
        sa.CheckConstraint(
            "reverses_id IS NULL OR reversal_reason IS NOT NULL",
            name="ck_collections_reversal_has_reason",
        ),
        # A negative amount is *only* ever a reversal, and a reversal is never positive.
        # Money arriving is a positive number; the one thing that may be negative is the
        # cancellation of money that was recorded as arriving and did not.
        #
        # `<= 0` rather than `< 0` on the reversal side because reversing a ₹0 row -- a
        # declared-zero cash row entered against the wrong shift -- negates to zero.
        sa.CheckConstraint(
            "(reverses_id IS NULL AND amount >= 0) "
            "OR (reverses_id IS NOT NULL AND amount <= 0)",
            name="ck_collections_amount_sign",
        ),
        sa.CheckConstraint(
            "reverses_id IS NULL OR reverses_id <> id",
            name="ck_collections_reversal_not_self",
        ),
        # ------------------------------------------------------------------
        # There is deliberately NO `UNIQUE (shift_id, mode)` here.
        #
        # §5.2 does require one *live* row per mode -- this outlet has one card machine and
        # one UPI QR, so a second live cash row is a mistake -- but that rule cannot be a
        # unique constraint, because it is incompatible with §6.9 above. A reversed row
        # stays in the table forever, so its replacement collides with it. Every partial
        # index fails the same way: the replacement carries `reverses_id IS NULL`, exactly
        # like the original it replaces. The only escape would be a `reversed_at` marker
        # stamped onto the original, which is an UPDATE on a financial row in a closed
        # shift -- precisely what §6.9 forbids.
        #
        # So the rule is enforced in app/services/collections.py::live_collection_for_mode,
        # and retry safety comes from idempotency_keys below rather than from a natural key.
        #
        # Written out at length because "add the obvious unique constraint" is exactly what
        # a later consistency pass would do, and it would break corrections for a table
        # that by then holds real money.
        # ------------------------------------------------------------------
    )

    # "Give me this shift's collections" -- every read of the collections page, and §6.8's
    # close precondition.
    op.create_index("ix_collections_shift", "collections", ["shift_id"])
    # The live-row-per-mode lookup, run on every POST before anything is written.
    op.create_index("ix_collections_shift_mode", "collections", ["shift_id", "mode"])

    # ----------------------------------------------------------------------
    # §6.10. "This is not optional": attendants use phones on patchy rural connectivity,
    # and a retry after a timeout must not create a second ₹5,000 row.
    # ----------------------------------------------------------------------
    op.create_table(
        "idempotency_keys",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        # The route template, not the resolved path: two different shifts are two different
        # requests and must not share a replay, which the shift_id inside the fingerprint
        # takes care of.
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column(
            "user_id", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=False
        ),
        # SHA-256 of the path params and canonical JSON body. Lets the store tell a genuine
        # retry (same key, same payload -> replay) from a client bug (same key, different
        # payload -> refuse). Without it, a reused key silently returns somebody else's
        # answer for a request that was never performed.
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        # NULL means in flight: the reservation row is inserted before the handler runs, so
        # that two simultaneous retries race on the unique constraint rather than both
        # writing money.
        sa.Column("response_status", sa.SmallInteger(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # §6.10's exact tuple. Scoped by user because one client's key must never replay
        # another's response, and by endpoint because a client reusing "retry-1" across two
        # different calls is careless, not malicious.
        sa.UniqueConstraint(
            "idempotency_key",
            "endpoint",
            "user_id",
            name="uq_idempotency_keys_key_endpoint_user",
        ),
    )
    # For the 24-hour expiry sweep (app/jobs/cleanup_idempotency_keys.py) and for the
    # freshness filter applied on every lookup.
    op.create_index(
        "ix_idempotency_keys_created_at", "idempotency_keys", ["created_at"]
    )

    # ----------------------------------------------------------------------
    # Phase 5 tidy-up, done here rather than in a migration of its own because the column
    # is one line and 0006 is already open. `opening_reading`, `closing_reading` and
    # `testing_quantity` all carry a non-negative CHECK; `manual_quantity_override` was
    # missed. The API blocks it with condecimal(ge=0), but the database did not, so a
    # fixture or an import could write a negative and it would surface as a 500 deep in
    # sales.py rather than a refusal at the boundary.
    # ----------------------------------------------------------------------
    op.create_check_constraint(
        "ck_nozzle_readings_override_non_negative",
        "nozzle_readings",
        "manual_quantity_override IS NULL OR manual_quantity_override >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_nozzle_readings_override_non_negative", "nozzle_readings", type_="check"
    )
    op.drop_table("idempotency_keys")
    op.drop_table("collections")
    collection_mode_enum.drop(op.get_bind(), checkfirst=True)
