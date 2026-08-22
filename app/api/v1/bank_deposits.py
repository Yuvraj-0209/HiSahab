"""Bank deposits -- cash leaving the locker for the bank (CLAUDE.md §5.2, §6.4, §6.9, §8).

Phase 10. §6.4 **subtracts** this from expected cash, and that is the whole of its meaning:
a deposit is not income, it is the locker emptying. Getting the sign wrong here would make
every pay-in look like a day's takings.

## Why the locker matters more than the drawer here

§14 records that this outlet's locker "carries a running balance that rolls forward on any
day with no bank deposit". So a deposit is the event that actually *moves* the rolling
balance §6.5 chains from day to day -- most days have none, and on the days there is one it
is usually large. A duplicated ₹1,00,000 deposit does not look like a rounding error; it
reads as a day that came up a lakh short.

That is why the deposit slip gets §5.3's one-attachment-one-live-row protection in full
(`live_deposit_for_attachment`), and why this route requires an `Idempotency-Key` like every
other money-creating POST.

## `business_date` is server-set, never client-supplied

§3 rule 7: it is derivable from the shift, so the server recomputes it rather than trusting
a client-supplied copy. §5.2 keeps the column because a deposit is filed against a trading
day and that is what reports group by -- but a client-supplied copy is a drift source with
no upside at all.

**Role floors (§8).** Recording a deposit is a *manager's* act -- attendants take money in,
they do not take it out -- so unlike collections and non-fuel sales the floor here is
`Role.manager`, not `Role.attendant`. Reversing on a *locked* shift is an admin's.
"""

from __future__ import annotations

import logging
from datetime import date
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
from app.models.attachment import Attachment
from app.models.cash import BankDeposit
from app.services import attachments as attachment_service, audit, cash as cash_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["bank deposits"])

_MAX_ROWS = 100


# --- schemas -----------------------------------------------------------------

# §3 rule 1. `gt=0` -- a ₹0 deposit records nothing, the same strict sign rule
# `non_fuel_sales`, `expenses` and `credit_sales` use.
MoneyValue = condecimal(max_digits=12, decimal_places=2, gt=0)


class BankDepositResponse(BaseModel):
    id: UUID
    shift_id: UUID
    business_date: date
    amount: Decimal
    bank_reference: str | None
    attachment_id: UUID | None
    reverses_id: UUID | None
    reversal_reason: str | None
    is_reversed: bool


class BankDepositPage(BaseModel):
    items: list[BankDepositResponse]
    total: Decimal
    cash_basis: str = (
        "total is SUBTRACTED in CLAUDE.md §6.4's expected_closing. A deposit is the "
        "locker emptying, not money arriving."
    )
    truncated: bool


class BankDepositCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # No `business_date`. §3 rule 7: it is derivable from the shift, so the server
    # recomputes it. Accepting one would let a client file a deposit against a day the
    # register never mentions -- the trap §7.2 already refuses for an upload's storage path.
    amount: MoneyValue
    bank_reference: str | None = Field(default=None, max_length=200)
    attachment_id: UUID | None = None


class BankDepositUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # No `shift_id` and no `business_date` -- see M16 and the note above. No `attachment_id`
    # either: §5.3 makes an attachment immutable once set, and there is no "while still
    # NULL" path here because a deposit slip is supplied at create or not at all.
    amount: MoneyValue | None = None
    bank_reference: str | None = Field(default=None, max_length=200)


class BankDepositReversal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)
    ]
    replacement_amount: MoneyValue | None = None
    replacement_reference: str | None = Field(default=None, max_length=200)


class BankDepositReversalResponse(BaseModel):
    reversal: BankDepositResponse
    replacement: BankDepositResponse | None
    original: BankDepositResponse


# --- helpers -----------------------------------------------------------------


def _to_response(row: BankDeposit, *, is_reversed: bool = False) -> BankDepositResponse:
    return BankDepositResponse(
        id=row.id,
        shift_id=row.shift_id,
        business_date=row.business_date,
        amount=row.amount,
        bank_reference=row.bank_reference,
        attachment_id=row.attachment_id,
        reverses_id=row.reverses_id,
        reversal_reason=row.reversal_reason,
        is_reversed=is_reversed,
    )


