"""Shifts -- the lifecycle every financial row hangs off (CLAUDE.md §5.2, §6.8, §4.7).

Four state-changing routes and three reads. The states are `open -> closed -> locked`, with
one sanctioned reversal (`closed -> open`) that an admin performs with a reason and which is
audit-logged, per §5.2.

**Role floors (§8):** attendants open and read their own; managers close; admins lock and
reopen. Ownership is enforced separately from role by `require_shift_access` in
app/api/deps.py -- an attendant may act only on a shift whose `attendant_id` is their own.

**All three of §6.8's close preconditions now exist**, each having landed with the phase
that owns its table: `MISSING_NOZZLE_READINGS` with Phase 5, `MISSING_COLLECTIONS` with
Phase 6, and `CREDIT_SALE_MISSING_RECEIPT` with Phase 9. None was ever stubbed ahead of its
table -- §11 forbids scaffolding, and an empty check that always passes is indistinguishable
from one that was forgotten. The last of them cannot fire through this application at all;
`close_shift` explains at the point of the check why it is still there.

§6.7's lock precondition, `UNREVIEWED_EXPENSES_EXIST`, landed with Phase 7 and is enforced
in `lock_shift` below.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.api.cursor import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    decode_shift_cursor,
    encode_shift_cursor,
)
from app.api.deps import (
    Actor,
    ShiftAccess,
    get_default_outlet_id,
    require_role,
    require_shift_access,
)
from app.core.audit import AuditAction
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.roles import Role
from app.core.shifts import ShiftStatus, can_transition
from app.db.session import get_db
from app.models.shift import OutletShiftTemplate, Shift
from app.models.user import OutletMembership
from app.services import (
    audit,
    collections as collection_service,
    credit as credit_service,
    expenses as expense_service,
    readings as reading_service,
    shifts as shift_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["shifts"])


# --- schemas -----------------------------------------------------------------


class ShiftResponse(BaseModel):
    id: UUID
    outlet_id: UUID
    business_date: date
    sequence: int
    started_at: datetime
    ended_at: datetime | None
    attendant_id: UUID
    status: ShiftStatus
    closed_by: UUID | None
    closed_at: datetime | None
    locked_by: UUID | None
    locked_at: datetime | None


class ShiftPage(BaseModel):
    items: list[ShiftResponse]
    next_cursor: str | None


class ShiftCreate(BaseModel):
    """Opening a shift.

    `sequence` is absent on purpose and `extra="forbid"` makes sending it a 422. It is
    assigned by the server from a read of the table (§4.7); letting a client choose would
    let it overwrite or skip a link in the chain.
    """

    model_config = ConfigDict(extra="forbid")

    business_date: date
    # Optional: defaults to the caller. An attendant may only ever name themselves -- see
    # the check in `open_shift`.
    attendant_id: UUID | None = None
    # Optional: default from the outlet's shift template for this sequence. Supplying them
    # explicitly wins and is logged, for the day the station opened late.
    started_at: datetime | None = None
    ended_at: datetime | None = None

    @field_validator("started_at", "ended_at")
    @classmethod
    def _must_be_timezone_aware(cls, value: datetime | None) -> datetime | None:
        """A naive timestamp is refused rather than assumed into a timezone.

        Same rule as `fuel_prices.effective_from`, and for a sharper reason here: guessing
        UTC for a value the user meant as IST moves a shift by 5.5 hours, which can move it
        across the 06:00 price revision and revalue the whole day (§6.3).
        """
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(
                "Timestamps must include a timezone offset, e.g. "
                "2026-08-18T06:00:00+05:30"
            )
        return value


class ShiftClose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Optional because the template already supplied one at open. Provided when the shift
    # actually ended at a different time from the usual.
    ended_at: datetime | None = None

    @field_validator("ended_at")
    @classmethod
    def _must_be_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("ended_at must include a timezone offset.")
        return value


class ShiftReopen(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Mandatory, and min_length is on the schema so an empty string is a 422 rather than an
    # audit row that records nothing. §5.2: a backwards transition without a stated reason
    # is exactly the thing the audit log exists to prevent.
    reason: str = Field(min_length=3, max_length=500)


def _to_response(shift: Shift) -> ShiftResponse:
    return ShiftResponse(
        id=shift.id,
        outlet_id=shift.outlet_id,
        business_date=shift.business_date,
        sequence=shift.sequence,
        started_at=shift.started_at,
        ended_at=shift.ended_at,
        attendant_id=shift.attendant_id,
        status=ShiftStatus(shift.status),
        closed_by=shift.closed_by,
        closed_at=shift.closed_at,
        locked_by=shift.locked_by,
        locked_at=shift.locked_at,
    )


def _audit_snapshot(shift: Shift) -> dict[str, object]:
    """The fields worth recording either side of a lifecycle change.

    Deliberately not every column: an audit entry that echoes the whole row makes the
    change itself hard to see. Status and the who/when stamps are what §5.2 asks for.
    """
    return {
        "status": shift.status,
        "ended_at": shift.ended_at,
        "closed_by": shift.closed_by,
        "closed_at": shift.closed_at,
        "locked_by": shift.locked_by,
        "locked_at": shift.locked_at,
    }


def _guard_transition(shift: Shift, target: ShiftStatus) -> None:
    """Refuse an illegal lifecycle move with a code that says which one it was."""
    current = ShiftStatus(shift.status)
    if can_transition(current, target):
        return

    if current is ShiftStatus.locked:
        raise AppError(
            status_code=409,
            code="SHIFT_LOCKED",
            detail=(
                "This shift is locked. Locking is final -- corrections must be recorded "
                "as reversal entries against it."
            ),
        )
    if target is ShiftStatus.locked:
        raise AppError(
            status_code=409,
            code="SHIFT_NOT_CLOSED",
            detail="A shift must be closed before it can be locked.",
        )
    raise AppError(
        status_code=409,
        code="SHIFT_NOT_OPEN",
        detail=f"A shift that is {current.value} cannot be closed.",
    )


# --- routes ------------------------------------------------------------------


@router.post("/shifts", response_model=ShiftResponse, status_code=201)
def open_shift(
    payload: ShiftCreate,
    actor: Actor = Depends(require_role(Role.attendant, get_default_outlet_id)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ShiftResponse:
    """Open a shift. Attendant floor, but an attendant may only name themselves.

    §8's permission table covers *writing on* an open shift and never says who may create
    one. Attendant floor is the reading that fits §4.7's operation: the whole day is typed
    in after the fact, and the salesman accountable for it must be able to start his own
    sheet. The ownership constraint below is what keeps that from becoming "any attendant
    can open a shift in anyone's name".
    """
    attendant_id = payload.attendant_id or actor.user.id

    if actor.role is Role.attendant and attendant_id != actor.user.id:
        raise AppError(
            status_code=403,
            code="NOT_YOUR_SHIFT",
            detail="An attendant may only open a shift in their own name.",
        )

    # §6.1: a business_date in the future is always a typo -- trading has not happened yet.
    # Evaluated in the outlet's local timezone, never UTC: at 02:00 IST the UTC date is
    # still yesterday, which would refuse a legitimate shift for five and a half hours
    # every night.
    today = shift_service.outlet_today(settings.TZ_DISPLAY)
    if payload.business_date > today:
        raise AppError(
            status_code=422,
            code="BUSINESS_DATE_IN_FUTURE",
            detail=(
                f"{payload.business_date.isoformat()} has not traded yet "
                f"(today is {today.isoformat()} at this outlet)."
            ),
        )

    membership = db.execute(
        select(OutletMembership).where(
            OutletMembership.user_id == attendant_id,
            OutletMembership.outlet_id == actor.outlet_id,
            OutletMembership.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if membership is None:
        raise AppError(
            status_code=409,
            code="ATTENDANT_NOT_AT_OUTLET",
            detail="That person is not an active member of this outlet.",
        )

    # §5.2: at most one open shift per outlet. This is what keeps §4.7's chain
    # unambiguous -- with two open, "the most recent closing reading" has no single answer.
    already_open = shift_service.open_shift(db, outlet_id=actor.outlet_id)
    if already_open is not None:
        raise AppError(
            status_code=409,
            code="SHIFT_ALREADY_OPEN",
            detail=(
                f"Shift {already_open.sequence} on "
                f"{already_open.business_date.isoformat()} is still open. Close it first."
            ),
        )

    # A new shift must extend the chain, never be spliced into the middle of it. A shift
    # inserted before the tip would leave the shift after it with two possible predecessors
    # and no way to say which reading it carried forward from.
    latest = shift_service.latest_shift(db, outlet_id=actor.outlet_id)
    if latest is not None and payload.business_date < latest.business_date:
        raise AppError(
            status_code=409,
            code="SHIFT_OUT_OF_SEQUENCE",
            detail=(
                f"The most recent shift is on {latest.business_date.isoformat()}. A shift "
                "cannot be inserted before it -- that would break the reading chain."
            ),
        )

    sequence = shift_service.next_sequence(
        db, outlet_id=actor.outlet_id, business_date=payload.business_date
    )

    started_at, ended_at = _resolve_window(
        db,
        outlet_id=actor.outlet_id,
        business_date=payload.business_date,
        sequence=sequence,
        payload=payload,
        previous=latest,
        tz_name=settings.TZ_DISPLAY,
    )

    if ended_at is not None and ended_at <= started_at:
        raise AppError(
            status_code=422,
            code="SHIFT_ENDS_BEFORE_IT_STARTS",
            detail="A shift cannot end at or before the time it started.",
        )

    shift = Shift(
        outlet_id=actor.outlet_id,
        business_date=payload.business_date,
        sequence=sequence,
        started_at=started_at,
        ended_at=ended_at,
        attendant_id=attendant_id,
        status=ShiftStatus.open.value,
        created_by=actor.user.id,
    )
    db.add(shift)
    db.flush()  # no relationship() anywhere, so the id has to be materialised explicitly

    audit.record(
        db,
        outlet_id=actor.outlet_id,
        table_name="shifts",
        record_id=shift.id,
        action=AuditAction.insert,
        changed_by=actor.user.id,
        new_values=_audit_snapshot(shift)
        | {
            "business_date": shift.business_date,
            "sequence": shift.sequence,
            "started_at": shift.started_at,
            "attendant_id": shift.attendant_id,
        },
    )
    # One commit for the shift and its audit row together -- see app/services/audit.py.
    db.commit()
    db.refresh(shift)

    logger.info(
        "shift opened",
        extra={
            "shift_id": str(shift.id),
            "business_date": shift.business_date.isoformat(),
            "sequence": shift.sequence,
            "attendant_id": str(shift.attendant_id),
        },
    )
    return _to_response(shift)


def _resolve_window(
    db: Session,
    *,
    outlet_id: UUID,
    business_date: date,
    sequence: int,
    payload: ShiftCreate,
    previous: Shift | None,
    tz_name: str,
) -> tuple[datetime, datetime | None]:
    """Work out the shift's start and end. Three sources, in descending order of authority.

    1. **The payload.** Whoever is entering the day says what actually happened.
    2. **A shift template for this sequence.** The outlet's usual hours (§5.1).
    3. **The previous shift's end time.** §4.7's chain, applied to the clock rather than
       to the totalizer: a shift that follows another starts when that one finished.

    Source 3 is what makes "add another shift" work at an outlet whose template describes
    only its usual day. This outlet has one template (06:00-22:00); a relief shift tacked
    on after it has no template of its own and should not need one.

    The chosen values are materialised onto the shift row, and the template is read *here
    and nowhere else*. Editing a template later must not revalue a shift that already
    traded, because §6.3 prices a whole shift from its own `started_at`.

    A shift with none of the three is refused rather than defaulted to `now()`. Days are
    typed in after the fact (§4.7), so `now()` is routinely the wrong day entirely, and a
    silently wrong `started_at` picks the wrong fuel rate with no error anywhere.
    """
    template = db.execute(
        select(OutletShiftTemplate).where(
            OutletShiftTemplate.outlet_id == outlet_id,
            OutletShiftTemplate.sequence == sequence,
            OutletShiftTemplate.is_active.is_(True),
        )
    ).scalar_one_or_none()

    default_start: datetime | None = None
    default_end: datetime | None = None
    if template is not None:
        default_start, default_end = shift_service.template_window(
            business_date, template.starts_at_local, template.ends_at_local, tz_name
        )
    elif previous is not None and previous.ended_at is not None:
        # The chain supplies the start; the end is genuinely unknown until the shift is
        # closed, so it stays None rather than being invented.
        default_start = previous.ended_at

    started_at = payload.started_at or default_start
    if started_at is None:
        raise AppError(
            status_code=422,
            code="SHIFT_START_TIME_REQUIRED",
            detail=(
                f"There is no shift template for shift {sequence} at this outlet and no "
                "previous shift to continue from. Provide started_at."
            ),
        )

    if payload.ended_at is not None:
        ended_at = payload.ended_at
    elif default_end is not None and default_end > started_at:
        ended_at = default_end
    else:
        # The template's end time only means anything alongside the template's start. Once
        # the caller overrides the start -- the station opened at 22:00, not 06:00 -- the
        # template's 22:00 end is no longer an end at all. Left unknown until close rather
        # than kept and then refused as "ends before it starts", which would be a confusing
        # error about a field the caller never sent.
        ended_at = None

    if (
        payload.started_at is not None
        and default_start is not None
        and payload.started_at != default_start
    ):
        logger.warning(
            "shift start deviates from the expected time",
            extra={
                "business_date": business_date.isoformat(),
                "sequence": sequence,
                "expected_start": default_start.isoformat(),
                "actual_start": payload.started_at.isoformat(),
            },
        )
    return started_at, ended_at


# Declared ABOVE /shifts/{shift_id}: FastAPI matches routes in declaration order, so with
# these the other way round "current" is parsed as a UUID path parameter and returns 422.
# Same rule the Phase 3 price routers carry.
@router.get("/shifts/current", response_model=ShiftResponse)
def read_current_shift(
    actor: Actor = Depends(require_role(Role.attendant)),
    db: Session = Depends(get_db),
) -> ShiftResponse:
    """The one shift currently open at the caller's outlet, if any."""
    shift = shift_service.open_shift(db, outlet_id=actor.outlet_id)
    if shift is None:
        raise AppError(
            status_code=404,
            code="NO_OPEN_SHIFT",
            detail="No shift is currently open at this outlet.",
        )
    if actor.role is Role.attendant and shift.attendant_id != actor.user.id:
        raise AppError(
            status_code=403,
            code="NOT_YOUR_SHIFT",
            detail="The open shift belongs to another attendant.",
        )
    return _to_response(shift)


