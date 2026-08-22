"""an index for the audit log's read path

Revision ID: 0014
Revises: 0013
Create Date: Phase 11 -- the trail becomes readable, so it needs to be readable *fast*

`audit_logs` has carried two indexes since 0004: `ix_audit_logs_record` on
`(table_name, record_id)` -- "what happened to this row" -- and `ix_audit_logs_changed_at` on
`changed_at` alone. Phase 11 adds `GET /audit-logs`, whose default query is neither of those:

    SELECT ... FROM audit_logs
     WHERE outlet_id = :outlet
     ORDER BY changed_at DESC, id DESC
     LIMIT :n

Every read is outlet-scoped (§8's per-outlet rule), and the sort key is the keyset cursor's
`(changed_at, id)` pair. `ix_audit_logs_changed_at` cannot serve the `outlet_id` filter, and
`ix_audit_logs_record` cannot serve the sort. Without this index the query is a sequential
scan plus a sort over **the fastest-growing table in the schema** -- every financial write
since Phase 4, plus every reference-data write from Phase 11 onward, and §13.17 records that
nothing ever removes a row.

## The index is ASC, and the plan said DESC

`docs/phase-11-plan.md` D6 specified `(outlet_id, changed_at DESC, id DESC)`. That is
unnecessary here and carries a real cost, so it is deliberately not what was built.

**Unnecessary:** a PostgreSQL btree scans backwards as cheaply as forwards. A DESC index only
earns its keep when the ordering is *mixed* -- `changed_at DESC, id ASC` -- because one index
cannot satisfy both directions at once. This sort is uniformly DESC on both columns, so a
plain ascending index serves it via a backward scan with no penalty. `ix_audit_logs_changed_at`
has been doing exactly that since 0004.

**Costly:** expressing DESC requires `sa.text("changed_at DESC")` in both the migration and
the model's `__table_args__`, which turns a plain column index into an expression index.
SQLAlchemy's autogenerate cannot reliably reflect and compare those, so `alembic check` starts
reporting drift that is not real -- and this project's verification checklist runs
`alembic check` on every phase. Trading a permanent false positive for a benefit that does not
exist is a bad trade.

`id` is the third column rather than being left out: the keyset predicate is a row comparison
`(changed_at, id) < (:last_changed_at, :last_id)`, and including the tiebreaker lets the whole
seek happen inside the index instead of filtering after it.

## What this migration deliberately does not add

* **A partial index per `table_name`.** `?table_name=` is an optional filter, and the leading
  `outlet_id` column already narrows the scan. Speculative until a query is slow (§11).
* **A GIN index on `old_values` / `new_values`.** Those columns are read and rendered, never
  filtered on -- there is no endpoint that searches inside a snapshot, and §6 of the plan puts
  one out of scope.

No columns, no enums, no tables, no constraints. The `_CONSTRAINT_ERRORS` map in
`app/core/errors.py` is untouched, because an index is not a unique constraint and cannot
raise a business error.
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_audit_logs_outlet_changed_at",
        "audit_logs",
        ["outlet_id", "changed_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_outlet_changed_at", table_name="audit_logs")
