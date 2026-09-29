"""The edit sheet for a saved reading must offer the meter-reset tick (§6.2).

`PATCH /shifts/{id}/readings/{nozzle}` has always accepted `meter_reset_occurred`, and the
admin's "Set manual quantity" button only appears once that flag is set. But the edit sheet
(`closingSheet`) offered closing and testing only -- the tick lived on the *first-entry* sheet.
So once a reading was saved, §6.2's one escape hatch was unreachable from the app.

Found on real data: D1 on 19 Sept 2026 was saved with a chained opening equal to its own
closing (1455561.16 both), so it sold 0 L, and an admin on the reopened shift had no way to
enter the real quantity. Structural rather than behavioural, per §13.18 -- the sheet is DOM
code that node cannot run without a browser.
"""

from __future__ import annotations

import pathlib
import re

_READINGS = (
    pathlib.Path(__file__).resolve().parent.parent
    / "app" / "static" / "js" / "screens" / "readings.js"
)


def _function_body(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    following = re.search(r"\n(?:async )?function \w+\(", source[start + 1 :])
    end = start + 1 + following.start() if following else len(source)
    return source[start:end]


def test_the_edit_sheet_offers_the_meter_reset_tick() -> None:
    body = _function_body(_READINGS.read_text(), "closingSheet")
    assert 'name: "meter_reset_occurred"' in body, (
        "closingSheet must render the meter-reset checkbox; without it the manual-quantity "
        "override cannot be reached for a reading that is already saved."
    )
    assert "meter_reset_occurred" in body.split("api.patch")[0], (
        "the tick must be sent in the PATCH body"
    )
