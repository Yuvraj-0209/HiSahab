"""Nozzle readings -- where the meter becomes money (CLAUDE.md §6.2, §6.3, §4.7).

Six routes. Five of them exist to get three numbers per nozzle onto a row correctly, and
the sixth turns those rows into rupees.

**The shape of the workflow (§4.7).** The day is typed in after the fact, in one sitting,
from a paper register. So `GET .../readings` hands back a worksheet with every opening
already filled in from the chain, and the attendant types only closing values. On a normal
day that is the entire interaction and no opening reading is ever keyed.

**Pre-filled is not assumed.** `opening_confirmed` is mandatory, because a carried opening
that nobody checked against the physical meter is the failure §4.7 spends four paragraphs
on: fuel siphoned between shifts still turns the totalizer, an assumed opening says it did
not, the quantity is absorbed into the next shift as sales that produced no cash, and this
outlet books the resulting shortfall as udhaar against the salesman's own name. A confirm
step costs one boolean and stops theft becoming an innocent person's debt.

**Role floors (§8).** Attendants enter readings on their own shift (ownership enforced by
`require_shift_access`). Managers review flags and read the sales figures. Admins anchor a
new meter and enter a manual quantity after a reset.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, condecimal
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import ShiftAccess, require_shift_access
from app.core.audit import AuditAction
from app.core.errors import AppError
from app.core.roles import Role
from app.core.shifts import ShiftStatus
from app.core.units import UnitOfMeasure
from app.db.session import get_db
from app.models.fuel import FuelType
from app.models.nozzle import Nozzle
from app.models.reading import NozzleReading
from app.models.shift import Shift
from app.services import audit, readings as reading_service, sales

logger = logging.getLogger(__name__)

router = APIRouter(tags=["readings"])


# --- schemas -----------------------------------------------------------------

# §3 rule 1 / rule 2: money and readings are NUMERIC(12,2); quantities NUMERIC(10,3).
# Declared as condecimal so a client sending 8 decimal places is a 422, not a silent
# round -- and so a float never enters the system at the boundary.
ReadingValue = condecimal(max_digits=12, decimal_places=2, ge=0)
QuantityValue = condecimal(max_digits=10, decimal_places=3, ge=0)


class ReadingResponse(BaseModel):
    id: UUID
    shift_id: UUID
    nozzle_id: UUID
    nozzle_label: str
    fuel_type_code: str
    # §4.5: echoed from the fuel type so no client ever has to guess whether this
    # totalizer counts litres or kilograms.
    unit_of_measure: UnitOfMeasure
    opening_reading: Decimal
    chained_opening_reading: Decimal | None
    opening_variance_reason: str | None
    closing_reading: Decimal | None
    testing_quantity: Decimal
    rollover_occurred: bool
    meter_reset_occurred: bool
    manual_quantity_override: Decimal | None
    override_reason: str | None
    requires_review: bool
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    review_note: str | None
    # Computed, never stored (§3 rule 8): sales are derived from readings and historical
    # prices server-side, always. None until there is a closing reading to derive from.
    quantity_sold: Decimal | None


class WorksheetLine(BaseModel):
    """One row of the entry sheet: a nozzle, what it carries in, and what is saved so far."""

    nozzle_id: UUID
    nozzle_label: str
    dispenser_label: str
    fuel_type_code: str
    unit_of_measure: UnitOfMeasure
    totalizer_max_value: Decimal
    # What the chain says this nozzle opens at (§4.7). None = no predecessor, so this
    # nozzle needs an admin to anchor it.
    chained_opening_reading: Decimal | None
    requires_anchor: bool
    reading: ReadingResponse | None


class Worksheet(BaseModel):
    shift_id: UUID
    shift_status: ShiftStatus
    lines: list[WorksheetLine]


class ReadingCreate(BaseModel):
    """Recording one nozzle for one shift.

    `opening_reading` is optional **and that is the normal case** -- omit it and the chained
    value is used. Supply it only to correct the chain (which then requires a reason and
    raises a review flag) or to anchor a nozzle with no history (admin only).
    """

    model_config = ConfigDict(extra="forbid")

    nozzle_id: UUID
    # §4.7's confirm-don't-assume rule, as one required boolean. Not defaulted to True:
    # a default would make "I checked the meter" the thing that happens when a client
    # forgets to ask, which is exactly the assumption this field exists to prevent.
    opening_confirmed: bool
    opening_reading: ReadingValue | None = None
    opening_variance_reason: str | None = Field(default=None, max_length=500)
    closing_reading: ReadingValue | None = None
    # §4.2: never defaulted away silently. 0 is a real answer -- CBG is not
    # calibration-tested here (§4.5) -- but it must be an answer, not an omission.
    testing_quantity: QuantityValue = Decimal("0")
    rollover_occurred: bool = False
    meter_reset_occurred: bool = False


class ReadingUpdate(BaseModel):
    """Correcting a reading while its shift is still open.

    `nozzle_id` and `opening_reading` are absent. The nozzle identifies the row, and the
    opening is §4.7's chained-and-confirmed value -- changing it after the fact would
    rewrite what somebody confirmed against a physical meter. A wrong opening is a wrong
    *chain*, fixed at its source and then reviewed downstream, not edited here.
    """

    model_config = ConfigDict(extra="forbid")

    closing_reading: ReadingValue | None = None
    testing_quantity: QuantityValue | None = None
    rollover_occurred: bool | None = None
    meter_reset_occurred: bool | None = None


class ReadingOverride(BaseModel):
    """§6.2's meter-reset escape hatch. Admin only, reason mandatory."""

    model_config = ConfigDict(extra="forbid")

    manual_quantity_override: QuantityValue
    # min_length on the schema so an empty string is a 422 rather than an audit row that
    # records nothing. Same rule as ShiftReopen.reason.
    override_reason: str = Field(min_length=3, max_length=500)


class ReadingReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_note: str = Field(min_length=3, max_length=500)


class SalesLineResponse(BaseModel):
    nozzle_id: UUID
    nozzle_label: str
    dispenser_label: str
    fuel_type_code: str
    unit_of_measure: UnitOfMeasure
    quantity_sold: Decimal | None
    rate_per_unit: Decimal | None
    sale_value: Decimal | None
    margin_per_unit: Decimal | None
    # Named `gross_fuel_margin`, never `profit` (§13.7). See the note on ShiftSales.
    gross_fuel_margin: Decimal | None


class ShiftSales(BaseModel):
    shift_id: UUID
    business_date: str
    # The instant every line was priced at, stated rather than implied, so a reader can
    # see which rate revision applied without inferring it (§6.3, §13.1).
    priced_at: datetime
    lines: list[SalesLineResponse]
    total_sale_value: Decimal
    total_gross_fuel_margin: Decimal
    # §4.5: quantities are keyed by unit and never summed across units. A litre of petrol
    # and a kilogram of CBG are not addable, and a single "total quantity" figure would be
    # a number with no meaning at all.
    quantity_by_unit: dict[str, Decimal]
    # §13.7 requires this figure to be labelled wherever it is displayed. Carried in the
    # payload rather than left to the frontend, because a mobile client hitting the same
    # endpoint (§2) must not be able to render an unlabelled "profit".
    margin_basis: str
    incomplete: bool


_MARGIN_BASIS = (
    "Gross dealer margin on quantity sold. Excludes stock revaluation: fuel held when the "
    "price moves gains or loses real money that never passes a nozzle and is not counted "
    "here. Excludes non-fuel income and the IOCL ledger. This is not business profit "
    "(CLAUDE.md §13.7, §13.9)."
)


# --- helpers -----------------------------------------------------------------


