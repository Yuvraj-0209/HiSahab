"""Expenses: the two §6.7 flagging rules, §6.9's reversal, and the §6.8-shaped lock check.

The database-aware half of Phase 7, mirroring `app/services/collections.py`. There is no
pure arithmetic counterpart because there is nothing to isolate: an expense is a figure a
human types against a category, not a figure derived from three others.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.expense import Expense
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
    """
    rows = db.execute(
        select(Expense.category, Expense.amount).where(Expense.shift_id == shift_id)
    ).all()
    totals: dict[str, Decimal] = {}
    for category, amount in rows:
        totals[category] = totals.get(category, Decimal("0.00")) + amount
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
                Expense.category == expense.category,
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
    """
    return list(
        db.execute(
            select(Expense)
            .where(Expense.shift_id == shift.id, Expense.requires_review.is_(True))
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
) -> tuple[Expense, Expense | None]:
    """Cancel an expense by appending, never by editing (§6.9).

    Mirrors `collections.reverse` exactly, including why `replacement_amount` is applied
    in the same transaction: without it, a correction on a *closed* shift is impossible,
    because the reversal lands and the follow-up POST is then refused by `writable=True`.

    The replacement carries the **original's** category, mode and description -- a
    correction is "the same expense, the right amount", not a new expense, and the reason
    for the change lives on the reversal row where §6.9 already puts it. Only the amount
    and, optionally, `paid_to` can differ.
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
        category=original.category,
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
        replacement = Expense(
            shift_id=original.shift_id,
            category=original.category,
            mode=original.mode,
            amount=replacement_amount,
            description=original.description,
            paid_to=(
                replacement_paid_to
                if replacement_paid_to is not None
                else original.paid_to
            ),
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
            "category": original.category,
            "amount": str(original.amount),
            "reason": reason,
            "replaced_with": str(replacement_amount) if replacement else None,
            "reversed_by": str(actor_id),
        },
    )
    return reversal, replacement
