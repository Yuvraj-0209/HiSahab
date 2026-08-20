"""Quantity sold and what it is worth (CLAUDE.md §6.2, §6.3).

**Every function in this module is pure.** No `Session`, no ORM object, no clock -- just
Decimals in and Decimals out. That is deliberate and it is the point of the file: §6.2 is
the most consequential arithmetic in the project and §10 requires it to be exhaustively
tested, so it is written where a test can reach every branch with a tuple of numbers and no
database at all. The database-aware half lives in app/services/readings.py.

Three rules govern everything here.

**1. Decimal, never float (§3 rule 1).** `0.1 + 0.2 != 0.3` in binary floating point, and
in a cash system that compounds into unexplainable variance. There is no `float` in this
module and there must never be one, including in a docstring example.

**2. A quantity is a measure, not a volume (§4.5).** This outlet sells CBG by the kilogram
through a nozzle with a totalizer exactly like a liquid one. Nothing here is named
`litres`, and nothing infers a unit -- the unit is an attribute of the fuel type, carried
alongside the number by the caller for display only. The arithmetic is identical either
way; the ceiling in `check_flow_rate_ceiling` is not, which is why it takes the fuel's own
rate rather than a constant.

**3. Never return a negative quantity (§6.2).** A negative quantity becomes negative
revenue, which reconciles to a cash surplus that never existed. Every branch below is
bounded so that it cannot, and `_reject_negative` is the backstop for the branch nobody
thought of.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from app.core.errors import AppError

logger = logging.getLogger(__name__)

# §3 rule 2: quantities are NUMERIC(10,3) -- millilitre and gram precision.
QUANTITY_PLACES = Decimal("0.001")
# §3 rule 1: money is NUMERIC(12,2).
MONEY_PLACES = Decimal("0.01")


def _quantise_quantity(value: Decimal) -> Decimal:
    """Round a quantity to the 3 decimal places the column stores.

    ROUND_HALF_UP rather than Python's default ROUND_HALF_EVEN (banker's rounding). Banker's
    rounding is the better choice for large aggregates because it does not bias a sum, but
    it is *surprising*: it rounds 0.0005 down and 0.0015 up, and an attendant checking the
    arithmetic by hand against a paper register would read that as a bug in the software.
    Predictability beats statistical neutrality when a human has to verify the number.
    """
    return value.quantize(QUANTITY_PLACES, rounding=ROUND_HALF_UP)


def _quantise_money(value: Decimal) -> Decimal:
    """Round money to paise. Same ROUND_HALF_UP reasoning as above."""
    return value.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def _reject_negative(quantity: Decimal, *, nozzle_label: str) -> Decimal:
    """The backstop for §6.2's "quantity_sold must never be negative".

    Every branch in `quantity_sold` is already bounded so this cannot fire. It exists
    because §6.2 says "if a code path can produce one, that path is wrong" -- so if one ever
    does, it must surface as a loud failure rather than as negative revenue that quietly
    reconciles to a cash surplus nobody can explain.

    500, not 422: reaching this line is a bug in this file, not bad input from a client.
    """
    if quantity < 0:
        logger.error(
            "computed a negative quantity sold",
            extra={"nozzle_label": nozzle_label, "quantity": str(quantity)},
        )
        raise AppError(
            status_code=500,
            code="NEGATIVE_QUANTITY_COMPUTED",
            detail=(
                "The system computed a negative quantity sold, which is impossible. "
                "This is a bug -- please report it with the request id."
            ),
        )
    return quantity


def gross_throughput(
    *,
    opening_reading: Decimal,
    closing_reading: Decimal,
    rollover_occurred: bool,
    totalizer_max_value: Decimal,
    nozzle_label: str,
) -> Decimal:
    """Everything the totalizer counted, before testing fuel is taken out (§6.2).

    Two cases, and the second one is why naive subtraction is not enough.

    **Normal:** `closing - opening`.

    **Rollover** (§4.3): totalizers roll over like an odometer at their maximum. A meter
    that read 999,990 and now reads 40 dispensed 50 units, not minus 999,950::

        (totalizer_max_value - opening_reading) + closing_reading

    `totalizer_max_value` is per nozzle, read from the row, because meters differ. It is
    NOT NULL on `nozzles` precisely so that this branch can never be reached without it.

    A decrease with neither flag set is refused rather than guessed. Guessing "it must have
    rolled over" would turn a fat-fingered closing reading into a phantom tankful of sales.
    """
    if rollover_occurred:
        if closing_reading >= opening_reading:
            # The flag says the meter wrapped, the numbers say it did not. Applying the
            # rollover formula here would add a whole totalizer cycle of imaginary fuel.
            raise AppError(
                status_code=422,
                code="ROLLOVER_NOT_APPLICABLE",
                detail=(
                    f"Nozzle {nozzle_label} is marked as having rolled over, but the "
                    f"closing reading ({closing_reading}) is not below the opening "
                    f"reading ({opening_reading}). Remove the rollover flag."
                ),
            )
        return (totalizer_max_value - opening_reading) + closing_reading

    if closing_reading < opening_reading:
        # §4.3: this IS a legitimate real-world event -- but only when someone says which
        # of the two events it was. Silently computing a negative here is the bug §6.2
        # exists to prevent.
        raise AppError(
            status_code=422,
            code="TOTALIZER_DECREASED",
            detail=(
                f"Nozzle {nozzle_label}: the closing reading ({closing_reading}) is below "
                f"the opening reading ({opening_reading}). If the meter rolled over, set "
                "rollover_occurred. If it was repaired or replaced, set "
                "meter_reset_occurred and ask an admin to enter the quantity."
            ),
        )

    return closing_reading - opening_reading


def quantity_sold(
    *,
    opening_reading: Decimal,
    closing_reading: Decimal | None,
    testing_quantity: Decimal,
    rollover_occurred: bool,
    meter_reset_occurred: bool,
    manual_quantity_override: Decimal | None,
    totalizer_max_value: Decimal,
    nozzle_label: str,
) -> Decimal | None:
    """What was actually sold through this nozzle (§6.2).

        quantity_sold = closing - opening - testing_quantity

    Returns `None` when there is no closing reading yet -- an open shift mid-entry. That is
    a legitimate "not known yet", distinct from zero, and a caller that treats it as zero
    would report a day of trading as having sold nothing.

    **`testing_quantity` is not a rounding detail (§4.2).** Dealers must keep a
    Weights & Measures-verified 5-litre standard measure and test delivery accuracy against
    it. That fuel is dispensed -- the totalizer counts it -- and poured back into the tank.
    It was never sold. Leaving it in produces a small, permanent, daily cash shortfall that
    is extremely hard to diagnose, and CLAUDE.md calls this the single most common bug in
    home-grown pump software. It is always 0 for CBG, which is not calibration-tested at
    this outlet (§4.5) -- but that is data, not an assumption this function may make.

    **Meter reset** (§4.3, §6.2): after a repair the reading pair is meaningless, so an
    admin states the quantity outright and no arithmetic is attempted. Inferring the split
    between "before the reset" and "after" is not possible from two numbers, and a plausible
    guess is worse than a refusal.
    """
    if meter_reset_occurred:
        if manual_quantity_override is None:
            raise AppError(
                status_code=422,
                code="METER_RESET_REQUIRES_OVERRIDE",
                detail=(
                    f"Nozzle {nozzle_label}'s meter was reset, so its readings cannot be "
                    "subtracted. An admin must enter the quantity sold and a reason."
                ),
            )
        return _reject_negative(
            _quantise_quantity(manual_quantity_override), nozzle_label=nozzle_label
        )

    if manual_quantity_override is not None:
        # An admin override outside a reset -- §8's "Override credit limit / manual litres"
        # power. It wins over the arithmetic, and `override_reason` is mandatory in the
        # database, so it can never be an unexplained number.
        return _reject_negative(
            _quantise_quantity(manual_quantity_override), nozzle_label=nozzle_label
        )

    if closing_reading is None:
        return None

    gross = gross_throughput(
        opening_reading=opening_reading,
        closing_reading=closing_reading,
        rollover_occurred=rollover_occurred,
        totalizer_max_value=totalizer_max_value,
        nozzle_label=nozzle_label,
    )

    if testing_quantity > gross:
        # §6.2 words this guard as `testing_quantity > (closing - opening)`. That expression
        # is negative in the rollover case, where it would fire on every reading including
        # correct ones -- so the comparison here is against gross throughput, which is the
        # same number in the normal case and the right one in both. A deliberate, narrow
        # deviation from the literal text, recorded here rather than left to be rediscovered.
        raise AppError(
            status_code=422,
            code="TESTING_EXCEEDS_THROUGHPUT",
            detail=(
                f"Nozzle {nozzle_label}: testing quantity ({testing_quantity}) is greater "
                f"than everything the meter counted this shift ({gross}). More fuel cannot "
                "have been tested than was dispensed."
            ),
        )

    return _reject_negative(
        _quantise_quantity(gross - testing_quantity), nozzle_label=nozzle_label
    )


def check_flow_rate_ceiling(
    *,
    gross_quantity: Decimal,
    max_flow_rate_per_minute: Decimal,
    started_at: datetime,
    ended_at: datetime,
    nozzle_label: str,
    unit_of_measure: str,
) -> None:
    """Refuse a quantity the dispenser could not physically have delivered (§6.2).

    Catches a mistyped extra digit, which §6.2 names as the most common data-entry error:
    58,432 typed as 584,320 is a plausible-looking reading that values a shift at ten times
    the truth, and nothing else in the system would question it.

    **The ceiling is per fuel type, never a global constant (§4.5).** A petrol nozzle does
    around 60 L/min while a CBG dispenser does single-digit kg/min; one shared number would
    either never fire or reject every real CBG sale. It is read from
    `fuel_types.max_flow_rate_per_minute`. §14 is explicit that `MAX_FLOW_RATE_LPM` from
    config must NOT be read here -- that value only seeds the column for litre fuels in
    migration 0003, and reading it would reimpose exactly the global constant §4.5 forbids.

    Checked against **gross** throughput rather than quantity sold: testing fuel physically
    passed through the nozzle too, so it counts towards what the dispenser had to deliver.
    This also makes the guard marginally stricter, which is the right direction for a check
    whose job is to catch typos.

    Deliberately generous. A real shift never approaches continuous maximum flow, so a
    reading that does is wrong rather than merely busy -- and a tight ceiling would reject
    honest data on the outlet's busiest day, which is the worst possible time to be refused.
    """
    minutes = Decimal((ended_at - started_at).total_seconds()) / Decimal(60)
    if minutes <= 0:  # pragma: no cover - ck_shifts_ended_after_started prevents this
        return

    ceiling = max_flow_rate_per_minute * minutes
    if gross_quantity > ceiling:
        logger.warning(
            "implied flow rate above the fuel's ceiling",
            extra={
                "nozzle_label": nozzle_label,
                "gross_quantity": str(gross_quantity),
                "ceiling": str(ceiling),
                "minutes": str(minutes),
            },
        )
        raise AppError(
            status_code=422,
            code="IMPLIED_FLOW_RATE_TOO_HIGH",
            detail=(
                f"Nozzle {nozzle_label} would have had to dispense {gross_quantity} "
                f"{unit_of_measure} in {minutes.quantize(Decimal('1'))} minutes, which is "
                f"above its maximum of {max_flow_rate_per_minute} {unit_of_measure} per "
                "minute. Check the closing reading for an extra digit."
            ),
        )


def sale_value(quantity: Decimal, rate_per_unit: Decimal) -> Decimal:
    """What that quantity was worth (§6.3).

        sale_value = quantity_sold x rate_at(fuel_type, transaction_time)

    The rate must come from `app/services/pricing.py::rate_at`, never from a "current
    price" column -- there is no such column, by design (§4.1). A price is a dated record,
    and valuing last month's sales at today's rate is the exact silent corruption §4.1
    exists to prevent.
    """
    return _quantise_money(quantity * rate_per_unit)


def dealer_profit(quantity: Decimal, margin_per_unit: Decimal) -> Decimal:
    """Gross dealer margin on the quantity sold (§4.6, §6.3).

        dealer_profit = quantity_sold x margin_at(fuel_type, transaction_time)

    Computable from the totalizer alone because the margin does not move when the retail
    price does: when retail rises Rs 1/litre the next tanker invoice rises Rs 1/litre too,
    so the gap is the constant. That single fact is what lets V1 report fuel profit while
    holding no purchase, tanker or stock data at all.

    **This is not business profit.** §13.7: it excludes stock revaluation entirely. Holding
    12 kL of petrol when the rate rises Rs 1 is a real Rs 12,000 gain that this system will
    never see, because nothing moved through a nozzle. Every caller must label the figure
    accordingly -- an unlabelled "profit" here is precisely the plausible-but-wrong number
    CLAUDE.md exists to prevent.
    """
    return _quantise_money(quantity * margin_per_unit)
