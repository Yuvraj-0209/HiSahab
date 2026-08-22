"""Credit sales -- udhaar issued during a shift (CLAUDE.md §5.2, §6.6, §6.8, §6.9, §6.10).

Mirrors `app/api/v1/expenses.py`: create, correct, list, reverse. The differences from that
module are all consequences of one thing -- a credit sale is money the pump is *owed*, so it
carries a customer and a mandatory receipt where an expense carries a category and an optional
one.

**Four routes.** Create, correct, list, reverse. There is no review route: §6.7's flagging is
an expense control, and a credit sale's equivalent guard is §6.6's credit limit, which refuses
at write time rather than flagging for later.

**The receipt is not conditional.** `credit_sales.attachment_id` is `NOT NULL` at the database
level (§6.6), so unlike §6.11's expense rule there is no threshold, no snapshot column and no
category flag to consult -- every sale carries one, always. `attachment_id` is therefore
required in the payload rather than optional, and Pydantic refuses its absence before any of
this runs.

**Retries do not duplicate money (§6.10).** Both money-creating POSTs require an
`Idempotency-Key`.

**Role floors (§8).** Attendants record udhaar on their own shift, ownership enforced by
`require_shift_access` and never re-implemented here. Reversing is a manager's act; reversing
on a *locked* shift is an admin's, mirroring `expenses.py` and `collections.py`.
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
from app.models.attachment import Attachment
from app.models.credit import CreditSale
from app.models.fuel import FuelType
from app.services import attachments as attachment_service
from app.services import audit, credit as credit_service, pricing

logger = logging.getLogger(__name__)

router = APIRouter(tags=["credit"])

# Same reasoning as expenses.py's and collections.py's: a shift's own credit sales are a
# handful of rows, not a history to page through, and a runaway correction loop should surface
# as a truncated list rather than an unbounded response.
_MAX_ROWS = 100

# §3 rule 1: money is NUMERIC(12,2) and Decimal end to end. `gt=0`, matching expenses rather
# than collections -- CLAUDE.md §5.2's Phase 9 amendment makes the sign rule strict, because a
# ₹0 udhaar records nothing and has no reason to exist.
MoneyValue = condecimal(max_digits=12, decimal_places=2, gt=0)

# §4.5: litres OR kilograms, per the fuel's own unit_of_measure. NUMERIC(10,3).
QuantityValue = condecimal(max_digits=10, decimal_places=3, gt=0)

# strip_whitespace runs BEFORE the length check -- see collections.py's CollectionReversal for
# why that ordering is the whole point. A reason of "   " must never reach `.strip()` and
# become an empty string in the database.
ReasonValue = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)
]

# How far the typed amount may drift from quantity x rate before it is worth a log line. One
# rupee absorbs ordinary rounding on a slip without going quiet about a mistyped digit.
_AMOUNT_DIVERGENCE_TOLERANCE = Decimal("1.00")


# --- schemas -----------------------------------------------------------------


class CreditSaleResponse(BaseModel):
    id: UUID
    shift_id: UUID
    credit_customer_id: UUID
    fuel_type_id: UUID | None
    quantity: Decimal | None
    amount: Decimal
    vehicle_number: str | None
    attachment_id: UUID
    limit_override_reason: str | None
    reverses_id: UUID | None
    reversal_reason: str | None
    is_reversed: bool
    created_at: str


class CreditSaleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credit_customer_id: UUID
    # Required, unlike `ExpenseCreate.attachment_id`. §6.6's receipt control is
    # unconditional here, so its absence is a payload error rather than a business one.
    attachment_id: UUID
    amount: MoneyValue
    # Null = a non-fuel credit sale: a can of oil, a puncture repair (§5.2).
    fuel_type_id: UUID | None = None
    quantity: QuantityValue | None = None
    vehicle_number: str | None = Field(default=None, max_length=32)
    # §6.6's admin override. Supplying it as a non-admin is a 403, not a silent no-op.
    limit_override_reason: ReasonValue | None = None


class CreditSaleUpdate(BaseModel):
    """What may change while the shift is still open.

    `credit_customer_id` is absent on purpose, and `extra="forbid"` turns an attempt to send
    it into a 422 rather than a silent no-op. Moving money to a different customer's ledger is
    not a correction of this row -- it is a different sale, and two balances are wrong until
    it is recorded as one. The correction path is §6.9's reversal, which leaves both the
    mistake and the fix visible.

    `attachment_id` is absent for the same reason it is immutable on an expense (§5.2):
    swapping one receipt for another either strands the old one as garbage §7.4 can never
    reclaim, or unlinks it and lets the sweep delete evidence for a sale that still exists.

    `fuel_type_id` is absent too -- changing it would reinterpret `quantity`'s unit (§4.5),
    turning litres into kilograms without touching the number.
    """

    model_config = ConfigDict(extra="forbid")

    amount: MoneyValue | None = None
    quantity: QuantityValue | None = None
    vehicle_number: str | None = Field(default=None, max_length=32)


class CreditSaleReversal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: ReasonValue
    replacement_amount: MoneyValue | None = None


class CreditSalePage(BaseModel):
    items: list[CreditSaleResponse]
    total: Decimal
    truncated: bool


class CreditSaleReversalResponse(BaseModel):
    reversal: CreditSaleResponse
    replacement: CreditSaleResponse | None
    original: CreditSaleResponse


# --- helpers -----------------------------------------------------------------


def _to_response(row: CreditSale, *, is_reversed: bool = False) -> CreditSaleResponse:
    return CreditSaleResponse(
        id=row.id,
        shift_id=row.shift_id,
        credit_customer_id=row.credit_customer_id,
        fuel_type_id=row.fuel_type_id,
        quantity=row.quantity,
        amount=row.amount,
        vehicle_number=row.vehicle_number,
        attachment_id=row.attachment_id,
        limit_override_reason=row.limit_override_reason,
        reverses_id=row.reverses_id,
        reversal_reason=row.reversal_reason,
        is_reversed=is_reversed,
        created_at=row.created_at.isoformat(),
    )


def _audit_snapshot(row: CreditSale) -> dict[str, object]:
    """The fields worth recording either side of a change.

    Who owes it, how much, and against what. Not the whole row: an audit entry that echoes
    every column makes the change itself hard to find.
    """
    return {
        "credit_customer_id": str(row.credit_customer_id),
        "amount": row.amount,
        "quantity": row.quantity,
        "fuel_type_id": str(row.fuel_type_id) if row.fuel_type_id else None,
        "vehicle_number": row.vehicle_number,
        "attachment_id": str(row.attachment_id),
        "limit_override_reason": row.limit_override_reason,
    }


def _load_sale(db: Session, shift_id: UUID, sale_id: UUID) -> CreditSale:
    """404 for a row that does not exist, 409 for one that belongs to another shift.

    The same two-step `expenses._load_expense` uses, and for the same reason: a caller who
    has a real id but the wrong shift in the URL has made a different mistake from one
    chasing a row that was never there.
    """
    sale = db.get(CreditSale, sale_id)
    if sale is None:
        raise AppError(
            status_code=404,
            code="CREDIT_SALE_NOT_FOUND",
            detail="No credit sale with that id.",
        )
    if sale.shift_id != shift_id:
        raise AppError(
            status_code=409,
            code="CREDIT_SALE_NOT_IN_SHIFT",
            detail="That credit sale belongs to a different shift.",
        )
    return sale


def _require_key(idempotency_key: str | None) -> str:
    """§6.10: not optional on a POST that creates a money record. See collections.py."""
    if not idempotency_key:
        raise AppError(
            status_code=400,
            code="IDEMPOTENCY_KEY_REQUIRED",
            detail=(
                "Send an Idempotency-Key header with this request. It is what stops a "
                "retry after a timeout from recording the same udhaar twice."
            ),
        )
    return idempotency_key


def _replayed(replay: idempotency.Replay) -> JSONResponse:
    return JSONResponse(status_code=replay.status_code, content=replay.body)


def _resolve_fuel_type(db: Session, *, fuel_type_id: UUID) -> FuelType:
    """A fuel type that exists and is still sold.

    409 rather than 404 for an unknown id, matching `fuel_prices.py` and `nozzles.py`: the
    missing row is not the one named in the URL, it is a reference the payload points at.
    """
    fuel_type = db.get(FuelType, fuel_type_id)
    if fuel_type is None:
        raise AppError(
            status_code=409,
            code="FUEL_TYPE_NOT_FOUND",
            detail="No fuel type with that id.",
        )
    return fuel_type


def _warn_if_amount_diverges(
    db: Session, *, shift, fuel_type_id: UUID | None, quantity: Decimal | None, amount: Decimal
) -> None:
    """§13.1's shift-start approximation, used as a sanity check rather than a rule.

    When both a fuel and a quantity are recorded, `quantity x rate_at(shift.started_at)` is
    what the slip *should* say. A large gap usually means a mistyped digit -- the same
    data-entry error §6.2's flow-rate ceiling exists to catch on the meter side.

    **It logs; it never refuses.** The amount on the slip is what the customer actually
    agreed to owe, and a manual discount, a rounded total or a part-payment at the pump are
    all real. Refusing them would teach staff to type whatever balances, which is exactly
    what §6.8 warns against for shift closes.

    `rate_at` raises `NO_PRICE_FOR_DATE` when no rate has been entered for this fuel yet, and
    that must not block a real sale either -- §6.8 makes the same argument about close
    preconditions inheriting a valuation refusal. So the lookup is wrapped and a missing
    price simply skips the check.
    """
    if fuel_type_id is None or quantity is None:
        return

    try:
        rate = pricing.rate_at(
            db,
            outlet_id=shift.outlet_id,
            fuel_type_id=fuel_type_id,
            at=shift.started_at,
        )
    except AppError:
        # A reference-data gap, not a bad sale. §6.8's reasoning.
        return

    expected = (quantity * rate).quantize(Decimal("0.01"))
    if abs(expected - amount) > _AMOUNT_DIVERGENCE_TOLERANCE:
        logger.warning(
            "credit sale amount diverges from quantity x rate",
            extra={
                "shift_id": str(shift.id),
                "fuel_type_id": str(fuel_type_id),
                "quantity": str(quantity),
                "rate": str(rate),
                "expected_amount": str(expected),
                "recorded_amount": str(amount),
            },
        )


# --- routes ------------------------------------------------------------------


@router.get("/shifts/{shift_id}/credit-sales", response_model=CreditSalePage)
def list_credit_sales(
    access: ShiftAccess = Depends(require_shift_access(Role.attendant)),
    db: Session = Depends(get_db),
) -> CreditSalePage:
    """Every udhaar issued on this shift, reversals included (§6.9: both rows stay visible)."""
    shift = access.shift
    rows = credit_service.all_sales(db, shift_id=shift.id)
    # Computed before truncation, same reason as expenses.py: a reversal that fell past the
    # cap would otherwise make the row it cancels read as still live.
    reversed_ids = {row.reverses_id for row in rows if row.reverses_id is not None}
    truncated = len(rows) > _MAX_ROWS
    rows = rows[:_MAX_ROWS]

    return CreditSalePage(
        items=[
            _to_response(row, is_reversed=row.id in reversed_ids) for row in rows
        ],
        total=credit_service.credit_sales_total(db, shift_id=shift.id),
        truncated=truncated,
    )


@router.post(
    "/shifts/{shift_id}/credit-sales", response_model=CreditSaleResponse, status_code=201
)
def create_credit_sale(
    payload: CreditSaleCreate,
    access: ShiftAccess = Depends(require_shift_access(Role.attendant, writable=True)),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Issue udhaar. Attendant floor, own shift only (§8)."""
    shift = access.shift
    actor = access.actor
    key = _require_key(idempotency_key)
    endpoint = "POST /shifts/{shift_id}/credit-sales"

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
        # Inside the try/except so a bad customer, fuel type or attachment releases the
        # idempotency key like every other refusal here -- otherwise a typo'd id would wedge
        # that key for 24 hours and the retry, with the right value, would be refused as a
        # duplicate.
        customer = credit_service.resolve_customer(
            db,
            customer_id=payload.credit_customer_id,
            outlet_id=shift.outlet_id,
            for_sale=True,
        )

        if payload.fuel_type_id is not None:
            _resolve_fuel_type(db, fuel_type_id=payload.fuel_type_id)
        elif payload.quantity is not None:
            # §4.5: the unit lives on the fuel type, so a quantity without one cannot be
            # interpreted. The database says so too (ck_credit_sales_quantity_needs_fuel_
            # type); this is the readable half of that belt and braces.
            raise AppError(
                status_code=422,
                code="QUANTITY_NEEDS_FUEL_TYPE",
                detail=(
                    "A quantity needs a fuel type -- without one there is no unit, and "
                    "12 could mean litres or kilograms."
                ),
            )

        # §6.6's limit, and the admin override. Checked before anything is written.
        if payload.limit_override_reason is not None:
            if not satisfies(actor.role, Role.admin):
                raise AppError(
                    status_code=403,
                    code="LIMIT_OVERRIDE_REQUIRES_ADMIN",
                    detail=(
                        "Only an admin may take a customer past their credit limit, and "
                        "the reason is recorded against the sale."
                    ),
                )
        else:
            credit_service.check_credit_limit(
                db, customer=customer, amount=payload.amount
            )

        # §7.2 step 6 / §5.3's one-attachment-one-live-row rule. Linked BEFORE the sale row
        # is constructed, so a refusal here leaves nothing written --
        # attachment_service.link()'s own docstring calls this ordering out.
        attachment = db.get(Attachment, payload.attachment_id)
        if attachment is None:
            raise AppError(
                status_code=404,
                code="ATTACHMENT_NOT_FOUND",
                detail="No attachment with that id.",
            )
        attachment_service.link(db, attachment=attachment, outlet_id=shift.outlet_id)

        _warn_if_amount_diverges(
            db,
            shift=shift,
            fuel_type_id=payload.fuel_type_id,
            quantity=payload.quantity,
            amount=payload.amount,
        )

        sale = CreditSale(
            shift_id=shift.id,
            credit_customer_id=customer.id,
            fuel_type_id=payload.fuel_type_id,
            quantity=payload.quantity,
            amount=payload.amount,
            vehicle_number=payload.vehicle_number,
            attachment_id=attachment.id,
            limit_override_reason=payload.limit_override_reason,
            created_by=actor.user.id,
        )
        db.add(sale)
        db.flush()

        audit.record(
            db,
            outlet_id=shift.outlet_id,
            table_name="credit_sales",
            record_id=sale.id,
            action=AuditAction.insert,
            changed_by=actor.user.id,
            # §6.6 requires the override to be audit-logged as well as stored on the row.
            # Folded into new_values the same way shifts.py folds a reopen reason in --
            # AuditAction has no `override` label and its labels are fixed at migration time.
            new_values=_audit_snapshot(sale),
        )
        db.commit()
        db.refresh(sale)
    except Exception:
        # Same reasoning as expenses.py: the reservation is committed before the work starts,
        # so a refusal here would otherwise leave the key wedged at REQUEST_IN_PROGRESS for
        # 24 hours.
        db.rollback()
        idempotency.discard(db, key=key, endpoint=endpoint, user_id=actor.user.id)
        raise

    response = _to_response(sale)
    body = jsonable_encoder(response)
    idempotency.store(
        db, key=key, endpoint=endpoint, user_id=actor.user.id, status_code=201, body=body
    )

    logger.info(
        "credit sale recorded",
        extra={
            "credit_sale_id": str(sale.id),
            "shift_id": str(shift.id),
            "credit_customer_id": str(customer.id),
            "amount": str(sale.amount),
            "limit_overridden": sale.limit_override_reason is not None,
        },
    )
    return response


