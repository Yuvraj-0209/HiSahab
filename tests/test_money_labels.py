"""A variance and a gap carry opposite signs, and the screens must say so (CLAUDE.md §6.4).

    gap      = accountable_cash − declared_cash    positive = short,   negative = surplus
    variance = actual_counted − expected_closing   positive = surplus, negative = short

Every variance in the app used to be labelled with `gapLabel`, so both the word and the
chart's direction were inverted: 15 Sept 2026 counted ₹455.00 against an expected
−₹1,69,190.75 and the day screen called the +₹1,69,645.75 surplus "short". The stored figures
were right throughout; only the reading of them was backwards -- which is exactly the kind of
error nobody notices until it has put a shortage on the wrong side of somebody's name.

Two layers, because either alone can pass vacuously:

* `money_assertions.mjs`, run under node where present (the `test_motion.py` exception to
  §13.18 -- money.js is pure, so nothing is installed), checks the labels themselves.
* A structural check, which needs no node, refuses any screen passing a variance to
  `gapLabel` -- so the bug cannot come back through a new screen copying an old one.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_ASSERTIONS = _ROOT / "tests" / "money_assertions.mjs"
_JS = _ROOT / "app" / "static" / "js"

_node = shutil.which("node")


@pytest.mark.skipif(_node is None, reason="node is not installed; see test_motion.py")
def test_the_money_label_assertions_pass() -> None:
    result = subprocess.run(
        [_node, str(_ASSERTIONS)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, (
        f"money assertions failed\n\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    # Not passing vacuously: the harness reports its own count as its last line.
    reported = int(result.stdout.strip().split("\n")[-1].split()[0])
    assert reported >= 8, f"only {reported} assertions ran"


def test_no_screen_labels_a_variance_with_gap_label() -> None:
    offenders = []
    for path in sorted(_JS.rglob("*.js")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if re.search(r"gapLabel\([^)]*variance", line):
                offenders.append(f"{path.relative_to(_ROOT)}:{number}: {line.strip()}")
    assert not offenders, (
        "A variance is counted − expected, so positive means SURPLUS -- the opposite of a "
        "gap. Use varianceLabel:\n" + "\n".join(offenders)
    )


def test_the_variance_chart_does_not_read_the_sign_itself() -> None:
    """The chart used its own `startsWith("-")` test and called a positive variance short.

    Direction comes from `varianceIsShort` in money.js, the one place the convention lives.
    """
    chart = (_JS / "ui" / "chart.js").read_text()
    assert "variance.trim().startsWith" not in chart
    assert "varianceIsShort(" in chart
