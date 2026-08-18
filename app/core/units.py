"""Units of measure (CLAUDE.md §4.5).

This outlet sells CBG by the kilogram alongside petrol and diesel by the litre. A quantity
in this system is therefore a *measure*, not necessarily a *volume*, and the unit is an
attribute of the fuel type -- never inferred from a column name and never assumed.

Deliberately mirrors app/core/roles.py, including the StrEnum choice: the member compares
equal to its own string value, which keeps the PostgreSQL enum, the JSON API and Python all
speaking one vocabulary.
"""

from __future__ import annotations

from enum import StrEnum


class UnitOfMeasure(StrEnum):
    """The units a fuel can be metered and priced in.

    Kept deliberately small. This is not a general unit-conversion facility: nothing in
    V1 ever converts between litres and kilograms, because nothing ever should. A litre of
    petrol and a kilogram of CBG are quantities of different fuels that happen to share a
    column type; they are never added together, and no report sums across units.
    """

    litre = "litre"
    kilogram = "kilogram"