@router.patch(
    "/shifts/{shift_id}/credit-sales/{sale_id}", response_model=CreditSaleResponse
)
def update_credit_sale(
    sale_id: UUID,
    payload: CreditSaleUpdate,
    access: ShiftAccess = Depends(require_shift_access(Role.attendant, writable=True)),
    db: Session = Depends(get_db),
) -> CreditSaleResponse:
    """Correct a figure while the shift is still open.

    No Idempotency-Key: a PATCH is idempotent by construction. Only reachable on an open
    shift -- `writable=True` refuses a closed or locked one, and from there §6.9's reversal
    route is the correction path.

    **The credit limit is deliberately not re-checked here.** Raising an amount past the
    limit on an open shift is the same act as issuing it there, so it looks like a hole --
    but closing it would mean an attendant correcting ₹500 to ₹520 gets refused for a limit
    the original sale already passed, with no way forward except a manager. The limit is a
    control on *extending* credit, and §6.6's own override path already exists for the case
    where somebody means it. Recorded here so it reads as a decision.
    """
    shift = access.shift
    actor = access.actor
    sale = _load_sale(db, shift.id, sale_id)

    if sale.reverses_id is not None:
        raise AppError(
            status_code=409,
            code="CANNOT_EDIT_A_REVERSAL",
            detail=(
                "This row is a reversal and records what was cancelled. Correct the "
                "replacement instead."
            ),
        )

    if credit_service.sale_reversal_of(db, sale_id=sale.id) is not None:
        raise AppError(
            status_code=409,
            code="CREDIT_SALE_ALREADY_REVERSED",
            detail=(
                "This credit sale has been reversed and is now history. Record the correct "
                "figure as a new sale."
            ),
        )

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise AppError(
            status_code=422,
            code="NO_FIELDS_TO_UPDATE",
            detail="Send at least one field to change.",
        )

    before = _audit_snapshot(sale)
    for field, value in changes.items():
        # `quantity` and `vehicle_number` are nullable, so an explicit null clears them --
        # unlike `amount`, whose column is NOT NULL and whose Pydantic type already refuses
        # a null before this runs.
        setattr(sale, field, value)

    if sale.quantity is not None and sale.fuel_type_id is None:
        raise AppError(
            status_code=422,
            code="QUANTITY_NEEDS_FUEL_TYPE",
            detail=(
                "A quantity needs a fuel type, and this sale has none. Reverse it and "
                "record the correct sale instead."
            ),
        )

    _warn_if_amount_diverges(
        db,
        shift=shift,
        fuel_type_id=sale.fuel_type_id,
        quantity=sale.quantity,
        amount=sale.amount,
    )

    audit.record(
        db,
        outlet_id=shift.outlet_id,
        table_name="credit_sales",
        record_id=sale.id,
        action=AuditAction.update,
        changed_by=actor.user.id,
        old_values=before,
        new_values=_audit_snapshot(sale),
    )
    db.commit()
    db.refresh(sale)

    logger.info(
        "credit sale updated",
        extra={"credit_sale_id": str(sale.id), "fields": sorted(changes)},
    )
    return _to_response(sale)


