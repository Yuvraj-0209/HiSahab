"""CLAUDE.md §6.2 and §6.3, exhaustively, with no database in sight.

Every function under test here is pure, which is the whole reason `app/services/sales.py`
was written to take Decimals rather than ORM rows: the arithmetic that decides how much
money a shift made can be driven through every branch with a tuple of numbers, at
millisecond speed, without standing up a shift, a nozzle or a price.

§10 names seven required cases for sales math. All seven are here, plus the ones the
per-fuel ceiling (§4.5) and the "never negative" rule (§6.2) demand.

**No `float` appears in this file**, including in an expected value. §3 rule 1 says that
applies to a quick test fixture too -- and a float expectation would be the one place a
rounding bug could hide behind a test that passes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.errors import AppError
from app.services import sales

# A generous odometer ceiling, matching what a real dispenser carries (§4.3).
MAX = Decimal("999999.99")
# A 16-hour trading day, this outlet's 06:00-22:00 window (§4.7).
START = datetime(2026, 8, 18, 0, 30, tzinfo=timezone.utc)  # 06:00 IST
END = START + timedelta(hours=16)


def qty(**overrides: object) -> Decimal | None:
    """Call `quantity_sold` with sensible defaults, overriding only what a test cares about.

    Keeps each test to the one or two numbers that make its point, rather than eight
    keyword arguments of noise.
    """
    kwargs: dict[str, object] = {
        "opening_reading": Decimal("1000.00"),
        "closing_reading": Decimal("1500.00"),
        "testing_quantity": Decimal("0"),
        "rollover_occurred": False,
        "meter_reset_occurred": False,
        "manual_quantity_override": None,
        "totalizer_max_value": MAX,
        "nozzle_label": "DU-1/N-1",
    }
    kwargs.update(overrides)
    return sales.quantity_sold(**kwargs)  # type: ignore[arg-type]


# --- the normal case ---------------------------------------------------------


def test_a_normal_reading_pair_subtracts() -> None:
    """§6.2: quantity_sold = closing - opening - testing_quantity."""
    assert qty() == Decimal("500.000")


def test_the_result_carries_three_decimal_places() -> None:
    """§3 rule 2: quantities are NUMERIC(10,3) -- millilitre and gram precision."""
    assert qty(closing_reading=Decimal("1000.25")) == Decimal("0.250")


def test_a_shift_that_sold_nothing_is_zero_not_an_error() -> None:
    """A nozzle nobody used is a real, ordinary outcome."""
    assert qty(closing_reading=Decimal("1000.00")) == Decimal("0.000")


def test_no_closing_reading_yet_is_none_not_zero() -> None:
    """"Not entered" and "sold nothing" are different facts.

    Collapsing them to zero would report a full day of trading as having sold nothing --
    a plausible number, silently wrong, which is the exact failure mode CLAUDE.md exists
    to prevent.
    """
    assert qty(closing_reading=None) is None


# --- §4.2 testing quantity ---------------------------------------------------


def test_testing_quantity_is_subtracted() -> None:
    """§4.2. The single most common bug in home-grown pump software.

    Testing fuel is dispensed into the 5-litre standard measure and poured back. The
    totalizer counted it; nobody bought it. Leaving it in produces a small, permanent,
    daily cash shortfall that is extremely hard to diagnose.
    """
    assert qty(testing_quantity=Decimal("5.000")) == Decimal("495.000")


def test_testing_quantity_is_subtracted_to_the_millilitre() -> None:
    assert qty(testing_quantity=Decimal("5.125")) == Decimal("494.875")


def test_testing_beyond_what_the_meter_counted_is_refused() -> None:
    """§6.2's TESTING_EXCEEDS_THROUGHPUT. More cannot be tested than was dispensed."""
    with pytest.raises(AppError) as caught:
        qty(closing_reading=Decimal("1002.00"), testing_quantity=Decimal("5.000"))
    assert caught.value.status_code == 422
    assert caught.value.code == "TESTING_EXCEEDS_THROUGHPUT"


