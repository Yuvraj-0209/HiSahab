"""Fix ck_expenses_description_length to trim only leading/trailing whitespace.

Revision ID: 0009
Revises: 0008
Create Date: Phase 7 -- caught while chasing 100% coverage on the create-expense route

`0008` wrote the description check the same way as `0007`'s reversal-reason check --
`regexp_replace(description, '\\s', '', 'g')`, stripping *every* whitespace character
before measuring length. That is right for a reason field, where the only question is
"does at least one non-whitespace character exist anywhere" (`0007`'s `~ '[^[:space:]]'`
regex, an existence check with no length arithmetic to go wrong).

It is wrong for a description, where the API's own validation
(`app/api/v1/expenses.py::DescriptionValue`, `StringConstraints(strip_whitespace=True,
min_length=3)`) trims only *leading and trailing* whitespace before measuring length. A
genuinely fine three-character description containing one internal space -- `"a b"` --
passed Pydantic and then failed this CHECK, because collapsing the internal space left
`"ab"`, two characters. An unmapped `IntegrityError` for that shows up as an opaque 500,
which is a worse failure than the theatre-defeating gap this constraint exists to close.

`regexp_replace(description, '^[whitespace]+|[whitespace]+$', '', 'g')` trims only the
ends, exactly mirroring `strip_whitespace=True`, so the database's answer and the API's
agree on every input rather than only on the ones without an internal space. See `_NOW`
below for the exact pattern.
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_expenses_description_length"
_WAS = (
    "description IS NOT NULL "
    r"AND char_length(regexp_replace(description, '\s', '', 'g')) >= 3"
)
_NOW = (
    "description IS NOT NULL "
    r"AND char_length(regexp_replace(description, '^\s+|\s+$', '', 'g')) >= 3"
)


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "expenses", type_="check")
    op.create_check_constraint(_CONSTRAINT, "expenses", _NOW)


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "expenses", type_="check")
    op.create_check_constraint(_CONSTRAINT, "expenses", _WAS)