@router.post(
    "/shifts/{shift_id}/credit-sales/{sale_id}/reversals",
    response_model=CreditSaleReversalResponse,
    status_code=201,
)
def reverse_credit_sale(
    sale_id: UUID,
    payload: CreditSaleReversal,
    access: ShiftAccess = Depends(require_shift_access(Role.manager)),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """§6.9's correction path. Manager floor; admin if the shift is locked.

    Deliberately **not** `writable=True`: the whole point of a reversal is that it works on a
    shift that can no longer be edited. §4.7 makes that the normal case here rather than an
    edge one -- the whole day is typed in after the fact, so a mistyped udhaar is routinely
    found once the shift is already closed.
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
    endpoint = "POST /shifts/{shift_id}/credit-sales/{sale_id}/reversals"

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

        reversal, replacement = credit_service.reverse_sale(
            db,
            original=original,
            reason=payload.reason,
            actor_id=actor.user.id,
            replacement_amount=payload.replacement_amount,
        )

        audit.record(
            db,
            outlet_id=shift.outlet_id,
            table_name="credit_sales",
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
                table_name="credit_sales",
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

    response = CreditSaleReversalResponse(
        reversal=_to_response(reversal),
        replacement=_to_response(replacement) if replacement is not None else None,
        original=_to_response(original, is_reversed=True),
    )
    body = jsonable_encoder(response)
    idempotency.store(
        db, key=key, endpoint=endpoint, user_id=actor.user.id, status_code=201, body=body
    )
    return response