def _audit_snapshot(row: BankDeposit) -> dict[str, object]:
    return {
        "amount": row.amount,
        "business_date": row.business_date,
        "bank_reference": row.bank_reference,
        "attachment_id": row.attachment_id,
        "reverses_id": row.reverses_id,
    }


def _load_deposit(db: Session, shift_id: UUID, deposit_id: UUID) -> BankDeposit:
    """409 rather than 404 for a wrong-shift row: it exists and the caller may be allowed to
    see it, but the URL asserts a parent-child relationship that is not true."""
    row = db.get(BankDeposit, deposit_id)
    if row is None:
        raise AppError(
            status_code=404,
            code="DEPOSIT_NOT_FOUND",
            detail="No bank deposit with that id.",
        )
    if row.shift_id != shift_id:
        raise AppError(
            status_code=409,
            code="DEPOSIT_NOT_IN_SHIFT",
            detail="That bank deposit belongs to a different shift.",
        )
    return row


def _require_key(idempotency_key: str | None) -> str:
    """§6.10. A retried ₹1,00,000 deposit does not read as a rounding error -- it reads as a
    day that came up a lakh short."""
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


@router.get("/shifts/{shift_id}/bank-deposits", response_model=BankDepositPage)
def list_bank_deposits(
    access: ShiftAccess = Depends(require_shift_access(Role.manager)),
    db: Session = Depends(get_db),
) -> BankDepositPage:
    """Every deposit made on this shift, reversals included (§6.9).

    Manager floor to read as well as to write: §8 gives attendants "read own shift" but a
    deposit is not part of the sheet they fill in, and the locker balance it moves is a
    supervisory figure.
    """
    shift = access.shift
    rows = cash_service.all_bank_deposits(db, shift_id=shift.id)
    reversed_ids = {row.reverses_id for row in rows if row.reverses_id is not None}
    truncated = len(rows) > _MAX_ROWS
    rows = rows[:_MAX_ROWS]

    return BankDepositPage(
        items=[_to_response(row, is_reversed=row.id in reversed_ids) for row in rows],
        # From the service, over the whole shift -- never a Python sum over the truncated
        # list. The Phase 9 defect this phase's Step 0 fixed on `credit_repayments`.
        total=cash_service.bank_deposits_total(db, shift_id=shift.id),
        truncated=truncated,
    )


