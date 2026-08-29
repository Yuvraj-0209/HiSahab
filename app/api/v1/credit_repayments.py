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
from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints, condecimal
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.api.cursor import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from app.api.deps import Actor, ShiftAccess, require_role, require_shift_access
from app.core import idempotency
from app.core.audit import AuditAction
from app.core.credit import CreditRepaymentMode
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.roles import Role, satisfies
from app.core.shifts import ShiftStatus
from app.db.session import get_db
from app.models.attachment import Attachment
from app.models.credit import CreditCustomer, CreditRepayment
from app.services import attachments as attachment_service
from app.services import audit, credit as credit_service
from app.services import shifts as shift_service

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
    # Nullable since Phase 16: `None` means the money arrived at the bank rather than at
    # this pump, and §6.4 therefore never sees it (§5.2).
    shift_id: UUID | None
    business_date: date
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


class DatedCreditRepaymentCreate(BaseModel):
    """A repayment that arrived at the bank rather than at this pump (§5.2).

    Separate from `CreditRepaymentCreate` rather than a widening of it, because the two
    genuinely differ: that one takes its shift from the path and its date from the shift,
    this one takes a date and has no shift at all. Both are `extra="forbid"`, so merging
    them would mean two optional fields that are each required in exactly one case -- a
    shape that validates nothing and reads as though either is acceptable anywhere.
    """

    model_config = ConfigDict(extra="forbid")

    credit_customer_id: UUID
    amount: MoneyValue
    mode: CreditRepaymentMode
    business_date: date
    attachment_id: UUID | None = None


class DatedCreditRepaymentPage(BaseModel):
    items: list[CreditRepaymentResponse]
    next_cursor: str | None


class CreditRepaymentReversal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: ReasonValue
    replacement_amount: MoneyValue | None = None