@router.get("/shifts", response_model=ShiftPage)
def list_shifts(
    business_date: date | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
    actor: Actor = Depends(require_role(Role.attendant)),
    db: Session = Depends(get_db),
) -> ShiftPage:
    """Shifts at the caller's outlet, newest first.

    §8: an attendant may read their own shift but not all shifts, so the list is filtered
    to theirs. Managers and admins see everything at the outlet.
    """
    statement = (
        select(Shift)
        .where(Shift.outlet_id == actor.outlet_id)
        .order_by(Shift.business_date.desc(), Shift.sequence.desc())
    )
    if actor.role is Role.attendant:
        statement = statement.where(Shift.attendant_id == actor.user.id)
    if business_date is not None:
        statement = statement.where(Shift.business_date == business_date)

    if cursor is not None:
        last_date, last_sequence = decode_shift_cursor(cursor)
        # Keyset, never OFFSET (§9, §14): a shift inserted mid-walk would otherwise shift
        # every later row down one and the reader would see a duplicate and miss one.
        statement = statement.where(
            tuple_(Shift.business_date, Shift.sequence) < tuple_(last_date, last_sequence)
        )

    # limit + 1 as a sentinel, so "is there another page" needs no second COUNT query.
    rows = db.execute(statement.limit(limit + 1)).scalars().all()
    has_more = len(rows) > limit
    page = rows[:limit]

    next_cursor = (
        encode_shift_cursor(page[-1].business_date, page[-1].sequence)
        if has_more and page
        else None
    )
    return ShiftPage(items=[_to_response(s) for s in page], next_cursor=next_cursor)


