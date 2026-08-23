"""Structural guarantees over the frontend assets (CLAUDE.md §13.18, §14).

§13.18 is honest that the JavaScript has no behavioural tests. These are the guarantees
Python *can* make about it, and they are chosen for one property: **each catches a failure
that is invisible in the Python suite and total in a browser.**

A syntax error in a module is the clearest case. Every Python test still passes -- the server
serves the file perfectly, with the right content type -- and the application is a blank
screen. Nothing in the suite before this file would have noticed.

## Why the checks are written the way they are

Two of them scan source text, which this codebase has learned to be careful about. Phase 10
recorded a structural test that tripped over the *comment documenting the rule it checked*,
and concluded "left as a text search, it would have taught the next person to delete the
comment". So the rules here are stated in **these docstrings**, in Python, and the scanners
deliberately skip comment lines in the files they read. A rule's explanation must never be
the thing that breaks its own test.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

_STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "static"
_JS = sorted(_STATIC.rglob("*.js"))

_node = shutil.which("node")


def _js_modules() -> list[pathlib.Path]:
    """Discovered by walking the directory, never a hardcoded list.

    The same construction `tests/test_audit_coverage.py` uses, for the same reason: a
    hardcoded list silently stops covering the file somebody adds next week.
    """
    assert _JS, "no JavaScript modules found -- did the static directory move?"
    return _JS


def _code_lines(path: pathlib.Path) -> list[tuple[int, str]]:
    """Source lines with comments and block comments stripped.

    Crude but sufficient, and the crudeness is deliberate: a real JS parser would be a
    dependency (§14). What matters is that a rule documented in a comment cannot fail the
    test that enforces it.
    """
    out: list[tuple[int, str]] = []
    in_block = False
    for number, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw
        if in_block:
            if "*/" in line:
                line = line.split("*/", 1)[1]
                in_block = False
            else:
                continue
        while "/*" in line:
            before, _, rest = line.partition("/*")
            if "*/" in rest:
                line = before + rest.split("*/", 1)[1]
            else:
                line = before
                in_block = True
                break
        line = re.sub(r"//.*$", "", line)
        if line.strip():
            out.append((number, line))
    return out


# --- the assets are valid -----------------------------------------------------


@pytest.mark.skipif(_node is None, reason="node is not installed; §14 forbids requiring one")
@pytest.mark.parametrize("path", _js_modules(), ids=lambda p: p.name)
def test_every_module_parses(path: pathlib.Path) -> None:
    """A syntax error is a blank page and a green suite.

    `node --check` parses without executing, so this needs no DOM and no dependency. It is
    the single highest-value check in this file: every other frontend failure at least shows
    *something*.
    """
    result = subprocess.run(
        [_node, "--check", str(path)], capture_output=True, text=True, timeout=30
    )

    assert result.returncode == 0, f"{path.name} does not parse:\n{result.stderr}"


def test_no_module_imports_from_outside_this_origin() -> None:
    """§14 forbids the npm dependency; a bare or absolute specifier is one by another route.

    Without a bundler a bare specifier (`import x from "motion"`) does not even resolve in a
    browser, so this is also a correctness check -- but the reason it is enforced rather than
    left to fail at runtime is §13.19: the session token is readable by script, so a
    third-party module is a session theft waiting for that host to be compromised.
    """
    offenders: list[str] = []
    pattern = re.compile(r"""(?:import|export)[^'"]*?from\s+['"]([^'"]+)['"]""")

    for path in _js_modules():
        for number, line in _code_lines(path):
            for specifier in pattern.findall(line):
                if not specifier.startswith("."):
                    offenders.append(f"{path.name}:{number}: {specifier}")

    assert offenders == [], f"non-relative imports: {offenders}"


# --- the two rules that produce silently wrong money --------------------------


def test_no_module_builds_markup_from_a_string() -> None:
    """§14: never innerHTML on a server-derived value.

    Every value this app renders is something a person typed into a database -- a customer
    name, an expense description, a reversal reason. With a script-readable token (§13.19),
    one of those containing `<img src=x onerror=...>` is a stolen session rather than a
    cosmetic glitch.

    `textContent` cannot produce an element, so `js/dom.js` routes every node through it and
    the safe path is also the shortest one to write. This test is what stops the first
    "just this once" from being a quiet exception.
    """
    forbidden = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")
    offenders: list[str] = []

    for path in _js_modules():
        for number, line in _code_lines(path):
            for token in forbidden:
                if token in line:
                    offenders.append(f"{path.name}:{number}: {line.strip()}")

    assert offenders == [], f"markup built from strings: {offenders}"


def test_no_money_value_is_parsed_into_a_float() -> None:
    """§3 rule 1 does not stop at the API boundary, and §14 now says so explicitly.

    JavaScript has no decimal type: `0.1 + 0.2 !== 0.3` there exactly as in Python. Every
    money figure this app displays -- totals, gaps, outstanding balances, variances -- is
    already computed server-side and arrives as a **string**, so there is nothing left for
    the client to add up. `parseFloat` on one is the same bug as `float` in a fixture, one
    language further out, and it produces a plausible wrong number rather than an error.

    `parseInt` is permitted: `max_upload_bytes` and a cursor limit are counts, not money.
    """
    offenders: list[str] = []

    for path in _js_modules():
        for number, line in _code_lines(path):
            if "parseFloat" in line:
                offenders.append(f"{path.name}:{number}: {line.strip()}")

    assert offenders == [], f"float parsing in the money path: {offenders}"


def test_the_dom_helper_is_the_only_place_that_sets_text() -> None:
    """A weaker guarantee than it sounds, and worth stating precisely.

    It does not forbid `textContent` elsewhere -- toasts and the nav set it directly, and
    that is fine because `textContent` is the *safe* operation. What this pins is that
    `js/dom.js` exists and is the module the others build through, so the innerHTML rule
    above has one place to be enforced rather than being a convention everybody remembers.
    """
    dom = _STATIC / "js" / "dom.js"

    assert dom.exists(), "js/dom.js is where the innerHTML rule is enforced"
    assert "textContent" in dom.read_text()
