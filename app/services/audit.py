"""Writing to the audit log (CLAUDE.md §5.3).

One function, called from every place that changes something worth remembering. Kept in
services/ rather than in a router so that a management command or a later phase's service
can write an audit row without going through HTTP.

**The caller commits.** `record()` only adds the row to the session, so the audit entry and
the change it describes land in the same transaction or neither does. A separately
committed audit row can describe a change that was subsequently rolled back, which is worse
than no log at all: it is a log that lies.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from decimal import Decimal

from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app.core.audit import AuditAction
from app.core.logging import request_id_ctx
from app.models.audit import AuditLog

logger = logging.getLogger(__name__)

# §3 rule 1: Decimal end-to-end, never float. `jsonable_encoder`'s default for Decimal is
# `float`, which is wrong here twice over. It is the forbidden type, and it silently drops
# the scale -- Decimal("5000.00") comes back as 5000.0, so an audit row can no longer show
# that the stored value carried two decimal places. An audit trail whose whole job is "what
# was this before" must reproduce the value exactly, so money is stringified instead.
#
# The string is what §5.3's consumer wants anyway: `Decimal(row["amount"])` round-trips
# exactly, while `Decimal(5000.0)` reintroduces binary floating point at the last step.
_ENCODERS = {Decimal: str}


def _encode(values: dict[str, Any] | None) -> dict[str, Any] | None:
    """Serialise a snapshot for JSONB, keeping money exact.

    Not optional plumbing: `json.dumps` refuses `Decimal` outright, and money fields are
    precisely what most needs auditing. Phase 3 hit exactly this in the 422 handler, where
    it turned every validation error on a money field into an opaque 500. Without this, the
    first Phase 6 collection or Phase 7 expense written through `record()` would raise
    *inside* the audit write and take the money transaction down with it.
    """
    if values is None:
        return None
    return jsonable_encoder(values, custom_encoder=_ENCODERS)


def record(
    db: Session,
    *,
    outlet_id: UUID,
    table_name: str,
    record_id: UUID,
    action: AuditAction,
    changed_by: UUID,
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
) -> AuditLog:
    """Stage one audit row. The caller is responsible for committing.

    `request_id` is read from the ContextVar in app/core/logging.py, which
    RequestIdMiddleware sets per request. A plain ContextVar rather than a FastAPI
    dependency, so this module honours the rule in app/services/__init__.py that services
    are callable without FastAPI -- outside a request it simply reads the default "-".
    """
    entry = AuditLog(
        outlet_id=outlet_id,
        table_name=table_name,
        record_id=record_id,
        action=action.value,
        changed_by=changed_by,
        old_values=_encode(old_values),
        new_values=_encode(new_values),
        request_id=request_id_ctx.get(),
    )
    db.add(entry)

    logger.info(
        "audit",
        extra={
            "audit_table": table_name,
            "audit_record_id": str(record_id),
            "audit_action": action.value,
            "changed_by": str(changed_by),
        },
    )
    return entry
