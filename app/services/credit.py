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
from decimal import Decimal
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.core.credit import CreditRepaymentMode
from app.core.errors import AppError
from app.models.attachment import Attachment
from app.models.credit import CreditCustomer, CreditRepayment, CreditSale
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
    """§6.6: `SUM(credit_sales.amount) - SUM(credit_repayments.amount)`.

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
    return sales - repaid


def outstanding_by_customer(db: Session, *, outlet_id: UUID) -> dict[UUID, Decimal]:
    """The same figure for every customer at an outlet, in two queries rather than 2N.

    `outstanding` above is the honest single-customer answer and stays the definition; this
    is the report's version, and the two must agree. Written as two grouped aggregates joined
    in Python instead of one FULL OUTER JOIN because a customer can have sales and no
    repayments, repayments and no sales, or -- after a full reversal -- rows summing to zero,
    and a Python dict merge handles all three without a correlated subquery per row.

    Customers with no rows at all are included, at ₹0.00: "who owes me nothing" is a real
    answer to "show me the ledger", and omitting them would make a new customer invisible.
    """
    balances: dict[UUID, Decimal] = {
        customer_id: Decimal("0.00")
        for customer_id in db.execute(
            select(CreditCustomer.id).where(CreditCustomer.outlet_id == outlet_id)
        ).scalars()
    }

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
    db: Session, *, shift_id: UUID, mode: CreditRepaymentMode | None = None
) -> Decimal:
    """Sum this shift's repayments, optionally narrowed to one mode.

    Written once and parameterised, in the shape `pricing._effective_row_at` established for
    `rate_at` / `margin_at`: the two public callers below differ by one `WHERE` clause, and
    the parts that would drift apart in two copies -- the shift scoping, the `coalesce` that
    turns "no rows" into ₹0.00 rather than `None`, and the decision *not* to filter reversals
    -- are exactly the parts that matter.

    **Reversals are included**, for the reason `outstanding` gives: a cancelled repayment must
    show as the reduction it is rather than vanish. A reversal inherits the original's `mode`,
    so a reversed cash repayment nets out inside the filter rather than escaping it.
    """
    conditions = [CreditRepayment.shift_id == shift_id]
    if mode is not None:
        conditions.append(CreditRepayment.mode == mode.value)
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
    return _repayments_sum(db, shift_id=shift_id, mode=CreditRepaymentMode.cash)


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
