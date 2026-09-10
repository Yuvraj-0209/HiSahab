"""The reading chain, and pricing a shift's readings (CLAUDE.md §4.7, §6.2, §6.3).

The database-aware half of Phase 5. `app/services/sales.py` holds the arithmetic and knows
nothing about a `Session`; this module knows how to find the numbers to feed it.

**The chain (§4.7) is the reason this file exists.** A nozzle's opening reading is never
typed. It is the most recent closing reading *for that same nozzle*, carried across shifts
and across days -- so an overnight closure and a 24-hour handover are the same thing, a gap
between one shift's close and the next one's open.

The lookup is deliberately "the most recent closing for **this nozzle**", not "the previous
shift's reading". A nozzle out of order for one shift, a nozzle installed mid-life, or a
whole skipped day would each snap a chain built on the latter, and would snap it silently.

**Pre-filled is not the same as confirmed.** Nothing in this module writes an opening
reading. It supplies what the chain predicts; the API requires a human to confirm it against
the physical meter, and stores both values so a mismatch is a permanent fact on the row
rather than an event nobody recorded. See the API module for that half.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.core.units import UnitOfMeasure
from app.models.fuel import FuelType
from app.models.nozzle import Nozzle
from app.models.reading import NozzleReading
from app.models.shift import Shift
from app.services import pricing, sales

logger = logging.getLogger(__name__)


def chained_opening(db: Session, *, nozzle_id: UUID, shift: Shift) -> Decimal | None:
    """The opening reading this nozzle carries into `shift`, or None if it has no history.

    "Most recent" means greatest `(business_date, sequence)` **strictly before this
    shift's** -- never greatest `created_at`. §4.7: the whole day is typed in after the
    fact, frequently out of order relative to when it traded, so insertion order says
    nothing at all about chain order. A test enters an older business date after a newer
    one and asserts this still resolves correctly.

    `None` means no predecessor: the first shift ever, or a newly installed nozzle. That is
    the anchor case, and §4.7 makes anchoring admin-only -- a starting meter value nobody
    can check is a number the rest of the chain is then built on.

    Rows with a NULL `closing_reading` are skipped: a shift still being entered has not
    produced a value to carry yet. The partial index
    `ix_nozzle_readings_nozzle_closed` serves exactly this predicate.
    """
    return db.execute(
        select(NozzleReading.closing_reading)
        .join(Shift, Shift.id == NozzleReading.shift_id)
        .where(
            NozzleReading.nozzle_id == nozzle_id,
            NozzleReading.closing_reading.is_not(None),
            Shift.outlet_id == shift.outlet_id,
            tuple_(Shift.business_date, Shift.sequence)
            < tuple_(shift.business_date, shift.sequence),
        )
        .order_by(Shift.business_date.desc(), Shift.sequence.desc())
        .limit(1)
    ).scalar_one_or_none()


def nozzles_in_scope(db: Session, *, shift: Shift) -> list[tuple[Nozzle, FuelType]]:
    """The nozzles this shift is expected to have a reading for.

    Two exclusions, both of which have a test:

    * **Inactive nozzles.** A decommissioned meter dispenses nothing.
    * **Nozzles installed after the shift.** A meter fitted next week cannot have a reading
      for today, and requiring one would make the shift impossible to close.

    Measured against the end of the shift window when it is known, because a nozzle
    commissioned at 10:00 during a 06:00-22:00 shift really did trade that shift.

    **Known limitation:** `is_active` is read as it stands *now*, and `nozzles` has no
    `deactivated_at`. So a nozzle retired today drops out of a shift from last month that it
    genuinely served. §6.8 words the close precondition as "any active nozzle", present
    tense, and adding a history column to satisfy a historical edge case is not worth it in
    V1 -- but it is a real approximation and it is written down rather than discovered.
    """
    window_end = shift.ended_at or shift.started_at
    return list(
        db.execute(
            select(Nozzle, FuelType)
            .join(FuelType, FuelType.id == Nozzle.fuel_type_id)
            .where(
                Nozzle.outlet_id == shift.outlet_id,
                Nozzle.is_active.is_(True),
                Nozzle.meter_installed_at <= window_end,
            )
            .order_by(Nozzle.dispenser_label, Nozzle.label)
        ).all()
    )


def readings_for_shift(db: Session, *, shift_id: UUID) -> dict[UUID, NozzleReading]:
    """This shift's saved readings, keyed by nozzle id."""
    rows = (
        db.execute(select(NozzleReading).where(NozzleReading.shift_id == shift_id))
        .scalars()
        .all()
    )
    return {row.nozzle_id: row for row in rows}


