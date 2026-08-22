"""Non-fuel sales -- lubricants, coolant, anything no meter counted (CLAUDE.md §6.4, §13.2).

Phase 10. §6.4 has named this term since the beginning and §5.2 gave it a column nowhere,
so until now there was no way to record that a bottle of oil was sold. That gap is not
cosmetic: it makes an honest salesman look like a thief in reverse.

## Why it is per shift

A ₹500 bottle of oil is in the salesman's hand and **not** in the meter-derived figure --
but it *is* inside the cash he counts into the locker. Compare derived fuel cash against his
declaration without it and he shows a ₹500 **surplus**, in his own name, on every day he
sells one. The figure has to sit beside the declaration it is checked against, and that is
the shift.

## Why there is no `mode`

The amount is added to §6.4's `total_sales`, never to the cash side. A card-paid oil sale is
already inside the card collections total, so adding it to the cash side too would understate
derived cash by exactly its amount. On the sales side the arithmetic is right however the
customer paid -- so this table does not need to know, and a `mode` column would be a
question with no consumer.

## What this is not

Not an itemised sales module. §12 puts one out of V1 and §13.2 says non-fuel income is "a
manual entry field". An amount and an optional note: no product, no stock, no unit price. It
is a *table* rather than a column on `shifts` only because it holds money, and §6.9 corrects
money by appending a reversal -- which a column cannot do.

**Role floors (§8).** Attendants record these on their own shift, ownership enforced by
`require_shift_access` and never re-implemented here. Reversing is a manager's act, and
reversing on a *locked* shift is an admin's.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, condecimal
from sqlalchemy.orm import Session

from app.api.deps import ShiftAccess, require_shift_access
from app.core import idempotency
from app.core.audit import AuditAction
from app.core.errors import AppError
from app.core.roles import Role, satisfies
from app.core.shifts import ShiftStatus
from app.db.session import get_db
from app.models.cash import NonFuelSale
from app.services import audit, cash as cash_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["non-fuel sales"])

# Matches `collections.py` and `expenses.py`. A shift records a handful of these; the cap
# turns a runaway correction loop into a truncated list rather than an unbounded response.
_MAX_ROWS = 100


# --- schemas -----------------------------------------------------------------

# §3 rule 1: Decimal end to end. `condecimal` at the boundary so three decimal places is a
# 422 rather than a silent round, and so a float never enters the system at all.
#
# `gt=0`, unlike `collections`' `ge=0`. §6.8 needs an explicit ₹0 *cash declaration* to be
# enterable -- zero as an answer, not as an omission -- but a ₹0 non-fuel sale records
# nothing at all, which is the same reasoning `expenses` uses for its strict sign rule.
MoneyValue = condecimal(max_digits=12, decimal_places=2, gt=0)


class NonFuelSaleResponse(BaseModel):
    id: UUID
    shift_id: UUID
    amount: Decimal
    description: str | None
    reverses_id: UUID | None
    reversal_reason: str | None
    is_reversed: bool


class NonFuelSalePage(BaseModel):
    items: list[NonFuelSaleResponse]
    total: Decimal
    sales_basis: str = (
        "total is added to CLAUDE.md §6.4's total_sales, NOT to the cash side. A "
        "card-paid non-fuel sale is already inside the card collections figure; adding "
        "it to cash as well would understate derived cash by its amount."
    )
    truncated: bool


class NonFuelSaleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: MoneyValue
    description: str | None = Field(default=None, max_length=200)


class NonFuelSaleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # No `shift_id`. Moving money to a different shift is a different row, not a correction
    # -- and it would move it to a different salesman's accountability figure. §6.9's
    # reversal is the path, the same rule `expenses` and `credit_sales` already apply.
    amount: MoneyValue | None = None
    description: str | None = Field(default=None, max_length=200)


class NonFuelSaleReversal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # `strip_whitespace` runs BEFORE the length check, which is the entire point: with a
    # bare `Field(min_length=3)` the check sees the raw string, so "   " passes at length 3
    # and is then stored as "". The 0007 lesson, and the database CHECK is the backstop.
    reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)
    ]
    replacement_amount: MoneyValue | None = None
    replacement_description: str | None = Field(default=None, max_length=200)


class NonFuelSaleReversalResponse(BaseModel):
    reversal: NonFuelSaleResponse
    replacement: NonFuelSaleResponse | None
    original: NonFuelSaleResponse


# --- helpers -----------------------------------------------------------------


def _to_response(row: NonFuelSale, *, is_reversed: bool = False) -> NonFuelSaleResponse:
    return NonFuelSaleResponse(
        id=row.id,
        shift_id=row.shift_id,
        amount=row.amount,
        description=row.description,
        reverses_id=row.reverses_id,
        reversal_reason=row.reversal_reason,
        is_reversed=is_reversed,
    )


def _audit_snapshot(row: NonFuelSale) -> dict[str, object]:
    """The fields worth recording either side of a change. `created_at` / `created_by`
    never change, so recording them would pad every row with noise."""
    return {
        "amount": row.amount,
        "description": row.description,
        "reverses_id": row.reverses_id,
    }


def _load_sale(db: Session, shift_id: UUID, sale_id: UUID) -> NonFuelSale:
    """Fetch a row, refusing one that belongs to a different shift.

    409 rather than 404: the row exists and the caller may well be allowed to see it, but
    the URL asserts a parent-child relationship that is not true. "Not found" would send
    somebody hunting for a row sitting right there.
    """
    row = db.get(NonFuelSale, sale_id)
    if row is None:
        raise AppError(
            status_code=404,
            code="NON_FUEL_SALE_NOT_FOUND",
            detail="No non-fuel sale with that id.",
        )
    if row.shift_id != shift_id:
        raise AppError(
            status_code=409,
            code="NON_FUEL_SALE_NOT_IN_SHIFT",
            detail="That non-fuel sale belongs to a different shift.",
        )
    return row


def _require_key(idempotency_key: str | None) -> str:
    """§6.10: not optional on a POST that creates a money record. A client that forgets the
    header is exactly the client whose retry duplicates a row, and a silent fallback would
    leave that to be discovered in a cash count."""
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


@router.get("/shifts/{shift_id}/non-fuel-sales", response_model=NonFuelSalePage)
def list_non_fuel_sales(
    access: ShiftAccess = Depends(require_shift_access(Role.attendant)),
    db: Session = Depends(get_db),
) -> NonFuelSalePage:
    """Everything sold off-meter on this shift, reversals included (§6.9)."""
    shift = access.shift
    rows = cash_service.all_non_fuel_sales(db, shift_id=shift.id)
    # Computed before truncation: a reversal past the cap would otherwise make the row it
    # cancels read as still live, which is the one thing this flag is for.
    reversed_ids = {row.reverses_id for row in rows if row.reverses_id is not None}
    truncated = len(rows) > _MAX_ROWS
    rows = rows[:_MAX_ROWS]

    return NonFuelSalePage(
        items=[_to_response(row, is_reversed=row.id in reversed_ids) for row in rows],
        # From the service, over the whole shift -- never a Python sum over the truncated
        # list above. That is the Phase 9 defect Step 0 of this phase fixed on
        # `credit_repayments`, and it is trivially easy to reintroduce here.
        total=cash_service.non_fuel_sales_total(db, shift_id=shift.id),
        truncated=truncated,
    )


@router.post(
    "/shifts/{shift_id}/non-fuel-sales",
    response_model=NonFuelSaleResponse,
    status_code=201,
)
def create_non_fuel_sale(
    payload: NonFuelSaleCreate,
    access: ShiftAccess = Depends(require_shift_access(Role.attendant, writable=True)),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Record a non-fuel sale. Attendant floor, own shift only (§8).

    Unlike `collections`, there is **no one-live-row rule**. A shift can genuinely sell three
    separate bottles of oil to three customers, and each is its own row. The lumping that
    justifies one row per collection mode -- one card machine, one QR, one register line --
    has no equivalent here.
    """
    shift = access.shift
    actor = access.actor
    key = _require_key(idempotency_key)
    endpoint = "POST /shifts/{shift_id}/non-fuel-sales"

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
        row = NonFuelSale(
            shift_id=shift.id,
            amount=payload.amount,
            description=payload.description,
            created_by=actor.user.id,
        )
        db.add(row)
        db.flush()

        audit.record(
            db,
            outlet_id=shift.outlet_id,
            table_name="non_fuel_sales",
            record_id=row.id,
            action=AuditAction.insert,
            changed_by=actor.user.id,
            new_values=_audit_snapshot(row),
        )
        db.commit()
        db.refresh(row)
    except Exception:
        # The reservation was committed before the work started, so a refusal here would
        # otherwise wedge the key at REQUEST_IN_PROGRESS for 24 hours -- locking the
        # attendant out of an action that never happened.
        db.rollback()
        idempotency.discard(db, key=key, endpoint=endpoint, user_id=actor.user.id)
        raise

    body = jsonable_encoder(_to_response(row))
    idempotency.store(
        db,
        key=key,
        endpoint=endpoint,
        user_id=actor.user.id,
        status_code=201,
        body=body,
    )

    logger.info(
        "non-fuel sale recorded",
        extra={
            "non_fuel_sale_id": str(row.id),
            "shift_id": str(shift.id),
            "amount": str(row.amount),
        },
    )
    return _to_response(row)


