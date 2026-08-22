"""The cash engine's database-aware half (CLAUDE.md §6.4, §6.5, §6.9).

Phase 10. Non-fuel sales and bank deposits land here first; §6.4's equation and §6.5's
rolling balance follow in later steps of this phase.

**There is no pure-arithmetic counterpart module.** `app/services/sales.py` exists because
§6.2 turns two meter readings into money and that deserves testing without a database.
§6.4's terms are all *sums of rows*, so isolating the addition would isolate the least
interesting part; what needs testing is which rows are summed, and that needs Postgres.

## One reversal implementation, not four

Phase 10 adds four tables carrying §6.9's shape. Phases 6 to 9 wrote `reverse` four times
already -- `collections`, `expenses`, `credit_sales`, `credit_repayments` -- and writing four
more would make eight near-identical copies of the rule that decides whether a correction is
recorded honestly.

That is the mistake migration 0012 made in miniature and paid for: a quantity CHECK written
four lines below an already sign-aware amount rule, and still not sign-aware itself, because
it was copied rather than shared. `append_reversal` below is written once.

The four older implementations are deliberately **not** retrofitted onto it. They are tested,
they work, and rewriting the correction path of every financial table in the codebase is not
what this phase is for. If they are ever unified, it should be its own change with its own
tests -- not a side effect of adding a table.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.core.collections import CollectionMode
from app.core.errors import AppError
from app.models.cash import BankDeposit, NonFuelSale
from app.models.shift import Shift
from app.models.shortfall import SalesmanShortfall, SalesmanShortfallSettlement
from app.services import (
    collections as collection_service,
    credit as credit_service,
    expenses as expense_service,
    readings as reading_service,
)

logger = logging.getLogger(__name__)

def reversal_of(db: Session, model: Any, *, row_id: UUID) -> UUID | None:
    """The id of the row that cancels this one, if any.

    Extracted so a route and a service ask the question the same way. Phase 7 Step 0 found
    them disagreeing on `collections`: `reverse` refused an already-reversed row while
    `PATCH` only refused a row that *was* a reversal, so a cancelled original stayed editable
    and its mode's net total could be driven negative.
    """
    return db.execute(
        select(model.id).where(model.reverses_id == row_id)
    ).scalar_one_or_none()


def append_reversal(
    db: Session,
    original: Any,
    *,
    reason: str,
    actor_id: UUID,
    carry: Sequence[str],
    replacement_amount: Decimal | None = None,
    replacement_values: dict[str, Any] | None = None,
    already_reversed_code: str,
    already_reversed_detail: str,
) -> tuple[Any, Any | None]:
    """Cancel a row by appending, never by editing (§6.9).

    The original is not touched. A new row carries the **negated** amount and points back at
    it, and both stay visible -- so "what did the system say on the day" survives the
    correction. This is how double-entry accounting has worked for 600 years and it is the
    only way to answer "who changed this, when, and what was it before".

    `carry` names the columns copied onto the reversal and the replacement. It is explicit
    rather than "every column except a few" because the exceptions are what matter: copying
    `id` or `created_at` would be wrong, and a blanket copy would silently start carrying any
    column a future migration adds -- including one that should not be inherited.

    `replacement_amount` is applied in the **same transaction** rather than left to a second
    request. Without that a correction on a *closed* shift is impossible: the reversal lands,
    and the follow-up POST is then refused by `writable=True`. It would also leave a window
    in which the shift's figure reads as zero.

    Two refusals, and they are different questions:

    * the row is *itself* a reversal -- negating a negation is not a correction, it is a
      second entry pretending to be one;
    * the row *has already been* reversed -- a concurrent double-reversal. The unique index
      `uq_<table>_reverses_id` is the backstop when two managers click at once, and its
      `_CONSTRAINT_ERRORS` entry turns that race into a 409 rather than an opaque 500.
    """
    model = type(original)

    if original.reverses_id is not None:
        raise AppError(
            status_code=409,
            code="CANNOT_REVERSE_A_REVERSAL",
            detail=(
                "This row is itself a reversal. To undo a reversal, record the correct "
                "figure as a new entry rather than negating the negation."
            ),
        )

    if reversal_of(db, model, row_id=original.id) is not None:
        raise AppError(
            status_code=409,
            code=already_reversed_code,
            detail=already_reversed_detail,
        )

    carried = {field: getattr(original, field) for field in carry}

    reversal = model(
        amount=-original.amount,
        reverses_id=original.id,
        reversal_reason=reason,
        created_by=actor_id,
        **carried,
    )
    db.add(reversal)
    db.flush()

    replacement = None
    if replacement_amount is not None:
        replacement = model(
            amount=replacement_amount,
            created_by=actor_id,
            **(carried | (replacement_values or {})),
        )
        db.add(replacement)
        db.flush()

    logger.warning(
        "financial row reversed",
        extra={
            "table": model.__tablename__,
            "row_id": str(original.id),
            "reversal_id": str(reversal.id),
            "amount": str(original.amount),
            "reason": reason,
            "replaced_with": str(replacement_amount) if replacement else None,
            "reversed_by": str(actor_id),
        },
    )
    return reversal, replacement


# --- non-fuel sales (§6.4) ----------------------------------------------------


def all_non_fuel_sales(db: Session, *, shift_id: UUID) -> list[NonFuelSale]:
    """Every row, reversals included, oldest first.

    §6.9: "Both rows remain visible." Ordered by `created_at` then `id`, because a reversal
    and its replacement are inserted in one transaction and PostgreSQL's `now()` is
    transaction-scoped -- they share a timestamp to the microsecond, and `reverses_id` rather
    than position is what says which row cancels which. The ordering only has to be *stable*.
    """
    return list(
        db.execute(
            select(NonFuelSale)
            .where(NonFuelSale.shift_id == shift_id)
            .order_by(NonFuelSale.created_at, NonFuelSale.id)
        )
        .scalars()
        .all()
    )


def non_fuel_sales_total(db: Session, *, shift_id: UUID) -> Decimal:
    """§6.4's non-fuel term for one shift.

    **This is added to `total_sales`, not to the cash side**, and the distinction is not
    cosmetic. A ₹500 bottle of oil paid by card is already inside the card collections total;
    adding it to the cash side as well would understate derived cash by exactly its amount,
    every time. On the sales side the arithmetic is correct however the customer paid, which
    is why this table has no `mode` column to filter on. §6.4 carries the worked example.

    Summed over every row, reversals included -- they carry negative amounts and net out, the
    convention `totals_by_category_range` states and `outstanding` follows.
    """
    return db.execute(
        select(func.coalesce(func.sum(NonFuelSale.amount), Decimal("0.00"))).where(
            NonFuelSale.shift_id == shift_id
        )
    ).scalar_one()


def reverse_non_fuel_sale(
    db: Session,
    *,
    original: NonFuelSale,
    reason: str,
    actor_id: UUID,
    replacement_amount: Decimal | None = None,
    replacement_description: str | None = None,
) -> tuple[NonFuelSale, NonFuelSale | None]:
    """§6.9 on `non_fuel_sales`. See `append_reversal` for the shape."""
    return append_reversal(
        db,
        original,
        reason=reason,
        actor_id=actor_id,
        carry=("shift_id", "description"),
        replacement_amount=replacement_amount,
        replacement_values=(
            {} if replacement_description is None else {"description": replacement_description}
        ),
        already_reversed_code="NON_FUEL_SALE_ALREADY_REVERSED",
        already_reversed_detail="This non-fuel sale has already been reversed.",
    )


# --- bank deposits (§6.4) -----------------------------------------------------


def _deposit_is_reversed() -> object:
    """SQL predicate: some other deposit points its `reverses_id` at this one.

    Computed rather than stored, for the reason `collections._is_reversed` gives: a
    `reversed_at` column would be an UPDATE on a financial row in a closed shift -- the thing
    §6.9 forbids -- and a second copy of a fact the FK already records.
    """
    reversal = BankDeposit.__table__.alias("deposit_reversal")
    return exists().where(reversal.c.reverses_id == BankDeposit.id)


def live_deposit_for_attachment(db: Session, *, attachment_id: UUID) -> UUID | None:
    """Is a *live* bank deposit already claiming this attachment? §5.3's
    one-attachment-one-live-row rule, from the deposit side.

    The third caller of that rule, after `expenses.live_expense_for_attachment` and
    `credit.live_credit_sale_for_attachment`, and it copies their shape exactly -- including
    what "live" means: **not itself a reversal, and not referenced by one.**

    A deposit slip is worth the same protection an expense receipt gets. One photograph of a
    ₹1,00,000 pay-in slip must not be able to justify two deposits, or §6.4 would subtract
    the money from the locker twice and the day would read ₹1,00,000 short with nothing
    pointing at why.
    """
    return db.execute(
        select(BankDeposit.id).where(
            BankDeposit.attachment_id == attachment_id,
            BankDeposit.reverses_id.is_(None),
            ~_deposit_is_reversed(),
        )
    ).scalar_one_or_none()


def all_bank_deposits(db: Session, *, shift_id: UUID) -> list[BankDeposit]:
    """Every row, reversals included, oldest first (§6.9: both rows stay visible)."""
    return list(
        db.execute(
            select(BankDeposit)
            .where(BankDeposit.shift_id == shift_id)
            .order_by(BankDeposit.created_at, BankDeposit.id)
        )
        .scalars()
        .all()
    )


def bank_deposits_total(db: Session, *, shift_id: UUID) -> Decimal:
    """§6.4's `bank_deposits` term for one shift -- money that left the locker for the bank.

    **Subtracted** from expected cash, which is the one thing to get right here: a deposit is
    not income, it is the locker emptying. Summed over every row, reversals included, so a
    cancelled deposit puts the money back rather than vanishing.
    """
    return db.execute(
        select(func.coalesce(func.sum(BankDeposit.amount), Decimal("0.00"))).where(
            BankDeposit.shift_id == shift_id
        )
    ).scalar_one()


def reverse_bank_deposit(
    db: Session,
    *,
    original: BankDeposit,
    reason: str,
    actor_id: UUID,
    replacement_amount: Decimal | None = None,
    replacement_reference: str | None = None,
) -> tuple[BankDeposit, BankDeposit | None]:
    """§6.9 on `bank_deposits`. See `append_reversal` for the shape.

    **`attachment_id` is carried onto the reversal and the replacement**, the inheritance
    §5.3 describes: the same deposit slip, the same piece of paper. `link()` is not called
    again for either, because by then the original is no longer live and re-checking would
    only re-verify what inheritance already guarantees. Exactly one live row holds the
    attachment throughout.

    `business_date` is carried too. It belongs to the shift, and a reversal happens on the
    same shift as the row it cancels -- so recomputing it would be a chance to get it wrong
    with no chance to get it more right.
    """
    return append_reversal(
        db,
        original,
        reason=reason,
        actor_id=actor_id,
        carry=("shift_id", "business_date", "bank_reference", "attachment_id"),
        replacement_amount=replacement_amount,
        replacement_values=(
            {}
            if replacement_reference is None
            else {"bank_reference": replacement_reference}
        ),
        already_reversed_code="DEPOSIT_ALREADY_REVERSED",
        already_reversed_detail="This deposit has already been reversed.",
    )


# --- shortfall settlements: §6.4's term (the ledger lives in shortfalls.py) ----


def cash_settlements_total(db: Session, *, shift_id: UUID) -> Decimal:
    """§6.4's `cash_shortfall_settlements` term for one shift.

    A salesman handing back what he owed. Every settlement is cash (§5.2 -- the owner's
    answer), so there is no mode to filter on and every row reaches the drawer.

    The **term** lives here, with §6.4's other terms; the **ledger** -- outstanding per
    salesman, the booking rules, the reversal path -- lives in `app/services/shortfalls.py`.
    That split is the same one `credit.py` and this module already have: `cash_repayments_
    total` sits beside the repayment rows, and the cash equation reads it from there.
    """
    return db.execute(
        select(
            func.coalesce(func.sum(SalesmanShortfallSettlement.amount), Decimal("0.00"))
        ).where(SalesmanShortfallSettlement.shift_id == shift_id)
    ).scalar_one()


def shortfalls_booked_total(db: Session, *, shift_id: UUID) -> Decimal:
    """§6.4's `shortfalls_booked` term for one shift -- **subtracted** from expected cash.

    What a salesman owes *instead of* holding. Without this term the same ₹500 is an asset
    twice: his debt, and cash the locker does not contain. See §6.4's worked example.

    Note this is the *booked* figure, not the computed gap. A gap nobody booked subtracts
    nothing and resurfaces at the next physical count as a variance with no name on it --
    which is the correct outcome of a manager choosing not to book, not a hole.
    """
    return db.execute(
        select(func.coalesce(func.sum(SalesmanShortfall.amount), Decimal("0.00"))).where(
            SalesmanShortfall.shift_id == shift_id
        )
    ).scalar_one()


# --- §6.4's per-shift cash position --------------------------------------------


@dataclass(frozen=True)
class CashPosition:
    """What one shift says about the cash a salesman should be holding (§6.4, §5.2).

    Two figures, and the whole design rests on keeping them apart:

    * **`accountable_cash`** -- derived from the meters and the other payment channels. What
      the system says he should have handed over.
    * **`declared_cash`** -- the `cash` collection row: what he says he counted into the
      locker. `None` when nobody has declared, which is a different answer from ₹0.00 and
      must never be coalesced with it (§6.8).

    `gap = accountable_cash - declared_cash`. **Positive is short, negative is a surplus.**

    §14 forbids summing the declared row into the derived figure, and nothing here does: the
    two are computed independently and only subtracted. That is the same shape §4.7 gives the
    meter chain -- the system predicts, a human confirms, both values are stored, and a
    disagreement leaves a trace instead of being absorbed into somebody's debt.

    **Nothing here is written anywhere.** A gap becomes a debt only when a manager books it
    (§5.2, §13.14). `gap` is `None` when nothing was declared, because there is no
    disagreement to measure yet -- not zero, which would read as "he counted exactly right".
    """

    shift_id: UUID
    business_date: date
    salesman_id: UUID
    metered_fuel_sales: Decimal
    non_fuel_sales: Decimal
    card_total: Decimal
    upi_total: Decimal
    wallet_total: Decimal
    credit_sales_total: Decimal
    cash_credit_repayments: Decimal
    cash_shortfall_settlements: Decimal
    cash_expenses: Decimal
    accountable_cash: Decimal
    declared_cash: Decimal | None
    gap: Decimal | None
    shortfalls_booked: Decimal
    # True when at least one in-scope nozzle has no quantity yet, so a reader can tell an
    # in-progress shift from one that genuinely sold nothing. Mirrors `ShiftSales.incomplete`.
    incomplete: bool


def shift_cash_position(db: Session, *, shift: Shift) -> CashPosition:
    """Assemble §6.4's per-shift figures for one shift.

        accountable_cash = metered_fuel_sales + non_fuel_sales
                         - card - upi - wallet
                         - credit_sales
                         + cash_credit_repayments
                         + cash_shortfall_settlements
                         - cash_expenses

    **Cash repayments and settlements are added; cash expenses are subtracted**, because all
    three physically pass through the salesman's hands during the shift and are therefore
    already inside the figure he declares. Leave any of them out and the comparison below
    manufactures a gap that nobody caused.

    **Priced with `price_only=True`** (§6.3). The cash question needs the rate, not the
    margin, and petrol and diesel margins have never been entered at this outlet (§14) -- so
    asking for profit here would refuse to reconcile every petrol day over a reference-data
    gap that has nothing to do with cash.

    A nozzle with no quantity yet contributes nothing and sets `incomplete`. It is **not**
    treated as zero: "not entered" and "sold nothing" are different facts, and a mid-entry
    shift must not report a gap the size of its own unentered readings.
    """
    lines = reading_service.shift_sales(db, shift=shift, price_only=True)
    metered = sum(
        (line.value for line in lines if line.value is not None), Decimal("0.00")
    )
    incomplete = any(line.quantity is None for line in lines)

    by_mode = collection_service.totals_by_mode(db, shift_id=shift.id)
    card = by_mode.get(CollectionMode.card.value, Decimal("0.00"))
    upi = by_mode.get(CollectionMode.upi.value, Decimal("0.00"))
    wallet = by_mode.get(CollectionMode.wallet.value, Decimal("0.00"))

    non_fuel = non_fuel_sales_total(db, shift_id=shift.id)
    credit_sales = credit_service.credit_sales_total(db, shift_id=shift.id)
    repayments = credit_service.cash_repayments_total(db, shift_id=shift.id)
    settlements = cash_settlements_total(db, shift_id=shift.id)
    expenses_paid = expense_service.cash_expenses_total(db, shift_id=shift.id)

    accountable = (
        metered
        + non_fuel
        - card
        - upi
        - wallet
        - credit_sales
        + repayments
        + settlements
        - expenses_paid
    )

    declared = collection_service.declared_cash(db, shift_id=shift.id)
    # None, not zero. Nobody has declared, so there is no disagreement to measure -- and a
    # zero here would read as "he counted exactly right", which is the opposite of the truth.
    gap = None if declared is None else accountable - declared

    return CashPosition(
        shift_id=shift.id,
        business_date=shift.business_date,
        # §5.2: the one person accountable for this shift's cash. Read from the shift, never
        # supplied, which is also where a booked shortfall gets its name from.
        salesman_id=shift.attendant_id,
        metered_fuel_sales=metered,
        non_fuel_sales=non_fuel,
        card_total=card,
        upi_total=upi,
        wallet_total=wallet,
        credit_sales_total=credit_sales,
        cash_credit_repayments=repayments,
        cash_shortfall_settlements=settlements,
        cash_expenses=expenses_paid,
        accountable_cash=accountable,
        declared_cash=declared,
        gap=gap,
        shortfalls_booked=shortfalls_booked_total(db, shift_id=shift.id),
        incomplete=incomplete,
    )
