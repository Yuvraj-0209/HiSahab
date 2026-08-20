"""Collections -- what the pump actually received (CLAUDE.md §5.2, §6.8, §6.9, §6.10).

Phase 5 made the meters produce rupees. This module records the other half of the day: the
money that arrived, tagged by how it arrived. The two figures are never derived from one
another, because the gap between them is the signal.

**Four routes.** Create, correct, list, reverse.

**One live row per mode (§5.2).** One card machine and one UPI QR, and a paper register
that writes one lumped figure per mode -- so a second live `cash` row is a mistake, and
`POST` refuses it with 409 `COLLECTION_ALREADY_EXISTS` so the client `PATCH`es instead.
This cannot be a unique constraint; see migration 0006's long comment, and
`app/services/collections.py::live_collection_for_mode`.

**Corrections append, never edit (§6.9).** While the shift is open a `PATCH` is fine --
nothing has been reconciled against yet. Once it is closed, the row is history: the
reversal route adds a negated row pointing back at the original, and both stay visible.

**Retries do not duplicate money (§6.10).** Both money-creating POSTs require an
`Idempotency-Key`. Neither the shape of the table nor the shape of the shift can protect
against a phone retrying a request that already landed.

**Role floors (§8).** Attendants record collections on their own shift, ownership enforced
by `require_shift_access` and never re-implemented here. Reversing is a manager's act, and
reversing on a *locked* shift is an admin's.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    condecimal,
)
from sqlalchemy.orm import Session

from app.api.deps import ShiftAccess, require_shift_access
from app.core import idempotency
from app.core.audit import AuditAction
from app.core.collections import CollectionMode
from app.core.errors import AppError
from app.core.roles import Role, satisfies
from app.core.shifts import ShiftStatus
from app.db.session import get_db
from app.models.collection import Collection
from app.services import audit, collections as collection_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["collections"])

# A shift holds one live row per mode -- four at most -- plus whatever reversal history has
# accumulated. Returned as a capped full list rather than a keyset page, matching
# `shift_templates.py`'s `_MAX_ROWS` and `/nozzles`. §9 asks for cursor pagination on list
# endpoints because `OFFSET` skips and duplicates rows under concurrent inserts; a list
# that is bounded by the number of payment channels an outlet has cannot page at all, and a
# cursor here would be ceremony around a single request. The cap exists so that a runaway
# correction loop surfaces as a truncated list rather than an unbounded response.
_MAX_ROWS = 100


# --- schemas -----------------------------------------------------------------

# §3 rule 1: money is NUMERIC(12,2) and Decimal end to end. `condecimal` at the boundary so
# a client sending three decimal places is a 422 rather than a silent round, and so a float
# never enters the system at all.
MoneyValue = condecimal(max_digits=12, decimal_places=2, ge=0)


class CollectionResponse(BaseModel):
    id: UUID
    shift_id: UUID
    mode: CollectionMode
    amount: Decimal
    reference: str | None
    reverses_id: UUID | None
    reversal_reason: str | None
    is_reversed: bool


class CollectionPage(BaseModel):
    """A shift's collections, with the two derived figures worth having on the page.

    `totals_by_mode` nets reversals into the sum, so a cancelled ₹60,000 shows as the
    reduction it is instead of vanishing.

    `declared_cash` is `null` when nobody has declared, and `"0.00"` when somebody declared
    zero. **Those are different answers** and the API keeps them apart -- see
    `app/services/collections.py::declared_cash`. A client must not coalesce them.
    """

    items: list[CollectionResponse]
    totals_by_mode: dict[str, Decimal]
    declared_cash: Decimal | None
    cash_basis: str = (
        "declared_cash is what the salesman says he counted, not a figure derived from "
        "sales. CLAUDE.md §6.4 computes cash as a residual and never reads this row; the "
        "gap between the two is the shortfall. Do not add this to a derived cash figure."
    )
    truncated: bool


class CollectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: CollectionMode
    # No `gt=0`. §6.8 requires an explicit zero to be enterable: on a day that genuinely
    # took no cash the salesman declares ₹0, and that is a different fact from having
    # entered nothing. Zero as an answer, never zero as an omission.
    amount: MoneyValue
    reference: str | None = Field(default=None, max_length=200)


class CollectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # No `mode`. Changing which channel money arrived through is not a correction of this
    # row, it is a different row -- and allowing it here would let a PATCH walk around the
    # one-live-row-per-mode rule by moving a second cash row's mode after the fact.
    amount: MoneyValue | None = None
    reference: str | None = Field(default=None, max_length=200)


class CollectionReversal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # §6.9: "a mandatory reason". `strip_whitespace` runs BEFORE the length check, which
    # is the whole point: with a bare `Field(min_length=3)` the check saw the raw string,
    # so "   " passed at length 3 and the handler's own `.strip()` then stored it as "".
    # A 60,000 negation with a blank reason is precisely the row §6.9 exists to prevent,
    # and `ck_collections_reversal_has_reason` only ever required NOT NULL.
    reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)
    ]
    # Applied in the same transaction. Without it a correction on a closed shift is
    # impossible: the reversal lands and the follow-up POST is refused by `writable=True`.
    replacement_amount: MoneyValue | None = None
    replacement_reference: str | None = Field(default=None, max_length=200)


class ReversalResponse(BaseModel):
    reversal: CollectionResponse
    replacement: CollectionResponse | None
    original: CollectionResponse


# --- helpers -----------------------------------------------------------------


def _to_response(collection: Collection, *, is_reversed: bool = False) -> CollectionResponse:
    return CollectionResponse(
        id=collection.id,
        shift_id=collection.shift_id,
        mode=CollectionMode(collection.mode),
        amount=collection.amount,
        reference=collection.reference,
        reverses_id=collection.reverses_id,
        reversal_reason=collection.reversal_reason,
        is_reversed=is_reversed,
    )


def _audit_snapshot(collection: Collection) -> dict[str, object]:
    """The fields worth recording either side of a change.

    Money and the reversal link. `created_at` and `created_by` never change, so recording
    them would pad every row with noise that carries no information about the change.
    """
    return {
        "mode": collection.mode,
        "amount": collection.amount,
        "reference": collection.reference,
        "reverses_id": collection.reverses_id,
    }


def _load_collection(db: Session, shift_id: UUID, collection_id: UUID) -> Collection:
    """Fetch a collection, refusing one that belongs to a different shift.

    The 409 rather than a 404 is deliberate: the row exists and the caller may well be
    allowed to see it, but the URL asserts a parent-child relationship that is not true.
    Reporting "not found" would send someone hunting for a row that is sitting right there.
    """
    collection = db.get(Collection, collection_id)
    if collection is None:
        raise AppError(
            status_code=404,
            code="COLLECTION_NOT_FOUND",
            detail="No collection with that id.",
        )
    if collection.shift_id != shift_id:
        raise AppError(
            status_code=409,
            code="COLLECTION_NOT_IN_SHIFT",
            detail="That collection belongs to a different shift.",
        )
    return collection


def _require_key(idempotency_key: str | None) -> str:
    """§6.10: the header is not optional on a POST that creates a money record.

    Refusing rather than defaulting to "no deduplication" is the point. A client that
    forgets the header is exactly the client whose retry will duplicate a ₹5,000 row, and a
    silent fallback would leave that failure to be discovered in a cash count.
    """
    if not idempotency_key:
        raise AppError(
            status_code=400,
            code="IDEMPOTENCY_KEY_REQUIRED",
            detail=(
                "Send an Idempotency-Key header with this request. It is what stops a "
                "retry after a timeout from recording the same money twice."
            ),
        )
    return idempotency_key


def _replayed(replay: idempotency.Replay) -> JSONResponse:
    return JSONResponse(status_code=replay.status_code, content=replay.body)


# --- routes ------------------------------------------------------------------


@router.get("/shifts/{shift_id}/collections", response_model=CollectionPage)
def list_collections(
    access: ShiftAccess = Depends(require_shift_access(Role.attendant)),
    db: Session = Depends(get_db),
) -> CollectionPage:
    """Everything received on this shift, reversals included (§6.9: both rows stay visible)."""
    shift = access.shift
    rows = collection_service.all_collections(db, shift_id=shift.id)
    # Computed before truncation: a reversal that fell past the cap would otherwise make
    # the row it cancels read as still live, which is the one thing this flag is for.
    reversed_ids = {row.reverses_id for row in rows if row.reverses_id is not None}
    truncated = len(rows) > _MAX_ROWS
    rows = rows[:_MAX_ROWS]

    return CollectionPage(
        items=[_to_response(row, is_reversed=row.id in reversed_ids) for row in rows],
        totals_by_mode=collection_service.totals_by_mode(db, shift_id=shift.id),
        declared_cash=collection_service.declared_cash(db, shift_id=shift.id),
        truncated=truncated,
    )


@router.post(
    "/shifts/{shift_id}/collections",
    response_model=CollectionResponse,
    status_code=201,
)
def create_collection(
    payload: CollectionCreate,
    access: ShiftAccess = Depends(require_shift_access(Role.attendant, writable=True)),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Record money received. Attendant floor, own shift only (§8)."""
    shift = access.shift
    actor = access.actor
    key = _require_key(idempotency_key)
    endpoint = "POST /shifts/{shift_id}/collections"

    replay = idempotency.begin(
        db,
        key=key,
        endpoint=endpoint,
        user_id=actor.user.id,
        request_fingerprint=idempotency.fingerprint(
            path_params={"shift_id": shift.id}, body=jsonable_encoder(payload)
        ),
    )
    if replay is not None:
        return _replayed(replay)

    try:
        existing = collection_service.live_collection_for_mode(
            db, shift_id=shift.id, mode=payload.mode
        )
        if existing is not None:
            raise AppError(
                status_code=409,
                code="COLLECTION_ALREADY_EXISTS",
                detail=(
                    f"This shift already has a {payload.mode.value} figure of "
                    f"{existing.amount}. This outlet records one figure per payment "
                    "channel per shift -- correct the existing one with PATCH rather than "
                    "adding a second."
                ),
            )

        collection = Collection(
            shift_id=shift.id,
            mode=payload.mode.value,
            amount=payload.amount,
            reference=payload.reference,
            created_by=actor.user.id,
        )
        db.add(collection)
        db.flush()

        audit.record(
            db,
            outlet_id=shift.outlet_id,
            table_name="collections",
            record_id=collection.id,
            action=AuditAction.insert,
            changed_by=actor.user.id,
            new_values=_audit_snapshot(collection),
        )
        db.commit()
        db.refresh(collection)
    except Exception:
        # The reservation was committed before the work started, so a refusal here would
        # otherwise leave the key wedged at REQUEST_IN_PROGRESS for 24 hours -- locking the
        # attendant out of an action that never happened.
        db.rollback()
        idempotency.discard(db, key=key, endpoint=endpoint, user_id=actor.user.id)
        raise

    body = jsonable_encoder(_to_response(collection))
    idempotency.store(
        db,
        key=key,
        endpoint=endpoint,
        user_id=actor.user.id,
        status_code=201,
        body=body,
    )

    logger.info(
        "collection recorded",
        extra={
            "collection_id": str(collection.id),
            "shift_id": str(shift.id),
            "mode": collection.mode,
            "amount": str(collection.amount),
        },
    )
    return _to_response(collection)


