"""The salesman shortfall ledger (CLAUDE.md §5.2, §6.4, §6.6, §13.14, §13.15, §14).

Phase 10. When a shift's drawer comes up short, this outlet books the difference as udhaar
against the salesman's own name (§14). This module is that ledger.

## What a shortfall is not

§14 forbids booking it as a `credit_sale`, and each reason stands on its own:

* it is the **outcome of a reconciliation**, not a sale -- nothing was dispensed;
* `credit_sales.attachment_id` is `NOT NULL` and there is no receipt for a shortfall, so
  satisfying that column would mean inventing evidence;
* it would mix staff debt into a real customer's outstanding balance, after which nobody can
  answer "what does this customer owe me" -- the figure §14 says the owner checks first.

So `salesman_id` is a foreign key to `user_profiles`, and `app/services/credit.py` never
learns this table exists.

## The arithmetic is `credit.py`'s, deliberately

    outstanding(salesman) = SUM(salesman_shortfalls.amount)
                          - SUM(salesman_shortfall_settlements.amount)

Over **every** row, reversals included -- they carry negative amounts and net out on their
own. **Computed, never stored.** §6.6's rule and the reasoning that deleted
`credit_sales.is_settled` transfer without modification, and §14's guardrail against a
denormalised running total covers this table too.

A settlement points at the **salesman**, not at a particular shortfall, for §5.2's reason:
one payment covering part of three debts has no honest per-row answer.

## Two figures nobody should confuse

* the **gap** (`app/services/cash.py::shift_cash_position`) -- what the arithmetic says;
* the **booked shortfall** -- what a human decided to hold the salesman to.

They are stored side by side on the row (`computed_gap` and `amount`) precisely so they can
differ and the difference stays legible. §4.7's predict-and-confirm shape, applied to a debt.

## What is missing, and it is missing on purpose

There is no write-off and no wage deduction. The owner's answer was that a shortfall is
repaid in cash, so `salesman_shortfall_settlements` has no `mode` column and every row
reaches §6.4's drawer. §13.15 records the cost: a ₹20 gap nobody will ever chase stays on a
salesman's balance permanently, and the balance only ever grows.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.shortfall import SalesmanShortfall, SalesmanShortfallSettlement
from app.services.cash import append_reversal

logger = logging.getLogger(__name__)


def outstanding(db: Session, *, salesman_id: UUID) -> Decimal:
    """§6.6's shape, applied to staff debt.

    **Every row, reversals included.** They carry negative amounts and net out, which is the
    convention `credit.outstanding` and `expenses.totals_by_category_range` both state:
    filtering to "live" rows would make a cancelled shortfall reappear as debt.

    **May legitimately be negative.** A salesman who overpays -- rounding ₹480 up to ₹500 --
    is owed money by the pump. Recorded here so it reads as a decision rather than an
    oversight the first time somebody sees a minus sign, exactly as §6.6 does for customers.
    """
    booked = db.execute(
        select(func.coalesce(func.sum(SalesmanShortfall.amount), Decimal("0.00"))).where(
            SalesmanShortfall.salesman_id == salesman_id
        )
    ).scalar_one()
    settled = db.execute(
        select(
            func.coalesce(func.sum(SalesmanShortfallSettlement.amount), Decimal("0.00"))
        ).where(SalesmanShortfallSettlement.salesman_id == salesman_id)
    ).scalar_one()
    return booked - settled


def outstanding_by_salesman(db: Session, *, outlet_id: UUID) -> dict[UUID, Decimal]:
    """The same figure for everyone who has ever been short at this outlet, in two queries
    rather than 2N.

    `outstanding` above stays the definition; this is the report's version, and the two must
    agree -- a test asserts it row by row, the way `credit.outstanding_by_customer`'s does,
    because two implementations of one rule is exactly the shape that drifts.

    **Scoped through the shift**, since these tables carry no `outlet_id` of their own (§5.0).
    Only salesmen with rows appear: unlike the customer report there is no "everyone at ₹0"
    baseline, because the population here is "staff who have been short", not "staff".
    """
    from app.models.shift import Shift

    balances: dict[UUID, Decimal] = {}

    booked = db.execute(
        select(SalesmanShortfall.salesman_id, func.sum(SalesmanShortfall.amount))
        .join(Shift, Shift.id == SalesmanShortfall.shift_id)
        .where(Shift.outlet_id == outlet_id)
        .group_by(SalesmanShortfall.salesman_id)
    ).all()
    for salesman_id, total in booked:
        balances[salesman_id] = balances.get(salesman_id, Decimal("0.00")) + total

    settled = db.execute(
        select(
            SalesmanShortfallSettlement.salesman_id,
            func.sum(SalesmanShortfallSettlement.amount),
        )
        .join(Shift, Shift.id == SalesmanShortfallSettlement.shift_id)
        .where(Shift.outlet_id == outlet_id)
        .group_by(SalesmanShortfallSettlement.salesman_id)
    ).all()
    for salesman_id, total in settled:
        balances[salesman_id] = balances.get(salesman_id, Decimal("0.00")) - total

    return balances


def all_shortfalls(db: Session, *, shift_id: UUID) -> list[SalesmanShortfall]:
    """Every shortfall booked against this shift, reversals included, oldest first."""
    return list(
        db.execute(
            select(SalesmanShortfall)
            .where(SalesmanShortfall.shift_id == shift_id)
            .order_by(SalesmanShortfall.created_at, SalesmanShortfall.id)
        )
        .scalars()
        .all()
    )


def all_settlements(db: Session, *, shift_id: UUID) -> list[SalesmanShortfallSettlement]:
    """Every settlement received on this shift, reversals included, oldest first."""
    return list(
        db.execute(
            select(SalesmanShortfallSettlement)
            .where(SalesmanShortfallSettlement.shift_id == shift_id)
            .order_by(
                SalesmanShortfallSettlement.created_at, SalesmanShortfallSettlement.id
            )
        )
        .scalars()
        .all()
    )


def warn_if_gap_differs(
    *, booked: Decimal, computed_gap: Decimal, salesman_id: UUID, shift_id: UUID
) -> None:
    """Log when a manager books something other than what the arithmetic said.

    **Warns, never refuses.** A manager may know part of a gap is a slip he has already
    corrected, and §6.8's reasoning applies directly: refusing a human's judgement sends the
    correction outside the system, where nothing can see it at all. The row keeps both
    figures, so the divergence is legible without being blocked.
    """
    if booked == computed_gap:
        return
    logger.warning(
        "shortfall booked for an amount other than the computed gap",
        extra={
            "salesman_id": str(salesman_id),
            "shift_id": str(shift_id),
            "booked": str(booked),
            "computed_gap": str(computed_gap),
        },
    )


def reverse_shortfall(
    db: Session,
    *,
    original: SalesmanShortfall,
    reason: str,
    actor_id: UUID,
    replacement_amount: Decimal | None = None,
) -> tuple[SalesmanShortfall, SalesmanShortfall | None]:
    """§6.9 on `salesman_shortfalls`.

    The correction path that matters most in this table: a shortfall booked on a mistyped
    reading is a debt against somebody who did nothing wrong, and §4.7 is written about
    exactly that. `computed_gap` and `reason` are carried onto the reversal so the record
    still says what was cancelled and why it was ever raised.

    A replacement carries the **original's** `computed_gap`, not a recomputed one. The gap is
    a fact about what the system said at booking time; recomputing it during a correction
    would quietly rewrite the system's own half of §4.7's predict-and-confirm pair.
    """
    return append_reversal(
        db,
        original,
        reason=reason,
        actor_id=actor_id,
        carry=("shift_id", "salesman_id", "computed_gap", "reason"),
        replacement_amount=replacement_amount,
        already_reversed_code="SHORTFALL_ALREADY_REVERSED",
        already_reversed_detail="This shortfall has already been reversed.",
    )


def reverse_settlement(
    db: Session,
    *,
    original: SalesmanShortfallSettlement,
    reason: str,
    actor_id: UUID,
    replacement_amount: Decimal | None = None,
) -> tuple[SalesmanShortfallSettlement, SalesmanShortfallSettlement | None]:
    """§6.9 on `salesman_shortfall_settlements`. A settlement that never arrived, or was
    counted twice -- the negative row puts the debt back and §6.4 stops adding the cash."""
    return append_reversal(
        db,
        original,
        reason=reason,
        actor_id=actor_id,
        carry=("shift_id", "salesman_id"),
        replacement_amount=replacement_amount,
        already_reversed_code="SETTLEMENT_ALREADY_REVERSED",
        already_reversed_detail="This settlement has already been reversed.",
    )
