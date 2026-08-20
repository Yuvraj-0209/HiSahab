"""Delete expired idempotency reservations.

    python -m app.jobs.cleanup_idempotency_keys [--dry-run]

§6.10 stores a response for 24 hours. Nothing reads a row older than that -- `begin()`
treats one as a free key and replaces it -- so what remains is dead weight on a table every
money-creating POST writes to.

A command run manually or from cron, matching §7.4's posture for orphaned attachments. §12
is explicit that this project does not build a job scheduler.

Safe to run at any time: it only removes rows the application already ignores, and the TTL
constant is the same one `app/core/idempotency.py` enforces, imported rather than repeated
so the two can never disagree about what "expired" means.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import delete, func, select

from app.core.idempotency import TTL
from app.db.session import SessionLocal
from app.models.idempotency import IdempotencyKey


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report how many rows would be deleted, and delete nothing",
    )
    args = parser.parse_args()

    cutoff = datetime.now(tz=timezone.utc) - TTL

    with SessionLocal() as session:
        expired = session.execute(
            select(func.count())
            .select_from(IdempotencyKey)
            .where(IdempotencyKey.created_at < cutoff)
        ).scalar_one()

        if args.dry_run:
            print(f"would delete {expired} expired idempotency key(s) older than {cutoff}")
            return 0

        session.execute(
            delete(IdempotencyKey).where(IdempotencyKey.created_at < cutoff)
        )
        session.commit()

    print(f"deleted {expired} expired idempotency key(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