class CreditRepaymentPage(BaseModel):
    items: list[CreditRepaymentResponse]
    total: Decimal
    # §6.4's terms, split out so Phase 10 does not have to re-derive which modes count.
    cash_total: Decimal
    # Phase 16. Udhaar settled on the machine is inside this shift's card/UPI collections,
    # which §6.4 subtracts -- so it is a term of the equation too, on the sales side. Shown
    # beside `cash_total` because "why is my card figure not the whole card figure" is
    # otherwise a question somebody has to ask.
    card_upi_total: Decimal
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
        business_date=row.business_date,
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
        "shift_id": str(row.shift_id) if row.shift_id else None,
        "business_date": row.business_date.isoformat(),
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
        card_upi_total=credit_service.card_upi_repayments_total(db, shift_id=shift.id),
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

        # §5.2's double-count guard: everything before the customer's opening balance is
        # already inside that figure, so an entry dated earlier would be counted twice.
        credit_service.refuse_entry_before_opening_balance(
            db, customer_id=customer.id, business_date=shift.business_date
        )

        repayment = CreditRepayment(
            credit_customer_id=customer.id,
            shift_id=shift.id,
            # §3 rule 7: taken from the shift, never from the client. §4.7 makes this the
            # normal case rather than an edge one -- the day is typed in after the fact, so
            # "today" would file the row under a date the register never mentions.
            business_date=shift.business_date,
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


# --- repayments that arrived at the bank (§5.2, Phase 16) ---------------------
#
# Manager floor, and that is not an arbitrary choice. `require_shift_access` applies §8's
# ownership axis -- "an attendant may write only to a shift whose `attendant_id` is their
# own" -- and a repayment with no shift has no such axis to check. Rather than invent one,
# the endpoint sits at the floor where ownership stops being the question.


@router.get("/credit-repayments", response_model=DatedCreditRepaymentPage)
def list_recent_repayments(
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
) -> Any:
    """Recent settlements across every customer at this outlet, newest first.

    The read side of the dated form below. A write screen with no read is the "record and
    hope" shape, and on the connectivity §6.10 was built for that is a real cost.

    Keyed on `(created_at, id)` through the shared `encode_cursor` / `decode_cursor` -- the
    same pair `/expenses/flagged` reuses, and deliberately not a third encoder (§9). Note it
    is entry order rather than business date: this list answers "did what I just typed
    land", which is a question about typing. The *ledger* orders by business date, because
    that answers a different question.
    """
    limit = max(1, min(limit, MAX_LIMIT))

    statement = (
        select(CreditRepayment)
        .join(
            CreditCustomer,
            CreditCustomer.id == CreditRepayment.credit_customer_id,
        )
        .where(CreditCustomer.outlet_id == actor.outlet_id)
        .order_by(CreditRepayment.created_at.desc(), CreditRepayment.id.desc())
    )
    if cursor is not None:
        last_created_at, last_id = decode_cursor(cursor)
        statement = statement.where(
            tuple_(CreditRepayment.created_at, CreditRepayment.id)
            < tuple_(last_created_at, last_id)
        )

    rows = list(db.execute(statement.limit(limit + 1)).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]

    reversed_ids = {
        row.reverses_id for row in rows if row.reverses_id is not None
    }
    return DatedCreditRepaymentPage(
        items=[
            _to_response(row, is_reversed=row.id in reversed_ids) for row in rows
        ],
        next_cursor=(
            encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
        ),
    )


@router.post("/credit-repayments", response_model=CreditRepaymentResponse, status_code=201)
def create_dated_repayment(
    payload: DatedCreditRepaymentCreate,
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Record a repayment that reached a bank account rather than this pump (§5.2).

    **This is the endpoint that unblocks reconstructing a ledger.** Before Phase 16
    `credit_repayments.shift_id` was `NOT NULL`, and §5.2 forbids modifying anything
    referencing a `locked` shift -- so a bank transfer on a day already locked could not be
    recorded at all, and one arriving on a day the outlet was shut had no shift to attach
    to. §4.7 says the whole day is typed in after the fact, which makes both the normal case
    here rather than the exception.

    It writes no shift, and therefore touches no term of §6.4. That is the entire meaning of
    the rule: every cash-engine sum is `WHERE shift_id = :shift_id`, so a row with no shift
    is invisible to the drawer by construction rather than by a filter somebody has to
    remember to write.

    `mode = cash` is refused. Cash can only land in a drawer, and a cash repayment nobody
    can attribute to a shift is money §6.4 would never count -- the same rule
    `ck_credit_repayments_cash_needs_shift` states at the database (§6.6, belt and braces).
    """
    key = _require_key(idempotency_key)
    endpoint = "POST /credit-repayments"

    if payload.mode is CreditRepaymentMode.cash:
        raise AppError(
            status_code=422,
            code="CASH_REPAYMENT_NEEDS_SHIFT",
            detail=(
                "Cash lands in a drawer, so a cash repayment has to be recorded against "
                "the shift it arrived on. Use the shift's repayments screen instead."
            ),
        )

    replay = idempotency.begin(
        db,
        key=key,
        endpoint=endpoint,
        user_id=actor.user.id,
        request_fingerprint=idempotency.fingerprint(
            path_params={}, body=jsonable_encoder(payload)
        ),
    )
    if replay is not None:
        return _replayed(replay)

    try:
        # `for_sale=False`: a deactivated customer may still pay off what they owe (§5.1).
        customer = credit_service.resolve_customer(
            db,
            customer_id=payload.credit_customer_id,
            outlet_id=actor.outlet_id,
            for_sale=False,
        )

        # §6.1: a future business date is always a data-entry error. Evaluated in the
        # outlet's local timezone, because at 23:00 IST the UTC date is still yesterday and
        # a correct entry would be refused.
        if payload.business_date > shift_service.outlet_today(
            get_settings().TZ_DISPLAY
        ):
            raise AppError(
                status_code=422,
                code="BUSINESS_DATE_IN_FUTURE",
                detail=(
                    "That business date is in the future. The money cannot have arrived "
                    "yet."
                ),
            )

        credit_service.refuse_entry_before_opening_balance(
            db, customer_id=customer.id, business_date=payload.business_date
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
                db, attachment=attachment, outlet_id=actor.outlet_id
            )

        repayment = CreditRepayment(
            credit_customer_id=customer.id,
            shift_id=None,
            business_date=payload.business_date,
            amount=payload.amount,
            mode=payload.mode.value,
            attachment_id=attachment.id if attachment is not None else None,
            created_by=actor.user.id,
        )
        db.add(repayment)
        db.flush()

        audit.record(
            db,
            outlet_id=actor.outlet_id,
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
        "dated credit repayment recorded",
        extra={
            "credit_repayment_id": str(repayment.id),
            "credit_customer_id": str(customer.id),
            "business_date": repayment.business_date.isoformat(),
            "amount": str(repayment.amount),
            "mode": repayment.mode,
        },
    )
    return JSONResponse(status_code=201, content=body)


@router.post(
    "/credit-repayments/{repayment_id}/reversals",
    response_model=CreditRepaymentReversalResponse,
    status_code=201,
)
def reverse_dated_repayment(
    repayment_id: UUID,
    payload: CreditRepaymentReversal,
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """§6.9's correction path for a repayment that has no shift.

    **This route exists because without it those rows were uncorrectable.** The other
    reversal route is `/shifts/{shift_id}/credit-repayments/{id}/reversals`, and a row with
    `shift_id IS NULL` can never reach it -- so a mistyped bank transfer stayed wrong in a
    customer's ledger forever, which §6.9 exists to make impossible. Found by driving the
    real app rather than in review.

    **It refuses a repayment that *does* have a shift**, with 409
    `REPAYMENT_BELONGS_TO_A_SHIFT`, and that is the load-bearing half. The shift-scoped route
    applies §5.2's locked-shift rule -- 403 `LOCKED_SHIFT_REVERSAL_REQUIRES_ADMIN` for a
    non-admin -- and a second route reaching the same rows without that check would not be a
    convenience, it would be the hole. Every row is reversible through exactly one path.
    """
    key = _require_key(idempotency_key)
    endpoint = "POST /credit-repayments/{repayment_id}/reversals"

    replay = idempotency.begin(
        db,
        key=key,
        endpoint=endpoint,
        user_id=actor.user.id,
        request_fingerprint=idempotency.fingerprint(
            path_params={"repayment_id": repayment_id}, body=jsonable_encoder(payload)
        ),
    )
    if replay is not None:
        return _replayed(replay)

    try:
        # Scoped through the customer, because this table carries no `outlet_id` of its own
        # (§5.0: derivable, so it waits). A row at another outlet is simply not found.
        original = db.execute(
            select(CreditRepayment)
            .join(
                CreditCustomer,
                CreditCustomer.id == CreditRepayment.credit_customer_id,
            )
            .where(
                CreditRepayment.id == repayment_id,
                CreditCustomer.outlet_id == actor.outlet_id,
            )
        ).scalar_one_or_none()

        if original is None:
            raise AppError(
                status_code=404,
                code="CREDIT_REPAYMENT_NOT_FOUND",
                detail="No repayment with that id.",
            )

        if original.shift_id is not None:
            raise AppError(
                status_code=409,
                code="REPAYMENT_BELONGS_TO_A_SHIFT",
                detail=(
                    "This repayment was recorded against a shift, so it is reversed from "
                    "that shift's repayments screen -- which is also where the locked-shift "
                    "rule is applied."
                ),
            )

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
            outlet_id=actor.outlet_id,
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
                outlet_id=actor.outlet_id,
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
    return JSONResponse(status_code=201, content=body)
