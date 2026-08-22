"""Salesman shortfalls and their settlement (CLAUDE.md §5.2, §6.4, §6.9, §8, §13.14).

Phase 10. §14 says this outlet books a cash shortfall as udhaar against the salesman's own
name. These are the routes that do it -- and the ones that make sure a human, not the
software, is the thing that decides.

## A gap is not a debt until somebody says so

`GET /shifts/{id}/cash-position` computes the gap and writes nothing. This module is where a
**manager** turns one into a debt, with a mandatory reason. §4.7's argument carries more
weight here than anywhere else in the document, because the output has a real person's name
on it: *"an assumed opening converts theft into a debt owed by someone who did nothing
wrong."* A ₹500 gap is more often a mistyped reading, a forgotten UPI figure or an unrecorded
udhaar slip than it is theft.

## `salesman_id` is never accepted from a client

It is read from `shifts.attendant_id` -- §5.2's *"exactly one name carries the drawer"*. A
client-supplied value would let a typo put a debt on the wrong person, and there is no second
source of truth to catch that. The one place a salesman *is* named in a payload is a
settlement, because money can arrive on a later shift worked by somebody else.

## What is deliberately absent

No write-off, no wage deduction. The owner's answer was that a shortfall is repaid in cash,
so a settlement has no `mode` and every one reaches §6.4's drawer. §13.15 records the cost.

**Role floors (§8).** Booking a shortfall and recording a settlement are manager acts;
reversing on a *locked* shift is an admin's. An attendant cannot see their own balance here
-- the ledger is a supervisory report.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints, condecimal
from sqlalchemy import literal, select, tuple_, union_all
from sqlalchemy.orm import Session

from app.api.cursor import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from app.api.deps import Actor, ShiftAccess, require_role, require_shift_access
from app.core import idempotency
from app.core.audit import AuditAction
from app.core.errors import AppError
from app.core.roles import Role, satisfies
from app.core.shifts import ShiftStatus
from app.db.session import get_db
from app.models.shortfall import SalesmanShortfall, SalesmanShortfallSettlement
from app.services import audit, cash as cash_service, shortfalls as shortfall_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["shortfalls"])

_MAX_ROWS = 100


# --- schemas -----------------------------------------------------------------

# §3 rule 1. `gt=0` -- a ₹0 shortfall records nothing and would put a name on a debt of
# nothing, which is worse than useless.
MoneyValue = condecimal(max_digits=12, decimal_places=2, gt=0)
ReasonValue = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)
]


class ShortfallResponse(BaseModel):
    id: UUID
    shift_id: UUID
    salesman_id: UUID
    amount: Decimal
    computed_gap: Decimal
    reason: str | None
    reverses_id: UUID | None
    reversal_reason: str | None
    is_reversed: bool


class ShortfallPage(BaseModel):
    items: list[ShortfallResponse]
    total: Decimal
    cash_basis: str = (
        "total is SUBTRACTED in CLAUDE.md §6.4's expected_closing. A booked shortfall is "
        "money the salesman owes INSTEAD of holding; without the subtraction it would be "
        "both his debt and cash the locker does not contain."
    )
    truncated: bool


class ShortfallCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # No `salesman_id`. Read from shifts.attendant_id -- see the module docstring.
    amount: MoneyValue
    reason: ReasonValue


class ShortfallReversal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: ReasonValue
    replacement_amount: MoneyValue | None = None


class ShortfallReversalResponse(BaseModel):
    reversal: ShortfallResponse
    replacement: ShortfallResponse | None
    original: ShortfallResponse


class SettlementResponse(BaseModel):
    id: UUID
    shift_id: UUID
    salesman_id: UUID
    amount: Decimal
    reverses_id: UUID | None
    reversal_reason: str | None
    is_reversed: bool


class SettlementPage(BaseModel):
    items: list[SettlementResponse]
    total: Decimal
    cash_basis: str = (
        "Every settlement is cash and is ADDED to CLAUDE.md §6.4's expected_closing -- the "
        "salesman handed money back and it is in the locker."
    )
    truncated: bool


class SettlementCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Named explicitly, unlike a shortfall: money can arrive on a later shift worked by
    # somebody else entirely, so it cannot be read off `shifts.attendant_id`.
    salesman_id: UUID
    amount: MoneyValue


class SettlementReversal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: ReasonValue
    replacement_amount: MoneyValue | None = None


class SettlementReversalResponse(BaseModel):
    reversal: SettlementResponse
    replacement: SettlementResponse | None
    original: SettlementResponse


class OutstandingRow(BaseModel):
    salesman_id: UUID
    full_name: str
    outstanding: Decimal


class OutstandingReport(BaseModel):
    items: list[OutstandingRow]
    basis: str = (
        "outstanding = SUM(shortfalls) - SUM(settlements), computed over every row "
        "including reversals, never stored (CLAUDE.md §6.6, §14). It may be negative: a "
        "salesman who rounds a repayment up is owed money by the pump."
    )


class LedgerEntry(BaseModel):
    id: UUID
    kind: str
    shift_id: UUID
    amount: Decimal
    balance_delta: Decimal
    is_reversal: bool
    created_at: str


class LedgerPage(BaseModel):
    items: list[LedgerEntry]
    next_cursor: str | None


# --- helpers -----------------------------------------------------------------


def _shortfall_response(
    row: SalesmanShortfall, *, is_reversed: bool = False
) -> ShortfallResponse:
    return ShortfallResponse(
        id=row.id,
        shift_id=row.shift_id,
        salesman_id=row.salesman_id,
        amount=row.amount,
        computed_gap=row.computed_gap,
        reason=row.reason,
        reverses_id=row.reverses_id,
        reversal_reason=row.reversal_reason,
        is_reversed=is_reversed,
    )


def _settlement_response(
    row: SalesmanShortfallSettlement, *, is_reversed: bool = False
) -> SettlementResponse:
    return SettlementResponse(
        id=row.id,
        shift_id=row.shift_id,
        salesman_id=row.salesman_id,
        amount=row.amount,
        reverses_id=row.reverses_id,
        reversal_reason=row.reversal_reason,
        is_reversed=is_reversed,
    )


def _shortfall_snapshot(row: SalesmanShortfall) -> dict[str, object]:
    """`computed_gap` is in the snapshot deliberately: an audit entry that recorded only the
    booked amount would lose the system's own half of §4.7's predict-and-confirm pair, which
    is the context anybody reviewing the debt later actually needs."""
    return {
        "salesman_id": row.salesman_id,
        "amount": row.amount,
        "computed_gap": row.computed_gap,
        "reason": row.reason,
        "reverses_id": row.reverses_id,
    }


def _settlement_snapshot(row: SalesmanShortfallSettlement) -> dict[str, object]:
    return {
        "salesman_id": row.salesman_id,
        "amount": row.amount,
        "reverses_id": row.reverses_id,
    }


def _load_shortfall(db: Session, shift_id: UUID, row_id: UUID) -> SalesmanShortfall:
    row = db.get(SalesmanShortfall, row_id)
    if row is None:
        raise AppError(
            status_code=404,
            code="SHORTFALL_NOT_FOUND",
            detail="No shortfall with that id.",
        )
    if row.shift_id != shift_id:
        raise AppError(
            status_code=409,
            code="SHORTFALL_NOT_IN_SHIFT",
            detail="That shortfall belongs to a different shift.",
        )
    return row


def _load_settlement(
    db: Session, shift_id: UUID, row_id: UUID
) -> SalesmanShortfallSettlement:
    row = db.get(SalesmanShortfallSettlement, row_id)
    if row is None:
        raise AppError(
            status_code=404,
            code="SETTLEMENT_NOT_FOUND",
            detail="No settlement with that id.",
        )
    if row.shift_id != shift_id:
        raise AppError(
            status_code=409,
            code="SETTLEMENT_NOT_IN_SHIFT",
            detail="That settlement belongs to a different shift.",
        )
    return row


def _require_key(idempotency_key: str | None) -> str:
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


def _guard_locked(access: ShiftAccess) -> None:
    """A reversal on a locked shift is an admin action (§6.9, §5.2)."""
    if ShiftStatus(access.shift.status) is ShiftStatus.locked and not satisfies(
        access.actor.role, Role.admin
    ):
        raise AppError(
            status_code=403,
            code="LOCKED_SHIFT_REVERSAL_REQUIRES_ADMIN",
            detail=(
                "This shift is locked. A reversal against it is an admin action, because "
                "locking is the point at which a day stops being anybody else's to change."
            ),
        )


# --- routes: shortfalls ------------------------------------------------------


@router.get("/shifts/{shift_id}/shortfalls", response_model=ShortfallPage)
def list_shortfalls(
    access: ShiftAccess = Depends(require_shift_access(Role.manager)),
    db: Session = Depends(get_db),
) -> ShortfallPage:
    """Every shortfall booked against this shift, reversals included (§6.9).

    Manager floor to read as well as write: a debt with somebody's name on it is not part of
    the sheet that person fills in.
    """
    shift = access.shift
    rows = shortfall_service.all_shortfalls(db, shift_id=shift.id)
    reversed_ids = {row.reverses_id for row in rows if row.reverses_id is not None}
    truncated = len(rows) > _MAX_ROWS
    rows = rows[:_MAX_ROWS]

    return ShortfallPage(
        items=[
            _shortfall_response(row, is_reversed=row.id in reversed_ids) for row in rows
        ],
        total=cash_service.shortfalls_booked_total(db, shift_id=shift.id),
        truncated=truncated,
    )


@router.post(
    "/shifts/{shift_id}/shortfalls", response_model=ShortfallResponse, status_code=201
)
def book_shortfall(
    payload: ShortfallCreate,
    access: ShiftAccess = Depends(require_shift_access(Role.manager)),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Turn a computed gap into a debt. **Manager floor, mandatory reason** (§13.14).

    Deliberately **not** `writable=True`. The gap is only knowable once the shift is closed
    and its readings are final, so refusing a closed shift would make this unreachable
    exactly when it is needed -- which is the same reason every reversal route since Phase 5
    declines that dependency.

    The **computed gap is recomputed here**, server-side, and stored alongside the booked
    amount. §3 rule 7: never trust a client-supplied derived figure. Storing both is §4.7's
    predict-and-confirm shape -- the system's number and the human's, side by side, so a
    disagreement is a fact on the row rather than something nobody recorded.

    A divergence **warns and writes**; it never refuses. §6.8's reasoning: a manager may know
    part of the gap is a slip already corrected, and refusing his judgement would send the
    correction somewhere the system cannot see it.
    """
    shift = access.shift
    actor = access.actor
    key = _require_key(idempotency_key)
    endpoint = "POST /shifts/{shift_id}/shortfalls"

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
        position = cash_service.shift_cash_position(db, shift=shift)
        # A gap of None means nobody has declared any cash yet (§6.8). Booking a debt then
        # would be inventing the salesman's half of the comparison -- exactly what §4.7
        # refuses to do for a meter reading, for the same reason.
        if position.gap is None:
            raise AppError(
                status_code=409,
                code="NO_CASH_DECLARED",
                detail=(
                    "Nobody has declared the cash for this shift, so there is no gap to "
                    "book against. Record the cash collection first -- an explicit ₹0 "
                    "counts as an answer."
                ),
            )

        shortfall_service.warn_if_gap_differs(
            booked=payload.amount,
            computed_gap=position.gap,
            salesman_id=shift.attendant_id,
            shift_id=shift.id,
        )

        row = SalesmanShortfall(
            shift_id=shift.id,
            # §5.2: exactly one name carries the drawer, and it is not the client's to pick.
            salesman_id=shift.attendant_id,
            amount=payload.amount,
            computed_gap=position.gap,
            reason=payload.reason,
            created_by=actor.user.id,
        )
        db.add(row)
        db.flush()

        audit.record(
            db,
            outlet_id=shift.outlet_id,
            table_name="salesman_shortfalls",
            record_id=row.id,
            action=AuditAction.insert,
            changed_by=actor.user.id,
            new_values=_shortfall_snapshot(row),
        )
        db.commit()
        db.refresh(row)
    except Exception:
        db.rollback()
        idempotency.discard(db, key=key, endpoint=endpoint, user_id=actor.user.id)
        raise

    body = jsonable_encoder(_shortfall_response(row))
    idempotency.store(
        db,
        key=key,
        endpoint=endpoint,
        user_id=actor.user.id,
        status_code=201,
        body=body,
    )

    logger.warning(
        "shortfall booked against a salesman",
        extra={
            "shortfall_id": str(row.id),
            "shift_id": str(shift.id),
            "salesman_id": str(row.salesman_id),
            "amount": str(row.amount),
            "computed_gap": str(row.computed_gap),
            "booked_by": str(actor.user.id),
        },
    )
    return _shortfall_response(row)


