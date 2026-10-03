"""Run the frontend's behavioural suite from pytest (Phase 23, CLAUDE.md §13.18, §15).

`pytest` stays the one command that answers "is this safe to ship". The Vitest suite under
frontend/src/ is where the client's money rules are tested as behaviour -- the gap and variance
sign words, a submission reusing its Idempotency-Key across a retry, the spring physics -- and a
suite nobody runs protects nothing.

Skipped when Node or the installed dependencies are absent, as the Phase 12 node harnesses
were: the Python suite must still run on a machine that only has Python.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

_FRONTEND = pathlib.Path(__file__).resolve().parents[1] / "frontend"

pytestmark = pytest.mark.skipif(
    shutil.which("npm") is None or not (_FRONTEND / "node_modules").is_dir(),
    reason="Node or frontend/node_modules is absent; run `npm ci` in frontend/",
)


def test_the_typecheck_passes() -> None:
    """`tsc --noEmit`: what Phase 12's node-based checks did by hand -- every module parses,
    every import resolves to a real export -- and the typed API contract besides. A field the
    server renames fails here instead of rendering `undefined` beside a rupee sign."""
    result = subprocess.run(
        ["npm", "run", "typecheck", "--silent"],
        cwd=_FRONTEND,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]


def test_the_vitest_suite_passes() -> None:
    result = subprocess.run(
        ["npm", "test", "--silent"],
        cwd=_FRONTEND,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    # Not vacuous: the suite must actually have found and run tests.
    assert " passed" in result.stdout, result.stdout[-2000:]
