"""Expenses -- what the pump paid out (CLAUDE.md §5.2, §6.7, §6.8, §6.9, §6.10).

Mirrors `app/api/v1/collections.py` for money-in-motion: create, correct, list, reverse.
`app/api/v1/readings.py`'s review route supplies the fifth shape, for §6.7's flag.

**Seven routes.** Create, correct, list, reverse, review, a cross-shift flagged queue, and
(Phase 8) a manager-floor month-end summary across a date range.

**§6.7's two flagging rules run after every amount-changing write** -- insert, an
amount-changing `PATCH`, and a reversal's replacement -- via
`app/services/expenses.py::apply_review_flags`. A bare reversal never re-runs them: it can
only subtract from a category's daily total, so it can never newly cross the threshold, and
flags are never auto-cleared regardless.

**Retries do not duplicate money (§6.10).** Both money-creating POSTs require an
`Idempotency-Key`.

**Role floors (§8).** Attendants record expenses on their own shift, ownership enforced by
`require_shift_access` and never re-implemented here. Reversing and reviewing are a
manager's acts; reversing on a *locked* shift is an admin's, mirroring `collections.py`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, condecimal
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.api.cursor import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from app.api.deps import Actor, ShiftAccess, require_role, require_shift_access
from app.core import idempotency
from app.core.audit import AuditAction
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.expenses import ExpenseMode, ExpensePaidFrom
from app.core.roles import Role, satisfies
from app.core.shifts import ShiftStatus
from app.db.session import get_db
from app.models.attachment import Attachment
from app.models.expense import Expense
from app.models.expense_category import ExpenseCategory
from app.models.shift import Shift
from app.services import attachments as attachment_service
from app.services import audit, expenses as expense_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["expenses"])

# Same reasoning as collections.py's _MAX_ROWS: a shift's own expenses are a handful of
# rows, not a history to page through, and a runaway correction loop should surface as a
# truncated list rather than an unbounded response.
_MAX_ROWS = 100


# --- schemas -----------------------------------------------------------------

# §3 rule 1: money is NUMERIC(12,2) and Decimal end to end. `gt=0`, not `ge=0` like
# collections' MoneyValue -- CLAUDE.md §5.2's Phase 7 amendment makes the sign rule strict,
# because a ₹0 expense records nothing and has no reason to exist (unlike a ₹0 cash
# collection, which is a genuine declaration under §6.8).
MoneyValue = condecimal(max_digits=12, decimal_places=2, gt=0)

# strip_whitespace runs BEFORE the length check -- see CollectionReversal's comment in
# collections.py for why that ordering is the whole point, not a stylistic choice. A
# reason of "   " must never reach `.strip()` and become an empty string in the database.
ReasonValue = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)
]
DescriptionValue = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)
]


class ExpenseResponse(BaseModel):
    id: UUID
    shift_id: UUID
    # Both, deliberately. `category_id` is the fact -- it is what the row stores and what a
    # client sends back on the next write. `category_code` saves every reader a second
    # request purely to render the word "maintenance", the same argument §4.5 makes for
    # always returning `unit_of_measure` alongside a quantity.
    category_id: UUID
    category_code: str
    mode: ExpenseMode
    # Phase 17. Read by §6.4's `accountable_cash` only, and only when `mode == cash`.
    paid_from: ExpensePaidFrom
    amount: Decimal
    description: str
    paid_to: str | None
    # §6.11. `receipt_required` is the snapshot taken at insert -- what the rule said that
    # day, not what `expense_categories.requires_receipt` says now (§6.11's whole point).
    attachment_id: UUID | None
    receipt_required: bool
    reverses_id: UUID | None
    reversal_reason: str | None
    requires_review: bool
    reviewed_by: UUID | None
    reviewed_at: str | None
    review_note: str | None
    is_reversed: bool


class ExpensePage(BaseModel):
    """A shift's expenses, with the derived figures worth having on the page.

    `totals_by_category` nets reversals into the sum, so a cancelled ₹5,000 shows as the
    reduction it is instead of vanishing -- same shape as `collections.py`'s
    `totals_by_mode`. `total` is the same sum collapsed across categories, computed here
    rather than by a client: §3 rule 1 / §14 forbid summing money in JavaScript, and the
    month-end summary route (`totals_by_category_range`, below) already computes this exact
    shape for a date range -- this is that pattern applied to one shift.
    """

    items: list[ExpenseResponse]
    totals_by_category: dict[str, Decimal]
    total: Decimal
    truncated: bool


class ExpenseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: UUID
    mode: ExpenseMode
    # Phase 17. Defaulted rather than required: `shift_cash` is the ordinary case, and
    # §5.2 explains why forcing the answer on every ₹20 chai entry would be the friction
    # §6.11 warns teaches staff to fake input. §13.33 records what that costs.
    paid_from: ExpensePaidFrom = ExpensePaidFrom.shift_cash
    amount: MoneyValue
    description: DescriptionValue
    paid_to: str | None = Field(default=None, max_length=200)
    # §6.11, §7.2 step 6. Optional even when the rule will end up requiring one -- omitting
    # it when required is refused with 422 EXPENSE_REQUIRES_RECEIPT, not accepted and then
    # silently non-compliant.
    attachment_id: UUID | None = None


class ExpenseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # No `category_id`. Changing what an expense was *for* is not a correction of this row,
    # it is a different row -- and allowing it here would let a PATCH silently move an
    # expense out of the category group §6.7's aggregate rule already flagged it under.
    mode: ExpenseMode | None = None
    # Phase 17. PATCHable, unlike `category_id` above: this is a classification of a payment
    # that already happened, not a change to what the money was for or how much it was, so
    # §6.9's reversal path would be disproportionate. It re-runs nothing -- it feeds neither
    # §6.11's receipt rule nor §6.7's aggregate, only §6.4's per-shift comparison.
    paid_from: ExpensePaidFrom | None = None
    amount: MoneyValue | None = None
    description: DescriptionValue | None = None
    paid_to: str | None = Field(default=None, max_length=200)
    # May only be set while currently NULL (§5.3, D3) -- swapping an already-set
    # attachment is refused with 409 ATTACHMENT_ALREADY_SET; correct via §6.9's reversal
    # instead. An explicit null is ignored like every other field here, never "clear this".
    attachment_id: UUID | None = None


class ExpenseReversal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: ReasonValue
    # Applied in the same transaction. Without it a correction on a closed shift is
    # impossible: the reversal lands and the follow-up POST is refused by `writable=True`.
    replacement_amount: MoneyValue | None = None
    replacement_paid_to: str | None = Field(default=None, max_length=200)


class ExpenseReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_note: ReasonValue


class ReversalResponse(BaseModel):
    reversal: ExpenseResponse
    replacement: ExpenseResponse | None
    original: ExpenseResponse


class FlaggedExpenseResponse(BaseModel):
    id: UUID
    shift_id: UUID
    business_date: str
    category_id: UUID
    category_code: str
    amount: Decimal
    description: str


class FlaggedExpensePage(BaseModel):
    items: list[FlaggedExpenseResponse]
    next_cursor: str | None


class ExpenseSummaryResponse(BaseModel):
    """§11's month-end category-wise expense summary. JSON only -- no screen exists until
    Phase 12.

    `from_`/`to` rather than `date_from`/`date_to`: `from` is a reserved word in Python, so
    the field is named `from_` and aliased to the query parameter's own name, `from`, for
    both request parsing and (FastAPI's `response_model_by_alias=True` default) the JSON
    response -- the client sees `"from"` and `"to"`, matching what it sent.
    """

    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    to: str
    totals_by_category: dict[str, Decimal]
    total: Decimal


# --- helpers -----------------------------------------------------------------


def _category_code(db: Session, category_id: UUID) -> str:
    """The single-row form. The FK guarantees the row exists, so this cannot miss."""
    return db.execute(
        select(ExpenseCategory.code).where(ExpenseCategory.id == category_id)
    ).scalar_one()


def _to_response(
    expense: Expense, *, category_code: str, is_reversed: bool = False
) -> ExpenseResponse:
    return ExpenseResponse(
        id=expense.id,
        shift_id=expense.shift_id,
        category_id=expense.category_id,
        category_code=category_code,
        mode=ExpenseMode(expense.mode),
        paid_from=ExpensePaidFrom(expense.paid_from),
        amount=expense.amount,
        description=expense.description,
        paid_to=expense.paid_to,
        attachment_id=expense.attachment_id,
        receipt_required=expense.receipt_required,
        reverses_id=expense.reverses_id,
        reversal_reason=expense.reversal_reason,
        requires_review=expense.requires_review,
        reviewed_by=expense.reviewed_by,
        reviewed_at=expense.reviewed_at.isoformat() if expense.reviewed_at else None,
        review_note=expense.review_note,
        is_reversed=is_reversed,
    )


def _audit_snapshot(expense: Expense) -> dict[str, object]:
    """The fields worth recording either side of a change.

    Money, what it was for, how it left, and the review state. Not the whole row: an audit
    entry that echoes every column makes the change itself hard to find.
    """
    return {
        "category_id": str(expense.category_id),
        "mode": expense.mode,
        # Phase 17. In the snapshot because a PATCH may change it, and §6.4 reads it:
        # an unexplained move between piles must be legible in the trail.
        "paid_from": expense.paid_from,
        "amount": expense.amount,
        "description": expense.description,
        "paid_to": expense.paid_to,
        "attachment_id": (
            str(expense.attachment_id) if expense.attachment_id is not None else None
        ),
        "receipt_required": expense.receipt_required,
        "reverses_id": expense.reverses_id,
        "requires_review": expense.requires_review,
    }


def _load_expense(db: Session, shift_id: UUID, expense_id: UUID) -> Expense:
    """Fetch an expense, refusing one that belongs to a different shift.

    409 rather than 404, matching `collections._load_collection`: the row exists and the
    caller may be allowed to see it, but the URL asserts a parent-child relationship that
    is not true.
    """
    expense = db.get(Expense, expense_id)
    if expense is None:
        raise AppError(
            status_code=404, code="EXPENSE_NOT_FOUND", detail="No expense with that id."
        )
    if expense.shift_id != shift_id:
        raise AppError(
            status_code=409,
            code="EXPENSE_NOT_IN_SHIFT",
            detail="That expense belongs to a different shift.",
        )
    return expense


def _require_key(idempotency_key: str | None) -> str:
    """§6.10: not optional on a POST that creates a money record. See collections.py."""
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


@router.get("/shifts/{shift_id}/expenses", response_model=ExpensePage)
def list_expenses(
    access: ShiftAccess = Depends(require_shift_access(Role.attendant)),
    db: Session = Depends(get_db),
) -> ExpensePage:
    """Everything paid out on this shift, reversals included (§6.9: both rows stay visible)."""
    shift = access.shift
    rows = expense_service.all_expenses(db, shift_id=shift.id)
    # Computed before truncation, same reason as collections.py: a reversal that fell past
    # the cap would otherwise make the row it cancels read as still live.
    reversed_ids = {row.reverses_id for row in rows if row.reverses_id is not None}
    truncated = len(rows) > _MAX_ROWS
    rows = rows[:_MAX_ROWS]

    codes = expense_service.category_codes(db, rows)
    totals_by_category = expense_service.totals_by_category(db, shift_id=shift.id)
    return ExpensePage(
        items=[
            _to_response(
                row,
                category_code=codes[row.category_id],
                is_reversed=row.id in reversed_ids,
            )
            for row in rows
        ],
        totals_by_category=totals_by_category,
        total=sum(totals_by_category.values(), Decimal("0.00")),
        truncated=truncated,
    )


@router.post(
    "/shifts/{shift_id}/expenses", response_model=ExpenseResponse, status_code=201
)
def create_expense(
    payload: ExpenseCreate,
    access: ShiftAccess = Depends(require_shift_access(Role.attendant, writable=True)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Record money paid out. Attendant floor, own shift only (§8)."""
    shift = access.shift
    actor = access.actor
    key = _require_key(idempotency_key)
    endpoint = "POST /shifts/{shift_id}/expenses"

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
        # Inside the try/except so a bad category or attachment releases the idempotency
        # key like every other refusal here -- otherwise a typo'd id would wedge that key
        # for 24 hours and the retry, with the *right* value, would be refused as a
        # duplicate.
        category = expense_service.resolve_category(
            db, category_id=payload.category_id, outlet_id=shift.outlet_id
        )

        # §7.2 step 6 / §5.3's one-attachment-one-live-row rule. Linked BEFORE the expense
        # row is even constructed, so a refusal here (unknown id, already claimed) leaves
        # nothing written -- attachment_service.link()'s own docstring calls this ordering
        # out explicitly.
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

        # §6.11. Evaluated against the category and amount actually being written, then
        # snapshotted onto the row below -- never recomputed later.
        receipt_required = expense_service.evaluate_receipt_required(
            category_requires_receipt=category.requires_receipt,
            amount=payload.amount,
            threshold=settings.EXPENSE_RECEIPT_THRESHOLD,
        )
        if receipt_required and attachment is None:
            raise AppError(
                status_code=422,
                code="EXPENSE_REQUIRES_RECEIPT",
                detail=(
                    f"The {category.code} category requires a receipt, or this amount "
                    "exceeds the receipt threshold. Upload a receipt first, then record "
                    "the expense."
                ),
            )

        expense = Expense(
            shift_id=shift.id,
            category_id=category.id,
            mode=payload.mode.value,
            paid_from=payload.paid_from.value,
            amount=payload.amount,
            description=payload.description,
            paid_to=payload.paid_to,
            attachment_id=attachment.id if attachment is not None else None,
            receipt_required=receipt_required,
            created_by=actor.user.id,
        )
        db.add(expense)
        db.flush()

        expense_service.apply_review_flags(
            db, shift=shift, expense=expense, threshold=settings.EXPENSE_REVIEW_THRESHOLD
        )

        audit.record(
            db,
            outlet_id=shift.outlet_id,
            table_name="expenses",
            record_id=expense.id,
            action=AuditAction.insert,
            changed_by=actor.user.id,
            new_values=_audit_snapshot(expense),
        )
        db.commit()
        db.refresh(expense)
    except Exception:
        # Same reasoning as collections.py: the reservation is committed before the work
        # starts, so a refusal here would otherwise leave the key wedged at
        # REQUEST_IN_PROGRESS for 24 hours.
        db.rollback()
        idempotency.discard(db, key=key, endpoint=endpoint, user_id=actor.user.id)
        raise

    response = _to_response(expense, category_code=category.code)
    body = jsonable_encoder(response)
    idempotency.store(
        db, key=key, endpoint=endpoint, user_id=actor.user.id, status_code=201, body=body
    )

    logger.info(
        "expense recorded",
        extra={
            "expense_id": str(expense.id),
            "shift_id": str(shift.id),
            "category_code": category.code,
            "amount": str(expense.amount),
        },
    )
    return response


@router.patch(
    "/shifts/{shift_id}/expenses/{expense_id}", response_model=ExpenseResponse
)
def update_expense(
    expense_id: UUID,
    payload: ExpenseUpdate,
    access: ShiftAccess = Depends(require_shift_access(Role.attendant, writable=True)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ExpenseResponse:
    """Correct a figure while the shift is still open.

    No Idempotency-Key: a PATCH is idempotent by construction. Only reachable on an open
    shift -- `writable=True` refuses a closed or locked one, and from there §6.9's
    reversal route is the correction path.
    """
    shift = access.shift
    actor = access.actor
    expense = _load_expense(db, shift.id, expense_id)

    if expense.reverses_id is not None:
        raise AppError(
            status_code=409,
            code="CANNOT_EDIT_A_REVERSAL",
            detail=(
                "A reversal records what was cancelled and why. Editing it would rewrite "
                "the correction itself. Record a fresh expense instead."
            ),
        )

    # Phase 7 Step 0 found this exact gap on `collections`: PATCH refused a row that *was*
    # a reversal but not one that *had been* reversed, so a cancelled original stayed
    # editable and its reversal kept negating a figure that no longer matched it.
    if expense_service.reversal_of(db, expense_id=expense.id) is not None:
        raise AppError(
            status_code=409,
            code="EXPENSE_ALREADY_REVERSED",
            detail=(
                "This expense has been reversed and its figure is now part of the "
                "record. Record the corrected amount as a new expense rather than "
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

    before = _audit_snapshot(expense)
    amount_changed = "amount" in changes and changes["amount"] is not None
    # Handled separately below, not by the generic loop: setting it needs
    # attachment_service.link() and the "only while NULL" immutability check (§5.3, D3),
    # not a bare setattr. Popped out first so the loop below never sees it -- an explicit
    # null here means the same "ignore it" the loop already gives every other field.
    new_attachment_id = changes.pop("attachment_id", None)

    for field, value in changes.items():
        # An explicit null is ignored rather than treated as "clear this", matching
        # collections.py and readings.py.
        if value is None:
            continue
        if field in ("mode", "paid_from"):
            # `Mapped[str]` expects the raw label, matching `create_expense`'s
            # `payload.mode.value` -- explicit, rather than relying on StrEnum's
            # str-inheritance to make an enum instance quietly acceptable to psycopg.
            # `paid_from` (Phase 17) is the same shape and needs the same unwrapping.
            value = value.value
        setattr(expense, field, value)

    if new_attachment_id is not None:
        if expense.attachment_id is not None:
            raise AppError(
                status_code=409,
                code="ATTACHMENT_ALREADY_SET",
                detail=(
                    "This expense already has a receipt attached. Swapping it is not "
                    "allowed -- correct the expense via a reversal instead (§6.9)."
                ),
            )
        attachment = db.get(Attachment, new_attachment_id)
        if attachment is None:
            raise AppError(
                status_code=404,
                code="ATTACHMENT_NOT_FOUND",
                detail="No attachment with that id.",
            )
        attachment_service.link(db, attachment=attachment, outlet_id=shift.outlet_id)
        expense.attachment_id = new_attachment_id

    if amount_changed:
        # §6.11: re-evaluated against the amount actually being written -- entering ₹900
        # and editing up to ₹9,000 must be able to start demanding a receipt, or the
        # threshold is defeated by a two-step entry. Reads the category's CURRENT
        # requires_receipt flag rather than a stored snapshot of it, which is deliberate
        # and not a contradiction of "never recomputed": that rule protects a row nobody
        # is touching. A PATCH is an active edit happening right now, and re-deriving the
        # rule from the current inputs (the live category, the new amount) is what makes
        # this PATCH's own check meaningful rather than checking against a stale flag.
        category = db.get(ExpenseCategory, expense.category_id)
        new_receipt_required = expense_service.evaluate_receipt_required(
            category_requires_receipt=category.requires_receipt,
            amount=expense.amount,
            threshold=settings.EXPENSE_RECEIPT_THRESHOLD,
        )
        if new_receipt_required and expense.attachment_id is None:
            raise AppError(
                status_code=422,
                code="EXPENSE_REQUIRES_RECEIPT",
                detail=(
                    "This amount now requires a receipt. Upload one and attach it "
                    "before -- or in the same request as -- raising the amount."
                ),
            )
        expense.receipt_required = new_receipt_required

        # §6.7's rules must see the new amount, not the one at insert. Three ₹400 entries
        # followed by an edit to ₹900 would otherwise never trip the category aggregate.
        expense_service.apply_review_flags(
            db, shift=shift, expense=expense, threshold=settings.EXPENSE_REVIEW_THRESHOLD
        )

    audit.record(
        db,
        outlet_id=shift.outlet_id,
        table_name="expenses",
        record_id=expense.id,
        action=AuditAction.update,
        changed_by=actor.user.id,
        old_values=before,
        new_values=_audit_snapshot(expense),
    )
    db.commit()
    db.refresh(expense)

    logger.info(
        "expense updated",
        extra={"expense_id": str(expense.id), "fields": sorted(changes)},
    )
    return _to_response(
        expense, category_code=_category_code(db, expense.category_id)
    )


@router.post(
    "/shifts/{shift_id}/expenses/{expense_id}/reversals",
    response_model=ReversalResponse,
    status_code=201,
)
def reverse_expense(
    expense_id: UUID,
    payload: ExpenseReversal,
    access: ShiftAccess = Depends(require_shift_access(Role.manager)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Cancel an expense by appending a negated row (§6.9). Manager floor; admin if locked.

    Mirrors `collections.py::reverse_collection` exactly, including why it is deliberately
    not `writable=True` (a miscount usually surfaces during reconciliation, which happens
    after close) and why a locked shift is admin-only rather than refused (a reversal
    modifies nothing referencing the locked shift, it only appends).
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
    endpoint = "POST /shifts/{shift_id}/expenses/{expense_id}/reversals"

    replay = idempotency.begin(
        db,
        key=key,
        endpoint=endpoint,
        user_id=actor.user.id,
        request_fingerprint=idempotency.fingerprint(
            path_params={"shift_id": shift.id, "expense_id": expense_id},
            body=jsonable_encoder(payload),
        ),
    )
    if replay is not None:
        return _replayed(replay)

    try:
        original = _load_expense(db, shift.id, expense_id)
        before = _audit_snapshot(original)

        reversal, replacement = expense_service.reverse(
            db,
            original=original,
            reason=payload.reason,  # already stripped by StringConstraints
            actor_id=actor.user.id,
            replacement_amount=payload.replacement_amount,
            replacement_paid_to=payload.replacement_paid_to,
            receipt_threshold=settings.EXPENSE_RECEIPT_THRESHOLD,
        )

        if replacement is not None:
            # A fresh live row, evaluated the same way a new expense is -- never after a
            # bare reversal, which can only subtract from a group's total.
            expense_service.apply_review_flags(
                db,
                shift=shift,
                expense=replacement,
                threshold=settings.EXPENSE_REVIEW_THRESHOLD,
            )

        audit.record(
            db,
            outlet_id=shift.outlet_id,
            table_name="expenses",
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
                table_name="expenses",
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

    category_code = _category_code(db, original.category_id)
    result = ReversalResponse(
        # All three rows share the original's category by construction -- a correction is
        # "the same expense, the right amount" (§6.9), so one lookup covers the lot.
        reversal=_to_response(reversal, category_code=category_code),
        replacement=(
            _to_response(replacement, category_code=category_code)
            if replacement is not None
            else None
        ),
        original=_to_response(original, category_code=category_code, is_reversed=True),
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


@router.patch(
    "/shifts/{shift_id}/expenses/{expense_id}/review", response_model=ExpenseResponse
)
def review_expense(
    expense_id: UUID,
    payload: ExpenseReview,
    access: ShiftAccess = Depends(require_shift_access(Role.manager)),
    db: Session = Depends(get_db),
) -> ExpenseResponse:
    """Sign off an expense flagged for review. Manager floor (§8).

    Mirrors `readings.py::review_reading` exactly: deliberately not `writable=True`
    (review is the one thing that must still work on a closed shift, since a pattern is
    usually noticed while reconciling, which happens after close), and refused outright on
    a locked shift, because §5.2 is absolute that nothing referencing a locked shift may
    be modified -- including the flag pointing at it.
    """
    shift = access.shift
    actor = access.actor
    if ShiftStatus(shift.status) is ShiftStatus.locked:
        raise AppError(
            status_code=409,
            code="SHIFT_LOCKED",
            detail=(
                "This shift is locked and nothing on it can be changed, including review "
                "flags."
            ),
        )

    expense = _load_expense(db, shift.id, expense_id)
    if not expense.requires_review:
        raise AppError(
            status_code=409,
            code="EXPENSE_NOT_FLAGGED",
            detail="This expense is not flagged for review.",
        )

    before = _audit_snapshot(expense)
    expense.requires_review = False
    expense.reviewed_by = actor.user.id
    expense.reviewed_at = datetime.now(tz=timezone.utc)
    note = payload.review_note
    expense.review_note = f"{expense.review_note}\n{note}" if expense.review_note else note

    audit.record(
        db,
        outlet_id=shift.outlet_id,
        table_name="expenses",
        record_id=expense.id,
        action=AuditAction.update,
        changed_by=actor.user.id,
        old_values=before,
        new_values=_audit_snapshot(expense) | {"review_note": note},
    )
    db.commit()
    db.refresh(expense)

    logger.info(
        "expense review cleared",
        extra={"expense_id": str(expense.id), "reviewed_by": str(actor.user.id)},
    )
    return _to_response(
        expense, category_code=_category_code(db, expense.category_id)
    )


@router.get("/expenses/flagged", response_model=FlaggedExpensePage)
def list_flagged_expenses(
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
) -> FlaggedExpensePage:
    """Every unreviewed flagged expense at this outlet, oldest first, cursor-paginated.

    A backlog across days is genuinely unbounded, unlike a single shift's handful of rows
    (`GET /shifts/{id}/expenses`), so this is the one expenses endpoint that pages rather
    than caps. Reuses `app/api/cursor.py`'s `encode_cursor` / `decode_cursor` on
    `(created_at, id)` -- the same pair `fuel_prices` and `fuel_margins` use on
    `effective_from`, since the sort key is a `(TIMESTAMPTZ, UUID)` tuple either way.

    Manager floor, not attendant: this is the review queue, and §8 gives review to
    managers and admins only. Retires a gap Phase 5 carried knowingly -- `nozzle_readings`
    has had a partial review index since Phase 5 with no endpoint ever running the query
    it was built for; `expenses` does not repeat that.
    """
    statement = (
        select(Expense)
        .join(Shift, Shift.id == Expense.shift_id)
        # Joined rather than looked up per row: this endpoint pages, so a per-row lookup
        # would be `limit` extra round trips on every scroll.
        .join(ExpenseCategory, ExpenseCategory.id == Expense.category_id)
        .where(Shift.outlet_id == actor.outlet_id, Expense.requires_review.is_(True))
        .order_by(Expense.created_at.desc(), Expense.id.desc())
    )
    if cursor is not None:
        last_created_at, last_id = decode_cursor(cursor)
        statement = statement.where(
            tuple_(Expense.created_at, Expense.id) < tuple_(last_created_at, last_id)
        )

    rows_with_shift = db.execute(
        statement.add_columns(Shift.business_date, ExpenseCategory.code).limit(limit + 1)
    ).all()
    has_more = len(rows_with_shift) > limit
    page = rows_with_shift[:limit]

    return FlaggedExpensePage(
        items=[
            FlaggedExpenseResponse(
                id=expense.id,
                shift_id=expense.shift_id,
                business_date=business_date.isoformat(),
                category_id=expense.category_id,
                category_code=category_code,
                amount=expense.amount,
                description=expense.description,
            )
            for expense, business_date, category_code in page
        ],
        next_cursor=(
            encode_cursor(page[-1][0].created_at, page[-1][0].id)
            if has_more and page
            else None
        ),
    )


# A year is the natural unit for a report titled "month-end" -- twelve calls' worth of
# range in one request. Unbounded would let one query scan the whole table as the business
# ages; this makes the bound structural rather than assumed, the same posture _MAX_ROWS
# takes above.
_MAX_SUMMARY_RANGE_DAYS = 366


@router.get("/expenses/summary", response_model=ExpenseSummaryResponse)
def get_expense_summary(
    date_from: date = Query(alias="from"),
    date_to: date = Query(alias="to"),
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
) -> ExpenseSummaryResponse:
    """§11's month-end category-wise expense summary. Manager floor (§8): this reports
    across every shift at the outlet, not one attendant's own shift, the same reasoning
    that puts `GET /expenses/flagged` at the manager floor too.
    """
    if date_from > date_to:
        raise AppError(
            status_code=422,
            code="INVALID_DATE_RANGE",
            detail="`from` must not be after `to`.",
        )
    if (date_to - date_from).days > _MAX_SUMMARY_RANGE_DAYS:
        raise AppError(
            status_code=422,
            code="INVALID_DATE_RANGE",
            detail=f"The range cannot exceed {_MAX_SUMMARY_RANGE_DAYS} days.",
        )

    totals = expense_service.totals_by_category_range(
        db, outlet_id=actor.outlet_id, date_from=date_from, date_to=date_to
    )
    total = sum(totals.values(), Decimal("0.00"))

    return ExpenseSummaryResponse(
        from_=date_from.isoformat(),
        to=date_to.isoformat(),
        totals_by_category=totals,
        total=total,
    )