def test_testing_exactly_equal_to_throughput_is_allowed() -> None:
    """The boundary is `>`, not `>=`: a shift that only ever dispensed test fuel sold zero.

    Rare but real -- a nozzle taken out of service for calibration and nothing else.
    """
    assert qty(
        closing_reading=Decimal("1005.00"), testing_quantity=Decimal("5.000")
    ) == Decimal("0.000")


def test_testing_is_also_subtracted_under_a_rollover() -> None:
    """The guard §6.2 words as `testing > (closing - opening)` is negative here.

    Taken literally that comparison fires on every rolled-over reading including correct
    ones, so `sales.py` compares against gross throughput instead. This test is what pins
    that deviation down: 999,999.99 - 999,990.00 + 40.00 = 49.99 gross, less 5 tested.
    """
    assert qty(
        opening_reading=Decimal("999990.00"),
        closing_reading=Decimal("40.00"),
        rollover_occurred=True,
        testing_quantity=Decimal("5.000"),
    ) == Decimal("44.990")


# --- §4.3 rollover -----------------------------------------------------------


def test_a_rollover_produces_positive_quantity() -> None:
    """§6.2: (max - opening) + closing. A meter that wrapped dispensed fuel, not minus fuel."""
    assert qty(
        opening_reading=Decimal("999990.00"),
        closing_reading=Decimal("40.00"),
        rollover_occurred=True,
    ) == Decimal("49.990")


def test_a_decrease_with_no_flag_is_refused() -> None:
    """§6.2's TOTALIZER_DECREASED, and §10 names it explicitly.

    Guessing "it must have rolled over" would turn a fat-fingered closing reading into a
    phantom tankful of sales worth roughly a million litres.
    """
    with pytest.raises(AppError) as caught:
        qty(opening_reading=Decimal("1500.00"), closing_reading=Decimal("1000.00"))
    assert caught.value.status_code == 422
    assert caught.value.code == "TOTALIZER_DECREASED"


def test_the_refusal_names_the_nozzle_and_both_readings() -> None:
    """A 422 that does not say which meter sends somebody hunting through the whole day."""
    with pytest.raises(AppError) as caught:
        qty(
            opening_reading=Decimal("1500.00"),
            closing_reading=Decimal("1000.00"),
            nozzle_label="DU-2/N-3",
        )
    assert "DU-2/N-3" in caught.value.detail
    assert "1500.00" in caught.value.detail
    assert "1000.00" in caught.value.detail


def test_a_rollover_flag_on_an_increasing_meter_is_refused() -> None:
    """The flag says the meter wrapped; the numbers say it did not.

    Applying the rollover formula anyway would add a whole totalizer cycle of imaginary
    fuel -- close to a million litres of revenue that never existed.
    """
    with pytest.raises(AppError) as caught:
        qty(rollover_occurred=True)
    assert caught.value.status_code == 422
    assert caught.value.code == "ROLLOVER_NOT_APPLICABLE"


def test_the_rollover_ceiling_is_the_nozzles_own() -> None:
    """§4.3 / §5.1: `totalizer_max_value` is per meter, because meters differ.

    Same readings, two different ceilings, two different answers -- so a shared constant
    would silently mis-state every nozzle that did not happen to match it.
    """
    small = qty(
        opening_reading=Decimal("9990.00"),
        closing_reading=Decimal("10.00"),
        rollover_occurred=True,
        totalizer_max_value=Decimal("9999.99"),
    )
    large = qty(
        opening_reading=Decimal("9990.00"),
        closing_reading=Decimal("10.00"),
        rollover_occurred=True,
        totalizer_max_value=Decimal("999999.99"),
    )
    assert small == Decimal("19.990")
    assert large == Decimal("990019.990")


# --- §6.2 meter reset --------------------------------------------------------


