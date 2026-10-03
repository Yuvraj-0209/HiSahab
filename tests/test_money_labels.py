"""A variance and a gap carry opposite signs, and the screens must say so (CLAUDE.md §6.4).

    gap      = accountable_cash − declared_cash    positive = short,   negative = surplus
    variance = actual_counted − expected_closing   positive = surplus, negative = short

Every variance in the app used to be labelled with `gapLabel`, so both the word and the
chart's direction were inverted: 15 Sept 2026 counted ₹455.00 against an expected
−₹1,69,190.75 and the day screen called the +₹1,69,645.75 surplus "short". The stored figures
were right throughout; only the reading of them was backwards -- which is exactly the kind of
error nobody notices until it has put a shortage on the wrong side of somebody's name.

Two layers, because either alone can pass vacuously:

* `frontend/src/lib/money.test.ts` (Vitest, run from pytest by `test_frontend_suite.py`) checks
  the labels themselves. Phase 23 moved it there from `money_assertions.mjs`.
* The structural checks below, which need no Node, refuse any screen passing a variance to
  `gapLabel` -- so the bug cannot come back through a new screen copying an old one.
"""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SRC = _ROOT / "frontend" / "src"


def test_no_screen_labels_a_variance_with_gap_label() -> None:
    sources = [path for path in _SRC.rglob("*.tsx") if ".test." not in path.name]
    assert sources, "no screens found -- did frontend/src move?"
    offenders = []
    for path in sorted(sources):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if re.search(r"gapLabel\([^)]*variance", line):
                offenders.append(f"{path.relative_to(_ROOT)}:{number}: {line.strip()}")
    assert not offenders, (
        "A variance is counted − expected, so positive means SURPLUS -- the opposite of a "
        "gap. Use varianceLabel:\n" + "\n".join(offenders)
    )


def test_the_variance_chart_does_not_read_the_sign_itself() -> None:
    """The chart once used its own `startsWith("-")` test and called a positive variance short.

    Direction comes from `varianceIsShort` in lib/money.ts, the one place the convention lives.
    """
    chart = (_SRC / "ui" / "chart.tsx").read_text()
    assert "variance.trim().startsWith" not in chart
    assert "varianceIsShort(" in chart