@router.patch(
    "/shifts/{shift_id}/collections/{collection_id}", response_model=CollectionResponse
)
def update_collection(
    collection_id: UUID,
    payload: CollectionUpdate,
    access: ShiftAccess = Depends(require_shift_access(Role.attendant, writable=True)),
    db: Session = Depends(get_db),
) -> CollectionResponse:
    """Correct a figure while the shift is still open.

    No Idempotency-Key: a PATCH is idempotent by construction. Sending the same amount
    twice leaves the row in the same state, so a retry cannot duplicate money.

    Only reachable on an open shift -- `writable=True` refuses a closed or locked one, and
    from there §6.9's reversal route is the correction path.
    """
    shift = access.shift
    actor = access.actor
    collection = _load_collection(db, shift.id, collection_id)

    if collection.reverses_id is not None:
        raise AppError(
            status_code=409,
            code="CANNOT_EDIT_A_REVERSAL",
            detail=(
                "A reversal records what was cancelled and why. Editing it would rewrite "
                "the correction itself. Record a fresh collection instead."
            ),
        )

    # The mirror of the check above, and the one that was missing. A row that *has been*
    # reversed is finished: the reversal still carries the negated original amount, so
    # editing the original to 58,000 after a 60,000 reversal leaves the mode netting to
    # -2,000 and the audit log claiming a row cancelled at 60,000 now reads 58,000.
    # §6.9's guarantee is that the original is never touched; `writable=True` alone does
    # not deliver it, because a reversal is legal on an *open* shift too.
    if collection_service.reversal_of(db, collection_id=collection.id) is not None:
        raise AppError(
            status_code=409,
            code="COLLECTION_ALREADY_REVERSED",
            detail=(
                "This collection has been reversed and its figure is now part of the "
                "record. Record the corrected amount as a new collection rather than "
                "editing a cancelled one."
            ),
        )

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise AppError(
            status_code=422,
            code="NO_FIELDS_TO_UPDATE",
            detail="Provide at least one field to change.",
        )

    before = _audit_snapshot(collection)
    for field, value in changes.items():
        # An explicit null is ignored rather than treated as "clear this", matching
        # `readings.py`: a client that omits a field and one that sends null both mean
        # "leave it alone" far more often than they mean "erase it".
        if value is None:
            continue
        setattr(collection, field, value)

    audit.record(
        db,
        outlet_id=shift.outlet_id,
        table_name="collections",
        record_id=collection.id,
        action=AuditAction.update,
        changed_by=actor.user.id,
        old_values=before,
        new_values=_audit_snapshot(collection),
    )
    db.commit()
    db.refresh(collection)

    logger.info(
        "collection updated",
        extra={"collection_id": str(collection.id), "fields": sorted(changes)},
    )
    return _to_response(collection)