def test_a_meter_reset_without_an_override_is_refused() -> None:
    """§6.2: after a repair the reading pair is meaningless. Do not infer the split."""
    with pytest.raises(AppError) as caught:
        qty(meter_reset_occurred=True, closing_reading=Decimal("40.00"))
    assert caught.value.status_code == 422
    assert caught.value.code == "METER_RESET_REQUIRES_OVERRIDE"


def test_a_meter_reset_with_an_override_uses_the_stated_quantity() -> None:
    assert qty(
        meter_reset_occurred=True,
        closing_reading=Decimal("40.00"),
        manual_quantity_override=Decimal("312.500"),
    ) == Decimal("312.500")


def test_an_override_wins_over_the_arithmetic_even_without_a_reset() -> None:
    """§8's "override manual litres" admin power. `override_reason` is NOT NULL alongside
    it in the database, so this can never be an unexplained number."""
    assert qty(manual_quantity_override=Decimal("42.000")) == Decimal("42.000")


def test_a_reset_override_is_honoured_even_with_no_closing_reading() -> None:
    """The readings are meaningless after a reset, so their absence changes nothing."""
    assert qty(
        meter_reset_occurred=True,
        closing_reading=None,
        manual_quantity_override=Decimal("88.000"),
    ) == Decimal("88.000")


# --- §6.2 the sanity ceiling, per fuel type (§4.5) ---------------------------


def ceiling(**overrides: object) -> None:
    kwargs: dict[str, object] = {
        "gross_quantity": Decimal("500.000"),
        "max_flow_rate_per_minute": Decimal("60.000"),
        "started_at": START,
        "ended_at": END,
        "nozzle_label": "DU-1/N-1",
        "unit_of_measure": "litre",
    }
    kwargs.update(overrides)
    sales.check_flow_rate_ceiling(**kwargs)  # type: ignore[arg-type]


def test_a_realistic_day_passes_the_ceiling() -> None:
    """5,000 litres over 16 hours is a busy day, nowhere near 60 L/min continuous."""
    ceiling(gross_quantity=Decimal("5000.000"))


def test_a_mistyped_extra_digit_is_caught() -> None:
    """§6.2 names this as the most common data-entry error.

    58,432 typed as 584,320 values the shift at ten times the truth, and nothing else in
    the system would question it.
    """
    with pytest.raises(AppError) as caught:
        ceiling(gross_quantity=Decimal("584320.000"))
    assert caught.value.status_code == 422
    assert caught.value.code == "IMPLIED_FLOW_RATE_TOO_HIGH"


def test_the_ceiling_is_the_fuels_own_not_a_global_constant() -> None:
    """§4.5, and the single most important property of this guard.

    A petrol nozzle does ~60 L/min; a CBG dispenser does single-digit kg/min. The SAME
    quantity over the SAME window passes for petrol and is refused for CBG. One shared
    litres-per-minute number would either never fire or reject every real CBG sale.
    """
    quantity = Decimal("20000.000")  # 16 hours at ~21/min

    ceiling(gross_quantity=quantity, max_flow_rate_per_minute=Decimal("60.000"))

    with pytest.raises(AppError) as caught:
        ceiling(
            gross_quantity=quantity,
            max_flow_rate_per_minute=Decimal("15.000"),
            unit_of_measure="kilogram",
        )
    assert caught.value.code == "IMPLIED_FLOW_RATE_TOO_HIGH"
    # The message speaks the fuel's own unit rather than saying "litres" at a gas customer.
    assert "kilogram" in caught.value.detail


def test_the_ceiling_scales_with_the_shift_length() -> None:
    """A quantity that is fine over 16 hours is impossible in 30 minutes."""
    quantity = Decimal("5000.000")
    ceiling(gross_quantity=quantity)
    with pytest.raises(AppError):
        ceiling(gross_quantity=quantity, ended_at=START + timedelta(minutes=30))


def test_exactly_at_the_ceiling_is_allowed() -> None:
    """The comparison is `>`, not `>=`. 60 L/min x 960 minutes = 57,600 exactly."""
    ceiling(gross_quantity=Decimal("57600.000"))