def flag_downstream_reading(
    db: Session, *, shift: Shift, nozzle_id: UUID, note: str
) -> NozzleReading | None:
    """Mark the reading that carried this nozzle's value forward as needing review (§13.10).

    This is the whole of §13.10's revised approach, and what it does **not** do is the
    point: it does not touch `opening_reading`. §4.7 stores the chained opening on the row
    precisely so that correcting one shift cannot silently rewrite the next shift's history,
    and a recomputing cascade would be that rewrite -- with the added indignity that the
    rewritten figure would look exactly like a reading somebody had confirmed.

    So the stale value stays, visibly stale, with a note saying which shift moved beneath
    it. A human reconciles two numbers they can both see. Nothing is invented.

    **The lookup follows the chain, not the calendar.** This is the exact inverse of
    `chained_opening`, and it has to be: that function deliberately skips shifts with no
    closing reading for this nozzle (§4.7 -- "the most recent closing reading *for that
    nozzle*", not "the previous shift's"), so the row that carried this value forward is
    not necessarily in the very next shift. With the nozzle out of order for one shift, it
    is two shifts away; after a skipped day, further still. Phase 6 Step 0 fixed a version
    that asked only the next shift and returned quietly when it had no reading for the
    nozzle -- leaving the row that *did* carry the value silently stale, which is the one
    outcome §13.10 exists to prevent.
    """
    reading = db.execute(
        select(NozzleReading)
        .join(Shift, Shift.id == NozzleReading.shift_id)
        .where(
            NozzleReading.nozzle_id == nozzle_id,
            Shift.outlet_id == shift.outlet_id,
            tuple_(Shift.business_date, Shift.sequence)
            > tuple_(shift.business_date, shift.sequence),
        )
        .order_by(Shift.business_date, Shift.sequence)
        .limit(1)
    ).scalar_one_or_none()
    if reading is None:
        return None
    following_id = reading.shift_id

    reading.requires_review = True
    # Appended rather than replaced: a shift reopened twice must not lose the first note.
    reading.review_note = (
        f"{reading.review_note}\n{note}" if reading.review_note else note
    )
    # Any earlier sign-off is void -- the thing that was reviewed has changed underneath it.
    reading.reviewed_by = None
    reading.reviewed_at = None

    logger.warning(
        "downstream reading flagged for review",
        extra={
            "reading_id": str(reading.id),
            "shift_id": str(following_id),
            "nozzle_id": str(nozzle_id),
        },
    )
    return reading


@dataclass(frozen=True)
class SalesLine:
    """One nozzle's contribution to a shift, priced.

    `quantity` is `None` when the nozzle has no closing reading yet -- deliberately not
    zero. "Not entered" and "sold nothing" are different facts, and collapsing them would
    report a full day of trading as having sold nothing at all.

    `margin_per_unit` and `profit` are `None` for **two** distinct reasons, and a caller that
    displays them must not treat either as zero (§13.7):

    * the nozzle has no quantity yet, so nothing can be valued at all; or
    * `shift_sales` was called with `price_only=True`, because §6.4 wanted the value and
      deliberately did not ask what it earned.

    Both mean "not known", never "nothing". `value` is `None` only for the first reason.
    """

    nozzle: Nozzle
    fuel_type: FuelType
    reading: NozzleReading | None
    quantity: Decimal | None
    rate_per_unit: Decimal | None
    margin_per_unit: Decimal | None
    value: Decimal | None
    profit: Decimal | None

    @property
    def unit_of_measure(self) -> UnitOfMeasure:
        """§4.5: read from the fuel type, never inferred from the number."""
        return UnitOfMeasure(self.fuel_type.unit_of_measure)


