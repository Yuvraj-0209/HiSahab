"""A shift's cash position -- what the meters say against what the salesman says
(CLAUDE.md §5.2, §6.4, §8, §13.14).

Phase 10. One route, and it **writes nothing**.

## The two figures, and why they are never merged

§5.2 is explicit that a `cash` collection row is *not* a term in §6.4's equation. Cash is
derived as the residual; the declared row is the **independent observation the derived figure
is checked against**:

* **accountable_cash** -- what the meters and the other payment channels say the salesman
  should have handed over.
* **declared_cash** -- what he says he counted into the locker.

The gap between them is the shortfall this outlet books as udhaar against his own name
(§14). §14 also forbids summing the two, and nothing here does: they are computed
independently and only ever subtracted.

## Why it writes nothing

§4.7's argument, which carries more weight here than anywhere else in the document because
the output has a person's name on it: *"an assumed opening converts theft into a debt owed by
someone who did nothing wrong."* A ₹500 gap is more often a mistyped reading, a forgotten UPI
figure or an unrecorded udhaar slip than it is theft. So this endpoint computes and shows;
a **manager** books, with a reason (§13.14, Step 7).

## Role floor

Manager (§8). Attendants may read their own shift's data-entry sheets, but this is a report
about them -- and the person a shortfall would be booked against is the last one who should
be able to run the calculation privately before anybody else sees it. `collections.py`'s
permission tests already make that argument for the declared figure.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import ShiftAccess, require_shift_access
from app.core.roles import Role
from app.db.session import get_db
from app.services import cash as cash_service

router = APIRouter(tags=["cash"])


class CashPositionResponse(BaseModel):
    """Every term of §6.4's per-shift half, plus the two figures that get compared.

    Returned in full rather than as a single number on purpose. §5.2's argument for storing
    `expected_closing`'s components applies to showing them too: a manager told "you are
    ₹500 short" and nothing else cannot check the claim, and this is the number that decides
    whether a debt lands on somebody's name.
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
    card_upi_credit_repayments: Decimal
    cash_shortfall_settlements: Decimal
    cash_expenses: Decimal

    accountable_cash: Decimal
    declared_cash: Decimal | None
    gap: Decimal | None
    shortfalls_booked: Decimal
    incomplete: bool

    gap_basis: str = (
        "gap = accountable_cash - declared_cash. Positive means short, negative means a "
        "surplus, and null means nobody has declared yet -- which is NOT zero. Nothing "
        "here is written: a gap becomes a debt only when a manager books it (CLAUDE.md "
        "§13.14)."
    )
    margin_basis: str = (
        "Priced with rate_at only. No profit figure is computed here -- CLAUDE.md §6.4 "
        "needs what the fuel was worth, not what it earned, and a missing dealer margin "
        "must not make a day unreconcilable (§6.3)."
    )


@router.get("/shifts/{shift_id}/cash-position", response_model=CashPositionResponse)
def read_cash_position(
    access: ShiftAccess = Depends(require_shift_access(Role.manager)),
    db: Session = Depends(get_db),
) -> CashPositionResponse:
    """§6.4's per-shift figures. Manager floor; nothing is written.

    Deliberately **not** `writable=True`. The gap is only knowable once the shift is closed
    and its readings are final, so refusing a closed shift would make the endpoint unreachable
    exactly when it matters. Same reasoning as Phase 5's review route and every reversal route
    since.
    """
    position = cash_service.shift_cash_position(db, shift=access.shift)
    return CashPositionResponse(**vars(position))