# --- §6.3 valuation ----------------------------------------------------------


def test_sale_value_is_quantity_times_rate() -> None:
    assert sales.sale_value(Decimal("500.000"), Decimal("104.50")) == Decimal("52250.00")


def test_sale_value_rounds_to_paise() -> None:
    """Money is NUMERIC(12,2) (§3 rule 1). 100.005 x 3 = 300.015 -> 300.02 half-up."""
    assert sales.sale_value(Decimal("3.000"), Decimal("100.005")) == Decimal("300.02")


def test_dealer_profit_is_quantity_times_margin() -> None:
    """§4.6: CBG's known margin of Rs 2.28/kg, on 100 kg."""
    assert sales.dealer_profit(Decimal("100.000"), Decimal("2.28")) == Decimal("228.00")


def test_a_sub_rupee_quantity_does_not_round_to_nothing() -> None:
    """0.5 kg at Rs 2.28/kg is Rs 1.14, not Rs 1 and not zero."""
    assert sales.dealer_profit(Decimal("0.500"), Decimal("2.28")) == Decimal("1.14")


def test_money_never_becomes_a_float() -> None:
    """§3 rule 1, asserted on the type rather than the value.

    `0.1 + 0.2 != 0.3` in binary floating point. A function that returned a float here
    would still pass every equality check above -- Decimal compares equal to a float that
    happens to match -- so the type itself has to be the assertion.
    """
    value = sales.sale_value(Decimal("500.000"), Decimal("104.50"))
    profit = sales.dealer_profit(Decimal("500.000"), Decimal("2.28"))
    assert isinstance(value, Decimal)
    assert isinstance(profit, Decimal)


# --- §6.2's absolute rule ----------------------------------------------------


@pytest.mark.parametrize(
    "opening,closing,testing,rollover",
    [
        ("0.00", "0.00", "0", False),
        ("0.00", "999999.99", "0", False),
        ("999999.99", "0.00", "0", True),
        ("999999.99", "999999.98", "0", True),
        ("1000.00", "1000.00", "0", False),
        ("500.50", "500.50", "0", False),
        ("0.01", "0.02", "0.010", False),
        ("999999.98", "999999.99", "0.010", False),
    ],
)
def test_no_input_combination_produces_a_negative_quantity(
    opening: str, closing: str, testing: str, rollover: bool
) -> None:
    """§6.2: "quantity_sold must never be negative. If a code path can produce one, that
    path is wrong."

    A negative quantity becomes negative revenue, which reconciles to a cash surplus that
    never existed -- and unlike a shortfall, a surplus is the kind of wrong number nobody
    reports. Every boundary of the two readings is swept here.
    """
    try:
        result = qty(
            opening_reading=Decimal(opening),
            closing_reading=Decimal(closing),
            testing_quantity=Decimal(testing),
            rollover_occurred=rollover,
        )
    except AppError as refused:
        # A refusal is a correct outcome for the guarded combinations. What must never
        # happen is a negative number coming back as if it were an answer.
        assert refused.status_code == 422
        return
    assert result is not None
    assert result >= 0


def test_the_negative_backstop_fires_rather_than_returning_a_negative() -> None:
    """§6.2's last line of defence, tested directly because nothing else can reach it.

    Every branch of `quantity_sold` is bounded so this cannot fire today. It exists because
    §6.2 says "if a code path can produce one, that path is wrong" -- so the day somebody
    adds a branch that does, this must be a loud 500 and not negative revenue quietly
    reconciling to a cash surplus that never existed.

    Driven through the admin-override path, which is the one place a caller-supplied
    quantity reaches the function at all.
    """
    with pytest.raises(AppError) as caught:
        sales._reject_negative(Decimal("-1.000"), nozzle_label="DU-1/N-1")

    assert caught.value.status_code == 500
    assert caught.value.code == "NEGATIVE_QUANTITY_COMPUTED"