def compute_quantity(reading: NozzleReading, nozzle: Nozzle) -> Decimal | None:
    """Apply §6.2 to one stored row. Thin, on purpose -- the arithmetic is in sales.py.

    Raises `METER_RESET_REQUIRES_OVERRIDE` for a reset that no admin has resolved yet.
    Most callers want `quantity_if_known` instead; see the note there.
    """
    return sales.quantity_sold(
        opening_reading=reading.opening_reading,
        closing_reading=reading.closing_reading,
        testing_quantity=reading.testing_quantity,
        rollover_occurred=reading.rollover_occurred,
        meter_reset_occurred=reading.meter_reset_occurred,
        manual_quantity_override=reading.manual_quantity_override,
        totalizer_max_value=nozzle.totalizer_max_value,
        nozzle_label=nozzle.label,
    )


def awaiting_override(reading: NozzleReading) -> bool:
    """A meter reset that no admin has put a quantity to yet (§6.2).

    A legitimate intermediate state, and the distinction the first version of this module
    got wrong. **"The meter was replaced" is an observation** -- made by whoever is entering
    the day, who watched the engineer do it. **"312.5 litres went through it" is a
    judgement**, and §8 reserves that for an admin. They are separate acts, by separate
    people, usually at separate times.

    Treating the first as invalid until the second exists made a reset impossible to report
    at all: the attendant could not write the row, and the admin had nothing to override.
    Caught by `test_a_meter_reset_can_be_recorded_before_an_admin_states_the_quantity`.
    """
    return reading.meter_reset_occurred and reading.manual_quantity_override is None


def quantity_if_known(reading: NozzleReading, nozzle: Nozzle) -> Decimal | None:
    """§6.2's quantity, or None where the row legitimately cannot yield one yet.

    Two distinct kinds of None, both meaning "not knowable", neither meaning zero:

    * no closing reading -- the shift is still being entered;
    * a meter reset awaiting its admin override (see `awaiting_override`).

    What this does **not** do is swallow a genuine §6.2 failure. A decreased totalizer with
    no flag, or testing beyond throughput, still raises -- those are refusals, not gaps.
    §6.8's close precondition treats both kinds of None as blocking, so an unresolved reset
    cannot slip through into a closed shift (`_quantity_is_unknown`).
    """
    if awaiting_override(reading):
        return None
    return compute_quantity(reading, nozzle)


