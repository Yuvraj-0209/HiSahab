"""Structural guarantees over the frontend source (CLAUDE.md §13.18, §14).

Phase 12 wrote these against hand-written ES modules in `app/static/js/`. Phase 23 moved the
frontend to `frontend/src/` (React + TypeScript, built by Vite) and retargeted every rule that
still applies. Each catches a failure that is invisible in the Python suite and total, or
silently wrong, in a browser.

## What moved elsewhere, and why it is not lost

Three Phase 12 checks existed because nothing compiled the JavaScript: every module parses,
every module is reachable from the entry point, and every named import resolves to a real
export. TypeScript now does all three -- `tsc --noEmit` refuses a syntax error and an import
of something not exported, and Vite only bundles what the entry reaches -- and
`tests/test_frontend_suite.py` runs the typecheck from pytest. The fourth, "the DOM helper is
the only place that sets text", described a helper that no longer exists: React sets text, and
the rule it protected is now `test_no_module_builds_markup_from_a_string` below.

## Why the checks are written the way they are

They scan source text, which this codebase has learned to be careful about. Phase 10 recorded
a structural test that tripped over the *comment documenting the rule it checked*, and Phase 12
found the opposite: a comment describing an endpoint silently satisfied the search for it. So
the rules are stated in **these docstrings**, and the scanners strip comments from the files
they read.
"""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "frontend" / "src"


def _modules() -> list[pathlib.Path]:
    """Discovered by walking the directory, never a hardcoded list, so the file somebody adds
    next week is covered. Tests and the generated API types are not application code."""
    modules = sorted(
        path
        for path in _SRC.rglob("*")
        if path.suffix in {".ts", ".tsx"}
        and ".test." not in path.name
        and path.name != "schema.d.ts"
    )
    assert len(modules) > 20, "frontend modules not found -- did frontend/src move?"
    return modules


