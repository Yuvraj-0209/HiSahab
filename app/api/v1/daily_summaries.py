"""The daily cash summary -- §6.4's answer and §6.5's rolling balance (CLAUDE.md §5.2, §6.4,
§6.5, §8, §13.16).

Phase 10, and the phase's point. Everything since Phase 5 has been assembling terms; this is
where they are added up, compared against a physical count when one exists, and frozen.

## The figure is stored, not recomputed

§5.2: *"if a calculation bug is fixed six months from now, you still need to know what the
system told the manager on that day."* Every component is snapshotted too, for the same
reason applied term by term -- a manager checking a ₹300 variance needs the breakdown **as it
stood**, not as recomputed after a reversal landed underneath it. Otherwise the total and its
own explanation disagree, and the explanation is the half he can verify by hand.

## §6.5, restated for a locker

The original rule assumed a drawer counted nightly and refused to finalise day N when day N−1
had no count. This outlet has no fixed counting moment (§14), so that rule would have blocked
every day forever. The chain is therefore:

    opening = actual_counted(previous)   if the locker was counted
            = expected_closing(previous) if it was not
            = an admin's figure          if there is no previous day at all

The count wins wherever there is one, which is §6.5's actual principle. A physical count is an
occasional **audit that re-anchors the chain**, exactly as a confirmed meter reading
re-anchors §4.7's.

## Finalising is terminal, and needs every shift locked

Creating requires every shift on the date to be `closed` or `locked`; finalising requires them
all `locked`. §6.5 chains days together, so a stale `expected_closing` does not stay local --
it propagates into every opening balance after it. `locked` is the only state in which §5.2
guarantees the inputs cannot move.

An admin may **unfinalise** with a mandatory reason, audit-logged -- §6.8's shift-reopen shape.

**Role floors (§8).** Manager creates and records the count; **admin** finalises and
unfinalises. Seeding the very first opening balance is admin-only, which is §8's "seed
initial opening balance" row.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, StringConstraints, condecimal
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Actor, require_role
from app.core.audit import AuditAction
from app.core.cash import OpeningBalanceSource
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.roles import Role
from app.core.shifts import ShiftStatus
from app.db.session import get_db
from app.models.cash import DailyCashSummary
from app.services import audit, cash as cash_service, shifts as shift_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["daily cash summaries"])

_MAX_ROWS = 100


# --- schemas -----------------------------------------------------------------

MoneyValue = condecimal(max_digits=12, decimal_places=2)
ReasonValue = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)
]


class SummaryResponse(BaseModel):
    id: UUID
    business_date: date
    opening_balance: Decimal
    opening_balance_source: OpeningBalanceSource
    expected_closing: Decimal
    actual_counted: Decimal | None
    variance: Decimal | None

    metered_fuel_sales: Decimal
    non_fuel_sales_total: Decimal
    card_total: Decimal
    upi_total: Decimal
    wallet_total: Decimal
    credit_sales_total: Decimal
    cash_credit_repayments: Decimal
    cash_shortfall_settlements: Decimal
    cash_expenses: Decimal
    bank_deposits_total: Decimal
    shortfalls_booked: Decimal

    is_finalised: bool
    requires_review: bool
    review_note: str | None
    notes: str | None

    variance_basis: str = (
        "variance = actual_counted - expected_closing, and is null when nobody counted -- "
        "which is most days under CLAUDE.md §6.5's locker model. Null means 'no variance "
        "known', NOT a variance of zero. The variance is recorded and never auto-corrected."
    )


class SummaryPage(BaseModel):
    items: list[SummaryResponse]
    truncated: bool


class SummaryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business_date: date
    # Required only when there is no previous summary -- §6.5's anchor, admin-only. Supplying
    # one when a previous day exists is refused: the figure is derived, not typed.
    opening_balance: MoneyValue | None = None
    actual_counted: MoneyValue | None = None
    notes: str | None = None


class SummaryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # No component fields and no `expected_closing`. §3 rule 8: never store a computed figure
    # the client sent. The only things a human contributes after the fact are the physical
    # count and a note explaining it.
    actual_counted: MoneyValue | None = None
    notes: str | None = None


class Unfinalise(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: ReasonValue


# --- helpers -----------------------------------------------------------------


def _to_response(row: DailyCashSummary) -> SummaryResponse:
    return SummaryResponse(
        id=row.id,
        business_date=row.business_date,
        opening_balance=row.opening_balance,
        opening_balance_source=OpeningBalanceSource(row.opening_balance_source),
        expected_closing=row.expected_closing,
        actual_counted=row.actual_counted,
        variance=row.variance,
        metered_fuel_sales=row.metered_fuel_sales,
        non_fuel_sales_total=row.non_fuel_sales_total,
        card_total=row.card_total,
        upi_total=row.upi_total,
        wallet_total=row.wallet_total,
        credit_sales_total=row.credit_sales_total,
        cash_credit_repayments=row.cash_credit_repayments,
        cash_shortfall_settlements=row.cash_shortfall_settlements,
        cash_expenses=row.cash_expenses,
        bank_deposits_total=row.bank_deposits_total,
        shortfalls_booked=row.shortfalls_booked,
        is_finalised=row.is_finalised,
        requires_review=row.requires_review,
        review_note=row.review_note,
        notes=row.notes,
    )


def _audit_snapshot(row: DailyCashSummary) -> dict[str, object]:
    return {
        "business_date": row.business_date,
        "opening_balance": row.opening_balance,
        "opening_balance_source": row.opening_balance_source,
        "expected_closing": row.expected_closing,
        "actual_counted": row.actual_counted,
        "is_finalised": row.is_finalised,
    }


def _load(db: Session, outlet_id: UUID, business_date: date) -> DailyCashSummary:
    row = db.execute(
        select(DailyCashSummary).where(
            DailyCashSummary.outlet_id == outlet_id,
            DailyCashSummary.business_date == business_date,
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError(
            status_code=404,
            code="SUMMARY_NOT_FOUND",
            detail="No cash summary for that business date.",
        )
    return row


def _reject_future(business_date: date, settings: Settings) -> None:
    """§6.1: a business date in the future is always a data-entry error -- trading has not
    happened yet. Evaluated in the outlet's local timezone, not UTC, because at 23:00 IST the
    UTC date is still yesterday and a correct entry would be refused."""
    if business_date > shift_service.outlet_today(settings.TZ_DISPLAY):
        raise AppError(
            status_code=422,
            code="BUSINESS_DATE_IN_FUTURE",
            detail=(
                "That business date is in the future. Trading has not happened yet, so "
                "there is nothing to reconcile."
            ),
        )


def _require_shifts_settled(
    db: Session, *, outlet_id: UUID, business_date: date, locked: bool
) -> None:
    """Every shift on the date must be closed (to summarise) or locked (to finalise).

    An **open** shift means the day is still being traded or typed in, and a summary computed
    over it is a snapshot of something still moving.

    Finalising demands `locked` because §6.5 chains days: a stale `expected_closing` does not
    stay local, it propagates into every opening balance after it. §5.2 guarantees nothing
    referencing a locked shift may be modified, and §6.8 makes `locked` terminal, so that is
    the only state in which the snapshot is guaranteed to remain true. It also inherits
    §6.7's quality gate for free -- a shift cannot lock while a flagged expense is unreviewed.
    """
    shifts = cash_service.shifts_on(
        db, outlet_id=outlet_id, business_date=business_date
    )
    unsettled = [
        shift
        for shift in shifts
        if ShiftStatus(shift.status) is ShiftStatus.open
        or (locked and ShiftStatus(shift.status) is not ShiftStatus.locked)
    ]
    if not unsettled:
        return
    labels = ", ".join(f"#{shift.sequence} ({shift.status})" for shift in unsettled)
    if locked:
        raise AppError(
            status_code=409,
            code="DAY_NOT_LOCKED",
            detail=(
                f"Every shift on this date must be locked before the day can be finalised. "
                f"Still open or merely closed: {labels}."
            ),
        )
    raise AppError(
        status_code=409,
        code="DAY_HAS_OPEN_SHIFTS",
        detail=(
            f"This date still has an open shift ({labels}). Close it before reconciling "
            "the day."
        ),
    )


# --- routes ------------------------------------------------------------------


@router.get("/daily-summaries", response_model=SummaryPage)
def list_summaries(
    limit: int = Query(default=30, ge=1, le=_MAX_ROWS),
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
) -> SummaryPage:
    """The most recent days, newest first. Manager floor (§8: reports)."""
    rows = list(
        db.execute(
            select(DailyCashSummary)
            .where(DailyCashSummary.outlet_id == actor.outlet_id)
            .order_by(DailyCashSummary.business_date.desc())
            .limit(limit + 1)
        )
        .scalars()
        .all()
    )
    truncated = len(rows) > limit
    return SummaryPage(
        items=[_to_response(row) for row in rows[:limit]], truncated=truncated
    )


@router.get("/daily-summaries/{business_date}", response_model=SummaryResponse)
def read_summary(
    business_date: date,
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
) -> SummaryResponse:
    """One day's stored figures. Never recomputed on read (§5.2)."""
    return _to_response(_load(db, actor.outlet_id, business_date))