def _load_nozzle(db: Session, nozzle_id: UUID, shift: Shift) -> tuple[Nozzle, FuelType]:
    """Fetch a nozzle and refuse it if it cannot belong to this shift.

    The outlet check is the one that matters. In V1 there is one outlet so it can never
    fire -- which is exactly why it is written now: the day a second outlet exists, a
    reading pointed at the wrong outlet's meter would value a shift off a stranger's
    totalizer, and nothing else in the system would notice.
    """
    row = db.execute(
        select(Nozzle, FuelType)
        .join(FuelType, FuelType.id == Nozzle.fuel_type_id)
        .where(Nozzle.id == nozzle_id)
    ).first()
    if row is None:
        raise AppError(
            status_code=404, code="NOZZLE_NOT_FOUND", detail="No nozzle with that id."
        )

    nozzle, fuel_type = row
    if nozzle.outlet_id != shift.outlet_id:
        raise AppError(
            status_code=409,
            code="NOZZLE_NOT_AT_OUTLET",
            detail="That nozzle belongs to a different outlet.",
        )
    if not nozzle.is_active:
        raise AppError(
            status_code=409,
            code="NOZZLE_INACTIVE",
            detail=f"Nozzle {nozzle.label} has been decommissioned.",
        )

    window_end = shift.ended_at or shift.started_at
    if nozzle.meter_installed_at > window_end:
        raise AppError(
            status_code=409,
            code="NOZZLE_NOT_YET_INSTALLED",
            detail=(
                f"Nozzle {nozzle.label} was installed on "
                f"{nozzle.meter_installed_at.date().isoformat()}, after this shift."
            ),
        )
    return nozzle, fuel_type


def _validate_math(reading: NozzleReading, nozzle: Nozzle, fuel_type: FuelType,
                   shift: Shift) -> Decimal | None:
    """Run every §6.2 guard against a row before it is written.

    Called *before* `db.add()` / `db.commit()`, so a rejected reading leaves no trace. §10
    requires the `TOTALIZER_DECREASED` case to write no row, and this ordering is what
    makes that true for all of the guards at once rather than one at a time.
    """
    quantity = reading_service.quantity_if_known(reading, nozzle)
    if quantity is None:
        # Either no closing reading yet, or a meter reset awaiting an admin's figure. Both
        # are legitimate states for a row that is still being entered, and neither is a
        # reason to refuse the write -- §6.8's close precondition is what stops a shift
        # being finalised while a quantity is still unknown.
        return None

    # §6.2's sanity ceiling needs a shift duration, and `ended_at` is nullable until close.
    # When it is unknown the ceiling is skipped here and re-run at close, where Phase 4's
    # SHIFT_END_TIME_REQUIRED guarantees an end time -- so a mistyped extra digit is caught
    # at entry in the normal case and at close in every case. Refusing entry outright for a
    # missing `ended_at` would be an error about a field the caller never sent.
    if shift.ended_at is None:
        logger.info(
            "flow-rate ceiling deferred to close; shift has no end time yet",
            extra={"shift_id": str(shift.id), "nozzle_label": nozzle.label},
        )
        return quantity

    # An admin override is exempt, matching `readings_service.revalidate_flow_rates` --
    # one rule about overrides, applied at both sites that check the ceiling. Holding a
    # human's signed figure against a mechanical flow rate would refuse the one path that
    # exists for when the meter itself lied, which is precisely when an override is
    # entered. Phase 6 Step 0: the two sites previously disagreed, so such a row was
    # refused here and then never re-checked at close.
    if (
        reading.closing_reading is not None
        and not reading.meter_reset_occurred
        and reading.manual_quantity_override is None
    ):
        gross = sales.gross_throughput(
            opening_reading=reading.opening_reading,
            closing_reading=reading.closing_reading,
            rollover_occurred=reading.rollover_occurred,
            totalizer_max_value=nozzle.totalizer_max_value,
            nozzle_label=nozzle.label,
        )
        sales.check_flow_rate_ceiling(
            gross_quantity=gross,
            # §4.5 / §14: the fuel's OWN ceiling. MAX_FLOW_RATE_LPM from config seeds this
            # column in migration 0003 and must never be read here -- doing so would
            # reimpose the single global litres-per-minute figure §4.5 rules out, and it
            # would reject every real CBG sale.
            max_flow_rate_per_minute=fuel_type.max_flow_rate_per_minute,
            started_at=shift.started_at,
            ended_at=shift.ended_at,
            nozzle_label=nozzle.label,
            unit_of_measure=fuel_type.unit_of_measure,
        )
    return quantity