@router.get("/shifts/{shift_id}", response_model=ShiftResponse)
def read_shift(
    access: ShiftAccess = Depends(require_shift_access(Role.attendant)),
) -> ShiftResponse:
    """One shift. Attendants may read only their own (§8, enforced in the dependency)."""
    return _to_response(access.shift)


@router.patch("/shifts/{shift_id}/close", response_model=ShiftResponse)
def close_shift(
    payload: ShiftClose,
    access: ShiftAccess = Depends(require_shift_access(Role.manager)),
    db: Session = Depends(get_db),
) -> ShiftResponse:
    """Close a shift. Manager floor (§8)."""
    shift = access.shift
    _guard_transition(shift, ShiftStatus.closed)
    before = _audit_snapshot(shift)

    ended_at = payload.ended_at or shift.ended_at
    if ended_at is None:
        raise AppError(
            status_code=422,
            code="SHIFT_END_TIME_REQUIRED",
            detail=(
                "This shift has no end time and its template did not supply one. "
                "Provide ended_at."
            ),
        )
    if ended_at <= shift.started_at:
        raise AppError(
            status_code=422,
            code="SHIFT_ENDS_BEFORE_IT_STARTS",
            detail="A shift cannot end at or before the time it started.",
        )

    # The end time is applied before the preconditions run, because §6.2's flow-rate
    # ceiling is measured against the shift's duration and the value being set right now is
    # what that duration is. Safe to mutate before the checks: nothing is committed until
    # the end of this function, and `get_db` closes the session -- rolling back -- if any
    # of them raises.
    shift.ended_at = ended_at

    # ---------------------------------------------------------------------
    # §6.8's close preconditions. Each lands with the phase that owns its table:
    #   MISSING_NOZZLE_READINGS      -- Phase 5, below
    #   MISSING_COLLECTIONS          -- Phase 6, below
    #   CREDIT_SALE_MISSING_RECEIPT  -- Phase 9, below
    # All three now exist. None of them was ever stubbed ahead of its table, per §11's rule
    # against scaffolding: an empty check that always passes is indistinguishable from a
    # check that was forgotten.
    # ---------------------------------------------------------------------
    missing = reading_service.missing_closing_readings(db, shift=shift)
    if missing:
        raise AppError(
            status_code=409,
            code="MISSING_NOZZLE_READINGS",
            detail=(
                "These nozzles have no usable closing reading yet: "
                f"{', '.join(missing)}. A shift cannot be closed until every meter is "
                "accounted for -- an unread nozzle is fuel that left the tank with no "
                "sale against it."
            ),
        )

    # §6.2's sanity ceiling, re-run now that `ended_at` is known. A reading entered while
    # the shift had no end time skipped it; this is where that gap closes.
    reading_service.revalidate_flow_rates(db, shift=shift)

    # §6.8, Phase 6. Ordered after MISSING_NOZZLE_READINGS because an unread meter is the
    # more fundamental failure: until the meters are in, there is no figure for the cash to
    # be reconciled against.
    #
    # **This check fires on absence, never on a mismatch.** It does not compare collections
    # against sales and must never be changed to. A shift whose collections total ₹40,000
    # against ₹95,000 of metered fuel closes normally -- udhaar issued during the shift
    # accounts for part of that gap and the rest is the variance §6.4 exists to record.
    # Refusing to close until the two agree would leave the salesman in front of a form
    # with exactly one freely adjustable field, and he would type whatever balanced it. The
    # result would be a perfectly reconciled system that reports nothing.
    #
    # An explicit ₹0 satisfies it, which is the whole point: on a genuinely cashless day
    # the salesman declares zero, and that is a different fact from having entered nothing.
    if collection_service.missing_cash_declaration(db, shift=shift):
        raise AppError(
            status_code=409,
            code="MISSING_COLLECTIONS",
            detail=(
                "Fuel went through the meters on this shift but no cash figure has been "
                "recorded. Enter the cash taken -- and enter 0 if no cash was taken, so "
                "that a cashless day is on the record as an answer rather than a blank."
            ),
        )

    # §6.8, Phase 9 -- the last of the three, and the only one that cannot fire through this
    # application. `credit_sales.attachment_id` is NOT NULL (§6.6) and every write path calls
    # `attachment_service.link()`, which stamps `linked_at`, so a row created through the API
    # satisfies this by construction.
    #
    # Kept anyway, and the reasoning is worth stating because it looks like dead code and is
    # not. A row written *outside* the API -- a fixture, a data migration, the bulk import of
    # the paper register this outlet will eventually want -- can carry an attachment that was
    # never linked, and §6.8 names this precondition explicitly. It is the same argument
    # `_CONSTRAINT_ERRORS` makes for constraints the API refuses first: belt and braces, at
    # the cost of one indexed query per close.
    #
    # Contrast the two `INSUFFICIENT_ROLE` branches Phase 8 deleted as genuinely dead: those
    # could not be reached by *any* caller through any path, because `attendant` is already
    # the role floor. This one has a caller; it just is not an HTTP request.
    unreceipted = credit_service.sales_missing_receipt(db, shift=shift)
    if unreceipted:
        raise AppError(
            status_code=409,
            code="CREDIT_SALE_MISSING_RECEIPT",
            detail=(
                f"{len(unreceipted)} credit sale(s) on this shift point at a receipt that "
                "was never linked. Udhaar without a confirmed receipt is a debt with "
                "nothing behind it -- the slip has to be on the record before the day can "
                "be closed."
            ),
        )

    shift.status = ShiftStatus.closed.value
    shift.closed_by = access.actor.user.id
    shift.closed_at = datetime.now(tz=timezone.utc)

    audit.record(
        db,
        outlet_id=shift.outlet_id,
        table_name="shifts",
        record_id=shift.id,
        action=AuditAction.status_change,
        changed_by=access.actor.user.id,
        old_values=before,
        new_values=_audit_snapshot(shift),
    )
    db.commit()
    db.refresh(shift)

    logger.info("shift closed", extra={"shift_id": str(shift.id)})
    return _to_response(shift)


