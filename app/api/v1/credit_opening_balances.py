"""What a customer already owed when this system started counting (§5.2, §6.6, §8).

**Phase 16.** §6.6 computes outstanding from rows and rightly forbids a stored running
total, because it drifts. The consequence nobody had written down until somebody sat down
to enter real data: the software's ledger begins the day the software does, and this pump's
began years earlier. A customer already owing ₹12,400 read as square, so his first repayment
drove the balance *negative* -- the pump appearing to owe him money -- and §6.6's credit
limit was being checked against a figure wrong by his entire history.

## Admin only, and the parallel is exact

§8 puts this at the admin floor for the reason §6.5 puts the cash locker's seeded opening
balance there: it is a figure **nothing else in the system can check**. Every other money row
here is corroborated by something -- a meter, a machine total, a receipt -- and this one is a
person's word about the past. The one who can write it should be the one who answers for it.

## One live row per customer, refused in the service rather than by a constraint

§5.2 works this through in full for `collections`, and every word transfers: a reversed row
stays in the table forever, so its replacement collides on any natural key, and every
partial-index variant fails identically because the replacement also carries
`reverses_id IS NULL`. A second live row is refused with 409 `OPENING_BALANCE_ALREADY_SET`
and the correction path is §6.9's reversal, like every other money row here.

## No `Idempotency-Key`, deliberately

§6.10 requires one on every POST that creates a money record *and could duplicate one*. This
one cannot: the one-live-row rule means a retried request is refused with 409 rather than
creating a second row. That is the same reasoning §6.10 gives for nozzle readings, whose
`UNIQUE (shift_id, nozzle_id)` makes them idempotent by construction, and the same reasoning
`POST /daily-summaries` relies on. The reversal endpoint *does* take a key, because a retried
reversal genuinely would create a second negative row.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints, condecimal
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Actor, require_role
from app.core import idempotency
from app.core.audit import AuditAction
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.roles import Role
from app.db.session import get_db
from app.models.credit import CreditCustomer, CreditOpeningBalance
from app.services import audit
from app.services import credit as credit_service
from app.services import shifts as shift_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["credit"])

# **No `gt=0`, and no `ge=0` either.** Every other money field in this codebase constrains the
# sign; this one must not. §6.6 states that an outstanding balance may legitimately be
# negative -- a customer who paid in advance or rounded a bill up is owed money by the pump --
# and ₹0.00 is a real answer meaning "checked, and square" (§6.8). The table carries no sign
# CHECK for the same reason, and this is the API half of that decision.
MoneyValue = condecimal(max_digits=12, decimal_places=2)

ReasonValue = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)
]


# --- schemas -----------------------------------------------------------------


class OpeningBalanceResponse(BaseModel):
    id: UUID
    credit_customer_id: UUID
    as_of_date: date
    amount: Decimal
    reverses_id: UUID | None
    reversal_reason: str | None
    is_reversed: bool
    created_at: str


class OpeningBalanceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credit_customer_id: UUID
    as_of_date: date
    amount: MoneyValue


class OpeningBalanceReversal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: ReasonValue
    replacement_amount: MoneyValue | None = None


class OpeningBalanceReversalResponse(BaseModel):
    reversal: OpeningBalanceResponse
    replacement: OpeningBalanceResponse | None
    original: OpeningBalanceResponse


class OpeningBalanceListItem(BaseModel):
    """One customer and the opening balance standing for them, if any.

    **`opening_balance` is `None` when nobody has entered one**, and that is not the same as
    ₹0.00. §6.8's distinction, and §14 forbids a client collapsing the two with `?? 0`: an
    absent figure means *we have not looked at this customer*, a zero one means *somebody
    checked and they were square*. The screen has to be able to say which.
    """

    credit_customer_id: UUID
    name: str
    is_active: bool
    opening_balance_id: UUID | None
    opening_balance: Decimal | None
    as_of_date: date | None
    outstanding: Decimal


class OpeningBalancePage(BaseModel):
    items: list[OpeningBalanceListItem]


# --- helpers -----------------------------------------------------------------


def _to_response(
    row: CreditOpeningBalance, *, is_reversed: bool = False
) -> OpeningBalanceResponse:
    return OpeningBalanceResponse(
        id=row.id,
        credit_customer_id=row.credit_customer_id,
        as_of_date=row.as_of_date,
        amount=row.amount,
        reverses_id=row.reverses_id,
        reversal_reason=row.reversal_reason,
        is_reversed=is_reversed,
        created_at=row.created_at.isoformat(),
    )


def _audit_snapshot(row: CreditOpeningBalance) -> dict[str, object]:
    return {
        "credit_customer_id": str(row.credit_customer_id),
        "as_of_date": row.as_of_date.isoformat(),
        # `Decimal`, encoded to a string by `services/audit.py`'s encoder -- never a float,
        # because an audit row has to reproduce the scale exactly (§14).
        "amount": row.amount,
    }


def _load(db: Session, *, balance_id: UUID, outlet_id: UUID) -> CreditOpeningBalance:
    """Fetch one row, scoped to the caller's outlet.

    The join to `credit_customers` is how tenancy is enforced: this table carries no
    `outlet_id` of its own, because §5.0's rule says a derivable one should wait.
    """
    row = db.execute(
        select(CreditOpeningBalance)
        .join(
            CreditCustomer,
            CreditCustomer.id == CreditOpeningBalance.credit_customer_id,
        )
        .where(
            CreditOpeningBalance.id == balance_id,
            CreditCustomer.outlet_id == outlet_id,
        )
    ).scalar_one_or_none()

    if row is None:
        raise AppError(
            status_code=404,
            code="OPENING_BALANCE_NOT_FOUND",
            detail="No opening balance with that id.",
        )
    return row


def _require_key(idempotency_key: str | None) -> str:
    if not idempotency_key:
        raise AppError(
            status_code=400,
            code="IDEMPOTENCY_KEY_REQUIRED",
            detail="This endpoint creates a money record and requires an Idempotency-Key.",
        )
    return idempotency_key


# --- routes ------------------------------------------------------------------


@router.get("/credit-opening-balances", response_model=OpeningBalancePage)
def list_opening_balances(
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
) -> Any:
    """Every customer at this outlet, with their opening balance where one was entered.

    Manager floor to read, admin to write -- §8's usual split, and it means a manager can see
    what the ledger rests on without being able to move it.

    Returns every customer, including those with no opening balance, because "who have we not
    entered yet" is the question this screen exists to answer while a ledger is being set up.
    An absent `opening_balance` is `None` and must stay `None` all the way to the screen.

    Not paginated, matching `GET /credit-customers`: a pump's customer list is bounded
    reference data rather than a history to page through (§9's documented exception).
    """
    customers = list(
        db.execute(
            select(CreditCustomer)
            .where(CreditCustomer.outlet_id == actor.outlet_id)
            .order_by(CreditCustomer.name)
        )
        .scalars()
        .all()
    )
    openings = credit_service.opening_balances_by_customer(
        db, outlet_id=actor.outlet_id
    )
    balances = credit_service.outstanding_by_customer(db, outlet_id=actor.outlet_id)

    return OpeningBalancePage(
        items=[
            OpeningBalanceListItem(
                credit_customer_id=customer.id,
                name=customer.name,
                is_active=customer.is_active,
                opening_balance_id=(
                    openings[customer.id].id if customer.id in openings else None
                ),
                opening_balance=(
                    openings[customer.id].amount if customer.id in openings else None
                ),
                as_of_date=(
                    openings[customer.id].as_of_date if customer.id in openings else None
                ),
                outstanding=balances.get(customer.id, Decimal("0.00")),
            )
            for customer in customers
        ]
    )


@router.post(
    "/credit-opening-balances", response_model=OpeningBalanceResponse, status_code=201
)
def create_opening_balance(
    payload: OpeningBalanceCreate,
    actor: Actor = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
) -> Any:
    """Anchor a customer's ledger to a real figure on a real date. Admin only (§8).

    Three refusals, and each is a different mistake:

    * the customer belongs to another outlet -- 404, from `resolve_customer`. The id arrives
      in the body rather than the path, so there is no outlet resolver to authorise against;
      the lookup is scoped to `actor.outlet_id` and simply does not find it;
    * a live opening balance already exists -- 409 `OPENING_BALANCE_ALREADY_SET`;
    * the customer already has entries dated before `as_of_date` -- 409
      `ENTRIES_BEFORE_OPENING_BALANCE`, because the opening figure contains that period
      already and keeping both would count the same money twice.

    `amount` may be zero or negative. Neither is an error -- see the note beside `MoneyValue`.
    """
    # `for_sale=False`: a deactivated customer still has a history worth recording, and
    # §5.1's asymmetry is about new *debt*, not about stating what is already owed.
    customer = credit_service.resolve_customer(
        db,
        customer_id=payload.credit_customer_id,
        outlet_id=actor.outlet_id,
        for_sale=False,
    )

    # §6.1, evaluated in the outlet's timezone: a balance "as of" a date that has not arrived
    # describes a future nobody can have observed.
    if payload.as_of_date > shift_service.outlet_today(get_settings().TZ_DISPLAY):
        raise AppError(
            status_code=422,
            code="BUSINESS_DATE_IN_FUTURE",
            detail=(
                "That date is in the future. An opening balance states what was owed on a "
                "day that has already happened."
            ),
        )

    balance = credit_service.set_opening_balance(
        db,
        customer=customer,
        as_of_date=payload.as_of_date,
        amount=payload.amount,
        actor_id=actor.user.id,
    )

    audit.record(
        db,
        outlet_id=actor.outlet_id,
        table_name="credit_opening_balances",
        record_id=balance.id,
        action=AuditAction.insert,
        changed_by=actor.user.id,
        new_values=_audit_snapshot(balance),
    )
    db.commit()
    db.refresh(balance)

    logger.info(
        "credit opening balance set",
        extra={
            "opening_balance_id": str(balance.id),
            "credit_customer_id": str(customer.id),
            "as_of_date": balance.as_of_date.isoformat(),
            "amount": str(balance.amount),
        },
    )
    return _to_response(balance)


@router.post(
    "/credit-opening-balances/{balance_id}/reversals",
    response_model=OpeningBalanceReversalResponse,
    status_code=201,
)
def reverse_opening_balance(
    balance_id: UUID,
    payload: OpeningBalanceReversal,
    actor: Actor = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Correct an opening balance by appending, never by editing (§6.9). Admin only.

    `replacement_amount` is applied in the same transaction rather than left to a second
    request, for the reason `cash.append_reversal` gives generally and one specific to this
    table: between the two requests the customer's outstanding would read as though their
    whole history had been forgiven, and that figure is what §6.6's credit limit is measured
    against.

    Unlike the create above, this **does** take an `Idempotency-Key`: a retried reversal
    genuinely would append a second negative row.
    """
    key = _require_key(idempotency_key)
    endpoint = "POST /credit-opening-balances/{balance_id}/reversals"

    replay = idempotency.begin(
        db,
        key=key,
        endpoint=endpoint,
        user_id=actor.user.id,
        request_fingerprint=idempotency.fingerprint(
            path_params={"balance_id": balance_id}, body=jsonable_encoder(payload)
        ),
    )
    if replay is not None:
        return JSONResponse(
            status_code=replay.response_status or 201, content=replay.response_body
        )

    try:
        original = _load(db, balance_id=balance_id, outlet_id=actor.outlet_id)
        before = _audit_snapshot(original)

        reversal, replacement = credit_service.reverse_opening_balance(
            db,
            original=original,
            reason=payload.reason,
            actor_id=actor.user.id,
            replacement_amount=payload.replacement_amount,
        )

        audit.record(
            db,
            outlet_id=actor.outlet_id,
            table_name="credit_opening_balances",
            record_id=reversal.id,
            action=AuditAction.reversal,
            changed_by=actor.user.id,
            old_values=before,
            # `AuditAction` has no slot for a reason, so it rides in `new_values` -- the
            # shape every other reversal route in this codebase already uses.
            new_values=_audit_snapshot(reversal) | {"reason": reversal.reversal_reason},
        )
        if replacement is not None:
            audit.record(
                db,
                outlet_id=actor.outlet_id,
                table_name="credit_opening_balances",
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

    response = OpeningBalanceReversalResponse(
        reversal=_to_response(reversal),
        replacement=_to_response(replacement) if replacement is not None else None,
        original=_to_response(original, is_reversed=True),
    )
    body = jsonable_encoder(response)
    idempotency.store(
        db, key=key, endpoint=endpoint, user_id=actor.user.id, status_code=201, body=body
    )
    return JSONResponse(status_code=201, content=body)