@router.patch(
    "/shifts/{shift_id}/non-fuel-sales/{sale_id}", response_model=NonFuelSaleResponse
)
def update_non_fuel_sale(
    sale_id: UUID,
    payload: NonFuelSaleUpdate,
    access: ShiftAccess = Depends(require_shift_access(Role.attendant, writable=True)),
    db: Session = Depends(get_db),
) -> NonFuelSaleResponse:
    """Correct a figure while the shift is still open.

    No Idempotency-Key: a PATCH is idempotent by construction -- sending the same amount
    twice leaves the row in the same state, so a retry cannot duplicate money.
    """
    shift = access.shift
    actor = access.actor
    row = _load_sale(db, shift.id, sale_id)

    if row.reverses_id is not None:
        raise AppError(
            status_code=409,
            code="CANNOT_EDIT_A_REVERSAL",
            detail=(
                "A reversal records what was cancelled and why. Editing it would rewrite "
                "the correction itself. Record a fresh non-fuel sale instead."
            ),
        )

    # The mirror of the check above. A row that *has been* reversed is finished: the
    # reversal still carries the negated original amount, so editing the original to ₹300
    # after a ₹500 reversal leaves the shift netting to -₹200 and the audit log claiming a
    # row cancelled at ₹500 now reads ₹300.
    if cash_service.reversal_of(db, NonFuelSale, row_id=row.id) is not None:
        raise AppError(
            status_code=409,
            code="NON_FUEL_SALE_ALREADY_REVERSED",
            detail=(
                "This non-fuel sale has been reversed and its figure is now part of the "
                "record. Record the corrected amount as a new sale rather than editing a "
                "cancelled one."
            ),
        )

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise AppError(
            status_code=422,
            code="NO_FIELDS_TO_UPDATE",
            detail="Provide at least one field to change.",
        )

    before = _audit_snapshot(row)
    for field, value in changes.items():
        # An explicit null is ignored rather than treated as "clear this", matching
        # `collections.py` and `readings.py`. `description` is the only nullable column
        # here and clearing it destroys the only human context the row carries.
        if value is None:
            continue
        setattr(row, field, value)

    audit.record(
        db,
        outlet_id=shift.outlet_id,
        table_name="non_fuel_sales",
        record_id=row.id,
        action=AuditAction.update,
        changed_by=actor.user.id,
        old_values=before,
        new_values=_audit_snapshot(row),
    )
    db.commit()
    db.refresh(row)

    logger.info(
        "non-fuel sale updated",
        extra={"non_fuel_sale_id": str(row.id), "fields": sorted(changes)},
    )
    return _to_response(row)


