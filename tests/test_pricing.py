"""The rate and margin lookups (CLAUDE.md §5.1, §6.3, §4.6).

These are the tests that matter most in Phase 3. `rate_at` is the single function every
later phase multiplies quantities by, and the way it fails is not a crash -- it is a
plausible number for the wrong instant. Each case below pins one property of "greatest
effective_from <= at" that a naive reimplementation would get wrong.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine

from app.core.errors import AppError
from app.db.session import SessionLocal
from app.services.pricing import margin_at, rate_at

# A fixed instant rather than now(), so a test that passes at 23:59 still passes at 00:01.
T0 = datetime(2026, 3, 1, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _outlet() -> UUID:
    from app.core.config import get_settings

    return get_settings().DEFAULT_OUTLET_ID


def test_rate_is_the_one_in_effect_not_the_latest_entered(
    db, make_user, make_fuel_price, fuel_type_ids
) -> None:
    """§4.1's whole reason for existing: a sale is valued at its historical rate.

    Two rates exist, a month apart. Asking about a moment between them must return the
    older one -- the naive `ORDER BY effective_from DESC LIMIT 1` without the `<= at`
    filter returns the newer, and every historical report is then silently wrong.
    """
    admin = make_user("admin")
    petrol = fuel_type_ids["PETROL"]
    make_fuel_price(petrol, "100.00", T0, entered_by=admin)
    make_fuel_price(petrol, "105.50", T0 + timedelta(days=30), entered_by=admin)

    rate = rate_at(
        db, outlet_id=_outlet(), fuel_type_id=petrol, at=T0 + timedelta(days=10)
    )

    assert rate == Decimal("100.00")


def test_a_later_price_does_not_change_an_earlier_valuation(
    db, make_user, make_fuel_price, fuel_type_ids
) -> None:
    """Entering tomorrow's rate must not revalue yesterday. §10's "Pricing" case."""
    admin = make_user("admin")
    petrol = fuel_type_ids["PETROL"]
    make_fuel_price(petrol, "100.00", T0, entered_by=admin)

    before = rate_at(db, outlet_id=_outlet(), fuel_type_id=petrol, at=T0 + timedelta(days=1))
    make_fuel_price(petrol, "999.00", T0 + timedelta(days=5), entered_by=admin)
    after = rate_at(db, outlet_id=_outlet(), fuel_type_id=petrol, at=T0 + timedelta(days=1))

    assert before == after == Decimal("100.00")


def test_the_boundary_is_inclusive(
    db, make_user, make_fuel_price, fuel_type_ids
) -> None:
    """A rate stamped 06:00 is live *at* 06:00, not from 06:00.000001.

    The `<=` vs `<` distinction is one character in the query and half a shift of
    mispriced fuel in production, since revisions land exactly on the hour.
    """
    admin = make_user("admin")
    diesel = fuel_type_ids["DIESEL"]
    make_fuel_price(diesel, "90.00", T0, entered_by=admin)

    assert rate_at(db, outlet_id=_outlet(), fuel_type_id=diesel, at=T0) == Decimal("90.00")


def test_no_prior_price_raises_rather_than_returning_zero(
    db, fuel_type_ids
) -> None:
    """The contract that keeps a missing rate from valuing a shift at nothing."""
    with pytest.raises(AppError) as exc:
        rate_at(
            db,
            outlet_id=_outlet(),
            fuel_type_id=fuel_type_ids["PREMIUM_PETROL"],
            at=T0,
        )

    assert exc.value.status_code == 409
    assert exc.value.code == "NO_PRICE_FOR_DATE"


def test_prices_are_isolated_by_fuel_type(
    db, make_user, make_fuel_price, fuel_type_ids
) -> None:
    """A petrol rate must never answer a question about diesel."""
    admin = make_user("admin")
    make_fuel_price(fuel_type_ids["PETROL"], "100.00", T0, entered_by=admin)

    with pytest.raises(AppError):
        rate_at(db, outlet_id=_outlet(), fuel_type_id=fuel_type_ids["DIESEL"], at=T0)


def test_prices_are_isolated_by_outlet(
    db, engine: Engine, make_user, make_fuel_price, fuel_type_ids
) -> None:
    """§5.1: rates vary by OMC, state and district. A second outlet's price is not ours.

    V1 has one outlet, so this asserts a property nothing exercises yet -- which is exactly
    why it is worth pinning now, while the lookup is still small enough to reason about.
    """
    from sqlalchemy import text

    admin = make_user("admin")
    other_outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO outlets (id, name) VALUES (:id, 'Other')").bindparams(
                id=other_outlet
            )
        )
    try:
        make_fuel_price(
            fuel_type_ids["DIESEL"], "77.00", T0, entered_by=admin, outlet_id=other_outlet
        )
        with pytest.raises(AppError):
            rate_at(db, outlet_id=_outlet(), fuel_type_id=fuel_type_ids["DIESEL"], at=T0)
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE fuel_prices DISABLE TRIGGER trg_fuel_prices_append_only")
            )
            connection.execute(
                text("DELETE FROM fuel_prices WHERE outlet_id = :id").bindparams(
                    id=other_outlet
                )
            )
            connection.execute(
                text("ALTER TABLE fuel_prices ENABLE TRIGGER trg_fuel_prices_append_only")
            )
            connection.execute(
                text("DELETE FROM outlets WHERE id = :id").bindparams(id=other_outlet)
            )


def test_rate_is_a_decimal_not_a_float(
    db, make_user, make_fuel_price, fuel_type_ids
) -> None:
    """§3 rule 1. A float here would poison every sale value downstream."""
    admin = make_user("admin")
    make_fuel_price(fuel_type_ids["PETROL"], "104.21", T0, entered_by=admin)

    rate = rate_at(db, outlet_id=_outlet(), fuel_type_id=fuel_type_ids["PETROL"], at=T0)

    assert isinstance(rate, Decimal)
    assert rate == Decimal("104.21")


# --- margins -----------------------------------------------------------------


def test_margin_lookup_behaves_like_the_rate_lookup(
    db, make_user, make_fuel_margin, fuel_type_ids
) -> None:
    admin = make_user("admin")
    cbg = fuel_type_ids["CBG"]
    make_fuel_margin(cbg, "2.28", T0, entered_by=admin)
    make_fuel_margin(cbg, "2.50", T0 + timedelta(days=90), entered_by=admin)

    assert margin_at(
        db, outlet_id=_outlet(), fuel_type_id=cbg, at=T0 + timedelta(days=1)
    ) == Decimal("2.28")


def test_no_prior_margin_raises_rather_than_returning_zero(db, fuel_type_ids) -> None:
    """A zero margin is a believable number and a completely false one."""
    with pytest.raises(AppError) as exc:
        margin_at(db, outlet_id=_outlet(), fuel_type_id=fuel_type_ids["DIESEL"], at=T0)

    assert exc.value.status_code == 409
    assert exc.value.code == "NO_MARGIN_FOR_DATE"


def test_margin_is_unchanged_by_price_revisions(
    db, make_user, make_fuel_price, make_fuel_margin, fuel_type_ids
) -> None:
    """§4.6 -- the single fact the whole profit model rests on.

    Retail revisions pass straight through to the purchase invoice, so the dealer margin
    does not move when the price does. Enter a margin, revise the price twice, and the
    margin must still read ₹2.28. If this test ever fails, profit reporting is wrong and
    so is the decision to store margin directly instead of deriving it from purchases.
    """
    admin = make_user("admin")
    cbg = fuel_type_ids["CBG"]
    make_fuel_margin(cbg, "2.28", T0, entered_by=admin)
    make_fuel_price(cbg, "75.00", T0, entered_by=admin)
    make_fuel_price(cbg, "76.00", T0 + timedelta(days=1), entered_by=admin)
    make_fuel_price(cbg, "78.50", T0 + timedelta(days=2), entered_by=admin)

    for offset in (0, 1, 2, 3):
        assert margin_at(
            db, outlet_id=_outlet(), fuel_type_id=cbg, at=T0 + timedelta(days=offset)
        ) == Decimal("2.28")


def test_cbg_profit_arithmetic_is_exact(
    db, make_user, make_fuel_margin, fuel_type_ids
) -> None:
    """The worked example from §4.6: 10,000 kg at ₹2.28 is ₹22,800, exactly.

    Trivial arithmetic, deliberately pinned. In binary floating point 2.28 * 10000 is
    22799.999999999996, and a system that reports that as profit has failed at the one
    thing this project exists to do.
    """
    admin = make_user("admin")
    make_fuel_margin(fuel_type_ids["CBG"], "2.28", T0, entered_by=admin)

    margin = margin_at(db, outlet_id=_outlet(), fuel_type_id=fuel_type_ids["CBG"], at=T0)
    profit = margin * Decimal("10000")

    assert profit == Decimal("22800.00")
