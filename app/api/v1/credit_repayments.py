"""Credit repayments -- a customer settling an old bill (CLAUDE.md §4.4, §5.2, §6.4, §6.6).

Mirrors `app/api/v1/credit_sales.py`, minus the two things that make a sale complicated: there
is no credit limit to check (money coming *in* never breaches one) and no mandatory receipt
(the pump writes the receipt, so there is no counterparty document to demand).

**The cash here has no sale behind it on the day it arrives.** That is §4.4's whole point and
the reason §6.4 carries `cash_credit_repayments` as its own term: without it, expected cash is
wrong every time somebody settles up. Phase 10 assembles that equation; this module records
the rows and the `mode` that decides which of them count.

**Only `mode = cash` will enter §6.4.** A customer settling by UPI or bank transfer moves no
money through the drawer, and adding it to expected cash would invent a shortfall on the very
day they paid -- which §14 records this outlet books as udhaar against the salesman's own name.

**A deactivated customer may still repay** (§5.1). You retire somebody precisely to stop the
debt growing while they pay it off, so `resolve_customer` is called with `for_sale=False` here
and the inactive check does not run.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints, condecimal
from sqlalchemy.orm import Session

from app.api.deps import ShiftAccess, require_shift_access
from app.core import idempotency
from app.core.audit import AuditAction
from app.core.credit import CreditRepaymentMode
from app.core.errors import AppError
from app.core.roles import Role, satisfies
from app.core.shifts import ShiftStatus
from app.db.session import get_db
from app.models.attachment import Attachment
from app.models.credit import CreditRepayment
from app.services import attachments as attachment_service
from app.services import audit, credit as credit_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["credit"])

_MAX_ROWS = 100

MoneyValue = condecimal(max_digits=12, decimal_places=2, gt=0)

ReasonValue = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)
]


# --- schemas -----------------------------------------------------------------


class CreditRepaymentResponse(BaseModel):
    id: UUID
    shift_id: UUID
    credit_customer_id: UUID
    amount: Decimal
    mode: CreditRepaymentMode
    attachment_id: UUID | None
    reverses_id: UUID | None
    reversal_reason: str | None
    is_reversed: bool
    created_at: str


class CreditRepaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credit_customer_id: UUID
    amount: MoneyValue
    mode: CreditRepaymentMode
    # Optional, unlike a credit sale's. A repayment is money coming in and the pump writes
    # the receipt; there is no vendor document to photograph. A bank-transfer advice or a
    # deposit slip is worth keeping when there is one, hence the field.
    attachment_id: UUID | None = None


class CreditRepaymentUpdate(BaseModel):
    """What may change while the shift is still open.

    `credit_customer_id` is absent for the reason `CreditSaleUpdate` gives: moving a payment
    to another customer's ledger leaves two balances wrong, and §6.9's reversal is the path.
    `attachment_id` is absent because a receipt, once attached, is immutable (§5.2).
    """

    model_config = ConfigDict(extra="forbid")

    amount: MoneyValue | None = None
    mode: CreditRepaymentMode | None = None


class CreditRepaymentReversal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: ReasonValue
    replacement_amount: MoneyValue | None = None


class CreditRepaymentPage(BaseModel):
    items: list[CreditRepaymentResponse]
    total: Decimal
    # §6.4's term, split out so Phase 10 does not have to re-derive which modes count.
    cash_total: Decimal
    truncated: bool


class CreditRepaymentReversalResponse(BaseModel):
    reversal: CreditRepaymentResponse
    replacement: CreditRepaymentResponse | None
    original: CreditRepaymentResponse


# --- helpers -----------------------------------------------------------------


def _to_response(
    row: CreditRepayment, *, is_reversed: bool = False
) -> CreditRepaymentResponse:
    return CreditRepaymentResponse(
        id=row.id,
        shift_id=row.shift_id,
        credit_customer_id=row.credit_customer_id,
        amount=row.amount,
        mode=CreditRepaymentMode(row.mode),
        attachment_id=row.attachment_id,
        reverses_id=row.reverses_id,
        reversal_reason=row.reversal_reason,
        is_reversed=is_reversed,
        created_at=row.created_at.isoformat(),
    )


def _audit_snapshot(row: CreditRepayment) -> dict[str, object]:
    return {
        "credit_customer_id": str(row.credit_customer_id),
        "amount": row.amount,
        "mode": row.mode,
        "attachment_id": str(row.attachment_id) if row.attachment_id else None,
    }


def _load_repayment(
    db: Session, shift_id: UUID, repayment_id: UUID
) -> CreditRepayment:
    repayment = db.get(CreditRepayment, repayment_id)
    if repayment is None:
        raise AppError(
            status_code=404,
            code="CREDIT_REPAYMENT_NOT_FOUND",
            detail="No repayment with that id.",
        )
    if repayment.shift_id != shift_id:
        raise AppError(
            status_code=409,
            code="CREDIT_REPAYMENT_NOT_IN_SHIFT",
            detail="That repayment belongs to a different shift.",
        )
    return repayment


def _require_key(idempotency_key: str | None) -> str:
    if not idempotency_key:
        raise AppError(
            status_code=400,
            code="IDEMPOTENCY_KEY_REQUIRED",
            detail=(
                "Send an Idempotency-Key header with this request. It is what stops a "
                "retry after a timeout from recording the same payment twice."
            ),
        )
    return idempotency_key


def _replayed(replay: idempotency.Replay) -> JSONResponse:
    return JSONResponse(status_code=replay.status_code, content=replay.body)


# --- routes ------------------------------------------------------------------


@router.get(
    "/shifts/{shift_id}/credit-repayments", response_model=CreditRepaymentPage
)
def list_credit_repayments(
    access: ShiftAccess = Depends(require_shift_access(Role.attendant)),
    db: Session = Depends(get_db),
) -> CreditRepaymentPage:
    """Every settlement received on this shift, reversals included (§6.9)."""
    shift = access.shift
    rows = credit_service.all_repayments(db, shift_id=shift.id)
    reversed_ids = {row.reverses_id for row in rows if row.reverses_id is not None}
    truncated = len(rows) > _MAX_ROWS
    rows = rows[:_MAX_ROWS]

    return CreditRepaymentPage(
        items=[_to_response(row, is_reversed=row.id in reversed_ids) for row in rows],
        # Both totals aggregate in SQL over the whole shift, never over `rows` -- which has
        # just been truncated to `_MAX_ROWS`. Phase 9 summed the truncated list here, so a
        # shift with more than 100 repayments under-reported both figures, and `cash_total`
        # is a term of §6.4. Fixed in Phase 10 Step 0; the sibling routers were always right.
        total=credit_service.repayments_total(db, shift_id=shift.id),
        # §6.4's `cash_credit_repayments` term, filtered to the one mode that reaches the
        # drawer. Answered in the service, next to the rows it sums.
        cash_total=credit_service.cash_repayments_total(db, shift_id=shift.id),
        truncated=truncated,
    )


@router.post(
    "/shifts/{shift_id}/credit-repayments",
    response_model=CreditRepaymentResponse,
    status_code=201,
)
def create_credit_repayment(
    payload: CreditRepaymentCreate,
    access: ShiftAccess = Depends(require_shift_access(Role.attendant, writable=True)),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Record a settlement. Attendant floor, own shift only (§8)."""
    shift = access.shift
    actor = access.actor
    key = _require_key(idempotency_key)
    endpoint = "POST /shifts/{shift_id}/credit-repayments"

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
        # `for_sale=False`: a deactivated customer may still pay off what they owe (§5.1).
        # Refusing their money would strand a balance that nothing could ever clear.
        customer = credit_service.resolve_customer(
            db,
            customer_id=payload.credit_customer_id,
            outlet_id=shift.outlet_id,
            for_sale=False,
        )

        attachment: Attachment | None = None
        if payload.attachment_id is not None:
            attachment = db.get(Attachment, payload.attachment_id)
            if attachment is None:
                raise AppError(
                    status_code=404,
                    code="ATTACHMENT_NOT_FOUND",
                    detail="No attachment with that id.",
                )
            attachment_service.link(
                db, attachment=attachment, outlet_id=shift.outlet_id
            )

        repayment = CreditRepayment(
            credit_customer_id=customer.id,
            shift_id=shift.id,
            amount=payload.amount,
            mode=payload.mode.value,
            attachment_id=attachment.id if attachment is not None else None,
            created_by=actor.user.id,
        )
        db.add(repayment)
        db.flush()

        audit.record(
            db,
            outlet_id=shift.outlet_id,
            table_name="credit_repayments",
            record_id=repayment.id,
            action=AuditAction.insert,
            changed_by=actor.user.id,
            new_values=_audit_snapshot(repayment),
        )
        db.commit()
        db.refresh(repayment)
    except Exception:
        db.rollback()
        idempotency.discard(db, key=key, endpoint=endpoint, user_id=actor.user.id)
        raise

    response = _to_response(repayment)
    body = jsonable_encoder(response)
    idempotency.store(
        db, key=key, endpoint=endpoint, user_id=actor.user.id, status_code=201, body=body
    )

    logger.info(
        "credit repayment recorded",
        extra={
            "credit_repayment_id": str(repayment.id),
            "shift_id": str(shift.id),
            "credit_customer_id": str(customer.id),
            "amount": str(repayment.amount),
            "mode": repayment.mode,
        },
    )
    return response


