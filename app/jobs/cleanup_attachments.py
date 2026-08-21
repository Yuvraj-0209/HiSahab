"""Delete abandoned uploads (CLAUDE.md §7.4).

    python -m app.jobs.cleanup_attachments [--dry-run]

§7.4: an unlinked attachment older than 24 hours is garbage -- an upload that started (a
row was written, an object landed in storage) and was never referenced by an expense or
credit sale. A management command run manually or via cron, matching
`app/jobs/cleanup_idempotency_keys.py`'s own posture and shape. §12 is explicit that this
project does not build a job scheduler.

**This hard-deletes, and that is not a breach of §3 rule 6.** That rule protects
*financial* tables. `attachments` is not one, and `app/services/attachments.py::orphans`
only ever returns rows with `linked_at IS NULL` -- by definition referenced by no financial
row. **A linked attachment is never deleted here, at any age**, including one whose
expense was later reversed: §6.9 keeps both rows, so the receipt stays evidence.

**Delete the storage object first, then the row.** If the object delete fails (a real
outage, not a missing object -- `StorageBackend.delete()` already treats a 404 as success),
this run skips that one attachment and moves on; the row survives and the next run tries
again. Committing per row rather than once at the end means a mid-run failure leaves
already-processed rows cleaned up rather than rolling the whole batch back.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.session import SessionLocal
from app.services.attachments import ORPHAN_TTL, orphans
from app.services.storage import StorageBackend, build_storage


def main(argv: list[str] | None = None, *, storage: StorageBackend | None = None) -> int:
    """`argv=None` reads real `sys.argv`; tests pass a list directly (see
    `app/jobs/provision_user.py` for the same shape) so failures surface as Python
    tracebacks rather than depending on which interpreter is on PATH.

    `storage=None` builds the real backend from config, same as every other entry point.
    Tests inject a `LocalStorage` rooted in their own `tmp_path` instead -- this command
    runs standalone, outside the ASGI app, so there is no `get_storage` dependency to
    override the way `tests/conftest.py::client` does for the API.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report how many attachments would be deleted, and delete nothing",
    )
    args = parser.parse_args(argv)

    cutoff = datetime.now(tz=timezone.utc) - ORPHAN_TTL
    if storage is None:
        storage = build_storage(get_settings())

    with SessionLocal() as session:
        rows = orphans(session, cutoff=cutoff)

        if args.dry_run:
            print(f"would delete {len(rows)} unlinked attachment(s) older than {cutoff}")
            return 0

        deleted = 0
        for row in rows:
            try:
                storage.delete(bucket=row.bucket, paths=[row.storage_path])
            except AppError as exc:
                # Loud, not swallowed -- the Phase 7 audit's lesson (§14's "keep it
                # loud") applies here too: a skipped row should be visible to whoever
                # runs this by hand or reads the cron log, not silently retried forever
                # with no trace.
                print(
                    f"skip {row.id} ({row.storage_path}): {exc.detail}", file=sys.stderr
                )
                continue
            session.delete(row)
            session.commit()
            deleted += 1

    print(f"deleted {deleted} unlinked attachment(s) (of {len(rows)} eligible)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