@router.patch("/shifts/{shift_id}/lock", response_model=ShiftResponse)
def lock_shift(
    access: ShiftAccess = Depends(require_shift_access(Role.admin)),
    db: Session = Depends(get_db),
) -> ShiftResponse:
    """Lock a shift. Admin only (§8). Final -- nothing reopens a locked shift."""
    shift = access.shift
    _guard_transition(shift, ShiftStatus.locked)
    before = _audit_snapshot(shift)

    # §6.7, Phase 7. A flagged row can belong to *this* shift even though the aggregate
    # rule that set it may have summed a category across every shift on the business
    # date -- app/services/expenses.py::apply_review_flags flags every live row in the
    # group, in whichever shift each one happens to sit. The check here stays
    # shift-scoped: locking is a per-shift action, and a row flagged on a *different*
    # shift blocks that other shift's lock, not this one's.
    flagged = expense_service.unreviewed_flagged_expenses(db, shift=shift)
    if flagged:
        codes = expense_service.category_codes(db, flagged)
        named = ", ".join(
            f"{codes[expense.category_id]} {expense.amount}" for expense in flagged
        )
        raise AppError(
            status_code=409,
            code="UNREVIEWED_EXPENSES_EXIST",
            detail=(
                f"This shift has {len(flagged)} flagged expense(s) still awaiting "
                f"review: {named}. A shift cannot be locked until every flagged expense "
                "has been signed off."
            ),
        )

    shift.status = ShiftStatus.locked.value
    shift.locked_by = access.actor.user.id
    shift.locked_at = datetime.now(tz=timezone.utc)

    audit.record(
        db,
        outlet_id=shift.outlet_id,
        table_name="shifts",
        record_id=shift.id,
        action=AuditAction.status_change,
        changed_by=access.actor.user.id,
        old_values=before,
        new_values=_audit_snapshot(shift),
    )
    db.commit()
    db.refresh(shift)

    logger.info("shift locked", extra={"shift_id": str(shift.id)})
    return _to_response(shift)