@router.post(
    "/shifts/{shift_id}/shortfalls/{shortfall_id}/reversals",
    response_model=ShortfallReversalResponse,
    status_code=201,
)
def reverse_shortfall(
    shortfall_id: UUID,
    payload: ShortfallReversal,
    access: ShiftAccess = Depends(require_shift_access(Role.manager)),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Cancel a booked shortfall (§6.9). Manager floor; admin if the shift is locked.

    The most important correction path in this phase. A shortfall booked on a mistyped
    reading is a debt against somebody who did nothing wrong, and §4.7 exists because that
    outcome is worse than the error that caused it.
    """
    shift = access.shift
    actor = access.actor
    _guard_locked(access)

    key = _require_key(idempotency_key)
    endpoint = "POST /shifts/{shift_id}/shortfalls/{shortfall_id}/reversals"

    replay = idempotency.begin(
        db,
        key=key,
        endpoint=endpoint,
        user_id=actor.user.id,
        request_fingerprint=idempotency.fingerprint(
            path_params={"shift_id": shift.id, "shortfall_id": shortfall_id},
            body=jsonable_encoder(payload),
        ),
    )
    if replay is not None:
        return _replayed(replay)

    try:
        original = _load_shortfall(db, shift.id, shortfall_id)
        before = _shortfall_snapshot(original)

        reversal, replacement = shortfall_service.reverse_shortfall(
            db,
            original=original,
            reason=payload.reason,
            actor_id=actor.user.id,
            replacement_amount=payload.replacement_amount,
        )

        audit.record(
            db,
            outlet_id=shift.outlet_id,
            table_name="salesman_shortfalls",
            record_id=reversal.id,
            action=AuditAction.reversal,
            changed_by=actor.user.id,
            old_values=before,
            new_values=_shortfall_snapshot(reversal)
            | {"reason": reversal.reversal_reason},
        )
        if replacement is not None:
            audit.record(
                db,
                outlet_id=shift.outlet_id,
                table_name="salesman_shortfalls",
                record_id=replacement.id,
                action=AuditAction.insert,
                changed_by=actor.user.id,
                new_values=_shortfall_snapshot(replacement)
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

    result = ShortfallReversalResponse(
        reversal=_shortfall_response(reversal),
        replacement=(
            _shortfall_response(replacement) if replacement is not None else None
        ),
        original=_shortfall_response(original, is_reversed=True),
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


# --- routes: settlements -----------------------------------------------------


@router.get(
    "/shifts/{shift_id}/shortfall-settlements", response_model=SettlementPage
)
def list_settlements(
    access: ShiftAccess = Depends(require_shift_access(Role.manager)),
    db: Session = Depends(get_db),
) -> SettlementPage:
    """Every settlement received on this shift, reversals included (§6.9)."""
    shift = access.shift
    rows = shortfall_service.all_settlements(db, shift_id=shift.id)
    reversed_ids = {row.reverses_id for row in rows if row.reverses_id is not None}
    truncated = len(rows) > _MAX_ROWS
    rows = rows[:_MAX_ROWS]

    return SettlementPage(
        items=[
            _settlement_response(row, is_reversed=row.id in reversed_ids) for row in rows
        ],
        total=cash_service.cash_settlements_total(db, shift_id=shift.id),
        truncated=truncated,
    )


@router.post(
    "/shifts/{shift_id}/shortfall-settlements",
    response_model=SettlementResponse,
    status_code=201,
)
def record_settlement(
    payload: SettlementCreate,
    access: ShiftAccess = Depends(require_shift_access(Role.manager, writable=True)),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Record a salesman paying back what he owed. Manager floor (§8).

    `writable=True` here, unlike booking: a settlement is cash arriving *now*, on the shift
    currently being worked, so the open-shift rule is the right one. Money arriving against a
    debt from three weeks ago still lands on today's shift, which is why `salesman_id` is a
    payload field here and read off the shift when booking.

    **Larger than outstanding is accepted** and drives the balance negative -- a salesman
    rounding ₹480 up to ₹500 is real, and §6.6 makes exactly this decision for customers.
    """
    shift = access.shift
    actor = access.actor
    key = _require_key(idempotency_key)
    endpoint = "POST /shifts/{shift_id}/shortfall-settlements"

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
        row = SalesmanShortfallSettlement(
            shift_id=shift.id,
            salesman_id=payload.salesman_id,
            amount=payload.amount,
            created_by=actor.user.id,
        )
        db.add(row)
        db.flush()

        audit.record(
            db,
            outlet_id=shift.outlet_id,
            table_name="salesman_shortfall_settlements",
            record_id=row.id,
            action=AuditAction.insert,
            changed_by=actor.user.id,
            new_values=_settlement_snapshot(row),
        )
        db.commit()
        db.refresh(row)
    except Exception:
        db.rollback()
        idempotency.discard(db, key=key, endpoint=endpoint, user_id=actor.user.id)
        raise

    body = jsonable_encoder(_settlement_response(row))
    idempotency.store(
        db,
        key=key,
        endpoint=endpoint,
        user_id=actor.user.id,
        status_code=201,
        body=body,
    )

    logger.info(
        "shortfall settlement recorded",
        extra={
            "settlement_id": str(row.id),
            "shift_id": str(shift.id),
            "salesman_id": str(row.salesman_id),
            "amount": str(row.amount),
        },
    )
    return _settlement_response(row)


@router.post(
    "/shifts/{shift_id}/shortfall-settlements/{settlement_id}/reversals",
    response_model=SettlementReversalResponse,
    status_code=201,
)
def reverse_settlement(
    settlement_id: UUID,
    payload: SettlementReversal,
    access: ShiftAccess = Depends(require_shift_access(Role.manager)),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Cancel a settlement (§6.9). The negative row puts the debt back and stops §6.4 adding
    cash that never arrived."""
    shift = access.shift
    actor = access.actor
    _guard_locked(access)

    key = _require_key(idempotency_key)
    endpoint = "POST /shifts/{shift_id}/shortfall-settlements/{settlement_id}/reversals"

    replay = idempotency.begin(
        db,
        key=key,
        endpoint=endpoint,
        user_id=actor.user.id,
        request_fingerprint=idempotency.fingerprint(
            path_params={"shift_id": shift.id, "settlement_id": settlement_id},
            body=jsonable_encoder(payload),
        ),
    )
    if replay is not None:
        return _replayed(replay)

    try:
        original = _load_settlement(db, shift.id, settlement_id)
        before = _settlement_snapshot(original)

        reversal, replacement = shortfall_service.reverse_settlement(
            db,
            original=original,
            reason=payload.reason,
            actor_id=actor.user.id,
            replacement_amount=payload.replacement_amount,
        )

        audit.record(
            db,
            outlet_id=shift.outlet_id,
            table_name="salesman_shortfall_settlements",
            record_id=reversal.id,
            action=AuditAction.reversal,
            changed_by=actor.user.id,
            old_values=before,
            new_values=_settlement_snapshot(reversal)
            | {"reason": reversal.reversal_reason},
        )
        if replacement is not None:
            audit.record(
                db,
                outlet_id=shift.outlet_id,
                table_name="salesman_shortfall_settlements",
                record_id=replacement.id,
                action=AuditAction.insert,
                changed_by=actor.user.id,
                new_values=_settlement_snapshot(replacement)
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

    result = SettlementReversalResponse(
        reversal=_settlement_response(reversal),
        replacement=(
            _settlement_response(replacement) if replacement is not None else None
        ),
        original=_settlement_response(original, is_reversed=True),
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


# --- routes: the ledger ------------------------------------------------------
#
# `/salesman-shortfalls/outstanding` is declared BEFORE `/salesman-shortfalls/{salesman_id}/
# ledger`. The two do not actually collide today -- the ledger carries an extra segment --
# but `router.py` and `credit_customers.py` both carry this ordering note, and the day
# somebody adds a bare `/salesman-shortfalls/{salesman_id}` the static path has to already
# be first or the report 422s trying to parse "outstanding" as a UUID.


@router.get("/salesman-shortfalls/outstanding", response_model=OutstandingReport)
def read_outstanding(
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
) -> OutstandingReport:
    """Who owes what. Manager floor (§8).

    Only salesmen with rows appear. Unlike the customer report there is no "everyone at ₹0"
    baseline, because the population here is "staff who have been short", not "staff" -- and
    listing every employee at zero would turn a short exception report into a roster.
    """
    from app.models.user import UserProfile

    balances = shortfall_service.outstanding_by_salesman(
        db, outlet_id=actor.outlet_id
    )
    names = {
        row.id: row.full_name
        for row in db.execute(
            select(UserProfile).where(UserProfile.id.in_(balances or [None]))
        ).scalars()
    }
    return OutstandingReport(
        items=[
            OutstandingRow(
                salesman_id=salesman_id,
                full_name=names.get(salesman_id, "(unknown)"),
                outstanding=amount,
            )
            for salesman_id, amount in sorted(
                balances.items(), key=lambda item: item[1], reverse=True
            )
        ]
    )


@router.get(
    "/salesman-shortfalls/{salesman_id}/ledger", response_model=LedgerPage
)
def read_ledger(
    salesman_id: UUID,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
) -> LedgerPage:
    """Every shortfall and settlement against one salesman, newest first. Manager floor.

    The evidence behind the outstanding figure. A balance nobody can take apart is a number
    that has to be trusted rather than checked -- and this one is a debt held against an
    employee, so being able to walk it line by line is the difference between a control and
    an accusation.

    A `UNION ALL` across the two tables rather than two requests the client merges, for the
    reason `credit_customers.py`'s ledger gives: taking the newest 50 of each and
    interleaving gives the newest 50 overall only by luck, and gets steadily wronger the more
    one-sided the account is.

    Reversals appear as their own lines rather than being netted away (§6.9). A salesman
    disputing a debt is entitled to see that one was raised and cancelled, not an account
    that silently never mentions it.
    """
    booked = select(
        SalesmanShortfall.id.label("id"),
        SalesmanShortfall.created_at.label("created_at"),
        literal("shortfall").label("kind"),
        SalesmanShortfall.amount.label("amount"),
        # A shortfall adds to what is owed, so the row's own sign is the balance effect.
        SalesmanShortfall.amount.label("balance_delta"),
        SalesmanShortfall.shift_id.label("shift_id"),
        SalesmanShortfall.reverses_id.label("reverses_id"),
    ).where(SalesmanShortfall.salesman_id == salesman_id)

    settled = select(
        SalesmanShortfallSettlement.id,
        SalesmanShortfallSettlement.created_at,
        literal("settlement"),
        SalesmanShortfallSettlement.amount,
        # A settlement reduces the debt, so its balance effect is the negation -- which also
        # makes a *reversed* settlement (already negative) correctly add the debt back,
        # without needing a second rule.
        -SalesmanShortfallSettlement.amount,
        SalesmanShortfallSettlement.shift_id,
        SalesmanShortfallSettlement.reverses_id,
    ).where(SalesmanShortfallSettlement.salesman_id == salesman_id)

    combined = union_all(booked, settled).subquery()
    statement = select(combined).order_by(
        combined.c.created_at.desc(), combined.c.id.desc()
    )
    if cursor is not None:
        last_created_at, last_id = decode_cursor(cursor)
        statement = statement.where(
            tuple_(combined.c.created_at, combined.c.id)
            < tuple_(last_created_at, last_id)
        )

    rows = db.execute(statement.limit(limit + 1)).all()
    has_more = len(rows) > limit
    page = rows[:limit]

    return LedgerPage(
        items=[
            LedgerEntry(
                id=row.id,
                kind=row.kind,
                shift_id=row.shift_id,
                amount=row.amount,
                balance_delta=row.balance_delta,
                is_reversal=row.reverses_id is not None,
                created_at=row.created_at.isoformat(),
            )
            for row in page
        ],
        next_cursor=(
            encode_cursor(page[-1].created_at, page[-1].id) if has_more and page else None
        ),
    )