@router.patch(
    "/shifts/{shift_id}/credit-repayments/{repayment_id}",
    response_model=CreditRepaymentResponse,
)
def update_credit_repayment(
    repayment_id: UUID,
    payload: CreditRepaymentUpdate,
    access: ShiftAccess = Depends(require_shift_access(Role.attendant, writable=True)),
    db: Session = Depends(get_db),
) -> CreditRepaymentResponse:
    """Correct a figure while the shift is still open. No Idempotency-Key: a PATCH is
    idempotent by construction."""
    shift = access.shift
    actor = access.actor
    repayment = _load_repayment(db, shift.id, repayment_id)

    if repayment.reverses_id is not None:
        raise AppError(
            status_code=409,
            code="CANNOT_EDIT_A_REVERSAL",
            detail=(
                "This row is a reversal and records what was cancelled. Correct the "
                "replacement instead."
            ),
        )

    if credit_service.repayment_reversal_of(db, repayment_id=repayment.id) is not None:
        raise AppError(
            status_code=409,
            code="CREDIT_REPAYMENT_ALREADY_REVERSED",
            detail=(
                "This repayment has been reversed and is now history. Record the correct "
                "figure as a new repayment."
            ),
        )

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise AppError(
            status_code=422,
            code="NO_FIELDS_TO_UPDATE",
            detail="Send at least one field to change.",
        )

    before = _audit_snapshot(repayment)
    for field, value in changes.items():
        # Both fields are NOT NULL and their Pydantic types already refuse a null, so there
        # is no explicit-null case to decide here -- unlike credit_customers' PATCH.
        setattr(repayment, field, value.value if field == "mode" else value)

    audit.record(
        db,
        outlet_id=shift.outlet_id,
        table_name="credit_repayments",
        record_id=repayment.id,
        action=AuditAction.update,
        changed_by=actor.user.id,
        old_values=before,
        new_values=_audit_snapshot(repayment),
    )
    db.commit()
    db.refresh(repayment)

    logger.info(
        "credit repayment updated",
        extra={"credit_repayment_id": str(repayment.id), "fields": sorted(changes)},
    )
    return _to_response(repayment)