@router.patch("/shifts/{shift_id}/reopen", response_model=ShiftResponse)
def reopen_shift(
    payload: ShiftReopen,
    access: ShiftAccess = Depends(require_shift_access(Role.admin)),
    db: Session = Depends(get_db),
) -> ShiftResponse:
    """Reopen a closed shift. Admin only, reason mandatory, audit-logged (§5.2, §6.8).

    **Mid-chain reopening is allowed as of Phase 5, and nothing is recomputed** (§13.10).
    Phase 4 refused it outright, because §4.7's chain carries a nozzle's closing reading
    forward into the next shift's opening and a mid-chain change leaves that stored opening
    stale. §13.10 originally said Phase 5 would lift the restriction by implementing a
    recomputing cascade. It does not, because it cannot: §4.7 stores the chained opening on
    the row *precisely* so that correcting one shift cannot silently rewrite the next
    shift's history, and a recomputing cascade is that rewrite -- one that would leave the
    rewritten figure looking exactly like a reading somebody had confirmed against a meter.

    So the restriction is lifted the other way. If a reading on this shift is subsequently
    changed, `app/api/v1/readings.py` leaves the following shift's opening exactly as it is
    and flags it for review with a note naming this shift. A human reconciles two numbers
    they can both see. Nothing is invented and nothing is overwritten.
    """
    shift = access.shift
    _guard_transition(shift, ShiftStatus.open)

    before = _audit_snapshot(shift)
    shift.status = ShiftStatus.open.value
    shift.closed_by = None
    shift.closed_at = None

    audit.record(
        db,
        outlet_id=shift.outlet_id,
        table_name="shifts",
        record_id=shift.id,
        action=AuditAction.status_change,
        changed_by=access.actor.user.id,
        old_values=before,
        # The reason lives in the audit row rather than on the shift: a column would hold
        # only the most recent one, and a shift reopened three times is exactly the case
        # somebody will need to reconstruct.
        new_values=_audit_snapshot(shift) | {"reason": payload.reason.strip()},
    )
    db.commit()
    db.refresh(shift)

    logger.warning(
        "shift reopened",
        extra={
            "shift_id": str(shift.id),
            "reopened_by": str(access.actor.user.id),
            "reason": payload.reason.strip(),
        },
    )
    return _to_response(shift)
