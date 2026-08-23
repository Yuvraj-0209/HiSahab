"""Reading the audit trail (CLAUDE.md §5.3, §8, §9).

The table has existed since Phase 4 and nothing could read it back. Every phase since has
written to a trail whose only reader was a raw SQL prompt -- and §5.3 says a cash system
requires an audit trail, which a trail nobody can read is not.

**Read-only, and structurally so.** There is no POST, no PATCH and no DELETE in this module,
and there must never be one. `audit_logs` is append-only, enforced by
`trg_audit_logs_append_only` from migration 0004 as well as by the absence of any route --
§6.6's belt-and-braces principle. A write route here would be a way to edit the record of
what everybody else did.

**Admin only** (§8), and that is not merely caution. Every other read at the manager floor is
a *report* -- shifts, the cash position, the month-end expense summary. This is the control
record, and partly the record of what managers did. It is also a leak boundary: `old_values`
and `new_values` on a `credit_customers` row carry `phone` and `credit_limit`, precisely the
fields §8 forbids an attendant from seeing in the customer list and §9 restricts to
manager-and-above on the customer's own detail route. A manager floor here would expose one
table's restricted columns through a different endpoint.

**No `/audit-logs/{id}`.** A single audit row is meaningless on its own; the question is
always "what happened to this record", which `?record_id=` answers. Not building one also
keeps the static-before-parameterised hazard (fuel_prices.py's `/current`) from ever arising
here.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.api.cursor import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from app.api.deps import Actor, require_role
from app.core.audit import AuditAction
from app.core.roles import Role
from app.db.session import get_db
from app.models.audit import AuditLog

logger = logging.getLogger(__name__)

router = APIRouter(tags=["audit"])


# --- schemas ------------------------------------------------------------------


class AuditLogResponse(BaseModel):
    """One recorded change.

    `old_values` / `new_values` are passed through exactly as stored. `services/audit.py`
    already stringified every `Decimal` on the way in (§3 rule 1 -- `jsonable_encoder`'s
    default for Decimal is `float`, which is the forbidden type *and* drops the scale), so
    money is a string in JSONB and stays one here. Re-encoding at read time would be a second
    place a money value could be floated, for no benefit.

    `record_id` is deliberately not a foreign key on the table (§5.3): it points at rows in
    many tables and has to survive its target being restructured. So it is returned as a bare
    UUID with no attempt to resolve or validate it.
    """

    id: UUID
    table_name: str
    record_id: UUID
    action: AuditAction
    changed_by: UUID
    changed_at: datetime
    old_values: dict[str, Any] | None
    new_values: dict[str, Any] | None
    request_id: str


class AuditLogPage(BaseModel):
    items: list[AuditLogResponse]
    next_cursor: str | None


# --- helpers ------------------------------------------------------------------


def _to_response(row: AuditLog) -> AuditLogResponse:
    return AuditLogResponse(
        id=row.id,
        table_name=row.table_name,
        record_id=row.record_id,
        action=AuditAction(row.action),
        changed_by=row.changed_by,
        changed_at=row.changed_at,
        old_values=row.old_values,
        new_values=row.new_values,
        request_id=row.request_id,
    )


# --- routes -------------------------------------------------------------------


@router.get("/audit-logs", response_model=AuditLogPage)
def list_audit_logs(
    table_name: str | None = Query(default=None),
    record_id: UUID | None = Query(default=None),
    changed_by: UUID | None = Query(default=None),
    # Typed as the enum, so an invalid value is a 422 from FastAPI rather than a silently
    # empty page. The silent version is the dangerous one -- the same argument
    # `FuelTypeUpdate` makes about refusing an immutable field rather than ignoring it: an
    # admin filtering on a typo and getting zero rows would reasonably conclude nothing
    # happened.
    action: AuditAction | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
    actor: Actor = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
) -> AuditLogPage:
    """The trail, newest first, cursor-paginated (§9 -- never `OFFSET`).

    Keyset predicate on `(changed_at, id)` so a change recorded mid-walk cannot make the
    reader skip or repeat a row. That is not a hypothetical here: this table gains a row on
    every financial write in the system, so a page walk during trading is being paged
    *underneath* far more often than any other list endpoint in this API.

    The id is part of the key rather than decoration. Audit rows share a `changed_at` to the
    microsecond routinely -- a reversal and its replacement are inserted in one transaction,
    and PostgreSQL's `now()` is transaction-scoped -- so ordering on the timestamp alone is
    not total, and a page boundary landing inside a tie would drop or repeat rows. Migration
    0014's index is `(outlet_id, changed_at, id)` to match, ascending because a btree scans
    backwards just as cheaply.
    """
    statement = (
        select(AuditLog)
        # §8: always scoped to the outlet the caller holds a role at. Never
        # DEFAULT_OUTLET_ID -- the outlet comes from the actor, so this stays correct the day
        # a second outlet exists.
        .where(AuditLog.outlet_id == actor.outlet_id)
        .order_by(AuditLog.changed_at.desc(), AuditLog.id.desc())
    )

    if table_name is not None:
        statement = statement.where(AuditLog.table_name == table_name)
    if record_id is not None:
        # No existence check, and none is possible: `record_id` is not a foreign key (§5.3).
        # An id that never existed returns an empty page rather than a 404, because nothing
        # can distinguish "no such row" from "that row was never changed", and inventing a
        # 404 would claim knowledge this table does not have.
        statement = statement.where(AuditLog.record_id == record_id)
    if changed_by is not None:
        statement = statement.where(AuditLog.changed_by == changed_by)
    if action is not None:
        statement = statement.where(AuditLog.action == action.value)

    if cursor is not None:
        last_changed_at, last_id = decode_cursor(cursor)
        statement = statement.where(
            tuple_(AuditLog.changed_at, AuditLog.id)
            < tuple_(last_changed_at, last_id)
        )

    # One row beyond the page, purely to learn whether another page exists -- cheaper and
    # more accurate than a COUNT, which would also be wrong the moment a row is inserted.
    rows = db.execute(statement.limit(limit + 1)).scalars().all()
    has_more = len(rows) > limit
    page = rows[:limit]

    return AuditLogPage(
        items=[_to_response(row) for row in page],
        next_cursor=(
            encode_cursor(page[-1].changed_at, page[-1].id)
            if has_more and page
            else None
        ),
    )
