"""Udhaar: outstanding balances, §6.6's credit limit, and §6.9's reversals.

The database-aware half of Phase 9, mirroring `app/services/expenses.py` and
`app/services/collections.py`: pure orchestration, `AppError` for every refusal, no FastAPI
import anywhere.

There is no pure-arithmetic counterpart module. §6.6's outstanding balance is a `SUM` minus a
`SUM` -- it is a query, not a formula, and extracting `a - b` into a testable function would
be ceremony around a subtraction. Contrast `app/services/sales.py`, which exists because
§6.2's rollover and testing-quantity rules are genuinely intricate and worth isolating from
the database entirely.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import case, exists, func, select
from sqlalchemy.orm import Session, aliased

from app.core.credit import CreditRepaymentMode
from app.core.errors import AppError
from app.core.shifts import ShiftStatus
from app.models.attachment import Attachment
from app.models.credit import (
    CreditCustomer,
    CreditOpeningBalance,
    CreditRepayment,
    CreditSale,
)
from app.models.fuel import FuelType
from app.models.shift import Shift

logger = logging.getLogger(__name__)


def _sale_is_reversed() -> object:
    """SQL predicate: some other credit sale points its `reverses_id` at this one.

    Computed rather than stored, for the same reason as `collections._is_reversed` and
    `expenses._is_reversed` -- a `reversed_at` column would be an UPDATE on a financial row in
    a closed shift, which is precisely what §6.9 forbids, and a second copy of a fact the
    `reverses_id` FK already records.
    """
    reversal = CreditSale.__table__.alias("sale_reversal")
    return exists().where(reversal.c.reverses_id == CreditSale.id)


def resolve_customer(
    db: Session, *, customer_id: UUID, outlet_id: UUID, for_sale: bool
) -> CreditCustomer:
    """Load a customer, refusing the two ways the id can be wrong at write time.

    Mirrors `expenses.resolve_category` exactly, including both response codes:

    **Cross-outlet is a 404, not a 403.** A caller allowed to issue udhaar at their own outlet
    learns nothing about whether some id exists elsewhere -- the posture §7.3 takes for
    attachments. Leaking "that id is real, just not yours" costs nothing to close now and
    cannot be closed later without changing a code clients depend on.

    **Inactive is a 409, not a 404.** The row plainly exists; the conflict is with the world,
    not the payload.

    **`for_sale` is the asymmetry §5.1 describes, and it is the whole reason this takes a
    flag.** A deactivated customer refuses a new *sale* but still accepts a *repayment*. You
    deactivate somebody precisely to stop the debt growing while they pay off what they owe;
    refusing their money would be backwards, and would strand a balance that nothing could
    ever clear.
    """
    customer = db.get(CreditCustomer, customer_id)

    if customer is None or customer.outlet_id != outlet_id:
        raise AppError(
            status_code=404,
            code="CREDIT_CUSTOMER_NOT_FOUND",
            detail="No credit customer with that id at this outlet.",
        )

    if for_sale and not customer.is_active:
        raise AppError(
            status_code=409,
            code="CREDIT_CUSTOMER_INACTIVE",
            detail=(
                f"{customer.name} has been deactivated and cannot take new udhaar. "
                "Repayments against their existing balance are still accepted."
            ),
        )

    return customer


def outstanding(db: Session, *, customer_id: UUID) -> Decimal:
    """§6.6, Phase 16:

        SUM(credit_opening_balances.amount)   <- what they already owed
      + SUM(credit_sales.amount)
      - SUM(credit_repayments.amount)

    **The opening balance is the term this was missing until Phase 16**, and its absence was
    not a rounding matter. §6.6 computes from rows and rightly forbids a stored total -- so
    the software's ledger began the day the software did, while this pump's began years
    earlier. A customer already owing 12,400 read as square, his first repayment drove the
    balance negative, and `check_credit_limit` below was measuring against a figure wrong by
    his entire history.

    **Every row, reversals included.** They carry negative amounts and net out on their own,
    which is the same convention `expenses.totals_by_category_range` follows and states: a
    reversal that has not yet been replaced must show as the reduction it is, not vanish.
    Filtering to live rows here would make a cancelled udhaar reappear as debt (§14).

    **Computed, never stored.** §6.6 forbids a denormalised running total because it drifts,
    and Phase 9 deleted `credit_sales.is_settled` for the same reason plus one of its own: a
    per-row settled flag has no honest value once a single repayment covers part of three
    bills.

    **The result may be negative**, and that is not an error -- a customer who pays in advance
    or rounds a payment up is owed money by the pump (§6.6).
    """
    opening = db.execute(
        select(
            func.coalesce(func.sum(CreditOpeningBalance.amount), Decimal("0.00"))
        ).where(CreditOpeningBalance.credit_customer_id == customer_id)
    ).scalar_one()
    sales = db.execute(
        select(func.coalesce(func.sum(CreditSale.amount), Decimal("0.00"))).where(
            CreditSale.credit_customer_id == customer_id
        )
    ).scalar_one()
    repaid = db.execute(
        select(func.coalesce(func.sum(CreditRepayment.amount), Decimal("0.00"))).where(
            CreditRepayment.credit_customer_id == customer_id
        )
    ).scalar_one()
    return opening + sales - repaid


def outstanding_by_customer(db: Session, *, outlet_id: UUID) -> dict[UUID, Decimal]:
    """The same figure for every customer at an outlet, in two queries rather than 2N.

    `outstanding` above is the honest single-customer answer and stays the definition; this
    is the report's version, and the two must agree. Written as two grouped aggregates joined
    in Python instead of one FULL OUTER JOIN because a customer can have sales and no
    repayments, repayments and no sales, or -- after a full reversal -- rows summing to zero,
    and a Python dict merge handles all three without a correlated subquery per row.

    Customers with no rows at all are included, at ₹0.00: "who owes me nothing" is a real
    answer to "show me the ledger", and omitting them would make a new customer invisible.

    **₹0.00 here does not mean somebody checked.** §6.8's distinction, and §14 forbids a
    screen collapsing the two: a customer with no opening balance is one nobody has entered
    yet. `live_opening_balance` is how a caller tells the difference.
    """
    balances: dict[UUID, Decimal] = {
        customer_id: Decimal("0.00")
        for customer_id in db.execute(
            select(CreditCustomer.id).where(CreditCustomer.outlet_id == outlet_id)
        ).scalars()
    }

    opening = db.execute(
        select(
            CreditOpeningBalance.credit_customer_id,
            func.sum(CreditOpeningBalance.amount),
        )
        .join(
            CreditCustomer,
            CreditCustomer.id == CreditOpeningBalance.credit_customer_id,
        )
        .where(CreditCustomer.outlet_id == outlet_id)
        .group_by(CreditOpeningBalance.credit_customer_id)
    ).all()
    for customer_id, total in opening:
        balances[customer_id] = balances.get(customer_id, Decimal("0.00")) + total

    sales = db.execute(
        select(CreditSale.credit_customer_id, func.sum(CreditSale.amount))
        .join(CreditCustomer, CreditCustomer.id == CreditSale.credit_customer_id)
        .where(CreditCustomer.outlet_id == outlet_id)
        .group_by(CreditSale.credit_customer_id)
    ).all()
    for customer_id, total in sales:
        balances[customer_id] = balances.get(customer_id, Decimal("0.00")) + total

    repayments = db.execute(
        select(CreditRepayment.credit_customer_id, func.sum(CreditRepayment.amount))
        .join(CreditCustomer, CreditCustomer.id == CreditRepayment.credit_customer_id)
        .where(CreditCustomer.outlet_id == outlet_id)
        .group_by(CreditRepayment.credit_customer_id)
    ).all()
    for customer_id, total in repayments:
        balances[customer_id] = balances.get(customer_id, Decimal("0.00")) - total

    return balances


def check_credit_limit(
    db: Session, *, customer: CreditCustomer, amount: Decimal
) -> None:
    """§6.6's limit. Raises 409 `CREDIT_LIMIT_EXCEEDED`, or returns having allowed the sale.

    ```
    if credit_limit is not None and outstanding + amount > credit_limit:  -> refuse
    ```

    **Strictly `>`**, matching §6.7's review threshold and §6.11's receipt threshold: landing
    exactly on the limit is allowed, one paisa over is not. Every threshold in this codebase
    compares the same way, deliberately, so nobody has to remember which is which.

    **`credit_limit IS NULL` means no limit** and is never coerced to zero -- that would
    refuse every sale to the customers who are trusted most (§14).

    **Not serialised** (§13.13). Two sales issued in the same instant both read the pre-sale
    balance and both pass, so a customer can end up marginally over. Deliberately not fixed
    with `SELECT ... FOR UPDATE`: this codebase holds no row locks anywhere, §13.12 means one
    outlet has one open shift and therefore effectively one writer, and the failure mode is a
    limit exceeded by one sale -- visible in the very next balance read -- not money lost or
    double-counted.
    """
    if customer.credit_limit is None:
        return

    projected = outstanding(db, customer_id=customer.id) + amount
    if projected > customer.credit_limit:
        raise AppError(
            status_code=409,
            code="CREDIT_LIMIT_EXCEEDED",
            detail=(
                f"This sale would take {customer.name} to ₹{projected} against a credit "
                f"limit of ₹{customer.credit_limit}. An admin can override this with a "
                "reason."
            ),
        )


def live_credit_sale_for_attachment(db: Session, *, attachment_id: UUID) -> UUID | None:
    """Is a *live* credit sale already claiming this attachment? §5.3's
    one-attachment-one-live-row rule, from the credit side.

    Called by `app/services/attachments.py::link`, alongside its expense counterpart --
    `expenses.live_expense_for_attachment`, whose shape this copies exactly, including what
    "live" means: not itself a reversal, and not referenced by one.

    That definition is what lets a `credit_sales` reversal carry the original's
    `attachment_id` (§5.2, §6.9). Once the reversal exists the original is no longer live, so
    this returns `None` for it -- which matters more here than it does for expenses, because
    `credit_sales.attachment_id` is `NOT NULL` and the reversal has nowhere else to get one.
    """
    return db.execute(
        select(CreditSale.id).where(
            CreditSale.attachment_id == attachment_id,
            CreditSale.reverses_id.is_(None),
            ~_sale_is_reversed(),
        )
    ).scalar_one_or_none()


def sale_reversal_of(db: Session, *, sale_id: UUID) -> UUID | None:
    """The id of the row that cancels this one, if any.

    Extracted so `reverse_sale` and the routes ask the question the same way -- Phase 7
    Step 0 found `collections` letting these two answers drift apart, which left a cancelled
    original still editable.
    """
    return db.execute(
        select(CreditSale.id).where(CreditSale.reverses_id == sale_id)
    ).scalar_one_or_none()


def repayment_reversal_of(db: Session, *, repayment_id: UUID) -> UUID | None:
    """`sale_reversal_of` for the other table."""
    return db.execute(
        select(CreditRepayment.id).where(CreditRepayment.reverses_id == repayment_id)
    ).scalar_one_or_none()


def reverse_sale(
    db: Session,
    *,
    original: CreditSale,
    reason: str,
    actor_id: UUID,
    replacement_amount: Decimal | None = None,
) -> tuple[CreditSale, CreditSale | None]:
    """Cancel a credit sale by appending, never by editing (§6.9).

    Mirrors `expenses.reverse`, including why `replacement_amount` is applied in the same
    transaction: without it a correction on a *closed* shift is impossible, because the
    reversal lands and the follow-up POST is then refused by `writable=True`.

    **Both the reversal and the replacement inherit the original's `attachment_id`** (§5.2).
    This is not the optional convenience it is for expenses -- `credit_sales.attachment_id` is
    `NOT NULL`, so inheritance is the only way a reversal row can exist at all without either
    weakening the column to a CHECK (which §14 forbids by name) or demanding a second
    photograph of a cancellation, which §6.11 refuses on principle.

    §5.3's rule still holds throughout: the original is reversed and so not live, the reversal
    is itself a reversal and so not live, and the replacement is the single live claimant.
    `attachment_service.link()` is deliberately not called again for either row.

    The replacement carries the original's customer, fuel type, quantity and vehicle -- a
    correction is "the same sale, the right amount", not a new sale, and the reason lives on
    the reversal row where §6.9 puts it.

    **`limit_override_reason` is deliberately not inherited.** It records that an admin
    overrode §6.6's limit for *that* decision, at that moment; a correction is a different
    decision. The replacement is not re-checked against the limit either -- see the route,
    which is where that policy belongs.
    """
    if original.reverses_id is not None:
        raise AppError(
            status_code=409,
            code="CANNOT_REVERSE_A_REVERSAL",
            detail=(
                "This row is itself a reversal. To undo a reversal, record the correct "
                "figure as a new credit sale rather than negating the negation."
            ),
        )

    if sale_reversal_of(db, sale_id=original.id) is not None:
        raise AppError(
            status_code=409,
            code="ALREADY_REVERSED",
            detail="This credit sale has already been reversed.",
        )

    reversal = CreditSale(
        shift_id=original.shift_id,
        credit_customer_id=original.credit_customer_id,
        fuel_type_id=original.fuel_type_id,
        # Negated with the amount: a reversal cancels the whole line, quantity included, so
        # that a per-fuel udhaar report nets to zero the same way the money does.
        quantity=-original.quantity if original.quantity is not None else None,
        amount=-original.amount,
        vehicle_number=original.vehicle_number,
        attachment_id=original.attachment_id,
        reverses_id=original.id,
        reversal_reason=reason,
        created_by=actor_id,
    )
    db.add(reversal)
    db.flush()

    replacement: CreditSale | None = None
    if replacement_amount is not None:
        replacement = CreditSale(
            shift_id=original.shift_id,
            credit_customer_id=original.credit_customer_id,
            fuel_type_id=original.fuel_type_id,
            quantity=original.quantity,
            amount=replacement_amount,
            vehicle_number=original.vehicle_number,
            attachment_id=original.attachment_id,
            created_by=actor_id,
        )
        db.add(replacement)
        db.flush()

    logger.warning(
        "credit sale reversed",
        extra={
            "credit_sale_id": str(original.id),
            "reversal_id": str(reversal.id),
            "shift_id": str(original.shift_id),
            "credit_customer_id": str(original.credit_customer_id),
            "amount": str(original.amount),
            "reason": reason,
            "replaced_with": str(replacement_amount) if replacement else None,
            "reversed_by": str(actor_id),
        },
    )
    return reversal, replacement


def reverse_repayment(
    db: Session,
    *,
    original: CreditRepayment,
    reason: str,
    actor_id: UUID,
    replacement_amount: Decimal | None = None,
) -> tuple[CreditRepayment, CreditRepayment | None]:
    """`reverse_sale` for the other table (§6.9).

    A repayment that never actually cleared -- a bounced cheque, a UPI reversal, a figure
    typed against the wrong customer -- has to be undoable, and §4.7 makes this the normal
    case rather than an edge one: the whole day is typed in after the fact, so the mistake is
    routinely found once the shift is already closed.

    The replacement inherits the original's mode and attachment. `attachment_id` is nullable
    here, unlike `credit_sales`', so inheritance is a convenience rather than a necessity --
    but it is the same convenience for the same reason (§5.3: nobody photographs one piece of
    paper twice).
    """
    if original.reverses_id is not None:
        raise AppError(
            status_code=409,
            code="CANNOT_REVERSE_A_REVERSAL",
            detail=(
                "This row is itself a reversal. To undo a reversal, record the correct "
                "figure as a new repayment rather than negating the negation."
            ),
        )

    if repayment_reversal_of(db, repayment_id=original.id) is not None:
        raise AppError(
            status_code=409,
            code="ALREADY_REVERSED",
            detail="This repayment has already been reversed.",
        )

    reversal = CreditRepayment(
        credit_customer_id=original.credit_customer_id,
        shift_id=original.shift_id,
        # Carried, not recomputed. A correction restates the amount, never when the money
        # arrived -- and moving the date would silently change which §6.4 shift, or which
        # side of an opening balance, the row belongs to (§5.2).
        business_date=original.business_date,
        amount=-original.amount,
        mode=original.mode,
        attachment_id=original.attachment_id,
        reverses_id=original.id,
        reversal_reason=reason,
        created_by=actor_id,
    )
    db.add(reversal)
    db.flush()

    replacement: CreditRepayment | None = None
    if replacement_amount is not None:
        replacement = CreditRepayment(
            credit_customer_id=original.credit_customer_id,
            shift_id=original.shift_id,
            business_date=original.business_date,
            amount=replacement_amount,
            mode=original.mode,
            attachment_id=original.attachment_id,
            created_by=actor_id,
        )
        db.add(replacement)
        db.flush()

    logger.warning(
        "credit repayment reversed",
        extra={
            "credit_repayment_id": str(original.id),
            "reversal_id": str(reversal.id),
            "shift_id": str(original.shift_id),
            "credit_customer_id": str(original.credit_customer_id),
            "amount": str(original.amount),
            "reason": reason,
            "replaced_with": str(replacement_amount) if replacement else None,
            "reversed_by": str(actor_id),
        },
    )
    return reversal, replacement


def all_sales(db: Session, *, shift_id: UUID) -> list[CreditSale]:
    """Every credit sale on this shift, reversals included, oldest first.

    §6.9: "Both rows remain visible." Ordered by `created_at` then `id` for the same reason as
    `expenses.all_expenses` -- a reversal and its replacement are inserted in one request and
    must never appear to a reader in the wrong order.
    """
    return list(
        db.execute(
            select(CreditSale)
            .where(CreditSale.shift_id == shift_id)
            .order_by(CreditSale.created_at, CreditSale.id)
        )
        .scalars()
        .all()
    )


def all_repayments(db: Session, *, shift_id: UUID) -> list[CreditRepayment]:
    """`all_sales` for the other table."""
    return list(
        db.execute(
            select(CreditRepayment)
            .where(CreditRepayment.shift_id == shift_id)
            .order_by(CreditRepayment.created_at, CreditRepayment.id)
        )
        .scalars()
        .all()
    )


def credit_sales_total(db: Session, *, shift_id: UUID) -> Decimal:
    """§6.4's `credit_sales_amount` term for one shift -- the number this whole phase exists
    to make computable.

    Phase 10 subtracts this from metered sales to derive expected cash. Every row is summed,
    reversals included, for the reason `outstanding` gives: a cancelled udhaar must reduce the
    figure, not disappear from it.

    Built here rather than in Phase 10 because it is one line and it belongs beside the rows
    it sums -- but nothing calls it until §6.4's equation exists. That is the same shape
    `attachments.orphans()` had between Phase 8's Steps 3 and 10.
    """
    return db.execute(
        select(func.coalesce(func.sum(CreditSale.amount), Decimal("0.00"))).where(
            CreditSale.shift_id == shift_id
        )
    ).scalar_one()


def _repayments_sum(
    db: Session,
    *,
    shift_id: UUID,
    modes: Collection[CreditRepaymentMode] | None = None,
) -> Decimal:
    """Sum this shift's repayments, optionally narrowed to a set of modes.

    Written once and parameterised, in the shape `pricing._effective_row_at` established for
    `rate_at` / `margin_at`: the two public callers below differ by one `WHERE` clause, and
    the parts that would drift apart in two copies -- the shift scoping, the `coalesce` that
    turns "no rows" into ₹0.00 rather than `None`, and the decision *not* to filter reversals
    -- are exactly the parts that matter.

    **Reversals are included**, for the reason `outstanding` gives: a cancelled repayment must
    show as the reduction it is rather than vanish. A reversal inherits the original's `mode`,
    so a reversed cash repayment nets out inside the filter rather than escaping it.

    **Shift-scoped, and that is now load-bearing rather than incidental.** Since Phase 16 a
    repayment may carry no shift at all -- money that reached a bank account rather than this
    pump (§5.2). Such a row matches no shift here, which is exactly right: §6.4 must never see
    it. The `WHERE shift_id = :shift_id` below is the whole implementation of that rule.

    Phase 16 widened `mode` to `modes` because there are now two mode-filtered callers and the
    second wants two labels. One membership test rather than two near-identical functions.
    """
    conditions = [CreditRepayment.shift_id == shift_id]
    if modes is not None:
        conditions.append(
            CreditRepayment.mode.in_([mode.value for mode in modes])
        )
    return db.execute(
        select(
            func.coalesce(func.sum(CreditRepayment.amount), Decimal("0.00"))
        ).where(*conditions)
    ).scalar_one()


def repayments_total(db: Session, *, shift_id: UUID) -> Decimal:
    """Every settlement received on this shift, all modes.

    Not a term of §6.4 -- it is the figure the repayments page shows. It exists as a service
    function rather than a `sum()` in the router because the router sums a *truncated* list:
    Phase 9 wrote it inline over `rows[:_MAX_ROWS]`, so a shift with more than 100 repayments
    under-reported. Aggregating in SQL over the whole shift is what every sibling router
    already does (`totals_by_category`, `totals_by_mode`, `credit_sales_total`).
    """
    return _repayments_sum(db, shift_id=shift_id)


def cash_repayments_total(db: Session, *, shift_id: UUID) -> Decimal:
    """§6.4's `cash_credit_repayments` term for one shift.

    **Only `mode = cash`.** A customer settling an old bill by bank transfer or card moves no
    money through the drawer, so adding it to expected cash would invent a shortfall on the
    very day they paid -- the argument `app/core/credit.py` makes beside the enum that defines
    the modes.

    Lives here rather than inline in `app/api/v1/credit_repayments.py`, where Phase 9 first
    wrote it, so §6.4's equation reads every term from one layer. A router is not where a term
    of the cash equation belongs, and Phase 10's engine must not import a router to find one.
    """
    return _repayments_sum(db, shift_id=shift_id, modes=(CreditRepaymentMode.cash,))


def card_upi_repayments_total(db: Session, *, shift_id: UUID) -> Decimal:
    """§6.4's `card_upi_credit_repayments` term for one shift. **Phase 16.**

    The bug this closes was live on real money: the owner confirms customers settle udhaar on
    the card machine. That settlement is inside `collections.mode = 'card'` -- the machine's
    whole-day total -- which §6.4 subtracts from metered sales to derive cash. Nothing put it
    back, because it is not a sale and `cash_repayments_total` above filters it out. So
    `accountable_cash` came out low by exactly the settlement, and since
    `gap = accountable - declared` the salesman read as holding a **surplus** nobody gave him.

    It therefore enters on the **sales** side, where §6.4 already puts a card-paid bottle of
    oil, and for the identical reason. Putting it on the cash side would be wrong by the same
    amount in the same direction -- the money never entered the drawer (§14).

    **This term depends on `collections.mode = 'card'` meaning the machine's total**, which is
    what §5.2 says it is: one lumped figure per mode, read off the terminal. If a salesman ever
    typed a card figure that already excluded settlements, this would double-count. That is a
    data-entry contract, not an arithmetic one, and it is stated here because nothing else
    would say it.

    `bank_transfer` is deliberately absent: it never touched a machine at this pump.
    """
    return _repayments_sum(
        db,
        shift_id=shift_id,
        modes=(CreditRepaymentMode.card, CreditRepaymentMode.upi),
    )


# --- opening balances (§5.2, §6.6 -- Phase 16) --------------------------------


def live_opening_balance(
    db: Session, *, customer_id: UUID
) -> CreditOpeningBalance | None:
    """The one opening balance currently standing for this customer, or `None`.

    **`None` means nobody has entered this customer's history**, which is a different fact
    from an entered ₹0.00 (§6.8, §14). `outstanding` above returns ₹0.00 for both, correctly
    -- the arithmetic is the same -- so a caller that needs to tell them apart asks here.

    *Live* means what it means for `collections` in §5.2: not itself a reversal, and not
    referenced by one. There is deliberately no unique constraint expressing this, because a
    reversed row stays in the table forever and its replacement would collide with it.
    """
    # A correlated NOT EXISTS over an alias of the same table -- "nothing points at me" --
    # rather than a self-join, so it reads the way the other liveness checks in this module do.
    inner = aliased(CreditOpeningBalance)
    return db.execute(
        select(CreditOpeningBalance)
        .where(
            CreditOpeningBalance.credit_customer_id == customer_id,
            CreditOpeningBalance.reverses_id.is_(None),
            ~exists().where(inner.reverses_id == CreditOpeningBalance.id),
        )
        .order_by(CreditOpeningBalance.created_at.desc())
    ).scalars().first()


def opening_balances_by_customer(
    db: Session, *, outlet_id: UUID
) -> dict[UUID, CreditOpeningBalance]:
    """Every customer's live opening balance at an outlet, in one query rather than N.

    The bulk form of `live_opening_balance`, and it keeps that function's meaning exactly: a
    customer absent from the returned mapping has no opening balance entered, and a customer
    present with `amount == 0` was checked and found square. Do not fill the gaps with zeros
    -- that is precisely the collapse §14 forbids.
    """
    inner = aliased(CreditOpeningBalance)
    rows = db.execute(
        select(CreditOpeningBalance)
        .join(
            CreditCustomer,
            CreditCustomer.id == CreditOpeningBalance.credit_customer_id,
        )
        .where(
            CreditCustomer.outlet_id == outlet_id,
            CreditOpeningBalance.reverses_id.is_(None),
            ~exists().where(inner.reverses_id == CreditOpeningBalance.id),
        )
    ).scalars().all()
    return {row.credit_customer_id: row for row in rows}


def earliest_entry_date(db: Session, *, customer_id: UUID) -> date | None:
    """The business date of this customer's oldest credit sale or repayment.

    Used only by `set_opening_balance` below, to refuse an opening balance that would
    double-count rows already recorded before it.
    """
    oldest_sale = db.execute(
        select(func.min(Shift.business_date))
        .join(CreditSale, CreditSale.shift_id == Shift.id)
        .where(CreditSale.credit_customer_id == customer_id)
    ).scalar_one()
    oldest_repayment = db.execute(
        select(func.min(CreditRepayment.business_date)).where(
            CreditRepayment.credit_customer_id == customer_id
        )
    ).scalar_one()

    candidates = [d for d in (oldest_sale, oldest_repayment) if d is not None]
    return min(candidates) if candidates else None


def refuse_entry_before_opening_balance(
    db: Session, *, customer_id: UUID, business_date: date
) -> None:
    """§5.2's double-count guard, applied when a sale or repayment is recorded.

    The opening figure already contains everything before `as_of_date`, so an entry dated
    earlier would be counted twice -- once inside the opening balance and once on its own.
    The guard exists in both directions; `set_opening_balance` below is the other one.
    """
    opening = live_opening_balance(db, customer_id=customer_id)
    if opening is None or business_date >= opening.as_of_date:
        return

    raise AppError(
        status_code=409,
        code="BEFORE_OPENING_BALANCE_DATE",
        detail=(
            f"This customer's ledger starts on {opening.as_of_date.isoformat()}, and "
            f"everything before that is already inside their opening balance. An entry "
            f"dated {business_date.isoformat()} would be counted twice."
        ),
    )


def set_opening_balance(
    db: Session,
    *,
    customer: CreditCustomer,
    as_of_date: date,
    amount: Decimal,
    actor_id: UUID,
) -> CreditOpeningBalance:
    """Anchor a customer's ledger to a real figure on a real date (§5.2, §6.6).

    Two refusals, and they are the same guard from opposite sides:

    * a live opening balance already exists -- 409 `OPENING_BALANCE_ALREADY_SET`. Correct it
      with a reversal and a reason, never by writing a second one. §5.2 explains at length why
      this is a service check rather than a unique constraint;
    * the customer already has a sale or repayment dated **before** `as_of_date` -- 409
      `ENTRIES_BEFORE_OPENING_BALANCE`. The opening figure contains that period already, so
      keeping both would double-count it.

    `amount` may be zero (§6.8: somebody checked and they were square) or negative (§6.6: a
    customer who paid in advance is owed money by the pump). Neither is an error, which is why
    this table carries no sign CHECK.

    The caller commits, as everywhere else -- so the row and its `audit_logs` entry land in
    one transaction or neither (§14).
    """
    if live_opening_balance(db, customer_id=customer.id) is not None:
        raise AppError(
            status_code=409,
            code="OPENING_BALANCE_ALREADY_SET",
            detail=(
                "This customer already has an opening balance. Reverse it with a reason "
                "and enter the corrected figure -- a money row is never edited in place."
            ),
        )

    earliest = earliest_entry_date(db, customer_id=customer.id)
    if earliest is not None and earliest < as_of_date:
        raise AppError(
            status_code=409,
            code="ENTRIES_BEFORE_OPENING_BALANCE",
            detail=(
                f"This customer already has entries from {earliest.isoformat()}, which is "
                f"before {as_of_date.isoformat()}. An opening balance on that date would "
                "count the same money twice."
            ),
        )

    balance = CreditOpeningBalance(
        credit_customer_id=customer.id,
        as_of_date=as_of_date,
        amount=amount,
        created_by=actor_id,
    )
    db.add(balance)
    db.flush()
    return balance


def opening_balance_reversal_of(db: Session, *, balance_id: UUID) -> UUID | None:
    """The id of the row that cancels this one, if any. Matches `sale_reversal_of` above."""
    return db.execute(
        select(CreditOpeningBalance.id).where(
            CreditOpeningBalance.reverses_id == balance_id
        )
    ).scalar_one_or_none()


def reverse_opening_balance(
    db: Session,
    *,
    original: CreditOpeningBalance,
    reason: str,
    actor_id: UUID,
    replacement_amount: Decimal | None = None,
) -> tuple[CreditOpeningBalance, CreditOpeningBalance | None]:
    """§6.9's correction path: cancel by appending, never by editing.

    Hand-rolled in the shape of `reverse_sale` above rather than delegating to
    `cash.append_reversal`, which is the newer shared implementation -- **`app/services/cash.py`
    imports this module**, so importing it back would be circular. The duplication is four
    lines and the alternative is an import cycle or a third module holding one function.

    `as_of_date` is carried onto both the reversal and any replacement: a correction restates
    *what* was owed on a date, never *which* date. Moving the date is a different act, and it
    would silently change which historical entries the double-count guard refuses.

    Note the reversal's amount is `-original.amount` with no sign assertion, because this
    table has no sign CHECK: §6.6 permits a negative opening balance, so a reversal here may
    legitimately be positive. `reverses_id` is what distinguishes them (§5.2).
    """
    if original.reverses_id is not None:
        raise AppError(
            status_code=409,
            code="CANNOT_REVERSE_A_REVERSAL",
            detail=(
                "This row is itself a reversal. To undo a reversal, record the correct "
                "figure as a new opening balance rather than negating the negation."
            ),
        )

    if opening_balance_reversal_of(db, balance_id=original.id) is not None:
        raise AppError(
            status_code=409,
            code="ALREADY_REVERSED",
            detail="This opening balance has already been reversed.",
        )

    reversal = CreditOpeningBalance(
        credit_customer_id=original.credit_customer_id,
        as_of_date=original.as_of_date,
        amount=-original.amount,
        reverses_id=original.id,
        reversal_reason=reason,
        created_by=actor_id,
    )
    db.add(reversal)
    db.flush()

    replacement = None
    if replacement_amount is not None:
        replacement = CreditOpeningBalance(
            credit_customer_id=original.credit_customer_id,
            as_of_date=original.as_of_date,
            amount=replacement_amount,
            created_by=actor_id,
        )
        db.add(replacement)
        db.flush()

    logger.warning(
        "credit opening balance reversed",
        extra={
            "opening_balance_id": str(original.id),
            "reversal_id": str(reversal.id),
            "customer_id": str(original.credit_customer_id),
            "amount": str(original.amount),
            "reason": reason,
            "replaced_with": str(replacement_amount) if replacement else None,
            "reversed_by": str(actor_id),
        },
    )
    return reversal, replacement


def sales_missing_receipt(db: Session, *, shift: Shift) -> list[UUID]:
    """§6.8's `CREDIT_SALE_MISSING_RECEIPT` close precondition.

    **This cannot fire through the API, and that is not the same as it being dead code.**
    `credit_sales.attachment_id` is `NOT NULL`, and every write path calls
    `attachment_service.link()`, which stamps `linked_at`. So a row created through this
    application always satisfies the check by construction.

    It still earns its place, for the reason `app/core/errors.py::_CONSTRAINT_ERRORS` gives
    about constraints the API refuses first: a row written *outside* the API -- a fixture, a
    data migration, a future bulk import of the paper register -- can carry an attachment that
    was never linked, and §6.8 names this precondition explicitly. The cost is one indexed
    query per close.

    Contrast the two `INSUFFICIENT_ROLE` branches Phase 8 deleted as dead: those could not be
    reached by *any* caller, through any path, because `attendant` is already the role floor.
    This one has a caller; it just is not the API.
    """
    # An attachment row must exist (the FK guarantees that) *and* have been linked.
    # `linked_at IS NULL` is what "uploaded but never claimed" means (§7.4), so a credit sale
    # pointing at one is a receipt nobody ever actually attached.
    return list(
        db.execute(
            select(CreditSale.id)
            .join(Attachment, Attachment.id == CreditSale.attachment_id)
            .where(CreditSale.shift_id == shift.id, Attachment.linked_at.is_(None))
            .order_by(CreditSale.created_at, CreditSale.id)
        )
        .scalars()
        .all()
    )


# --- the statement over a window (§6.6 -- Phase 21) ---------------------------

_ZERO = Decimal("0.00")

# The expanded view's line cap. Generous for a fortnight -- this outlet writes dozens of slips,
# not thousands -- and it bounds a 366-day window. **The totals never depend on the lines**:
# they are grouped SUMs, so a truncated list still sits under exact figures.
MAX_STATEMENT_LINES = 5000


@dataclass(frozen=True)
class StatementLine:
    """One row of a customer's account inside the statement's window.

    `period` is `"in_range"` for `[from, to]` and `"since"` for after `to`. An opening balance
    dated inside the window is tagged `in_range` for display but is **counted in
    `owed_before`**, not in `udhaar_in` -- §6.6: it is the debt from before this software
    existed, compressed onto one date.
    """

    id: UUID
    customer_id: UUID
    kind: str  # "opening" | "sale" | "repayment"
    period: str  # "in_range" | "since"
    business_date: date
    amount: Decimal
    is_reversal: bool
    is_reversed: bool
    reversal_reason: str | None
    shift_id: UUID | None
    # Sales only. `unit_of_measure` travels with `quantity` because a quantity without its
    # unit is meaningless -- this outlet sells CBG by the kilogram (§4.5).
    fuel_display_name: str | None
    quantity: Decimal | None
    unit_of_measure: str | None
    vehicle_number: str | None
    # Repayments only.
    mode: str | None
    bank_reference: str | None
    created_at: datetime


@dataclass(frozen=True)
class StatementRow:
    """One customer's six statement figures, plus the two that make them add up.

    `owes_today = billed + opening_since + udhaar_since - paid_since`, exactly -- the
    "since" figures are unbounded above rather than capped at today, and that is what makes
    the identity exact rather than approximately true: §6.1 refuses a future business date on
    every write path, so "after `to`" and "after `to`, up to today" are the same rows.
    """

    customer_id: UUID
    name: str
    phone: str
    is_active: bool
    opening_balance_entered: bool
    owed_before: Decimal
    udhaar_in: Decimal
    repaid_in: Decimal
    billed: Decimal
    # An opening balance dated after `to`: a customer whose ledger starts after the window.
    # Almost always zero, and only there so `owes_today` can be reconciled to the columns.
    opening_since: Decimal
    udhaar_since: Decimal
    paid_since: Decimal
    owes_today: Decimal


@dataclass(frozen=True)
class StatementTotals:
    owed_before: Decimal
    udhaar_in: Decimal
    repaid_in: Decimal
    billed: Decimal
    opening_since: Decimal
    udhaar_since: Decimal
    paid_since: Decimal
    owes_today: Decimal


@dataclass(frozen=True)
class PeriodStatement:
    date_from: date
    date_to: date
    rows: list[StatementRow]
    totals: StatementTotals
    lines: list[StatementLine]
    lines_truncated: bool
    # §6.6 counts udhaar on a still-open shift -- it is real udhaar and belongs on the bill --
    # but figures from an open shift can still change, so the screen says so.
    open_shift_count: int


def _bucket(column, predicate) -> object:
    """`SUM(CASE WHEN predicate THEN amount ELSE 0 END)`, coalesced to ₹0.00."""
    return func.coalesce(func.sum(case((predicate, column), else_=_ZERO)), _ZERO)


def period_statement(
    db: Session, *, outlet_id: UUID, date_from: date, date_to: date
) -> PeriodStatement:
    """§6.6's statement over `[date_from, date_to]`, for every customer at the outlet: the
    per-customer figures from `statement_rows`, plus the expanded view's lines and the count of
    shifts still open inside the window.
    """
    rows, totals = statement_rows(
        db, outlet_id=outlet_id, date_from=date_from, date_to=date_to
    )

    lines, truncated = _statement_lines(
        db, outlet_id=outlet_id, date_from=date_from, date_to=date_to
    )

    open_shift_count = db.execute(
        select(func.count())
        .select_from(Shift)
        .where(
            Shift.outlet_id == outlet_id,
            Shift.status == ShiftStatus.open.value,
            Shift.business_date >= date_from,
            Shift.business_date <= date_to,
        )
    ).scalar_one()

    return PeriodStatement(
        date_from=date_from,
        date_to=date_to,
        rows=rows,
        totals=totals,
        lines=lines,
        lines_truncated=truncated,
        open_shift_count=open_shift_count,
    )


def statement_rows(
    db: Session, *, outlet_id: UUID, date_from: date, date_to: date
) -> tuple[list[StatementRow], StatementTotals]:
    """Every customer's statement figures over `[date_from, date_to]`, and their totals.

    Split out of `period_statement` in Phase 26 so the Summary tab's udhaar bridge can ask the
    statement's own question rather than a copy of it. Before that, the Summary card summed two
    §6.4 *drawer* terms and so never saw a bank-transfer repayment -- the drift that a second
    implementation of one sum always produces eventually (§6.6's Phase 26 note).

    **Every row counts, reversals included**, which is `outstanding`'s convention: a reversal
    carries its original's shift and date (§6.9), so the pair nets inside one period. A sale is
    dated by its shift's `business_date` -- the same join the ledger uses, because a sale has
    no date of its own (§6.1) -- and never by `created_at`, since §4.7 says the day is typed in
    after the fact.

    Totals are three grouped aggregates, one per table, merged per customer in Python: the
    shape of `outstanding_by_customer` and for the same reason -- a customer may appear in any
    subset of the three. `owes_today` comes from `outstanding_by_customer` itself rather than
    from these buckets, so it is the definition and not a second implementation of it; the
    test suite asserts the two agree to the paisa.

    **`paid_since` is not allocated to any bill** (§13.40). Nothing here decides whether a
    particular bill was cleared.
    """
    first, last = date_from, date_to

    customers = db.execute(
        select(CreditCustomer).where(CreditCustomer.outlet_id == outlet_id)
    ).scalars().all()

    openings = {
        row.credit_customer_id: row
        for row in db.execute(
            select(
                CreditOpeningBalance.credit_customer_id,
                _bucket(
                    CreditOpeningBalance.amount, CreditOpeningBalance.as_of_date <= last
                ).label("before"),
                _bucket(
                    CreditOpeningBalance.amount, CreditOpeningBalance.as_of_date > last
                ).label("since"),
                func.count(case((CreditOpeningBalance.as_of_date >= first, 1))).label(
                    "active"
                ),
            )
            .join(
                CreditCustomer,
                CreditCustomer.id == CreditOpeningBalance.credit_customer_id,
            )
            .where(CreditCustomer.outlet_id == outlet_id)
            .group_by(CreditOpeningBalance.credit_customer_id)
        ).all()
    }

    sales = {
        row.credit_customer_id: row
        for row in db.execute(
            select(
                CreditSale.credit_customer_id,
                _bucket(CreditSale.amount, Shift.business_date < first).label("before"),
                _bucket(
                    CreditSale.amount,
                    (Shift.business_date >= first) & (Shift.business_date <= last),
                ).label("in_range"),
                _bucket(CreditSale.amount, Shift.business_date > last).label("since"),
                func.count(case((Shift.business_date >= first, 1))).label("active"),
            )
            .join(Shift, Shift.id == CreditSale.shift_id)
            # Scoped by the customer's outlet, not the shift's, exactly as
            # `outstanding_by_customer` is -- so `billed` and `owes_today` can never be
            # drawing on two different sets of rows.
            .join(CreditCustomer, CreditCustomer.id == CreditSale.credit_customer_id)
            .where(CreditCustomer.outlet_id == outlet_id)
            .group_by(CreditSale.credit_customer_id)
        ).all()
    }

    repayments = {
        row.credit_customer_id: row
        for row in db.execute(
            select(
                CreditRepayment.credit_customer_id,
                _bucket(
                    CreditRepayment.amount, CreditRepayment.business_date < first
                ).label("before"),
                _bucket(
                    CreditRepayment.amount,
                    (CreditRepayment.business_date >= first)
                    & (CreditRepayment.business_date <= last),
                ).label("in_range"),
                _bucket(
                    CreditRepayment.amount, CreditRepayment.business_date > last
                ).label("since"),
                func.count(case((CreditRepayment.business_date >= first, 1))).label(
                    "active"
                ),
            )
            .join(CreditCustomer, CreditCustomer.id == CreditRepayment.credit_customer_id)
            .where(CreditCustomer.outlet_id == outlet_id)
            .group_by(CreditRepayment.credit_customer_id)
        ).all()
    }

    today_balances = outstanding_by_customer(db, outlet_id=outlet_id)
    entered = opening_balances_by_customer(db, outlet_id=outlet_id)

    rows: list[StatementRow] = []
    for customer in customers:
        opening = openings.get(customer.id)
        sale = sales.get(customer.id)
        repayment = repayments.get(customer.id)

        owed_before = (
            (opening.before if opening else _ZERO)
            + (sale.before if sale else _ZERO)
            - (repayment.before if repayment else _ZERO)
        )
        udhaar_in = sale.in_range if sale else _ZERO
        repaid_in = repayment.in_range if repayment else _ZERO
        billed = owed_before + udhaar_in - repaid_in
        opening_since = opening.since if opening else _ZERO
        udhaar_since = sale.since if sale else _ZERO
        paid_since = repayment.since if repayment else _ZERO
        owes_today = today_balances.get(customer.id, _ZERO)

        has_activity = any(
            part is not None and part.active > 0 for part in (opening, sale, repayment)
        )
        has_balance = any(
            figure != _ZERO for figure in (owed_before, billed, paid_since, owes_today)
        )
        if not (has_activity or has_balance):
            continue

        rows.append(
            StatementRow(
                customer_id=customer.id,
                name=customer.name,
                phone=customer.phone,
                is_active=customer.is_active,
                opening_balance_entered=customer.id in entered,
                owed_before=owed_before,
                udhaar_in=udhaar_in,
                repaid_in=repaid_in,
                billed=billed,
                opening_since=opening_since,
                udhaar_since=udhaar_since,
                paid_since=paid_since,
                owes_today=owes_today,
            )
        )

    # Biggest bill first, as the hub sorts by biggest debt. Name breaks ties so the order is
    # stable between two loads of the same window.
    rows.sort(key=lambda row: (-row.billed, row.name.casefold(), str(row.customer_id)))

    totals = StatementTotals(
        owed_before=sum((row.owed_before for row in rows), _ZERO),
        udhaar_in=sum((row.udhaar_in for row in rows), _ZERO),
        repaid_in=sum((row.repaid_in for row in rows), _ZERO),
        billed=sum((row.billed for row in rows), _ZERO),
        opening_since=sum((row.opening_since for row in rows), _ZERO),
        udhaar_since=sum((row.udhaar_since for row in rows), _ZERO),
        paid_since=sum((row.paid_since for row in rows), _ZERO),
        owes_today=sum((row.owes_today for row in rows), _ZERO),
    )

    return rows, totals


def _statement_lines(
    db: Session, *, outlet_id: UUID, date_from: date, date_to: date
) -> tuple[list[StatementLine], bool]:
    """Every opening, sale and repayment dated on or after `date_from`, capped.

    Three queries rather than a `UNION ALL`, unlike the ledger: the three tables carry
    different detail columns (fuel and vehicle on a sale, mode and reference on a repayment),
    and a union would force every one of them into every branch as a typed NULL. The ledger's
    reason for a union -- ordering the newest N correctly across tables -- applies here only
    at the cap, and each query fetches one row past it so the merged cut is still exact.
    """
    cap = MAX_STATEMENT_LINES

    def _period(day: date) -> str:
        return "in_range" if day <= date_to else "since"

    opening_reversal = aliased(CreditOpeningBalance)
    opening_rows = db.execute(
        select(
            CreditOpeningBalance,
            exists()
            .where(opening_reversal.reverses_id == CreditOpeningBalance.id)
            .label("is_reversed"),
        )
        .join(CreditCustomer, CreditCustomer.id == CreditOpeningBalance.credit_customer_id)
        .where(
            CreditCustomer.outlet_id == outlet_id,
            CreditOpeningBalance.as_of_date >= date_from,
        )
        .limit(cap + 1)
    ).all()

    sale_rows = db.execute(
        select(
            CreditSale,
            Shift.business_date,
            FuelType.display_name,
            FuelType.unit_of_measure,
            _sale_is_reversed().label("is_reversed"),
        )
        .join(Shift, Shift.id == CreditSale.shift_id)
        .join(CreditCustomer, CreditCustomer.id == CreditSale.credit_customer_id)
        .outerjoin(FuelType, FuelType.id == CreditSale.fuel_type_id)
        .where(CreditCustomer.outlet_id == outlet_id, Shift.business_date >= date_from)
        .limit(cap + 1)
    ).all()

    repayment_reversal = aliased(CreditRepayment)
    repayment_rows = db.execute(
        select(
            CreditRepayment,
            exists()
            .where(repayment_reversal.reverses_id == CreditRepayment.id)
            .label("is_reversed"),
        )
        .join(CreditCustomer, CreditCustomer.id == CreditRepayment.credit_customer_id)
        .where(
            CreditCustomer.outlet_id == outlet_id,
            CreditRepayment.business_date >= date_from,
        )
        .limit(cap + 1)
    ).all()

    lines: list[StatementLine] = []
    for opening, is_reversed in opening_rows:
        lines.append(
            StatementLine(
                id=opening.id,
                customer_id=opening.credit_customer_id,
                kind="opening",
                period=_period(opening.as_of_date),
                business_date=opening.as_of_date,
                amount=opening.amount,
                is_reversal=opening.reverses_id is not None,
                is_reversed=bool(is_reversed),
                reversal_reason=opening.reversal_reason,
                shift_id=None,
                fuel_display_name=None,
                quantity=None,
                unit_of_measure=None,
                vehicle_number=None,
                mode=None,
                bank_reference=None,
                created_at=opening.created_at,
            )
        )
    for sale, business_date, fuel_name, unit, is_reversed in sale_rows:
        lines.append(
            StatementLine(
                id=sale.id,
                customer_id=sale.credit_customer_id,
                kind="sale",
                period=_period(business_date),
                business_date=business_date,
                amount=sale.amount,
                is_reversal=sale.reverses_id is not None,
                is_reversed=bool(is_reversed),
                reversal_reason=sale.reversal_reason,
                shift_id=sale.shift_id,
                fuel_display_name=fuel_name,
                quantity=sale.quantity,
                unit_of_measure=unit,
                vehicle_number=sale.vehicle_number,
                mode=None,
                bank_reference=None,
                created_at=sale.created_at,
            )
        )
    for repayment, is_reversed in repayment_rows:
        lines.append(
            StatementLine(
                id=repayment.id,
                customer_id=repayment.credit_customer_id,
                kind="repayment",
                period=_period(repayment.business_date),
                business_date=repayment.business_date,
                amount=repayment.amount,
                is_reversal=repayment.reverses_id is not None,
                is_reversed=bool(is_reversed),
                reversal_reason=repayment.reversal_reason,
                shift_id=repayment.shift_id,
                fuel_display_name=None,
                quantity=None,
                unit_of_measure=None,
                vehicle_number=None,
                mode=repayment.mode,
                bank_reference=repayment.bank_reference,
                created_at=repayment.created_at,
            )
        )

    # Oldest first within a customer: a bill reads top to bottom in date order. The opening
    # balance sorts first on its date, because it is the anchor the rest of the account is
    # measured from (the ledger's rule, reversed for an oldest-first list).
    rank = {"opening": 0, "sale": 1, "repayment": 1}
    lines.sort(
        key=lambda line: (
            str(line.customer_id),
            line.business_date,
            rank[line.kind],
            line.created_at,
            str(line.id),
        )
    )
    truncated = len(lines) > cap
    return lines[:cap], truncated
