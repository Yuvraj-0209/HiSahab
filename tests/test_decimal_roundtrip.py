"""Money must be Decimal end to end (CLAUDE.md §3 rule 1).

This is the rule whose violation produces silently wrong money figures rather than
a crash, so it is asserted against a real PostgreSQL connection rather than assumed
from documentation.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Engine, text


def test_numeric_comes_back_as_decimal(engine: Engine) -> None:
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT CAST('0.10' AS numeric(12,2))")
        ).scalar_one()

    assert isinstance(value, Decimal)
    assert not isinstance(value, float)
    assert value == Decimal("0.10")


def test_numeric_arithmetic_is_exact_in_the_database(engine: Engine) -> None:
    """The 0.1 + 0.2 case CLAUDE.md §3 calls out by name."""
    with engine.connect() as connection:
        total = connection.execute(
            text("SELECT CAST('0.1' AS numeric(12,2)) + CAST('0.2' AS numeric(12,2))")
        ).scalar_one()

    assert total == Decimal("0.3")


def test_numeric_arithmetic_is_exact_in_python() -> None:
    assert Decimal("0.1") + Decimal("0.2") == Decimal("0.3")
    # The same sum in binary floating point, for contrast -- this is what the rule
    # exists to prevent.
    assert 0.1 + 0.2 != 0.3


def test_volume_precision_survives_three_decimal_places(engine: Engine) -> None:
    """Volumes are NUMERIC(10,3) -- millilitre precision (§3 rule 2)."""
    with engine.connect() as connection:
        litres = connection.execute(
            text("SELECT CAST('12.345' AS numeric(10,3))")
        ).scalar_one()

    assert litres == Decimal("12.345")