@router.post("/daily-summaries", response_model=SummaryResponse, status_code=201)
def create_summary(
    payload: SummaryCreate,
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Compute §6.4 for one business date and store the answer. Manager floor (§8).

    **No `Idempotency-Key`** (§6.10). `UNIQUE (outlet_id, business_date)` makes this
    naturally idempotent: a retry gets 409 `SUMMARY_ALREADY_EXISTS` and creates nothing,
    which is exactly the reasoning §6.10 gives for nozzle readings. Building a reservation
    store around a POST that cannot duplicate would be ceremony.

    **The opening balance is derived, never typed** -- except for the very first day at an
    outlet, which has no predecessor and is therefore the anchor. §4.7's argument: the anchor
    is the first row itself, not a separate seed record, because two copies of "where the
    locker started" would eventually disagree.
    """
    _reject_future(payload.business_date, settings)

    existing = db.execute(
        select(DailyCashSummary.id).where(
            DailyCashSummary.outlet_id == actor.outlet_id,
            DailyCashSummary.business_date == payload.business_date,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise AppError(
            status_code=409,
            code="SUMMARY_ALREADY_EXISTS",
            detail=(
                "This business date already has a cash summary. Update the count with "
                "PATCH rather than creating a second one."
            ),
        )

    _require_shifts_settled(
        db,
        outlet_id=actor.outlet_id,
        business_date=payload.business_date,
        locked=False,
    )

    previous = cash_service.previous_summary(
        db, outlet_id=actor.outlet_id, business_date=payload.business_date
    )

    if previous is None:
        # §6.5's anchor. Admin-only and required, mirroring §4.7's ANCHOR_REQUIRES_ADMIN:
        # somebody has to count the locker once and say what was in it, and that figure
        # silently becomes the basis of every day that follows.
        if payload.opening_balance is None:
            raise AppError(
                status_code=422,
                code="OPENING_BALANCE_REQUIRED",
                detail=(
                    "This is the first cash summary at this outlet, so there is no previous "
                    "day to carry a balance from. An admin must count the locker and enter "
                    "the opening balance once."
                ),
            )
        if actor.role is not Role.admin:
            raise AppError(
                status_code=403,
                code="OPENING_BALANCE_REQUIRES_ADMIN",
                detail=(
                    "Seeding the first opening balance is an admin action -- every later "
                    "day's balance is chained from it."
                ),
            )
        opening = payload.opening_balance
        source = OpeningBalanceSource.seeded
    else:
        if payload.opening_balance is not None:
            raise AppError(
                status_code=409,
                code="OPENING_BALANCE_IS_CHAINED",
                detail=(
                    "The opening balance is carried from the previous day, not entered. "
                    "Correct the previous day's count instead."
                ),
            )
        opening, source = cash_service.opening_balance_from(previous)

    totals = cash_service.day_totals(
        db, outlet_id=actor.outlet_id, business_date=payload.business_date
    )
    closing = cash_service.expected_closing(opening_balance=opening, totals=totals)

    row = DailyCashSummary(
        outlet_id=actor.outlet_id,
        business_date=payload.business_date,
        opening_balance=opening,
        opening_balance_source=source.value,
        expected_closing=closing,
        actual_counted=payload.actual_counted,
        metered_fuel_sales=totals.metered_fuel_sales,
        non_fuel_sales_total=totals.non_fuel_sales_total,
        card_total=totals.card_total,
        upi_total=totals.upi_total,
        wallet_total=totals.wallet_total,
        credit_sales_total=totals.credit_sales_total,
        cash_credit_repayments=totals.cash_credit_repayments,
        cash_shortfall_settlements=totals.cash_shortfall_settlements,
        cash_expenses=totals.cash_expenses,
        bank_deposits_total=totals.bank_deposits_total,
        shortfalls_booked=totals.shortfalls_booked,
        notes=payload.notes,
        created_by=actor.user.id,
    )
    db.add(row)
    db.flush()

    audit.record(
        db,
        outlet_id=actor.outlet_id,
        table_name="daily_cash_summaries",
        record_id=row.id,
        action=AuditAction.insert,
        changed_by=actor.user.id,
        new_values=_audit_snapshot(row),
    )
    db.commit()
    db.refresh(row)

    logger.info(
        "daily cash summary created",
        extra={
            "summary_id": str(row.id),
            "business_date": row.business_date.isoformat(),
            "opening_balance": str(row.opening_balance),
            "opening_balance_source": row.opening_balance_source,
            "expected_closing": str(row.expected_closing),
            "incomplete": totals.incomplete,
        },
    )
    return _to_response(row)


@router.patch("/daily-summaries/{business_date}", response_model=SummaryResponse)
def update_summary(
    business_date: date,
    payload: SummaryUpdate,
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
) -> SummaryResponse:
    """Record the physical count, or a note. Manager floor (§8).

    **`expected_closing` is never touched.** §6.4: the variance is recorded, never
    auto-corrected -- *the variance is the signal*. "Fixing" the expected figure to match the
    count would delete exactly the information the count was taken to produce.

    No Idempotency-Key: a PATCH is idempotent by construction.
    """
    row = _load(db, actor.outlet_id, business_date)

    if row.is_finalised:
        raise AppError(
            status_code=409,
            code="SUMMARY_FINALISED",
            detail=(
                "This day is finalised. An admin must unfinalise it, with a reason, before "
                "anything on it can change."
            ),
        )

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise AppError(
            status_code=422,
            code="NO_FIELDS_TO_UPDATE",
            detail="Provide at least one field to change.",
        )

    before = _audit_snapshot(row)
    for field, value in changes.items():
        # An explicit null is ignored rather than treated as "clear this", matching every
        # sibling router. Clearing a count that was genuinely taken would silently break
        # §6.5's chain for every day after it.
        if value is None:
            continue
        setattr(row, field, value)

    audit.record(
        db,
        outlet_id=actor.outlet_id,
        table_name="daily_cash_summaries",
        record_id=row.id,
        action=AuditAction.update,
        changed_by=actor.user.id,
        old_values=before,
        new_values=_audit_snapshot(row),
    )
    db.commit()
    db.refresh(row)

    logger.info(
        "daily cash summary updated",
        extra={
            "summary_id": str(row.id),
            "business_date": row.business_date.isoformat(),
            "actual_counted": str(row.actual_counted),
            "variance": str(row.variance),
        },
    )
    return _to_response(row)


@router.patch(
    "/daily-summaries/{business_date}/finalise", response_model=SummaryResponse
)
def finalise_summary(
    business_date: date,
    actor: Actor = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
) -> SummaryResponse:
    """Freeze a day. **Admin only** (§8: "lock a shift / finalise a day").

    Two preconditions, and they are different questions:

    * every shift on the date is **locked** -- otherwise the stored figures describe inputs
      that can still move, and §6.5 propagates that staleness forward;
    * the previous day is **finalised** -- §6.5's `PRIOR_DAY_NOT_RECONCILED`, narrowed to
      this meaning because the original one ("nobody counted") would block every day forever
      at an outlet with a locker.
    """
    row = _load(db, actor.outlet_id, business_date)

    if row.is_finalised:
        raise AppError(
            status_code=409,
            code="SUMMARY_FINALISED",
            detail="This day is already finalised.",
        )

    _require_shifts_settled(
        db, outlet_id=actor.outlet_id, business_date=business_date, locked=True
    )

    previous = cash_service.previous_summary(
        db, outlet_id=actor.outlet_id, business_date=business_date
    )
    if previous is not None and not previous.is_finalised:
        raise AppError(
            status_code=409,
            code="PRIOR_DAY_NOT_RECONCILED",
            detail=(
                f"{previous.business_date.isoformat()} is not finalised yet. Days are "
                "chained (§6.5), so finalising out of order would freeze a balance whose "
                "own opening can still change."
            ),
        )

    before = _audit_snapshot(row)
    row.is_finalised = True
    row.finalised_by = actor.user.id
    row.finalised_at = datetime.now(tz=timezone.utc)

    audit.record(
        db,
        outlet_id=actor.outlet_id,
        table_name="daily_cash_summaries",
        record_id=row.id,
        action=AuditAction.status_change,
        changed_by=actor.user.id,
        old_values=before,
        new_values=_audit_snapshot(row),
    )
    db.commit()
    db.refresh(row)

    logger.info(
        "day finalised",
        extra={
            "summary_id": str(row.id),
            "business_date": row.business_date.isoformat(),
            "finalised_by": str(actor.user.id),
        },
    )
    return _to_response(row)


@router.patch(
    "/daily-summaries/{business_date}/unfinalise", response_model=SummaryResponse
)
def unfinalise_summary(
    business_date: date,
    payload: Unfinalise,
    actor: Actor = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
) -> SummaryResponse:
    """Reopen a finalised day, with a mandatory reason. **Admin only** (§8).

    §6.8's shift-reopen shape, one table further on: a backwards status transition is allowed
    but never silent, and the reason is audit-logged rather than discarded.

    The stored figures are **left exactly as they are**. Nothing is recomputed -- see §13.16.
    A day is unfinalised so that a human can look at it, not so the system can quietly
    rewrite what it said.
    """
    row = _load(db, actor.outlet_id, business_date)

    if not row.is_finalised:
        raise AppError(
            status_code=409,
            code="SUMMARY_NOT_FINALISED",
            detail="This day is not finalised.",
        )

    before = _audit_snapshot(row)
    row.is_finalised = False
    row.finalised_by = None
    row.finalised_at = None

    audit.record(
        db,
        outlet_id=actor.outlet_id,
        table_name="daily_cash_summaries",
        record_id=row.id,
        action=AuditAction.status_change,
        changed_by=actor.user.id,
        old_values=before,
        new_values=_audit_snapshot(row) | {"reason": payload.reason},
    )
    db.commit()
    db.refresh(row)

    logger.warning(
        "day unfinalised",
        extra={
            "summary_id": str(row.id),
            "business_date": row.business_date.isoformat(),
            "reason": payload.reason,
            "unfinalised_by": str(actor.user.id),
        },
    )
    return _to_response(row)