@router.post(
    "/shifts/{shift_id}/bank-deposits",
    response_model=BankDepositResponse,
    status_code=201,
)
def create_bank_deposit(
    payload: BankDepositCreate,
    access: ShiftAccess = Depends(require_shift_access(Role.manager, writable=True)),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Record cash paid into the bank. **Manager floor** (§8) -- attendants take money in,
    they do not take it out."""
    shift = access.shift
    actor = access.actor
    key = _require_key(idempotency_key)
    endpoint = "POST /shifts/{shift_id}/bank-deposits"

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
        # §7.2 step 7 / §5.3. Linked BEFORE the deposit row is constructed, so a refusal
        # here -- unknown id, another outlet's attachment, already claimed -- leaves nothing
        # written. `link()`'s own docstring calls this ordering out.
        attachment: Attachment | None = None
        if payload.attachment_id is not None:
            attachment = db.get(Attachment, payload.attachment_id)
            if attachment is None:
                raise AppError(
                    status_code=404,
                    code="ATTACHMENT_NOT_FOUND",
                    detail="No attachment with that id.",
                )
            attachment_service.link(db, attachment=attachment, outlet_id=shift.outlet_id)

        row = BankDeposit(
            shift_id=shift.id,
            # §3 rule 7: read off the shift, never taken from the client.
            business_date=shift.business_date,
            amount=payload.amount,
            bank_reference=payload.bank_reference,
            attachment_id=None if attachment is None else attachment.id,
            created_by=actor.user.id,
        )
        db.add(row)
        db.flush()

        audit.record(
            db,
            outlet_id=shift.outlet_id,
            table_name="bank_deposits",
            record_id=row.id,
            action=AuditAction.insert,
            changed_by=actor.user.id,
            new_values=_audit_snapshot(row),
        )
        db.commit()
        db.refresh(row)
    except Exception:
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
        "bank deposit recorded",
        extra={
            "bank_deposit_id": str(row.id),
            "shift_id": str(shift.id),
            "business_date": row.business_date.isoformat(),
            "amount": str(row.amount),
        },
    )
    return _to_response(row)


@router.patch(
    "/shifts/{shift_id}/bank-deposits/{deposit_id}", response_model=BankDepositResponse
)
def update_bank_deposit(
    deposit_id: UUID,
    payload: BankDepositUpdate,
    access: ShiftAccess = Depends(require_shift_access(Role.manager, writable=True)),
    db: Session = Depends(get_db),
) -> BankDepositResponse:
    """Correct a deposit while the shift is still open. No Idempotency-Key -- a PATCH is
    idempotent by construction."""
    shift = access.shift
    actor = access.actor
    row = _load_deposit(db, shift.id, deposit_id)

    if row.reverses_id is not None:
        raise AppError(
            status_code=409,
            code="CANNOT_EDIT_A_REVERSAL",
            detail=(
                "A reversal records what was cancelled and why. Editing it would rewrite "
                "the correction itself. Record a fresh deposit instead."
            ),
        )

    if cash_service.reversal_of(db, BankDeposit, row_id=row.id) is not None:
        raise AppError(
            status_code=409,
            code="DEPOSIT_ALREADY_REVERSED",
            detail=(
                "This deposit has been reversed and its figure is now part of the record. "
                "Record the corrected amount as a new deposit rather than editing a "
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
        # An explicit null means "leave it alone", matching every sibling router.
        if value is None:
            continue
        setattr(row, field, value)

    audit.record(
        db,
        outlet_id=shift.outlet_id,
        table_name="bank_deposits",
        record_id=row.id,
        action=AuditAction.update,
        changed_by=actor.user.id,
        old_values=before,
        new_values=_audit_snapshot(row),
    )
    db.commit()
    db.refresh(row)

    logger.info(
        "bank deposit updated",
        extra={"bank_deposit_id": str(row.id), "fields": sorted(changes)},
    )
    return _to_response(row)


@router.post(
    "/shifts/{shift_id}/bank-deposits/{deposit_id}/reversals",
    response_model=BankDepositReversalResponse,
    status_code=201,
)
def reverse_bank_deposit(
    deposit_id: UUID,
    payload: BankDepositReversal,
    access: ShiftAccess = Depends(require_shift_access(Role.manager)),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Cancel a deposit by appending a negated row (§6.9). Manager floor; admin if locked.

    Not `writable=True`: a deposit that never cleared, or was entered against the wrong day,
    is discovered after the shift closed. That is the case this route exists for.

    **The reversal inherits the deposit slip** (§5.3). Same paper, same evidence, and
    `link()` is not called again -- by then the original is no longer live, so exactly one
    live row holds the attachment throughout.
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
    endpoint = "POST /shifts/{shift_id}/bank-deposits/{deposit_id}/reversals"

    replay = idempotency.begin(
        db,
        key=key,
        endpoint=endpoint,
        user_id=actor.user.id,
        request_fingerprint=idempotency.fingerprint(
            path_params={"shift_id": shift.id, "deposit_id": deposit_id},
            body=jsonable_encoder(payload),
        ),
    )
    if replay is not None:
        return _replayed(replay)

    try:
        original = _load_deposit(db, shift.id, deposit_id)
        before = _audit_snapshot(original)

        reversal, replacement = cash_service.reverse_bank_deposit(
            db,
            original=original,
            reason=payload.reason,
            actor_id=actor.user.id,
            replacement_amount=payload.replacement_amount,
            replacement_reference=payload.replacement_reference,
        )

        audit.record(
            db,
            outlet_id=shift.outlet_id,
            table_name="bank_deposits",
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
                table_name="bank_deposits",
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

    result = BankDepositReversalResponse(
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
