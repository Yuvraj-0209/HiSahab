"""The spring math, checked rather than eyeballed (CLAUDE.md §13.18, §14).

§13.18 records that the frontend has no automated behavioural tests: §14 forbids npm, which
rules out Jest and Vitest, and a headless-browser runner is a build step. That limitation is
real and stands.

**This file is the exception it permits.** The pure half of the motion system -- the spring
integrator, Apple's momentum projection and the rubber-band curve -- is ordinary arithmetic
with no DOM in it, so `node` alone can check it: no `package.json`, no dependency, no build
step, nothing installed. Where node is absent the test skips rather than failing, so the
suite still passes on a machine that has never had it.

## Why these functions specifically

They are the ones whose bugs are *invisible in review and subtly wrong on screen*. A flick
that undershoots, a spring that takes two seconds to settle, a damping parameter that never
reaches the integrator so every "bouncy" preset is secretly critically damped -- none of those
throw, none of them look obviously broken in a screenshot, and a person checking by hand will
not catch them reliably. That is precisely the boundary §13.18 draws: a person judges whether
a sheet *feels* right, and a machine checks the arithmetic underneath it.

The assertions live in `tests/motion_assertions.mjs` rather than being generated from here,
so they can also be run directly during development:

    node tests/motion_assertions.mjs
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

_ASSERTIONS = pathlib.Path(__file__).resolve().parent / "motion_assertions.mjs"

_node = shutil.which("node")

pytestmark = pytest.mark.skipif(
    _node is None,
    reason=(
        "node is not installed. The motion assertions are a bonus, not a requirement: "
        "§14 forbids adding a JavaScript toolchain as a dependency, so the suite must "
        "pass without one."
    ),
)


def test_the_motion_assertions_pass() -> None:
    """Run the node harness and surface its output on failure.

    `check=False` plus an explicit assert rather than `check=True`, so a failure shows the
    harness's own output -- which names the assertion and prints the actual figure -- instead
    of a CalledProcessError that says only that the exit code was 1.
    """
    result = subprocess.run(
        [_node, str(_ASSERTIONS)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"motion assertions failed\n\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


def test_the_harness_is_not_passing_vacuously() -> None:
    """A harness that asserted nothing would exit 0 and read as coverage.

    The same hazard `tests/test_routes.py` documents about enumerating routes, and
    `tests/test_audit_coverage.py` about discovery: a structural test that cannot fail is
    worse than a missing one, because it occupies the space where the real test would go. So
    the count is asserted as a floor, and it has to move when assertions are added.
    """
    result = subprocess.run(
        [_node, str(_ASSERTIONS)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert "assertions passed" in result.stdout
    reported = int(result.stdout.strip().split("\n")[-1].split()[0])
    assert reported >= 16, f"only {reported} assertions ran"
