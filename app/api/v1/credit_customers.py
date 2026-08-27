"""Credit customers -- who may take udhaar (CLAUDE.md §5.1, §6.6, §8).

Mirrors `app/api/v1/expense_categories.py`: admin-managed reference data, outlet-scoped,
deactivate rather than delete, and a `resolve_outlet_from_*` dependency so `PATCH` authorises
against the row's own outlet rather than the caller's default.

**Two response shapes, and that is a permission boundary rather than a convenience.**

§8 gives attendants a customer *list* and nothing more. They need it to fill in an udhaar
form -- you cannot record a sale against a customer you cannot name -- but a phone number, a
credit limit and an outstanding balance are none of their business. So `GET /credit-customers`
returns `CreditCustomerListItem` (id, name, vehicles, active) to everyone, and the manager-
floor `GET /credit-customers/{id}` returns `CreditCustomerResponse` with the rest.

Written as two models rather than one model filtered at runtime, deliberately. A filter is a
line of code someone can delete without any test noticing; a type that simply has no `phone`
field cannot leak one. It also means the OpenAPI schema tells the truth about what each role
sees.

**Cross-outlet is 403 `NOT_A_MEMBER`, not 404**, unlike §7.3's attachments. That was a real
decision, not an oversight: attendants cannot read customer detail at all under §8, so the
sensitive fields are already role-gated, and the ordinary `require_role` posture every other
outlet-scoped resource uses stays consistent. Flagged for the owner in the Phase 9 plan as the
one place this phase chose the looser of two defensible options.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, condecimal, field_validator
from sqlalchemy import UUID as sa_UUID
from sqlalchemy import literal, select, union_all
from sqlalchemy.orm import Session

from app.api.cursor import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from app.api.deps import Actor, require_role
from app.core.audit import AuditAction
from app.core.errors import AppError
from app.core.roles import Role
from app.db.session import get_db
from app.services import audit
from app.models.credit import (
    CreditCustomer,
    CreditOpeningBalance,
    CreditRepayment,
    CreditSale,
)
from app.models.shift import Shift
from app.services import credit as credit_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["credit"])

# The same considered exception `expense_categories` and `fuel_types` take to §9's cursor
# rule: a pump's customer list is bounded reference data, not a history to page through. The
# cap makes the bound structural rather than assumed.
_MAX_ROWS = 500

# The ledger's own cap. Larger than a page and smaller than forever: one customer's whole
# account at a pump is realistically dozens of lines, and §13.31 records why this is a cap
# rather than a cursor -- the running balance is only meaningful as a walk from a known
# anchor, and a cursor's page two has no anchor.
_MAX_LEDGER_ROWS = 500

# Indian vehicle registrations, normalised: upper-cased with every space and hyphen removed,
# so `MH 12 AB 1234`, `mh12-ab-1234` and `MH12AB1234` all become one value. Not validated
# against a format -- trade plates, temporary numbers and other states' older series vary more
# than any regex worth maintaining, and refusing a real vehicle is worse than storing an odd
# one. Normalisation is what stops one vehicle becoming three; validation is not the job here.
_VEHICLE_STRIP = re.compile(r"[\s-]+")

MoneyValue = condecimal(max_digits=12, decimal_places=2, ge=0)


def _normalise_vehicles(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    cleaned = [_VEHICLE_STRIP.sub("", v).upper() for v in values]
    # Deduplicate while preserving order, then collapse an empty list to NULL: "no vehicles
    # recorded" and "an empty array" are the same fact and should not be two states in the
    # database.
    seen: list[str] = []
    for value in cleaned:
        if value and value not in seen:
            seen.append(value)
    return seen or None


class CreditCustomerListItem(BaseModel):
    """What every role may see. No phone, no credit limit, no balance -- see the module
    docstring."""

    id: UUID
    name: str
    vehicle_numbers: list[str] | None
    is_active: bool


class CreditCustomerResponse(BaseModel):
    """The manager-and-above view, including the balance §6.6 computes on read."""

    id: UUID
    name: str
    phone: str
    vehicle_numbers: list[str] | None
    credit_limit: Decimal | None
    is_active: bool
    # §6.6: computed from the rows every time, never stored. May be negative when a customer
    # has paid in advance.
    outstanding: Decimal


class LedgerEntry(BaseModel):
    """One line of a customer's account, from either table.

    `amount` is the row as recorded -- positive for a sale, positive for a repayment,
    negative for either one's reversal. `balance_delta` is what that row did to the
    outstanding figure: a repayment reduces the debt, so its delta is the negation.

    Both are returned because they answer different questions. `amount` is what a human
    would find on the slip; `balance_delta` is what §6.6's sum actually added. Deriving one
    from the other requires knowing the rule, and a reader with a printed ledger in front of
    them should not have to.
    """

    id: UUID
    kind: str
    # `None` on an opening balance, which belongs to no shift, and on a repayment that
    # arrived at the bank rather than at this pump (§5.2).
    shift_id: UUID | None
    business_date: date
    amount: Decimal
    balance_delta: Decimal
    # The customer's outstanding immediately after this line. Computed server-side in
    # `Decimal` and serialised as a string, because §14 forbids money arithmetic in
    # JavaScript -- and a running balance is nothing but money arithmetic.
    balance_after: Decimal
    is_reversal: bool
    created_at: str


class LedgerPage(BaseModel):
    """A customer's account, newest first, with the balance beside every line.

    **Capped rather than cursor-paginated, and that is a deliberate exception to §9** in the
    same shape as `/nozzles` and `/fuel-types` (§13.31).

    The whole value of this screen is `balance_after`, and a running balance is only
    meaningful as a walk from a known anchor. The walk here runs newest-first *downward from
    `outstanding`*, so every line's balance depends only on lines **newer** than itself --
    which means truncating the old end cannot make a displayed figure wrong. A cursor cannot
    promise that: page two has no anchor unless the token carries a money value, and a money
    value in a client-held token is a figure the server would then have to trust back.

    `opening_balance` is `None` when none was entered, and must stay `None` all the way to
    the screen: §6.8's distinction between an answer and an omission (§14).
    """

    items: list[LedgerEntry]
    opening_balance: Decimal | None
    outstanding: Decimal
    truncated: bool


class CreditCustomerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=1, max_length=30)
    vehicle_numbers: list[str] | None = None
    # `ge=0`, not `gt=0`: zero is a real answer -- a customer on the books who may take no
    # further udhaar -- and is distinct from omitting the field, which means no limit at all.
    credit_limit: MoneyValue | None = None

    @field_validator("name", "phone")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """Strip, then refuse whitespace-only.

        `min_length` alone passes `"   "`, which then reaches the database's own
        `~ '[^[:space:]]'` CHECK and surfaces as an opaque 500 rather than a field error.
        Migration 0007 had to fix exactly this shape for reversal reasons; this is the same
        lesson applied on the way in rather than after the fact.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class CreditCustomerUpdate(BaseModel):
    """What may change after creation.

    Everything here is editable, including `phone` -- unlike `expense_categories.code` and
    `fuel_types.code`, which are frozen because changing them retroactively relabels history.
    A phone number carries no such meaning: it identifies a person for the purpose of not
    creating them twice, and people genuinely change numbers. The unique constraint still
    applies, so a change onto somebody else's number is refused.

    `extra="forbid"` so an unknown field is a 422 rather than a silent no-op -- an admin who
    "edited" something and got a 200 back would reasonably believe it worked.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, min_length=1, max_length=30)
    vehicle_numbers: list[str] | None = None
    credit_limit: MoneyValue | None = None
    is_active: bool | None = None

    @field_validator("name", "phone")
    @classmethod
    def _not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


def resolve_outlet_from_credit_customer(
    customer_id: UUID, db: Session = Depends(get_db)
) -> UUID:
    """The outlet that owns this customer, for `require_role` to authorise against.

    Mirrors `expense_categories.py::resolve_outlet_from_expense_category`, including the 404
    living in the dependency rather than the handler: this runs first, and without it a
    request against a nonexistent customer would report a permission failure instead of a
    missing row.
    """
    outlet_id = db.execute(
        select(CreditCustomer.outlet_id).where(CreditCustomer.id == customer_id)
    ).scalar_one_or_none()
    if outlet_id is None:
        raise AppError(
            status_code=404,
            code="CREDIT_CUSTOMER_NOT_FOUND",
            detail="No credit customer with that id.",
        )
    return outlet_id


def _audit_snapshot(row: CreditCustomer) -> dict[str, object]:
    """What §5.3's trail keeps about a credit customer.

    `credit_limit` is why this table needed auditing, and the reasoning is §6.6's own. That
    section insists an admin *override* of the limit is both stored on the row and
    audit-logged, because allowing a sale past the limit is a decision somebody has to answer
    for. Quietly **raising the limit** reaches the same outcome for every future sale and, until
    Phase 11, recorded nothing at all -- the row held the new figure and no trace of the old.

    A null limit means *no limit* (§6.6) and is recorded as JSON null, never coerced to 0. The
    two are opposite facts: null trusts the customer without bound, 0 refuses them every sale.

    `phone` is included because it is the natural key §5.1 chose over the name -- names
    genuinely collide at a pump, phone numbers do not -- so a change to it is a change to who
    this row is, and it is the one field here whose edit could merge two people's ledgers.
    """
    return {
        "name": row.name,
        "phone": row.phone,
        "vehicle_numbers": row.vehicle_numbers,
        "credit_limit": row.credit_limit,
        "is_active": row.is_active,
    }


def _to_list_item(row: CreditCustomer) -> CreditCustomerListItem:
    return CreditCustomerListItem(
        id=row.id,
        name=row.name,
        vehicle_numbers=row.vehicle_numbers,
        is_active=row.is_active,
    )


def _to_response(row: CreditCustomer, *, outstanding: Decimal) -> CreditCustomerResponse:
    return CreditCustomerResponse(
        id=row.id,
        name=row.name,
        phone=row.phone,
        vehicle_numbers=row.vehicle_numbers,
        credit_limit=row.credit_limit,
        is_active=row.is_active,
        outstanding=outstanding,
    )


# Which fields an explicit `null` may actually apply to.
#
# `app/api/v1/expense_categories.py` skips *every* null on PATCH, on the reasoning that
# "not mentioned" and "explicitly cleared" should stay distinct. That is right for a table
# whose editable columns are all NOT NULL, and wrong here.
#
# `credit_limit` and `vehicle_numbers` are nullable, and for `credit_limit` null is the
# meaningful value: it is §6.6's "no limit". Lifting a cap -- a customer who has earned
# unlimited trust -- is a real operation an admin must be able to perform, and a blanket
# skip would make a limit, once set, impossible to remove. `name`, `phone` and `is_active`
# are NOT NULL, so a null there is a client bug that would reach the database as an
# IntegrityError and surface as a 500; those are skipped exactly as Phase 8 skips them.
_NULLABLE_FIELDS = {"credit_limit", "vehicle_numbers"}


def _refuse_duplicate_phone(
    db: Session, *, outlet_id: UUID, phone: str, exclude_id: UUID | None = None
) -> None:
    """§5.1's `(outlet_id, phone)` rule, checked before the insert so the caller gets a
    readable 409 rather than the constraint's.

    The constraint is still the authority -- `uq_credit_customers_outlet_phone` is in
    `_CONSTRAINT_ERRORS`, so the losing side of a genuine race gets the same code this raises
    rather than an opaque 500. That mapping is the whole subject of Phase 9's Step 0.
    """
    statement = select(CreditCustomer.id).where(
        CreditCustomer.outlet_id == outlet_id, CreditCustomer.phone == phone
    )
    if exclude_id is not None:
        statement = statement.where(CreditCustomer.id != exclude_id)

    if db.execute(statement).scalar_one_or_none() is not None:
        raise AppError(
            status_code=409,
            code="CREDIT_CUSTOMER_PHONE_EXISTS",
            detail=(
                f"A credit customer with phone {phone} already exists at this outlet. One "
                "customer must not become two ledgers."
            ),
        )


# --- routes ------------------------------------------------------------------

# NOTE: `/credit-customers/outstanding` is declared BEFORE `/credit-customers/{customer_id}`.
# FastAPI matches routes in declaration order, so the reverse would make "outstanding" parse
# as a customer id and 422 on the UUID. `router.py` carries the same hazard note.


@router.get("/credit-customers", response_model=list[CreditCustomerListItem])
def list_credit_customers(
    include_inactive: bool = Query(default=False),
    actor: Actor = Depends(require_role(Role.attendant)),
    db: Session = Depends(get_db),
) -> list[CreditCustomerListItem]:
    """Every customer this outlet extends credit to. Attendant floor -- it fills a form.

    Deliberately the lean projection for *every* role, managers included. A manager wanting
    balances asks for the outstanding report or a single customer; making this endpoint change
    shape by role would mean one payload that is sometimes safe and sometimes not, which is
    exactly the thing that goes wrong quietly.
    """
    statement = (
        select(CreditCustomer)
        .where(CreditCustomer.outlet_id == actor.outlet_id)
        .order_by(CreditCustomer.name)
    )
    if not include_inactive:
        statement = statement.where(CreditCustomer.is_active.is_(True))

    rows = db.execute(statement.limit(_MAX_ROWS)).scalars().all()
    return [_to_list_item(row) for row in rows]


@router.get("/credit-customers/outstanding", response_model=list[CreditCustomerResponse])
def list_outstanding_balances(
    include_settled: bool = Query(default=False),
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
) -> list[CreditCustomerResponse]:
    """Who owes what -- the report the owner actually wants. Manager floor (§8).

    Defaults to customers with a non-zero balance, because a list of everyone who owes
    nothing is noise on the one screen where the answer matters. `include_settled=true`
    returns the whole ledger.

    Ordered by balance descending, largest debt first. A customer in credit (negative
    balance, §6.6) sorts to the bottom, which is where they belong on a chase list.
    """
    balances = credit_service.outstanding_by_customer(db, outlet_id=actor.outlet_id)
    rows = (
        db.execute(
            select(CreditCustomer)
            .where(CreditCustomer.outlet_id == actor.outlet_id)
            .limit(_MAX_ROWS)
        )
        .scalars()
        .all()
    )

    items = [
        _to_response(row, outstanding=balances.get(row.id, Decimal("0.00")))
        for row in rows
        if include_settled or balances.get(row.id, Decimal("0.00")) != Decimal("0.00")
    ]
    items.sort(key=lambda item: item.outstanding, reverse=True)
    return items


@router.get("/credit-customers/{customer_id}", response_model=CreditCustomerResponse)
def get_credit_customer(
    customer_id: UUID,
    actor: Actor = Depends(
        require_role(Role.manager, resolve_outlet_from_credit_customer)
    ),
    db: Session = Depends(get_db),
) -> CreditCustomerResponse:
    """One customer, with the balance §6.6 computes on read. Manager floor (§8)."""
    # Not None: resolve_outlet_from_credit_customer already 404'd inside the dependency.
    customer = db.get(CreditCustomer, customer_id)
    return _to_response(
        customer, outstanding=credit_service.outstanding(db, customer_id=customer.id)
    )


@router.post("/credit-customers", response_model=CreditCustomerResponse, status_code=201)
def create_credit_customer(
    payload: CreditCustomerCreate,
    actor: Actor = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
) -> CreditCustomerResponse:
    """Add a customer the outlet extends credit to. Admin only (§8).

    **Do not create a customer standing for a salesman's cash shortfall.** This outlet books
    a shortfall as udhaar against the salesman's own name, but a shortfall is the outcome of a
    reconciliation rather than a sale: it has no receipt to satisfy `credit_sales`' `NOT NULL`
    and it would mix staff debt into the customer ledger, so "what does this customer owe me"
    would stop having an answer. Phase 10 gives shortfalls their own record type (§13.14), and
    §14 forbids this by name -- restated here at the one endpoint that could do it, the same
    way `create_expense_category` restates the fuel-purchase exclusion.

    No `Idempotency-Key`: a customer is reference data, not a money record (§6.10), and the
    unique phone is a natural key -- a retried create collides rather than duplicating.
    """
    phone = payload.phone
    _refuse_duplicate_phone(db, outlet_id=actor.outlet_id, phone=phone)

    row = CreditCustomer(
        outlet_id=actor.outlet_id,
        name=payload.name,
        phone=phone,
        vehicle_numbers=_normalise_vehicles(payload.vehicle_numbers),
        credit_limit=payload.credit_limit,
        created_by=actor.user.id,
    )
    db.add(row)
    # flush so the id exists for the audit row; one transaction carries both (§5.3).
    db.flush()

    audit.record(
        db,
        outlet_id=actor.outlet_id,
        table_name="credit_customers",
        record_id=row.id,
        action=AuditAction.insert,
        changed_by=actor.user.id,
        new_values=_audit_snapshot(row),
    )
    db.commit()
    db.refresh(row)

    logger.info(
        "credit customer created",
        extra={
            "credit_customer_id": str(row.id),
            "outlet_id": str(actor.outlet_id),
            "has_limit": row.credit_limit is not None,
        },
    )
    # A brand new customer owes nothing; computing it would be a query with one possible
    # answer.
    return _to_response(row, outstanding=Decimal("0.00"))


@router.patch(
    "/credit-customers/{customer_id}", response_model=CreditCustomerResponse
)
def update_credit_customer(
    customer_id: UUID,
    payload: CreditCustomerUpdate,
    actor: Actor = Depends(require_role(Role.admin, resolve_outlet_from_credit_customer)),
    db: Session = Depends(get_db),
) -> CreditCustomerResponse:
    """Edit a customer, or retire one. Admin only (§8).

    **Deactivating is how a customer leaves**, never a `DELETE` (§3 rule 6). A deactivated
    customer refuses new udhaar but still accepts repayments (§5.1) -- you retire somebody
    precisely to stop the debt growing while they pay it off.
    """
    # Not None: resolve_outlet_from_credit_customer already 404'd inside the dependency.
    customer = db.get(CreditCustomer, customer_id)

    # exclude_unset so that "not mentioned" and "explicitly set to null" stay distinct -- a
    # PATCH that omits is_active must not clear it. What each explicit null then *means* is
    # decided by _NULLABLE_FIELDS above.
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise AppError(
            status_code=422,
            code="NO_FIELDS_TO_UPDATE",
            detail="Send at least one field to change.",
        )

    if changes.get("phone") is not None and changes["phone"] != customer.phone:
        _refuse_duplicate_phone(
            db,
            outlet_id=customer.outlet_id,
            phone=changes["phone"],
            exclude_id=customer.id,
        )

    if changes.get("vehicle_numbers") is not None:
        changes["vehicle_numbers"] = _normalise_vehicles(changes["vehicle_numbers"])

    # Before the mutation, or old_values records the new state and the change is unreadable.
    before = _audit_snapshot(customer)

    for field, value in changes.items():
        if value is None and field not in _NULLABLE_FIELDS:
            continue
        setattr(customer, field, value)

    audit.record(
        db,
        outlet_id=customer.outlet_id,
        table_name="credit_customers",
        record_id=customer.id,
        # `update`, including a deactivation -- see fuel_types.py for why not `status_change`.
        # Deactivating a customer is §5.1's asymmetric retirement: no new udhaar, repayments
        # still accepted, so it is squarely an ordinary field change.
        action=AuditAction.update,
        changed_by=actor.user.id,
        old_values=before,
        new_values=_audit_snapshot(customer),
    )
    db.commit()
    db.refresh(customer)

    logger.info(
        "credit customer updated",
        extra={
            "credit_customer_id": str(customer.id),
            "fields": sorted(changes),
        },
    )
    return _to_response(
        customer, outstanding=credit_service.outstanding(db, customer_id=customer.id)
    )


@router.get("/credit-customers/{customer_id}/ledger", response_model=LedgerPage)
def get_credit_customer_ledger(
    customer_id: UUID,
    limit: int = Query(default=_MAX_LEDGER_ROWS, ge=1, le=_MAX_LEDGER_ROWS),
    actor: Actor = Depends(
        require_role(Role.manager, resolve_outlet_from_credit_customer)
    ),
    db: Session = Depends(get_db),
) -> LedgerPage:
    """Every line of one customer's account, newest first, with a running balance (§8).

    This is the evidence behind §6.6's outstanding figure. A balance nobody can take apart is
    a number the owner has to trust rather than check, and §14 says a plausible-but-wrong
    figure is this project's primary failure mode -- so the arithmetic has to be auditable
    line by line, not merely correct.

    **Three tables, `UNION ALL`, not three requests the client merges.** Merging client-side
    cannot order correctly: taking the newest N of each table and interleaving them gives the
    newest N overall only by luck, and gets steadily wronger the more one-sided an account
    is. The true ordering exists only in the combined result.

    **Ordered by business date, not `created_at`. Phase 16 changed this.** §4.7 says the whole
    day is typed in after the fact, often days later, so entry order is not economic order --
    and a running balance read in entry order is a column of numbers that never matches the
    slips. `created_at` stays as the tiebreaker within a date, because two rows on one day
    have no other ordering and it only has to be stable.

    Reversals appear as their own lines rather than being netted away, per §6.9: both rows
    remain visible, and a customer disputing a bill is entitled to see that a charge was
    raised and cancelled rather than an account that silently never mentions it.
    """
    # A sale's own sign is already its effect on the balance. A repayment's is the negation
    # -- which also makes a *reversed* repayment (already negative) correctly add the debt
    # back, with no second rule. An opening balance states the debt, so it reads like a sale.
    openings = select(
        CreditOpeningBalance.id.label("id"),
        CreditOpeningBalance.created_at.label("created_at"),
        CreditOpeningBalance.as_of_date.label("business_date"),
        literal("opening").label("kind"),
        CreditOpeningBalance.amount.label("amount"),
        CreditOpeningBalance.amount.label("balance_delta"),
        literal(None, type_=sa_UUID).label("shift_id"),
        CreditOpeningBalance.reverses_id.label("reverses_id"),
    ).where(CreditOpeningBalance.credit_customer_id == customer_id)

    sales = (
        select(
            CreditSale.id,
            CreditSale.created_at,
            Shift.business_date,
            literal("sale"),
            CreditSale.amount,
            CreditSale.amount,
            CreditSale.shift_id,
            CreditSale.reverses_id,
        )
        # A credit sale has no date of its own -- it belongs to a shift, and the shift owns
        # the business date (§6.1). This join is the only place that date exists.
        .join(Shift, Shift.id == CreditSale.shift_id)
        .where(CreditSale.credit_customer_id == customer_id)
    )

    repayments = select(
        CreditRepayment.id,
        CreditRepayment.created_at,
        CreditRepayment.business_date,
        literal("repayment"),
        CreditRepayment.amount,
        -CreditRepayment.amount,
        CreditRepayment.shift_id,
        CreditRepayment.reverses_id,
    ).where(CreditRepayment.credit_customer_id == customer_id)

    combined = union_all(openings, sales, repayments).subquery()
    rows = db.execute(
        select(combined)
        .order_by(
            combined.c.business_date.desc(),
            combined.c.created_at.desc(),
            combined.c.id.desc(),
        )
        .limit(limit + 1)
    ).all()

    truncated = len(rows) > limit
    page = rows[:limit]

    # Walk down from the current outstanding. Every line's `balance_after` therefore depends
    # only on the lines above it (newer than it), which is what makes the figures correct even
    # when the old end is truncated -- see `LedgerPage`.
    outstanding = credit_service.outstanding(db, customer_id=customer_id)
    running = outstanding
    entries: list[LedgerEntry] = []
    for row in page:
        entries.append(
            LedgerEntry(
                id=row.id,
                kind=row.kind,
                shift_id=row.shift_id,
                business_date=row.business_date,
                amount=row.amount,
                balance_delta=row.balance_delta,
                balance_after=running,
                is_reversal=row.reverses_id is not None,
                created_at=row.created_at.isoformat(),
            )
        )
        running = running - row.balance_delta

    live_opening = credit_service.live_opening_balance(db, customer_id=customer_id)

    return LedgerPage(
        items=entries,
        # `None`, never ₹0.00, when nobody has entered one (§6.8, §14).
        opening_balance=live_opening.amount if live_opening is not None else None,
        outstanding=outstanding,
        truncated=truncated,
    )