def _code_lines(path: pathlib.Path) -> list[tuple[int, str]]:
    """Source lines with `//` and `/* */` comments stripped (JSX's `{/* */}` included).

    Crude on purpose: a real TypeScript parser would be a dependency for a check that only has
    to stop prose from satisfying, or failing, a search.
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
        line = re.sub(r"(?<![:\\])//.*$", "", line)
        if line.strip():
            out.append((number, line))
    return out


def _name(path: pathlib.Path) -> str:
    return str(path.relative_to(_ROOT))


# --- the two rules that produce silently wrong money, or a stolen session -----------------


def test_no_module_builds_markup_from_a_string() -> None:
    """§14: never markup from a string, and `dangerouslySetInnerHTML` is that by another name.

    Every value this app renders is something a person typed into a database -- a customer
    name, an expense description, a reversal reason. With a script-readable token (§13.19),
    one containing `<img src=x onerror=...>` is a stolen session rather than a cosmetic glitch.
    React escapes text it renders; these are the ways around that escaping.
    """
    forbidden = ("dangerouslySetInnerHTML", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")
    offenders = [
        f"{_name(path)}:{number}: {line.strip()}"
        for path in _modules()
        for number, line in _code_lines(path)
        if any(token in line for token in forbidden)
    ]
    assert offenders == [], f"markup built from strings: {offenders}"


def test_no_money_value_is_parsed_into_a_float() -> None:
    """§3 rule 1 does not stop at the API boundary.

    JavaScript has no decimal type. Every money figure arrives from the server as a string,
    already computed, so there is nothing left for the client to add up; `parseFloat` on one
    produces a plausible wrong number rather than an error.
    """
    offenders = [
        f"{_name(path)}:{number}: {line.strip()}"
        for path in _modules()
        for number, line in _code_lines(path)
        if "parseFloat" in line
    ]
    assert offenders == [], f"float parsing in the money path: {offenders}"


def test_chart_geometry_is_assigned_from_the_server_never_derived() -> None:
    """A chart is where client-side maths looks harmless, because the output is a pixel.

    `bar_height_pct` and `share_pct` exist so it never happens: the server divides in Decimal
    and the client assigns a string. Re-scaling a page of bars to its own tallest would be
    `value / max` in JavaScript and a lie besides -- every page's tallest bar would reach the
    top. So every inline `height` or `width` in the chart module must be one of those fields.

    Phase 26 added three more of the same kind, each a percentage string computed in `Decimal`
    by the server: `bar_pct` (a category bar's length against the largest), and the udhaar
    bridge's `width_pct` and `offset_pct` -- where a bar *starts* is geometry too, so an inline
    `left` is held to the same rule as a width.
    """
    chart = _SRC / "ui" / "chart.tsx"
    assert chart.exists(), "ui/chart.tsx moved -- update this test"

    allowed = ("bar_height_pct", "share_pct", "bar_pct", "width_pct", "offset_pct")
    assignments = [
        (number, line.strip())
        for number, line in _code_lines(chart)
        if re.search(r"\b(height|width|left)\s*:", line)
    ]
    assert assignments, "no inline geometry found -- the scan is not looking at the chart"
    offenders = [f"chart.tsx:{number}: {line}" for number, line in assignments if not any(name in line for name in allowed)]
    assert offenders == [], f"chart geometry not assigned from a server percentage: {offenders}"
    # A bar's start is the server's offset, never a figure subtracted from another here.
    assert "left: row.offset_pct" in chart.read_text()

    # The donut's arcs are sized by the server's share too, never by a computed fraction.
    source = chart.read_text()
    assert "percentForGeometry(slice.share_pct)" in source
    assert 'pathLength={100}' in source


# --- the rule that produces a silently dead button ----------------------------------------


def test_no_module_calls_a_secure_context_only_api_unguarded() -> None:
    """`crypto.randomUUID` is `[SecureContext]`: undefined over LAN HTTP, and the failure is total.

    `http://192.168.1.23:8000` is how this app is reached from a phone on the local network,
    and there the property is simply missing: calling it throws inside every Add button's
    handler, and every money entry in the app is a dead tap with nothing to say why. A desktop
    on localhost can never reproduce it.

    So it may be named only in `api/client.ts`, and only beside a `crypto.getRandomValues`
    fallback -- which is not secure-context-gated, so the fallback is real randomness rather
    than `Math.random` (a guessable idempotency key is a replay handed to whoever guesses it).
    """
    offenders: list[str] = []
    for path in _modules():
        source = path.read_text()
        for number, line in _code_lines(path):
            if "crypto.randomUUID" not in line:
                continue
            if path == _SRC / "api" / "client.ts" and "crypto.getRandomValues" in source and "typeof crypto.randomUUID" in line:
                continue
            offenders.append(f"{_name(path)}:{number}: {line.strip()}")
    assert offenders == [], f"unguarded secure-context API: {offenders}"


# --- every feature the API has is reachable from the app ----------------------------------


def test_every_router_is_reachable_from_a_screen() -> None:
    """A router that ships with no way to reach it fails here, not in review.

    The same construction as `tests/test_audit_coverage.py`, for the same reason: the audit gap
    survived seven phases "because nothing failed when it was missing". Routers are discovered
    by listing the directory, so the module added next week is covered without anyone
    remembering to add it. The check is deliberately loose about HOW a screen reaches a router
    -- its URL prefix appearing anywhere in non-comment source -- because the failure it guards
    against is a router with nothing at all pointing at it.
    """
    routers = {
        path.stem
        for path in (_ROOT / "app" / "api" / "v1").glob("*.py")
        if path.stem not in {"__init__", "router"}
    }
    assert routers, "no routers found -- did app/api/v1 move?"

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
        "credit_opening_balances": "/credit-opening-balances",
        "non_fuel_sales": "/non-fuel-sales",
        "bank_deposits": "/bank-deposits",
        "cash_position": "/cash-position",
        "shortfalls": "shortfall",
        "daily_summaries": "/daily-summaries",
        "audit_logs": "/audit-logs",
        # Serves /bank-accounts, /bank-statements and /bank-transactions; this takes the one
        # only this router can satisfy.
        "bank_statements": "/bank-statements",
        "reports": "/reports",
        "users": "/users",
    }

    unmapped = routers - prefixes.keys()
    assert unmapped == set(), (
        f"new router(s) with no entry in this test's prefix map: {sorted(unmapped)}. "
        "Add the URL prefix, then make sure a screen actually calls it."
    )

    # "health": an operator's liveness probe, not a feature. Rendering it would be inventing a
    # screen to satisfy a test.
    exempt = {"health"}

    source = "\n".join("\n".join(line for _, line in _code_lines(path)) for path in _modules())
    unreachable = sorted(name for name in routers - exempt if prefixes[name] not in source)
    assert unreachable == [], f"routers no screen calls -- the feature exists in the API and not in the app: {unreachable}"


# --- motion (Phase 24) ------------------------------------------------------------------


_GSAP_IMPORT = re.compile(r"""(from\s+|import\s*\(\s*)["'](gsap|@gsap/react)(/[^"']*)?["']""")
_MOTION = _SRC / "motion"


def _animating_modules() -> list[pathlib.Path]:
    """Modules that choreograph with GSAP: the motion module itself, and everything importing
    one of its GSAP files (the spring and gesture modules are a different engine)."""
    gsap_files = {"gsap", "scroll", "flip", "draw", "story"}
    importer = re.compile(r"""from\s+["'][./]*(?:\.\./)*motion/(%s)["']""" % "|".join(gsap_files))
    return [
        path
        for path in _modules()
        if (path.parent == _MOTION and path.stem in gsap_files)
        or any(importer.search(line) for _, line in _code_lines(path))
    ]


def test_gsap_is_imported_only_through_the_motion_module() -> None:
    """§14 (Phase 24): `frontend/src/motion/` applies `prefers-reduced-motion` once, for every
    animation, and keeps each GSAP plugin in the chunk that uses it. A direct import skips both:
    the reduced-motion gate becomes something each file has to remember, and a plugin imported
    from a shared module lands in every chunk that shares it."""
    offenders = [
        f"{_name(path)}:{number}: {line.strip()}"
        for path in _modules()
        if _MOTION not in path.parents
        for number, line in _code_lines(path)
        if _GSAP_IMPORT.search(line)
    ]
    assert offenders == [], f"gsap imported outside src/motion/: {offenders}"


def test_no_animation_writes_text() -> None:
    """§14: a money figure is never tweened. Counting ₹0 up to ₹1,23,456 computes rupee values in
    JavaScript that never existed and shows them; scrambling one shows digits nobody sent.

    `Amount` rolls the *characters of the server's string* as elements React rendered, so no
    animating module needs to write text at all. The rule is therefore mechanical: a module that
    animates never touches `textContent` or `innerText`, and the GSAP plugins whose whole job is
    rewriting text are not used anywhere. (The toast's live region writes `textContent` to be
    announced, and does not animate with GSAP.)"""
    animating = _animating_modules()
    assert any(path.name == "Amount.tsx" for path in animating), "the scan found no animating modules"
    offenders = [
        f"{_name(path)}:{number}: {line.strip()}"
        for path in animating
        for number, line in _code_lines(path)
        if "textContent" in line or "innerText" in line
    ] + [
        f"{_name(path)}:{number}: {line.strip()}"
        for path in _modules()
        for number, line in _code_lines(path)
        if "ScrambleText" in line or "TextPlugin" in line
    ]
    assert offenders == [], f"animation that writes text: {offenders}"


def test_the_css_motion_tokens_mirror_the_gsap_ones() -> None:
    """One physics, two copies (Phase 24 D2). `src/motion/gsap.ts` names every duration and ease
    GSAP uses; `styles.css` mirrors them as `--dur-*` / `--ease-*` for the motion CSS does on its
    own (the screen entrance, the tab indicator, view transitions). If the copies drift, a
    CSS-driven entrance and a GSAP-driven one stop feeling like the same app."""
    source = (_SRC / "motion" / "gsap.ts").read_text()
    css = (_SRC / "styles.css").read_text()

    durations = re.findall(r"^\s+(\w+): (\d+\.\d+),$", source.split("export const DURATION", 1)[1].split("} as const", 1)[0], re.M)
    eases = re.findall(r'^\s+(\w+): \{ gsap: "[^"]+", css: "([^"]+)" \},$', source.split("export const EASE", 1)[1].split("} as const", 1)[0], re.M)
    assert len(durations) == 4 and len(eases) == 4, (durations, eases)

    def token(name: str) -> str:
        match = re.search(rf"--{name}:\s*([^;]+);", css)
        assert match, f"--{name} is not defined in styles.css"
        return match.group(1).strip()

    for name, seconds in durations:
        assert token(f"dur-{name}") == f"{round(float(seconds) * 1000)}ms", name
    for name, curve in eases:
        assert token(f"ease-{name}") == curve, name


def test_every_navigation_goes_through_use_go() -> None:
    """Phase 24 D3: `useGo` (src/app/navigation.ts) is `useNavigate` with a direction and a view
    transition. A screen that calls `useNavigate` directly arrives with no sense of where it came
    from -- not broken, just quietly unlike every other screen, which is how a consistent motion
    language erodes one file at a time."""
    offenders = [
        f"{_name(path)}:{number}: {line.strip()}"
        for path in _modules()
        if path != _SRC / "app" / "navigation.ts"
        for number, line in _code_lines(path)
        if "useNavigate" in line
    ]
    assert offenders == [], f"navigation that bypasses useGo: {offenders}"


# --- the front door (Phase 24 D7) -------------------------------------------------------


def _rupees(text: str) -> "Decimal":
    from decimal import Decimal

    return Decimal(text.replace("₹", "").replace(",", "").replace("−", "-"))


def test_the_front_doors_sample_figures_agree_with_each_other() -> None:
    """§14 (Phase 24): the front door shows sample figures, as literal strings, never computed in
    JavaScript. That makes them easy to get wrong silently -- a buyer who checks the gap
    against its terms, or a percentage against its rupees, should find the arithmetic right.
    Checked here, in Decimal, so the client never does money arithmetic even to verify itself."""
    from decimal import Decimal

    source = (_SRC / "showroom" / "samples.ts").read_text()

    def money(name: str) -> Decimal:
        match = re.search(rf'{name}: "(−?₹[\d,]+\.\d\d)"', source)
        assert match, name
        return _rupees(match.group(1))

    # The gap: accountable = metered − card − UPI − udhaar; declared short by the gap.
    terms = [_rupees(v) for v in re.findall(r'value: "(₹[\d,]+\.\d\d)", sign', source)]
    assert len(terms) == 4
    accountable = terms[0] - sum(terms[1:])
    assert accountable == money("accountable")
    assert accountable - money("declared") == money("gap")

    # The ledger and the bill.
    balances = [_rupees(v) for v in re.findall(r'balance: "(₹[\d,]+\.\d\d)"', source)]
    amounts = [_rupees(v) for v in re.findall(r'amount: "(−?₹[\d,]+\.\d\d)", balance', source)]
    running = Decimal("0")
    for amount, balance in zip(amounts, balances, strict=True):
        running += amount
        assert running == balance
    assert money("before") + money("udhaar") - money("repaid") == money("billed") == balances[-1]

    # Paytm settles yesterday's card + UPI as one credit.
    assert terms[1] + terms[2] == _rupees(re.search(r'PAYTM PAYMENTS SERVICES", amount: "(₹[\d,]+\.\d\d)"', source).group(1))

    # Each mix sums to the month's sales, and each share is its rupees over the total to 0.1%.
    month = money("sales")
    for block in ("FUEL_MIX", "PAYMENT_MIX"):
        body = source.split(f"export const {block}", 1)[1].split("];", 1)[0]
        rows = re.findall(r'share_pct: "([\d.]+)%".*?value: "(₹[\d,]+\.\d\d)"|value: "(₹[\d,]+\.\d\d)", share_pct: "([\d.]+)%"', body)
        pairs = [(Decimal(a or d), _rupees(b or c)) for a, b, c, d in rows]
        assert len(pairs) >= 3, block
        assert sum(value for _, value in pairs) == month, block
        for share, value in pairs:
            assert (value / month * 100).quantize(Decimal("0.1")) == share, (block, share, value)