@router.post(
    "/shifts/{shift_id}/non-fuel-sales/{sale_id}/reversals",
    response_model=NonFuelSaleReversalResponse,
    status_code=201,
)
def reverse_non_fuel_sale(
    sale_id: UUID,
    payload: NonFuelSaleReversal,
    access: ShiftAccess = Depends(require_shift_access(Role.manager)),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Cancel a non-fuel sale by appending a negated row (§6.9). Manager floor; admin if locked.

    Deliberately **not** `writable=True`. That dependency refuses closed shifts, and a closed
    shift is exactly where this is needed: a mistake surfaces during reconciliation, which
    happens after close. Same reasoning as Phase 5's review route and Phase 6's.
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
    endpoint = "POST /shifts/{shift_id}/non-fuel-sales/{sale_id}/reversals"

    replay = idempotency.begin(
        db,
        key=key,
        endpoint=endpoint,
        user_id=actor.user.id,
        request_fingerprint=idempotency.fingerprint(
            path_params={"shift_id": shift.id, "sale_id": sale_id},
            body=jsonable_encoder(payload),
        ),
    )
    if replay is not None:
        return _replayed(replay)

    try:
        original = _load_sale(db, shift.id, sale_id)
        before = _audit_snapshot(original)

        reversal, replacement = cash_service.reverse_non_fuel_sale(
            db,
            original=original,
            reason=payload.reason,  # already stripped by StringConstraints
            actor_id=actor.user.id,
            replacement_amount=payload.replacement_amount,
            replacement_description=payload.replacement_description,
        )

        audit.record(
            db,
            outlet_id=shift.outlet_id,
            table_name="non_fuel_sales",
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
                table_name="non_fuel_sales",
                record_id=replacement.id,
                action=AuditAction.insert,
                changed_by=actor.user.id,
                new_values=_audit_snapshot(replacement) | {"replaces": str(original.id)},
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

    result = NonFuelSaleReversalResponse(
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
