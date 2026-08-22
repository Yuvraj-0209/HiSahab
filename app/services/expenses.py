"""Expenses: the two §6.7 flagging rules, §6.9's reversal, and the §6.8-shaped lock check.

The database-aware half of Phase 7, mirroring `app/services/collections.py`. There is no
pure arithmetic counterpart because there is nothing to isolate: an expense is a figure a
human types against a category, not a figure derived from three others.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.expenses import ExpenseMode
from app.models.expense import Expense
from app.models.expense_category import ExpenseCategory
from app.models.shift import Shift

logger = logging.getLogger(__name__)


def _is_reversed() -> object:
    """SQL predicate: some other row points its `reverses_id` at this one.

    Computed rather than stored, for the same reason as `collections._is_reversed` -- a
    `reversed_at` column would be an UPDATE on a financial row in a closed shift, and a
    second copy of a fact the `reverses_id` FK already records.
    """
    reversal = Expense.__table__.alias("reversal")
    return exists().where(reversal.c.reverses_id == Expense.id)


def evaluate_receipt_required(
    *, category_requires_receipt: bool, amount: Decimal, threshold: Decimal
) -> bool:
    """§6.11's rule: `category.requires_receipt OR amount > threshold`.

    Computed here and only here, so `create_expense`, `update_expense` and `reverse`'s
    replacement branch cannot drift on the comparison. Strictly `>`, matching §6.7's
    threshold: exactly at the threshold does not require a receipt, one paisa over does.

    The caller decides what "the category" and "the amount" mean at the moment of the
    call -- at insert, the category and amount just typed in; on an amount-changing
    `PATCH`, the current category and the new amount (see `update_expense`'s docstring for
    why re-evaluation reads the category live there and that is not a contradiction of
    "never recomputed" -- that rule protects a row nobody is touching). The result is
    always snapshotted onto `expenses.receipt_required` by the caller, never left for a
    future read to recompute.
    """
    return category_requires_receipt or amount > threshold


def live_expense_for_attachment(db: Session, *, attachment_id: UUID) -> UUID | None:
    """Is a *live* expense already claiming this attachment? Powers §5.3's
    one-attachment-one-live-row rule from the attachments side, called by
    `app/services/attachments.py::link`. Mirrors `collections.live_collection_for_mode`'s
    shape and this module's own `_is_reversed` for what "live" means here: not itself a
    reversal, and not referenced by one.

    That definition is what lets a reversal's replacement inherit the original's
    `attachment_id` (§6.9's correction, D3 in the Phase 8 plan): once the reversal exists,
    the original is no longer live, so this returns `None` for it and the replacement's
    own claim is free to proceed.
    """
    return db.execute(
        select(Expense.id).where(
            Expense.attachment_id == attachment_id,
            Expense.reverses_id.is_(None),
            ~_is_reversed(),
        )
    ).scalar_one_or_none()


def all_expenses(db: Session, *, shift_id: UUID) -> list[Expense]:
    """Every row on this shift, reversals included, oldest first.

    §6.9: "Both rows remain visible." Ordered by `created_at` then `id` for the same
    reason as `collections.all_collections` -- a reversal and its replacement are
    inserted in one request and must never appear to a reader in the wrong order.
    """
    return list(
        db.execute(
            select(Expense)
            .where(Expense.shift_id == shift_id)
            .order_by(Expense.created_at, Expense.id)
        )
        .scalars()
        .all()
    )


def totals_by_category(db: Session, *, shift_id: UUID) -> dict[str, Decimal]:
    """Net spent per category on this shift, reversals included in the sum.

    Summed across every row rather than filtered to the live ones, so a reversal that has
    not yet been replaced shows as the reduction it is instead of vanishing -- same
    reasoning as `collections.totals_by_mode`.

    **Keyed by category `code`, not `category_id`.** Phase 8 turned the category into an FK
    (§5.1), and keying this by UUID would force every reader -- the API response, a report,
    a human reading a log line -- to carry a second lookup around just to say "maintenance".
    The code is unique per outlet and immutable, so it is a safe key and a legible one.
    """
    rows = db.execute(
        select(ExpenseCategory.code, Expense.amount)
        .join(ExpenseCategory, ExpenseCategory.id == Expense.category_id)
        .where(Expense.shift_id == shift_id)
    ).all()
    totals: dict[str, Decimal] = {}
    for code, amount in rows:
        totals[code] = totals.get(code, Decimal("0.00")) + amount
    return totals


def cash_expenses_total(db: Session, *, shift_id: UUID) -> Decimal:
    """§6.4's `cash_expenses` term for one shift -- **`mode = cash` rows, and only those.**

    This is the line Phase 7 added the `mode` column for. Before it, every expense was
    implicitly cash because there was nowhere to record otherwise, and a ₹40,000 electricity
    bill paid online read as a ₹40,000 hole in the drawer -- which §14 records this outlet
    would then book as udhaar against the salesman's own name. A `card` / `upi` /
    `bank_transfer` expense stays on the record for reporting and contributes nothing here.

    Summed over every row, reversals included, so a cancelled expense returns the money to
    the drawer rather than vanishing from the figure.

    Lives here rather than in `services/cash.py` for the reason `cash_repayments_total` lives
    in `services/credit.py`: a term of §6.4 belongs beside the rows it sums and next to the
    enum that decides which of them count.
    """
    return db.execute(
        select(func.coalesce(func.sum(Expense.amount), Decimal("0.00"))).where(
            Expense.shift_id == shift_id,
            Expense.mode == ExpenseMode.cash.value,
        )
    ).scalar_one()


def category_codes(db: Session, expenses: Sequence[Expense]) -> dict[UUID, str]:
    """Look up the human-readable codes for a batch of expenses in one query.

    `app/models/` uses column-level foreign keys and no `relationship()` anywhere, so there
    is no `expense.category.code` to reach for -- and that is a feature here, because the
    lazy-loaded version would issue one SELECT per row wherever a list is rendered.

    Lives in the service rather than in `app/api/v1/expenses.py` because two routers need
    it: the expenses endpoints, and `shifts.py`'s `UNREVIEWED_EXPENSES_EXIST` message, which
    names the flagged rows by category so a manager knows what they are being asked to look
    at. Phase 8 Step 2 found the second caller by breaking it.
    """
    ids = {expense.category_id for expense in expenses}
    if not ids:
        return {}
    return {
        row[0]: row[1]
        for row in db.execute(
            select(ExpenseCategory.id, ExpenseCategory.code).where(
                ExpenseCategory.id.in_(ids)
            )
        )
    }


def resolve_category(
    db: Session, *, category_id: UUID, outlet_id: UUID
) -> ExpenseCategory:
    """Load the category an expense is being filed under, refusing the two bad cases.

    Phase 8. Before this, `category` was an enum and the database did the checking for
    free -- an invalid value could not be expressed. An FK is weaker in one specific way:
    `expenses.category_id` guarantees the row *exists*, not that it belongs to this outlet
    or that it is still in use. Both are now the application's job.

    **Cross-outlet is a 404, not a 403.** A caller who is allowed to create expenses at
    their own outlet learns nothing about whether some id exists elsewhere -- the same
    posture §7.3 takes for attachments. Leaking "that id is real, just not yours" across
    tenants is a small hole that costs nothing to close now and cannot be closed later
    without changing a response code clients depend on.

    **Inactive is a 409, not a 404.** The row plainly exists and the caller can see it in
    `GET /expense-categories?include_inactive=true`; the conflict is with the world, not
    the payload. Retiring a category has to stop *new* expenses while leaving every
    historical row readable and reportable (§5.1) -- so this refuses here, at write time,
    and nothing anywhere filters old rows out.
    """
    category = db.get(ExpenseCategory, category_id)

    if category is None or category.outlet_id != outlet_id:
        raise AppError(
            status_code=404,
            code="CATEGORY_NOT_FOUND",
            detail="No expense category with that id at this outlet.",
        )

    if not category.is_active:
        raise AppError(
            status_code=409,
            code="CATEGORY_INACTIVE",
            detail=(
                f"The expense category {category.code} has been retired and cannot take "
                "new expenses. Existing expenses filed under it are unaffected."
            ),
        )

    return category


def totals_by_category_range(
    db: Session, *, outlet_id: UUID, date_from: date, date_to: date
) -> dict[str, Decimal]:
    """The month-end summary's arithmetic (§11): per-category totals across a date range,
    every shift at the outlet, reversals netted in -- the cross-shift, date-ranged widening
    of `totals_by_category`, which stays shift-scoped for the day-to-day expenses page.

    Summed across every row rather than filtered to the live ones, for the identical reason
    `totals_by_category` gives: a reversal that has not yet been replaced must show as the
    reduction it is, not vanish from the report.
    """
    rows = db.execute(
        select(ExpenseCategory.code, Expense.amount)
        .join(ExpenseCategory, ExpenseCategory.id == Expense.category_id)
        .join(Shift, Shift.id == Expense.shift_id)
        .where(
            Shift.outlet_id == outlet_id,
            Shift.business_date >= date_from,
            Shift.business_date <= date_to,
        )
    ).all()
    totals: dict[str, Decimal] = {}
    for code, amount in rows:
        totals[code] = totals.get(code, Decimal("0.00")) + amount
    return totals


def reversal_of(db: Session, *, expense_id: UUID) -> UUID | None:
    """The id of the row that cancels this one, if any.

    Extracted so `reverse` and the `PATCH` route ask the question the same way -- Phase 7
    Step 0 found `collections` letting these two answers drift apart, which let a
    cancelled original stay editable. `expenses` does not get to make that mistake once.
    """
    return db.execute(
        select(Expense.id).where(Expense.reverses_id == expense_id)
    ).scalar_one_or_none()


def apply_review_flags(
    db: Session, *, shift: Shift, expense: Expense, threshold: Decimal
) -> None:
    """§6.7's two flagging rules, run once `expense`'s amount is final.

    **Rule 1** -- a single row over the threshold -- is checked against `expense` alone.

    **Rule 2** -- the category's daily total -- is checked against every *live* row
    sharing `expense`'s outlet, `business_date` and category, joined across shifts,
    because §6.7 says "the sum of a single category for one `business_date`", not "for one
    shift". **Every live row in that group is flagged, not only the row that crossed the
    line** (CLAUDE.md §6.7's Phase 7 amendment): flagging only the row that tipped the
    balance shows a manager a trivial-looking ₹500 entry and hides the ₹1,300 pattern the
    rule exists to surface.

    Flagging can reach a row belonging to an *already-locked* shift elsewhere on the same
    business date. That is deliberate and has a direct precedent: `readings.
    flag_downstream_reading` mutates a review flag on any downstream shift with no
    locked-shift guard at all, because a review flag is a signal that something needs a
    human's attention, not a change to financial substance -- §5.2's "nothing referencing
    a locked shift may be modified" governs the money, not the note pointing at it.
    `review_expense`, which clears a flag, refuses on a locked shift; this function, which
    only ever sets one, does not need to.

    **Requires `expense` to already be flushed** so this function's own query can see it;
    the caller flushes before calling.

    Called after INSERT and after a PATCH that changes `amount`, and after a reversal's
    replacement (a fresh live row, evaluated the same way a new expense is). **Never**
    after a bare reversal: a reversal only ever subtracts a positive amount from the
    group's total, so it can never newly cross a threshold, and flags are never
    auto-cleared regardless (§6.7) -- so there is nothing for a bare reversal to trigger.
    """
    if expense.amount > threshold:
        expense.requires_review = True

    group = list(
        db.execute(
            select(Expense)
            .join(Shift, Shift.id == Expense.shift_id)
            .where(
                Shift.outlet_id == shift.outlet_id,
                Shift.business_date == shift.business_date,
                Expense.category_id == expense.category_id,
                Expense.reverses_id.is_(None),
                ~_is_reversed(),
            )
        )
        .scalars()
        .all()
    )
    total = sum((row.amount for row in group), Decimal("0.00"))
    if total > threshold:
        for row in group:
            row.requires_review = True


def unreviewed_flagged_expenses(db: Session, *, shift: Shift) -> list[Expense]:
    """§6.7's lock precondition predicate: this shift's own rows still needing a look.

    Scoped to `shift.id`, not the wider business date -- locking is a per-shift action
    (`PATCH /shifts/{id}/lock`), and a row flagged on a *different* shift by the aggregate
    rule above blocks that other shift's lock, not this one's. Returns rows, not a count,
    so the 409 can name them the way `missing_closing_readings` names nozzle labels.

    **Excludes a flagged row that has since been reversed** (§6.7's Phase 7 amendment): it
    is money a manager formally cancelled, both rows stay in the audit trail, and blocking
    a lock on cancelled money is friction with no control value. A bare reversal never
    clears the original's `requires_review` flag -- flags are never auto-cleared -- so
    without this exclusion a reversed expense would still block the shift forever, since
    `review_expense` has nothing left to sign off that changes the drawer.
    """
    return list(
        db.execute(
            select(Expense)
            .where(
                Expense.shift_id == shift.id,
                Expense.requires_review.is_(True),
                ~_is_reversed(),
            )
            .order_by(Expense.created_at, Expense.id)
        )
        .scalars()
        .all()
    )


def reverse(
    db: Session,
    *,
    original: Expense,
    reason: str,
    actor_id: UUID,
    replacement_amount: Decimal | None = None,
    replacement_paid_to: str | None = None,
    receipt_threshold: Decimal,
) -> tuple[Expense, Expense | None]:
    """Cancel an expense by appending, never by editing (§6.9).

    Mirrors `collections.reverse` exactly, including why `replacement_amount` is applied
    in the same transaction: without it, a correction on a *closed* shift is impossible,
    because the reversal lands and the follow-up POST is then refused by `writable=True`.

    The replacement carries the **original's** category, mode and description -- a
    correction is "the same expense, the right amount", not a new expense, and the reason
    for the change lives on the reversal row where §6.9 already puts it. Only the amount
    and, optionally, `paid_to` can differ.

    **The reversal never needs a receipt** (§6.11) -- it is a cancellation, not a spend,
    and there is nothing to photograph. Its `receipt_required` is left at the column's
    `false` default; nothing sets it.

    **The replacement inherits the original's `attachment_id` directly** (§5.3, D3) --
    not by calling `attachment_service.link()` again. By the time the replacement is
    built, the reversal above has already been flushed, so the original is no longer
    *live*; re-running the "already linked" check would find nothing blocking it and
    would only re-verify what D3 already guarantees by construction. `receipt_required`
    **is** re-evaluated against the replacement's own amount -- §6.11's PATCH rule applies
    here too, since a correction can push a small expense over the threshold. If it comes
    out `True` and the original had no attachment to inherit, `receipt_threshold` must be
    supplied and the correction is refused: there is no receipt to inherit and none was
    supplied, so silently creating a non-compliant row would defeat the rule the same way
    skipping the check on `PATCH` would.
    """
    if original.reverses_id is not None:
        raise AppError(
            status_code=409,
            code="CANNOT_REVERSE_A_REVERSAL",
            detail=(
                "This row is itself a reversal. To undo a reversal, record the correct "
                "figure as a new expense rather than negating the negation."
            ),
        )

    if reversal_of(db, expense_id=original.id) is not None:
        raise AppError(
            status_code=409,
            code="ALREADY_REVERSED",
            detail="This expense has already been reversed.",
        )

    reversal = Expense(
        shift_id=original.shift_id,
        category_id=original.category_id,
        mode=original.mode,
        amount=-original.amount,
        description=original.description,
        paid_to=original.paid_to,
        reverses_id=original.id,
        reversal_reason=reason,
        created_by=actor_id,
    )
    db.add(reversal)
    db.flush()

    replacement: Expense | None = None
    if replacement_amount is not None:
        category = db.get(ExpenseCategory, original.category_id)
        replacement_receipt_required = evaluate_receipt_required(
            category_requires_receipt=category.requires_receipt,
            amount=replacement_amount,
            threshold=receipt_threshold,
        )
        if replacement_receipt_required and original.attachment_id is None:
            raise AppError(
                status_code=422,
                code="EXPENSE_REQUIRES_RECEIPT",
                detail=(
                    "The corrected amount requires a receipt, and the original expense "
                    "has none to inherit. Upload a receipt first, then record the "
                    "correction."
                ),
            )

        replacement = Expense(
            shift_id=original.shift_id,
            category_id=original.category_id,
            mode=original.mode,
            amount=replacement_amount,
            description=original.description,
            paid_to=(
                replacement_paid_to
                if replacement_paid_to is not None
                else original.paid_to
            ),
            attachment_id=original.attachment_id,
            receipt_required=replacement_receipt_required,
            created_by=actor_id,
        )
        db.add(replacement)
        db.flush()

    logger.warning(
        "expense reversed",
        extra={
            "expense_id": str(original.id),
            "reversal_id": str(reversal.id),
            "shift_id": str(original.shift_id),
            "category_id": str(original.category_id),
            "amount": str(original.amount),
            "reason": reason,
            "replaced_with": str(replacement_amount) if replacement else None,
            "reversed_by": str(actor_id),
        },
    )
    return reversal, replacement