def shift_sales(
    db: Session,
    *,
    shift: Shift,
    price_only: bool = False,
    cache: pricing.LookupCache | None = None,
) -> list[SalesLine]:
    """Value every nozzle on a shift (§6.3).

        sale_value    = quantity_sold x rate_at(fuel_type, shift.started_at)
        dealer_profit = quantity_sold x margin_at(fuel_type, shift.started_at)

    **`price_only=True` asks the first question without the second** (§6.3, Phase 10). §6.4's
    cash equation needs to know what the fuel was *worth*, not what it *earned* -- and it must
    not inherit a refusal that has nothing to do with cash. `margin_at` raises 409
    NO_MARGIN_FOR_DATE when no margin exists, and §14 records that petrol and diesel dealer
    commissions have never been entered at this outlet, so a cash engine calling the default
    form would refuse to reconcile every petrol day it has ever traded.

    That is the same argument `collections.shift_moved_any_quantity` makes about §6.8's close
    preconditions: a reference-data gap must not make an unrelated operation impossible,
    because "your shift will not close" is a very confusing way to be told about a missing
    margin.

    When `price_only`, no margin is looked up at all and `margin_per_unit` / `profit` come
    back as **None -- never zero**. §13.7: an unlabelled zero profit is a plausible-looking
    figure and completely wrong, which is the failure mode this whole document exists to
    prevent. A caller that wants profit must ask for it and handle the refusal.

    **The price half is never relaxed.** A missing rate still raises, in both modes. A shift
    valued at zero reconciles to a cash surplus nobody can explain and nothing downstream
    would question it (§5.1).

    **The §13.1 approximation lives here.** Both figures value the *whole* shift at the rate
    effective at `started_at`, rather than apportioning across a revision that landed
    mid-shift. V1 has no per-transaction data (§12), so exact apportionment is not merely
    unimplemented -- it is impossible from the data that exists. `_warn_on_mid_shift_revision`
    below makes sure the approximation is never applied silently.

    It happens to be exact for this outlet (§6.3): its single shift starts at 06:00 IST,
    which is the revision instant itself, and `rate_at` compares with `<=`, so the shift
    picks up the new rate and one rate covers the whole day. That is a property of these
    trading hours, not a guarantee -- a 24-hour outlet's 02:00-10:00 shift straddles 06:00
    and the approximation applies in full.

    A missing price or margin raises (409) rather than returning zero. §5.1's helpers make
    that choice and it is the right one: a shift valued at zero is a plausible number and
    completely wrong, and nothing downstream would ever question it.
    """
    readings = readings_for_shift(db, shift_id=shift.id)
    lines: list[SalesLine] = []
    # Per shift, not per call: the approximation is a property of (shift, fuel), so two
    # nozzles on the same fuel must not warn -- or query -- twice. See the helper.
    seen_revision_warnings: set[UUID] = set()

    for nozzle, fuel_type in nozzles_in_scope(db, shift=shift):
        reading = readings.get(nozzle.id)
        if reading is None:
            lines.append(
                SalesLine(
                    nozzle=nozzle,
                    fuel_type=fuel_type,
                    reading=None,
                    quantity=None,
                    rate_per_unit=None,
                    margin_per_unit=None,
                    value=None,
                    profit=None,
                )
            )
            continue

        quantity = quantity_if_known(reading, nozzle)
        if quantity is None:
            lines.append(
                SalesLine(
                    nozzle=nozzle,
                    fuel_type=fuel_type,
                    reading=reading,
                    quantity=None,
                    rate_per_unit=None,
                    margin_per_unit=None,
                    value=None,
                    profit=None,
                )
            )
            continue

        rate = pricing.rate_at(
            db,
            outlet_id=shift.outlet_id,
            fuel_type_id=fuel_type.id,
            at=shift.started_at,
            cache=cache,
        )
        # Not looked up at all when the caller only wants value -- not looked up and
        # discarded. `margin_at` *raises*, so a lookup here would refuse the whole shift
        # before anything could choose to ignore the result.
        margin = (
            None
            if price_only
            else pricing.margin_at(
                db,
                outlet_id=shift.outlet_id,
                fuel_type_id=fuel_type.id,
                at=shift.started_at,
                cache=cache,
            )
        )
        _warn_on_mid_shift_revision(db, shift=shift, fuel_type=fuel_type, seen=seen_revision_warnings)

        lines.append(
            SalesLine(
                nozzle=nozzle,
                fuel_type=fuel_type,
                reading=reading,
                quantity=quantity,
                rate_per_unit=rate,
                margin_per_unit=margin,
                value=sales.sale_value(quantity, rate),
                # None, never Decimal("0.00") -- see the docstring and §13.7.
                profit=None if margin is None else sales.dealer_profit(quantity, margin),
            )
        )

    return lines


