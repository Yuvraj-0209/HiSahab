"""The three refusals every date-window read shares (CLAUDE.md §6.1, §9).

A window is a `from` and a `to`, both business dates, both inclusive. Three shapes of it are
data-entry errors rather than questions with an empty answer, and every endpoint that takes a
window refuses them the same way:

* `from` after `to`            -> 422 `INVALID_DATE_RANGE`
* longer than the endpoint's cap -> 422 `INVALID_DATE_RANGE`
* `to` after today at the outlet -> 422 `BUSINESS_DATE_IN_FUTURE` (§6.1: trading has not
  happened yet)

Extracted in Phase 21, when the credit statement became the second caller of what had been
`reports._resolve_window`'s private checks. One copy means the wording and the order of the
checks cannot drift between the Reports tab and the Credit tab. `expenses.py` and
`bank_statements.py` still carry their own older copies; converging them is not this phase's
job, and they are recorded in `docs/phase-21-notes.md` rather than silently left.

**Defaults are deliberately not here.** `reports` anchors a missing `to` on the outlet's most
recent trading day (§13.30); the credit statement requires both dates. Those are different
answers to a different question, and folding them in would make this helper take a flag for
every caller.
"""

from __future__ import annotations

from datetime import date

from app.core.errors import AppError


def validate_window(
    date_from: date, date_to: date, *, today: date, max_days: int
) -> None:
    """Refuse the three bad windows, in the order a reader would notice them.

    `today` is the outlet's local date (`shifts.outlet_today`), never the UTC date -- at 23:00
    IST the UTC date is still yesterday, and the current trading day would be refused as the
    future (§6.1).
    """
    if date_from > date_to:
        raise AppError(
            status_code=422,
            code="INVALID_DATE_RANGE",
            detail="`from` must not be after `to`.",
        )
    if (date_to - date_from).days + 1 > max_days:
        raise AppError(
            status_code=422,
            code="INVALID_DATE_RANGE",
            detail=(
                f"The range cannot exceed {max_days} days. A longer window would cost more "
                "to compute than one screen should."
            ),
        )
    if date_to > today:
        raise AppError(
            status_code=422,
            code="BUSINESS_DATE_IN_FUTURE",
            detail=(
                "That date is in the future. Trading has not happened yet, so there is "
                "nothing to report."
            ),
        )