@router.post(
    "/shifts/{shift_id}/collections/{collection_id}/reversals",
    response_model=ReversalResponse,
    status_code=201,
)
def reverse_collection(
    collection_id: UUID,
    payload: CollectionReversal,
    access: ShiftAccess = Depends(require_shift_access(Role.manager)),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Cancel a collection by appending a negated row (§6.9). Manager floor; admin if locked.

    Deliberately **not** `writable=True`. That dependency refuses closed shifts, and a
    closed shift is exactly where this route is needed: a miscount is discovered during
    reconciliation, which happens after close. Same reasoning as Phase 5's review route.

    A *locked* shift is admin-only rather than refused. §6.9 names locked shifts as needing
    the reversal path, and §5.2's "nothing referencing a locked shift may be modified"
    still holds -- a reversal modifies nothing, it appends. `app/api/deps.py`'s own
    SHIFT_LOCKED message already says corrections must be recorded as reversal entries.
    """
    shift = access.shift
    actor = access.actor

    if ShiftStatus(shift.status) is ShiftStatus.locked and not satisfies(
        actor.role, Role.admin
    ):
        raise AppError(
            status_code=403,
            code="LOCKED_SHIFT_REVERSAL_REQUIRES_ADMIN",
            detail=(
                "This shift is locked. A reversal against it is an admin action, because "
                "locking is the point at which a day stops being anybody else's to change."
            ),
        )

    key = _require_key(idempotency_key)
    endpoint = "POST /shifts/{shift_id}/collections/{collection_id}/reversals"

    replay = idempotency.begin(
        db,
        key=key,
        endpoint=endpoint,
        user_id=actor.user.id,
        request_fingerprint=idempotency.fingerprint(
            path_params={"shift_id": shift.id, "collection_id": collection_id},
            body=jsonable_encoder(payload),
        ),
    )
    if replay is not None:
        return _replayed(replay)

    try:
        original = _load_collection(db, shift.id, collection_id)
        before = _audit_snapshot(original)

        reversal, replacement = collection_service.reverse(
            db,
            original=original,
            reason=payload.reason,  # already stripped by StringConstraints
            actor_id=actor.user.id,
            replacement_amount=payload.replacement_amount,
            replacement_reference=payload.replacement_reference,
        )

        audit.record(
            db,
            outlet_id=shift.outlet_id,
            table_name="collections",
            record_id=reversal.id,
            action=AuditAction.reversal,
            changed_by=actor.user.id,
            old_values=before,
            new_values=_audit_snapshot(reversal) | {"reason": reversal.reversal_reason},
        )
        if replacement is not None:
            audit.record(
                db,
                outlet_id=shift.outlet_id,
                table_name="collections",
                record_id=replacement.id,
                action=AuditAction.insert,
                changed_by=actor.user.id,
                new_values=_audit_snapshot(replacement)
                | {"replaces": str(original.id)},
            )
        db.commit()
        db.refresh(original)
        db.refresh(reversal)
        if replacement is not None:
            db.refresh(replacement)
    except Exception:
        db.rollback()
        idempotency.discard(db, key=key, endpoint=endpoint, user_id=actor.user.id)
        raise

    result = ReversalResponse(
        reversal=_to_response(reversal),
        replacement=_to_response(replacement) if replacement is not None else None,
        original=_to_response(original, is_reversed=True),
    )
    idempotency.store(
        db,
        key=key,
        endpoint=endpoint,
        user_id=actor.user.id,
        status_code=201,
        body=jsonable_encoder(result),
    )
    return result