def _to_response(
    reading: NozzleReading, nozzle: Nozzle, fuel_type: FuelType, quantity: Decimal | None
) -> ReadingResponse:
    return ReadingResponse(
        id=reading.id,
        shift_id=reading.shift_id,
        nozzle_id=reading.nozzle_id,
        nozzle_label=nozzle.label,
        fuel_type_code=fuel_type.code,
        unit_of_measure=UnitOfMeasure(fuel_type.unit_of_measure),
        opening_reading=reading.opening_reading,
        chained_opening_reading=reading.chained_opening_reading,
        opening_variance_reason=reading.opening_variance_reason,
        closing_reading=reading.closing_reading,
        testing_quantity=reading.testing_quantity,
        rollover_occurred=reading.rollover_occurred,
        meter_reset_occurred=reading.meter_reset_occurred,
        manual_quantity_override=reading.manual_quantity_override,
        override_reason=reading.override_reason,
        requires_review=reading.requires_review,
        reviewed_by=reading.reviewed_by,
        reviewed_at=reading.reviewed_at,
        review_note=reading.review_note,
        quantity_sold=quantity,
    )


def _audit_snapshot(reading: NozzleReading) -> dict[str, object]:
    """The fields worth recording either side of a change.

    Everything that feeds §6.2's arithmetic, plus the review state. Not the whole row: an
    audit entry that echoes every column makes the change itself hard to find.
    """
    return {
        "opening_reading": reading.opening_reading,
        "chained_opening_reading": reading.chained_opening_reading,
        "closing_reading": reading.closing_reading,
        "testing_quantity": reading.testing_quantity,
        "rollover_occurred": reading.rollover_occurred,
        "meter_reset_occurred": reading.meter_reset_occurred,
        "manual_quantity_override": reading.manual_quantity_override,
        "requires_review": reading.requires_review,
    }


def _load_reading(db: Session, shift_id: UUID, nozzle_id: UUID) -> NozzleReading:
    reading = db.execute(
        select(NozzleReading).where(
            NozzleReading.shift_id == shift_id,
            NozzleReading.nozzle_id == nozzle_id,
        )
    ).scalar_one_or_none()
    if reading is None:
        raise AppError(
            status_code=404,
            code="READING_NOT_FOUND",
            detail="No reading has been recorded for that nozzle on this shift.",
        )
    return reading


# --- routes ------------------------------------------------------------------


@router.get("/shifts/{shift_id}/readings", response_model=Worksheet)
def read_worksheet(
    access: ShiftAccess = Depends(require_shift_access(Role.attendant)),
    db: Session = Depends(get_db),
) -> Worksheet:
    """The entry sheet for a shift, with every opening pre-filled from the chain (§4.7).

    This route is what makes "zero typing on a normal day" true. It is a read, so
    `writable` is False -- the sheet for a closed shift is still worth looking at.
    """
    shift = access.shift
    saved = reading_service.readings_for_shift(db, shift_id=shift.id)

    lines: list[WorksheetLine] = []
    for nozzle, fuel_type in reading_service.nozzles_in_scope(db, shift=shift):
        reading = saved.get(nozzle.id)
        chained = reading_service.chained_opening(db, nozzle_id=nozzle.id, shift=shift)
        lines.append(
            WorksheetLine(
                nozzle_id=nozzle.id,
                nozzle_label=nozzle.label,
                dispenser_label=nozzle.dispenser_label,
                fuel_type_code=fuel_type.code,
                unit_of_measure=UnitOfMeasure(fuel_type.unit_of_measure),
                totalizer_max_value=nozzle.totalizer_max_value,
                chained_opening_reading=chained,
                # A nozzle with no history and no reading yet needs an admin to state
                # where its meter started (§4.7). Surfaced here so the frontend can ask
                # for the right thing instead of letting an attendant hit a 403.
                requires_anchor=chained is None and reading is None,
                reading=(
                    None
                    if reading is None
                    else _to_response(
                        reading,
                        nozzle,
                        fuel_type,
                        _displayable_quantity(reading, nozzle),
                    )
                ),
            )
        )

    return Worksheet(
        shift_id=shift.id, shift_status=ShiftStatus(shift.status), lines=lines
    )


