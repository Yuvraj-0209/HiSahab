"""Reversal reasons must be more than whitespace.

Phase 7, Step 0. `ck_collections_reversal_has_reason` required NOT NULL and nothing more,
and the API's `Field(min_length=3)` measured the string *before* the handler stripped it --
so "   " passed validation at length 3 and stored as "". The result was a permanent
negation of real money with no stated cause, which is precisely the row §6.9 exists to
prevent.

The API fix (strip before validating) closes the reachable path. This closes the rest:
`app/services/collections.py::reverse` is callable from a management command or a test
without Pydantic anywhere in the picture, and §6.6's belt-and-braces rule applies for the
same reason it applies to the receipt constraint -- a client can bypass validation, but not
a database constraint.

A regex requiring one non-space character, rather than a length floor. The 3-character
minimum is a *UI* judgement about what makes a useful reason and belongs where it can be
tuned without a migration; what the database guarantees is the thing that can never be
legitimate -- a reason that says nothing at all.

`~ '[^[:space:]]'` rather than `btrim(...) <> ''`, because one-argument `btrim` strips
**spaces only**: a tab-only reason survived it. The test that trips this constraint
parametrises a tab for exactly that reason. The `IS NOT NULL` clause is not redundant --
`~` against NULL yields NULL, and a NULL CHECK counts as satisfied.

Phase 7's `ck_expenses_reversal_has_reason` must be written this way from the start rather
than copying the weaker Phase 6 shape.
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_collections_reversal_has_reason"
_WAS = "reverses_id IS NULL OR reversal_reason IS NOT NULL"
_NOW = (
    "reverses_id IS NULL "
    "OR (reversal_reason IS NOT NULL AND reversal_reason ~ '[^[:space:]]')"
)


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "collections", type_="check")
    op.create_check_constraint(_CONSTRAINT, "collections", _NOW)


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "collections", type_="check")
    op.create_check_constraint(_CONSTRAINT, "collections", _WAS)
