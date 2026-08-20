"""§6.10's replay store: a retried POST must not create a second money record.

**"This is not optional."** Attendants use phones on patchy rural connectivity. A request
that times out on the way back looks identical, from the phone, to one that never arrived --
so the client retries, and without this the pump has two ₹5,000 collections and a variance
nobody can explain.

## Why collections need this and readings did not

`nozzle_readings` is idempotent by construction: `UNIQUE (shift_id, nozzle_id)` means a
retried POST returns 409 and the client PATCHes. Phase 5 recorded that and deferred this
module. `collections` has no such key -- §6.9 means one mode legitimately holds several
rows over its lifetime, so the database cannot tell a correction from a duplicate.

## The shape

One reservation row is inserted **before** the handler runs, so two simultaneous retries
race on `uq_idempotency_keys_key_endpoint_user` rather than both writing money. The loser
reads what the winner stored:

* same key, same body, response stored  -> replay it verbatim, create nothing
* same key, same body, response absent  -> 409 `REQUEST_IN_PROGRESS`, the winner is mid-flight
* same key, **different** body          -> 422 `IDEMPOTENCY_KEY_REUSED`

That last case is a client bug, not a retry, and it must not silently return an answer to a
question nobody asked.

## Scope

The tuple is `(key, endpoint, user_id)`, exactly as §6.10 specifies. Scoped by user because
one client's key must never replay another's response; by endpoint because a client reusing
`"retry-1"` across two different calls is careless rather than malicious, and refusing one
of them outright would be worse than keeping them apart.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.idempotency import IdempotencyKey

logger = logging.getLogger(__name__)

# §6.10: "Store (key, endpoint, user_id) -> response for 24 hours."
TTL = timedelta(hours=24)

# Long enough for a UUID or a "shift-12-cash-2026-08-20" style key; short enough that the
# column is not an unbounded write target. 400 rather than 422 for an over-long key,
# consistent with `decode_cursor`: this is a protocol header the client controls, not a
# user-entered field somebody can correct on a form.
MAX_KEY_LENGTH = 200


@dataclass(frozen=True)
class Replay:
    """A stored response to return instead of running the handler."""

    status_code: int
    body: Any


def fingerprint(*, path_params: dict[str, Any], body: Any) -> str:
    """SHA-256 over the request's identity: which row, and what values.

    `sort_keys` and `default=str` so that two encodings of the same request hash the same.
    `default=str` matters for money: a `Decimal` is not JSON-serialisable, and §3 rule 1
    forbids reaching for `float` to make it so.
    """
    payload = json.dumps(
        {"path": {k: str(v) for k, v in sorted(path_params.items())}, "body": body},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _expired(row: IdempotencyKey) -> bool:
    return datetime.now(tz=timezone.utc) - row.created_at > TTL


def begin(
    db: Session,
    *,
    key: str,
    endpoint: str,
    user_id: Any,
    request_fingerprint: str,
) -> Replay | None:
    """Reserve this key, or return the response a previous request already stored.

    Returns `None` when the caller should go ahead and do the work. Returns a `Replay` when
    an identical request has already been answered.

    **Commits the reservation immediately.** The row has to be visible to a concurrent
    retry *before* the handler starts, which is the whole mechanism; holding it in an
    uncommitted transaction would let both requests through. The cost is that a handler
    which then fails leaves a reservation with a NULL response -- see `discard`.
    """
    if len(key) > MAX_KEY_LENGTH:
        raise AppError(
            status_code=400,
            code="IDEMPOTENCY_KEY_TOO_LONG",
            detail=f"An Idempotency-Key may be at most {MAX_KEY_LENGTH} characters.",
        )

    reservation = IdempotencyKey(
        idempotency_key=key,
        endpoint=endpoint,
        user_id=user_id,
        request_fingerprint=request_fingerprint,
    )
    db.add(reservation)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    else:
        return None

    existing = db.execute(
        select(IdempotencyKey).where(
            IdempotencyKey.idempotency_key == key,
            IdempotencyKey.endpoint == endpoint,
            IdempotencyKey.user_id == user_id,
        )
    ).scalar_one()

    if _expired(existing):
        # §6.10 stores a response for 24 hours, and this one is older. The key is free
        # again: delete the stale row and reserve it afresh, rather than replaying an
        # answer from a day ago or refusing a key the client may reasonably reuse.
        db.delete(existing)
        db.commit()
        return begin(
            db,
            key=key,
            endpoint=endpoint,
            user_id=user_id,
            request_fingerprint=request_fingerprint,
        )

    if existing.request_fingerprint != request_fingerprint:
        raise AppError(
            status_code=422,
            code="IDEMPOTENCY_KEY_REUSED",
            detail=(
                "This Idempotency-Key was already used for a different request. A key "
                "identifies one attempt at one action; reusing it for another would "
                "return an answer to a question that was never asked. Send a new key."
            ),
        )

    if existing.response_status is None:
        raise AppError(
            status_code=409,
            code="REQUEST_IN_PROGRESS",
            detail=(
                "An identical request is still being processed. Wait a moment and retry "
                "with the same key."
            ),
        )

    logger.info(
        "idempotent replay",
        extra={"endpoint": endpoint, "status": existing.response_status},
    )
    return Replay(status_code=existing.response_status, body=existing.response_body)


def store(
    db: Session,
    *,
    key: str,
    endpoint: str,
    user_id: Any,
    status_code: int,
    body: Any,
) -> None:
    """Record the response so the next identical request replays it.

    Called after the handler's own `commit()`, so a stored response always corresponds to
    work that actually landed.
    """
    row = db.execute(
        select(IdempotencyKey).where(
            IdempotencyKey.idempotency_key == key,
            IdempotencyKey.endpoint == endpoint,
            IdempotencyKey.user_id == user_id,
        )
    ).scalar_one()
    row.response_status = status_code
    row.response_body = body
    db.commit()


def discard(db: Session, *, key: str, endpoint: str, user_id: Any) -> None:
    """Release a reservation whose handler did not complete.

    Without this a refused request -- a 409 from a business rule, say -- would leave a
    reservation with no response, and every retry would meet `REQUEST_IN_PROGRESS` for the
    next 24 hours. The attendant would be locked out of an action that never happened.

    Only reservations are released. A row that already carries a response is left alone,
    because that response is the thing the store exists to protect.
    """
    row = db.execute(
        select(IdempotencyKey).where(
            IdempotencyKey.idempotency_key == key,
            IdempotencyKey.endpoint == endpoint,
            IdempotencyKey.user_id == user_id,
            IdempotencyKey.response_status.is_(None),
        )
    ).scalar_one_or_none()
    if row is None:
        return
    db.delete(row)
    db.commit()