def _warn_on_mid_shift_revision(
    db: Session, *, shift: Shift, fuel_type: FuelType, seen: set[UUID] | None = None
) -> None:
    """Log when §13.1's approximation is actually being applied (§6.3).

    §6.3 requires this warning and requires that it not be deleted on the strength of this
    outlet's convenient 06:00 start. A 24-hour outlet's night shift straddles the revision
    and the approximation applies to it in full.

    `seen` de-duplicates **per fuel within one shift** (Phase 19). Two nozzles dispensing
    petrol on one shift are one commercial fact and one approximation, so the second nozzle
    was repeating both the query and the log line. The warning itself is untouched: it still
    fires once for every (shift, fuel) the approximation is applied to, which is the unit
    §6.3 actually describes.
    """
    if shift.ended_at is None:
        return
    if seen is not None:
        if fuel_type.id in seen:
            return
        seen.add(fuel_type.id)
    revisions = pricing.revisions_within(
        db,
        outlet_id=shift.outlet_id,
        fuel_type_id=fuel_type.id,
        start=shift.started_at,
        end=shift.ended_at,
    )
    if revisions:
        logger.warning(
            "price revised inside the shift window; whole shift valued at the start rate",
            extra={
                "shift_id": str(shift.id),
                "fuel_type_code": fuel_type.code,
                "revisions": [r.isoformat() for r in revisions],
                "approximation": "CLAUDE.md §13.1",
            },
        )


def _quantity_is_unknown(reading: NozzleReading | None) -> bool:
    """True when this nozzle's quantity cannot be determined yet.

    Three ways that happens, and all three block a close:

    * no reading row at all;
    * a reading with no closing value -- the shift is still being entered;
    * a **meter reset with no admin override**. This one is easy to miss: the row may well
      carry a closing reading, but §6.2 says the pair is meaningless after a reset, so the
      number sitting in the column is not an answer. Treating it as one would value the
      shift off a reading the spec explicitly calls unusable.
    """
    if reading is None:
        return True
    if reading.manual_quantity_override is not None:
        return False
    return awaiting_override(reading) or reading.closing_reading is None


def missing_closing_readings(db: Session, *, shift: Shift) -> list[str]:
    """Labels of in-scope nozzles whose quantity is not yet knowable (§6.8).

    Returns labels rather than a count so the 409 can name them. "Three nozzles are
    missing readings" sends somebody hunting; "DU-1/N-2 and DU-2/N-1" does not.
    """
    readings = readings_for_shift(db, shift_id=shift.id)
    return [
        nozzle.label
        for nozzle, _fuel_type in nozzles_in_scope(db, shift=shift)
        if _quantity_is_unknown(readings.get(nozzle.id))
    ]



def revalidate_flow_rates(db: Session, *, shift: Shift) -> None:
    """Re-run §6.2's sanity ceiling for every reading, now that the window is known.

    The ceiling is `max_flow_rate_per_minute x shift duration in minutes`, and `ended_at`
    is nullable until close -- so a reading entered before the end time was known skipped
    the check. This is the second half of that arrangement: at close, Phase 4's
    `SHIFT_END_TIME_REQUIRED` guarantees an end time, so every reading is measured against
    a real window here. A mistyped extra digit is caught at entry in the normal case and at
    close in every case.

    Only the arithmetic branches are re-checked. A meter reset carries an admin's stated
    quantity rather than a metered one (§6.2), and holding a human's signed figure against
    a mechanical flow rate would refuse the one path that exists for when the meter lied.
    """
    if shift.ended_at is None:  # pragma: no cover - close guarantees an end time
        return

    saved = readings_for_shift(db, shift_id=shift.id)
    for nozzle, fuel_type in nozzles_in_scope(db, shift=shift):
        reading = saved.get(nozzle.id)
        if reading is None or reading.closing_reading is None:
            continue
        if reading.meter_reset_occurred or reading.manual_quantity_override is not None:
            continue

        gross = sales.gross_throughput(
            opening_reading=reading.opening_reading,
            closing_reading=reading.closing_reading,
            rollover_occurred=reading.rollover_occurred,
            totalizer_max_value=nozzle.totalizer_max_value,
            nozzle_label=nozzle.label,
        )
        sales.check_flow_rate_ceiling(
            gross_quantity=gross,
            # §4.5 / §14: the fuel's own column, never config's MAX_FLOW_RATE_LPM.
            max_flow_rate_per_minute=fuel_type.max_flow_rate_per_minute,
            started_at=shift.started_at,
            ended_at=shift.ended_at,
            nozzle_label=nozzle.label,
            unit_of_measure=fuel_type.unit_of_measure,
        )