@router.post(
    "/shifts/{shift_id}/credit-repayments/{repayment_id}/reversals",
    response_model=CreditRepaymentReversalResponse,
    status_code=201,
)
def reverse_credit_repayment(
    repayment_id: UUID,
    payload: CreditRepaymentReversal,
    access: ShiftAccess = Depends(require_shift_access(Role.manager)),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """§6.9's correction path. Manager floor; admin if the shift is locked.

    A bounced cheque, a UPI reversal, or a figure typed against the wrong customer. §4.7
    makes this the normal case rather than an edge one: the whole day is typed in after the
    fact, so the mistake is routinely found once the shift is already closed.
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
    endpoint = "POST /shifts/{shift_id}/credit-repayments/{repayment_id}/reversals"

    replay = idempotency.begin(
        db,
        key=key,
        endpoint=endpoint,
        user_id=actor.user.id,
        request_fingerprint=idempotency.fingerprint(
            path_params={"shift_id": shift.id, "repayment_id": repayment_id},
            body=jsonable_encoder(payload),
        ),
    )
    if replay is not None:
        return _replayed(replay)

    try:
        original = _load_repayment(db, shift.id, repayment_id)
        before = _audit_snapshot(original)

        reversal, replacement = credit_service.reverse_repayment(
            db,
            original=original,
            reason=payload.reason,
            actor_id=actor.user.id,
            replacement_amount=payload.replacement_amount,
        )

        audit.record(
            db,
            outlet_id=shift.outlet_id,
            table_name="credit_repayments",
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
                table_name="credit_repayments",
                record_id=replacement.id,
                action=AuditAction.insert,
                changed_by=actor.user.id,
                new_values=_audit_snapshot(replacement),
            )

        db.commit()
        db.refresh(reversal)
        db.refresh(original)
        if replacement is not None:
            db.refresh(replacement)
    except Exception:
        db.rollback()
        idempotency.discard(db, key=key, endpoint=endpoint, user_id=actor.user.id)
        raise

    response = CreditRepaymentReversalResponse(
        reversal=_to_response(reversal),
        replacement=_to_response(replacement) if replacement is not None else None,
        original=_to_response(original, is_reversed=True),
    )
    body = jsonable_encoder(response)
    idempotency.store(
        db, key=key, endpoint=endpoint, user_id=actor.user.id, status_code=201, body=body
    )
    return response
