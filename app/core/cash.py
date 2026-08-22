"""The cash engine's vocabulary (CLAUDE.md §6.4, §6.5).

Phase 10, Step 8 -- written here rather than with the models in Step 2, because until the
daily summary existed nothing imported it, and this repo deletes an uncovered helper rather
than carrying it (§11).

Mirrors app/core/collections.py, app/core/credit.py and app/core/expenses.py: a StrEnum, so
the member compares equal to its own string value and the PostgreSQL enum, the JSON API and
Python all speak one vocabulary.

**There is deliberately no settlement-mode enum here.** §5.2: a salesman repays a shortfall
in cash, so `salesman_shortfall_settlements` has no `mode` column and every row reaches
§6.4's drawer. A one-label enum built for a future nobody has committed to is the scaffolding
§11 forbids, and §5.0's derivability rule says a `mode` column added later backfills to
`'cash'` correctly -- because every row written before it genuinely is cash. §13.15 records
what that costs.
"""

from __future__ import annotations

from enum import StrEnum


class OpeningBalanceSource(StrEnum):
    """Which of §6.5's three branches produced a day's opening balance.

    §6.5 was written assuming a drawer counted every night. This outlet has a locker with no
    fixed counting moment (§14), so most days have no `actual_counted` at all and the balance
    carries forward arithmetically. That makes "where did this figure come from?" a real
    question with three real answers -- and a reader should not have to infer it from the
    previous row, which does not even work for the first day.

    `seeded` -- an admin typed it, once, because there was no previous day. The anchor, in
                §4.7's sense: the first row *is* the record, not a separate seed table.
    `counted` -- the previous day's locker was physically counted, and the count won. This is
                §6.5's headline rule and it still holds wherever a count exists: the physical
                cash carries forward, not the theoretical figure, so a ₹200 shortage stays
                visible in that day's variance and is absent from this day's opening.
    `carried` -- nobody counted, so the previous day's `expected_closing` carried forward.
                The fallback, never the default. It is what makes a locker workable, and it
                is also the branch in which an unnoticed error propagates -- which is exactly
                why it is recorded rather than assumed.
    """

    seeded = "seeded"
    counted = "counted"
    carried = "carried"