def _displayable_quantity(reading: NozzleReading, nozzle: Nozzle) -> Decimal | None:
    """§6.2's quantity for the worksheet, or None if the row cannot yield one.

    The **only** place in this module where a §6.2 failure is swallowed rather than
    returned. A saved row can reach an uncomputable state exactly one way -- a meter reset
    awaiting its admin override -- and the worksheet is precisely where somebody goes to
    see and fix that. A 422 here would hide the whole sheet, including the line explaining
    the problem. Every write path still validates and still refuses.
    """
    try:
        return reading_service.quantity_if_known(reading, nozzle)
    except AppError:
        return None


@router.post("/shifts/{shift_id}/readings", response_model=ReadingResponse, status_code=201)
def create_reading(
    payload: ReadingCreate,
    access: ShiftAccess = Depends(require_shift_access(Role.attendant, writable=True)),
    db: Session = Depends(get_db),
) -> ReadingResponse:
    """Record one nozzle for this shift.

    `writable=True` enforces §6.9 -- no writes to a closed or locked shift -- without this
    module re-implementing any part of that rule.
    """
    shift = access.shift
    actor = access.actor
    nozzle, fuel_type = _load_nozzle(db, payload.nozzle_id, shift)

    existing = db.execute(
        select(NozzleReading.id).where(
            NozzleReading.shift_id == shift.id,
            NozzleReading.nozzle_id == nozzle.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        # §6.10: this is also what makes readings idempotent without an Idempotency-Key
        # store. A retry after a timeout cannot create a second row; the client PATCHes.
        raise AppError(
            status_code=409,
            code="READING_ALREADY_EXISTS",
            detail=(
                f"Nozzle {nozzle.label} already has a reading on this shift. "
                "Use PATCH to correct it."
            ),
        )

    if not payload.opening_confirmed:
        raise AppError(
            status_code=422,
            code="OPENING_NOT_CONFIRMED",
            detail=(
                f"Check nozzle {nozzle.label}'s meter against the opening reading shown "
                "and confirm it. An unconfirmed opening hides fuel that moved between "
                "shifts."
            ),
        )

    chained = reading_service.chained_opening(db, nozzle_id=nozzle.id, shift=shift)
    opening, variance_reason, requires_review = _resolve_opening(
        payload=payload, chained=chained, nozzle=nozzle, actor_role=actor.role
    )

    reading = NozzleReading(
        shift_id=shift.id,
        nozzle_id=nozzle.id,
        opening_reading=opening,
        chained_opening_reading=chained,
        opening_variance_reason=variance_reason,
        closing_reading=payload.closing_reading,
        testing_quantity=payload.testing_quantity,
        rollover_occurred=payload.rollover_occurred,
        meter_reset_occurred=payload.meter_reset_occurred,
        requires_review=requires_review,
        created_by=actor.user.id,
    )

    # Before db.add(), so a refusal leaves nothing behind (§10).
    quantity = _validate_math(reading, nozzle, fuel_type, shift)

    db.add(reading)
    db.flush()  # no relationship() anywhere, so the id has to be materialised explicitly

    audit.record(
        db,
        outlet_id=shift.outlet_id,
        table_name="nozzle_readings",
        record_id=reading.id,
        action=AuditAction.insert,
        changed_by=actor.user.id,
        new_values=_audit_snapshot(reading)
        | {
            "shift_id": reading.shift_id,
            "nozzle_id": reading.nozzle_id,
            "nozzle_label": nozzle.label,
            "opening_variance_reason": reading.opening_variance_reason,
        },
    )
    db.commit()
    db.refresh(reading)

    if requires_review:
        logger.warning(
            "opening reading did not match the chain",
            extra={
                "reading_id": str(reading.id),
                "nozzle_label": nozzle.label,
                "chained": str(chained),
                "confirmed": str(opening),
            },
        )
    return _to_response(reading, nozzle, fuel_type, quantity)


def _resolve_opening(
    *, payload: ReadingCreate, chained: Decimal | None, nozzle: Nozzle, actor_role: Role
) -> tuple[Decimal, str | None, bool]:
    """Decide the opening reading, and whether it needs review (§4.7).

    Three cases:

    **Anchor** (`chained is None`) -- no predecessor, so there is nothing to carry. §4.7
    makes this admin-only, because the whole chain from here on is built on this one
    unverifiable number, and a wrong anchor silently mis-states the very first shift.

    **Confirmed match** -- the meter reads what the chain predicted. The normal day, and
    no opening value is sent at all.

    **Mismatch** -- the meter does *not* read what the chain predicted. Both values are
    kept, a reason is mandatory, and the row is flagged for review. Nothing is blamed on
    anybody and nothing is overwritten; a human is asked to look. That is the entire
    substance of §4.7's warning about theft becoming an innocent person's debt.
    """
    if chained is None:
        if payload.opening_reading is None:
            raise AppError(
                status_code=422,
                code="ANCHOR_READING_REQUIRED",
                detail=(
                    f"Nozzle {nozzle.label} has no previous closing reading to carry "
                    "forward. Its starting meter value must be entered once, by an admin."
                ),
            )
        if actor_role is not Role.admin:
            raise AppError(
                status_code=403,
                code="ANCHOR_REQUIRES_ADMIN",
                detail=(
                    f"Nozzle {nozzle.label} has never been read. Only an admin may set a "
                    "meter's starting value, because every later reading is measured "
                    "from it."
                ),
            )
        return payload.opening_reading, None, False

    if payload.opening_reading is None or payload.opening_reading == chained:
        return chained, None, False

    reason = (payload.opening_variance_reason or "").strip()
    if not reason:
        raise AppError(
            status_code=422,
            code="OPENING_VARIANCE_REASON_REQUIRED",
            detail=(
                f"Nozzle {nozzle.label}'s meter reads {payload.opening_reading} but the "
                f"previous shift closed it at {chained}. Fuel may have moved between "
                "shifts. Record what the meter actually shows and say why it differs -- "
                "this will be raised for review, not charged to anyone."
            ),
        )
    return payload.opening_reading, reason, True


@router.patch(
    "/shifts/{shift_id}/readings/{nozzle_id}", response_model=ReadingResponse
)
def update_reading(
    nozzle_id: UUID,
    payload: ReadingUpdate,
    access: ShiftAccess = Depends(require_shift_access(Role.attendant, writable=True)),
    db: Session = Depends(get_db),
) -> ReadingResponse:
    """Correct a reading while its shift is open."""
    shift = access.shift
    actor = access.actor
    nozzle, fuel_type = _load_nozzle(db, nozzle_id, shift)
    reading = _load_reading(db, shift.id, nozzle_id)

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise AppError(
            status_code=422,
            code="NO_FIELDS_TO_UPDATE",
            detail="Provide at least one field to change.",
        )

    before = _audit_snapshot(reading)
    previous_closing = reading.closing_reading

    for field, value in changes.items():
        if value is None:
            continue
        setattr(reading, field, value)

    quantity = _validate_math(reading, nozzle, fuel_type, shift)

    # §13.10. The chain carries a *closing* reading forward, so only a change to that value
    # can leave a later shift's stored opening stale. Testing quantity and the flags change
    # what was sold, not what the meter read, and the next shift is unaffected by them.
    downstream_flagged = False
    if reading.closing_reading != previous_closing:
        flagged = reading_service.flag_downstream_reading(
            db,
            shift=shift,
            nozzle_id=nozzle_id,
            note=(
                f"Opening may be stale: shift {shift.sequence} on "
                f"{shift.business_date.isoformat()} had its closing reading for "
                f"{nozzle.label} changed from {previous_closing} to "
                f"{reading.closing_reading} after this shift was recorded. The opening "
                "here has deliberately NOT been changed -- confirm which value is right."
            ),
        )
        downstream_flagged = flagged is not None
        if downstream_flagged:
            audit.record(
                db,
                outlet_id=shift.outlet_id,
                table_name="nozzle_readings",
                record_id=flagged.id,
                action=AuditAction.update,
                changed_by=actor.user.id,
                new_values={
                    "requires_review": True,
                    "reason": "upstream closing reading changed",
                },
            )

    audit.record(
        db,
        outlet_id=shift.outlet_id,
        table_name="nozzle_readings",
        record_id=reading.id,
        action=AuditAction.update,
        changed_by=actor.user.id,
        old_values=before,
        new_values=_audit_snapshot(reading),
    )
    db.commit()
    db.refresh(reading)

    logger.info(
        "reading updated",
        extra={
            "reading_id": str(reading.id),
            "fields": sorted(changes),
            "downstream_flagged": downstream_flagged,
        },
    )
    return _to_response(reading, nozzle, fuel_type, quantity)


@router.post(
    "/shifts/{shift_id}/readings/{nozzle_id}/override", response_model=ReadingResponse
)
def override_quantity(
    nozzle_id: UUID,
    payload: ReadingOverride,
    access: ShiftAccess = Depends(require_shift_access(Role.admin, writable=True)),
    db: Session = Depends(get_db),
) -> ReadingResponse:
    """Enter a quantity by hand after a meter reset. Admin only (§6.2, §8).

    A separate route rather than two more fields on PATCH, so the admin gate is visible in
    the URL and in the router rather than buried in a per-field role check. §13.3 records
    this as a deliberate approximation: the split between "before the reset" and "after"
    cannot be inferred from two readings, so a human states the figure and signs for it.
    """
    shift = access.shift
    actor = access.actor
    nozzle, fuel_type = _load_nozzle(db, nozzle_id, shift)
    reading = _load_reading(db, shift.id, nozzle_id)

    before = _audit_snapshot(reading)
    reading.manual_quantity_override = payload.manual_quantity_override
    reading.override_reason = payload.override_reason.strip()

    quantity = _validate_math(reading, nozzle, fuel_type, shift)

    # No downstream flag here, deliberately. An override changes the quantity sold, not the
    # closing reading, and it is the closing reading that the next shift carries forward --
    # so nothing downstream has gone stale.
    audit.record(
        db,
        outlet_id=shift.outlet_id,
        table_name="nozzle_readings",
        record_id=reading.id,
        action=AuditAction.update,
        changed_by=actor.user.id,
        old_values=before,
        new_values=_audit_snapshot(reading) | {"override_reason": reading.override_reason},
    )
    db.commit()
    db.refresh(reading)

    logger.warning(
        "manual quantity override entered",
        extra={
            "reading_id": str(reading.id),
            "nozzle_label": nozzle.label,
            "quantity": str(payload.manual_quantity_override),
            "reason": reading.override_reason,
            "entered_by": str(actor.user.id),
        },
    )
    return _to_response(reading, nozzle, fuel_type, quantity)


@router.patch(
    "/shifts/{shift_id}/readings/{nozzle_id}/review", response_model=ReadingResponse
)
def review_reading(
    nozzle_id: UUID,
    payload: ReadingReview,
    access: ShiftAccess = Depends(require_shift_access(Role.manager)),
    db: Session = Depends(get_db),
) -> ReadingResponse:
    """Sign off a reading flagged for review. Manager floor (§8).

    **Deliberately not `writable=True`.** Review is the one thing that must still work on a
    *closed* shift -- a mismatch is usually noticed while reconciling, which happens after
    close. A locked shift is refused below, because §5.2 is absolute that nothing
    referencing a locked shift may be modified.
    """
    shift = access.shift
    actor = access.actor
    if ShiftStatus(shift.status) is ShiftStatus.locked:
        raise AppError(
            status_code=409,
            code="SHIFT_LOCKED",
            detail=(
                "This shift is locked and nothing on it can be changed, including review "
                "flags."
            ),
        )

    nozzle, fuel_type = _load_nozzle(db, nozzle_id, shift)
    reading = _load_reading(db, shift.id, nozzle_id)

    if not reading.requires_review:
        raise AppError(
            status_code=409,
            code="READING_NOT_FLAGGED",
            detail=f"Nozzle {nozzle.label}'s reading is not flagged for review.",
        )

    before = _audit_snapshot(reading)
    reading.requires_review = False
    reading.reviewed_by = actor.user.id
    reading.reviewed_at = datetime.now(tz=timezone.utc)
    # Appended, never replaced: the note explaining *why* it was flagged is the context a
    # future reader needs, and overwriting it with the sign-off would delete the question
    # while keeping the answer.
    note = payload.review_note.strip()
    reading.review_note = f"{reading.review_note}\n{note}" if reading.review_note else note

    audit.record(
        db,
        outlet_id=shift.outlet_id,
        table_name="nozzle_readings",
        record_id=reading.id,
        action=AuditAction.update,
        changed_by=actor.user.id,
        old_values=before,
        new_values=_audit_snapshot(reading) | {"review_note": note},
    )
    db.commit()
    db.refresh(reading)

    logger.info(
        "reading review cleared",
        extra={"reading_id": str(reading.id), "reviewed_by": str(actor.user.id)},
    )
    return _to_response(
        reading, nozzle, fuel_type, _displayable_quantity(reading, nozzle)
    )


@router.get("/shifts/{shift_id}/sales", response_model=ShiftSales)
def read_shift_sales(
    access: ShiftAccess = Depends(require_shift_access(Role.manager)),
    db: Session = Depends(get_db),
) -> ShiftSales:
    """What this shift sold, and what it earned (§6.3).

    Manager floor: §8 gives attendants "read own shift" but not reports, and this is the
    first endpoint that is genuinely a report rather than a data-entry sheet.

    Every figure here is **derived, never stored** (§3 rule 8). Nothing a client ever sent
    contributes to it: quantities come from the totalizer readings and rates come from
    `fuel_prices` as of `shift.started_at`.
    """
    shift = access.shift
    lines = reading_service.shift_sales(db, shift=shift)

    quantity_by_unit: dict[str, Decimal] = {}
    total_value = Decimal("0.00")
    total_margin = Decimal("0.00")

    for line in lines:
        if line.quantity is None:
            continue
        unit = line.unit_of_measure.value
        # §4.5: accumulated per unit. Summing litres and kilograms into one number would
        # produce a figure that looks like a total and means nothing.
        quantity_by_unit[unit] = quantity_by_unit.get(unit, Decimal("0.000")) + line.quantity
        total_value += line.value or Decimal("0.00")
        total_margin += line.profit or Decimal("0.00")

    return ShiftSales(
        shift_id=shift.id,
        business_date=shift.business_date.isoformat(),
        priced_at=shift.started_at,
        lines=[
            SalesLineResponse(
                nozzle_id=line.nozzle.id,
                nozzle_label=line.nozzle.label,
                dispenser_label=line.nozzle.dispenser_label,
                fuel_type_code=line.fuel_type.code,
                unit_of_measure=line.unit_of_measure,
                quantity_sold=line.quantity,
                rate_per_unit=line.rate_per_unit,
                sale_value=line.value,
                margin_per_unit=line.margin_per_unit,
                gross_fuel_margin=line.profit,
            )
            for line in lines
        ],
        total_sale_value=total_value,
        total_gross_fuel_margin=total_margin,
        quantity_by_unit=quantity_by_unit,
        margin_basis=_MARGIN_BASIS,
        # True when at least one nozzle has no quantity yet, so a reader can tell an
        # in-progress shift from one that genuinely sold nothing.
        incomplete=any(line.quantity is None for line in lines),
    )
