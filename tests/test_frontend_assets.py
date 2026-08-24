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


def test_every_module_is_reachable_from_the_entry_point() -> None:
    """No orphans: a module nobody imports is dead code that still has to be maintained.

    Walks the import graph from js/main.js the way the browser does, following relative
    specifiers. Anything under js/ that the walk never reaches is either unused or was meant
    to be wired up and was forgotten -- and the second case is a feature that silently does
    not exist.
    """
    entry = _STATIC / "js" / "main.js"
    assert entry.exists()

    seen: set[pathlib.Path] = set()
    queue = [entry]
    pattern = re.compile(r"""(?:import|export)[^'"]*?from\s+['"]([^'"]+)['"]""")

    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        for specifier in pattern.findall(current.read_text()):
            if not specifier.startswith("."):
                continue  # bare specifiers cannot occur -- see the CDN test below
            target = (current.parent / specifier).resolve()
            if target.exists():
                queue.append(target)

    orphans = sorted(path.name for path in set(_js_modules()) - seen)

    assert orphans == [], f"modules nothing imports: {orphans}"


def test_every_named_import_resolves_to_a_real_export() -> None:
    """The blank-page bug, and the reason `node --check` did not catch it.

    `ui/sheet.js` and `ui/toast.js` both imported `project` from `motion/gesture.js`, where it
    does not exist -- it is exported by `motion/spring.js`. In ES modules that is a
    **link-time** error, not a runtime one: the browser refuses the entire module graph, so
    `main.js` never executes and the page renders as a blank white screen.

    Every test in this file passed. `node --check` parses one file at a time and has no idea
    what another module exports, the mount served all fifteen files with a 200, and the import
    graph walk only checked that the *file* existed -- not that the names came out of it.

    This is precisely the gap §13.18 admits to and the reason that section names structural
    checks as the half that has to be automated: the failure is invisible in Python, total in a
    browser, and produces no error anywhere a test was looking.

    Parsing is deliberately shallow -- a real JS parser would be a dependency (§14) -- and
    only handles the two forms this codebase actually uses: `export function/class/const NAME`
    and `import { a, b as c } from "./x.js"`. Default and namespace imports are not used here;
    if one ever is, this test skips it rather than guessing.
    """
    export_pattern = re.compile(r"^export\s+(?:async\s+)?(?:function|class|const|let|var)\s+(\w+)")
    import_pattern = re.compile(
        r"""import\s*\{([^}]*)\}\s*from\s*['"]([^'"]+)['"]""", re.MULTILINE
    )

    exports: dict[pathlib.Path, set[str]] = {}
    for path in _js_modules():
        names = set()
        for _, line in _code_lines(path):
            match = export_pattern.match(line.strip())
            if match:
                names.add(match.group(1))
        exports[path.resolve()] = names

    offenders: list[str] = []
    for path in _js_modules():
        source = "\n".join(line for _, line in _code_lines(path))
        for raw_names, specifier in import_pattern.findall(source):
            if not specifier.startswith("."):
                continue
            target = (path.parent / specifier).resolve()
            if target not in exports:
                offenders.append(f"{path.name}: imports from missing module {specifier}")
                continue
            for entry in raw_names.split(","):
                name = entry.strip().split(" as ")[0].strip()
                if not name:
                    continue
                if name not in exports[target]:
                    offenders.append(
                        f"{path.name}: imports '{name}' from {specifier}, "
                        f"which does not export it"
                    )

    assert offenders == [], "broken imports (the whole app fails to load):\n  " + "\n  ".join(
        offenders
    )


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


def test_every_router_is_reachable_from_a_screen() -> None:
    """A Phase 13 router that ships with no way to reach it fails here, not in review.

    **This is the deliverable of Step 13**, and it is the same construction
    `tests/test_audit_coverage.py` uses for the same reason. Phase 11's notes put it plainly:
    the audit gap survived seven phases "because nothing failed when it was missing". A screen
    that was never built is exactly that shape -- the API works, every test passes, and the
    feature simply does not exist for anybody using the application.

    Routers are discovered by **listing the directory**, never from a hardcoded list, so the
    module somebody adds next week is covered without anyone remembering to add it here.

    The check is deliberately loose about *how* a screen reaches a router: it looks for the
    router's URL prefix appearing in an `api.get/post/patch` path anywhere under `js/`. A
    stricter check would have to parse JavaScript properly, which needs a dependency (§14),
    and would buy little -- the failure this guards against is a router with *nothing at all*
    pointing at it, not a subtly wrong path.
    """
    routers = {
        path.stem
        for path in (_STATIC.parent / "api" / "v1").glob("*.py")
        if path.stem not in {"__init__", "router"}
    }
    assert routers, "no routers found -- did app/api/v1 move?"

    # Each router's URL prefix, as it appears in a client call. Derived from the module name
    # where they agree, with the handful of genuine exceptions named.
    prefixes = {
        "health": "/health",
        "me": "/me",
        "client_config": "/client-config",
        "fuel_types": "/fuel-types",
        "nozzles": "/nozzles",
        "fuel_prices": "/fuel-prices",
        "fuel_margins": "/fuel-margins",
        "shift_templates": "/shift-templates",
        "shifts": "/shifts",
        "readings": "/readings",
        "collections": "/collections",
        "expenses": "/expenses",
        "expense_categories": "/expense-categories",
        "uploads": "/uploads/receipt",
        "attachments": "/attachments/",
        "credit_customers": "/credit-customers",
        "credit_sales": "/credit-sales",
        "credit_repayments": "/credit-repayments",
        "non_fuel_sales": "/non-fuel-sales",
        "bank_deposits": "/bank-deposits",
        "cash_position": "/cash-position",
        "shortfalls": "shortfall",
        "daily_summaries": "/daily-summaries",
        "audit_logs": "/audit-logs",
        "reports": "/reports",
        "users": "/users",
    }

    unmapped = routers - prefixes.keys()
    assert unmapped == set(), (
        f"new router(s) with no entry in this test's prefix map: {sorted(unmapped)}. "
        "Add the URL prefix, then make sure a screen actually calls it."
    )

    # Routers that legitimately have no screen. Additions need a reason here, not just an
    # entry -- the same discipline `tests/test_audit_coverage.py` applies to `uploads.py`.
    #
    # "health": an operator's liveness probe, not a feature. It answers whether the process
    #           and Postgres are up, which is a thing a monitor asks and a salesman does not.
    #           Rendering it would be inventing a screen to satisfy a test.
    exempt = {"health"}

    # Comments STRIPPED, and this is not a detail. The first version of this test searched
    # raw source and passed while the audit-log screen was deliberately broken, because that
    # module's own docstring says "Phase 11 built `GET /audit-logs`" -- the prose describing
    # the endpoint satisfied the search for it.
    #
    # That is Phase 10's lesson arriving a third time: "left as a text search, it would have
    # taught the next person to delete the comment." Here it was worse than that -- the
    # comment did not break the test, it *silently satisfied* it, which is the version that
    # never gets noticed.
    source = "\n".join(
        "\n".join(line for _, line in _code_lines(path)) for path in _js_modules()
    )

    unreachable = sorted(
        name for name in routers - exempt if prefixes[name] not in source
    )

    assert unreachable == [], (
        "routers no screen calls -- the feature exists in the API and not in the app: "
        f"{unreachable}"
    )


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
