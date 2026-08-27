"""§8's matrix for the cash engine, plus the structural assertions every permissions file
here carries (CLAUDE.md §3, §5.0, §6.4, §8, §9, §14).

The value-level cases live with their own routers -- `test_non_fuel_sales.py`,
`test_bank_deposits.py`, `test_cash_position.py`, `test_daily_summaries_api.py`,
`test_shortfalls.py`. What is here instead reads the *source*: things a passing endpoint test
cannot prove, like "§6.4's equation never reads a cash collection row" or "the ownership rule
was not quietly re-implemented in this module".

Phase 10 inherits the four `test_credit_permissions.py` carries, and adds two of its own for
the guardrails this phase introduced.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.anyio

_ROUTERS = (
    "app/api/v1/non_fuel_sales.py",
    "app/api/v1/bank_deposits.py",
    "app/api/v1/cash_position.py",
    "app/api/v1/daily_summaries.py",
    "app/api/v1/shortfalls.py",
    # Phase 13. Outlet-and-date scoped like daily_summaries.py, so it belongs here and NOT in
    # _SHIFT_SCOPED below -- it has no shift-scoped dependency to reimplement.
    "app/api/v1/reports.py",
)
_MONEY_PATH = _ROUTERS + (
    "app/services/reporting.py",
    "app/services/cash.py",
    "app/services/shortfalls.py",
    "app/models/cash.py",
    "app/models/shortfall.py",
    "app/core/cash.py",
)

# The routers that hang off a shift. daily_summaries.py is outlet-and-date scoped and
# correctly has no shift-scoped dependency at all.
_SHIFT_SCOPED = (
    "app/api/v1/non_fuel_sales.py",
    "app/api/v1/bank_deposits.py",
    "app/api/v1/cash_position.py",
    "app/api/v1/shortfalls.py",
)


@pytest.mark.parametrize("path", _SHIFT_SCOPED)
def test_no_ownership_check_is_reimplemented_here(path: str) -> None:
    """§8's ownership rule must live in exactly one place: `require_shift_access`.

    A second copy is worse than no copy -- the two drift, and the drift shows up as an
    attendant able to write to somebody else's shift through one route and not another.

    `shortfalls.py` reads `shift.attendant_id`, which is not an ownership *check* but the
    §5.2 fact that exactly one name carries the drawer. The assertions below are on the
    comparison operators for that reason.
    """
    source = Path(path).read_text()

    assert "attendant_id !=" not in source, path
    assert "attendant_id ==" not in source, path
    assert "require_shift_access" in source, path


@pytest.mark.parametrize("path", _SHIFT_SCOPED)
def test_the_outlet_is_never_hardcoded_in_a_shift_scoped_router(path: str) -> None:
    """§8: when acting on an existing row the outlet comes from *the row*, never from config.
    `DEFAULT_OUTLET_ID` in a shift-scoped router would mean a second outlet's shifts silently
    authorising against the first one's membership."""
    source = Path(path).read_text()

    assert "DEFAULT_OUTLET_ID" not in source, path
    assert "get_default_outlet_id" not in source, path


@pytest.mark.parametrize("path", _MONEY_PATH)
def test_no_float_appears_in_the_money_path(path: str) -> None:
    """§3 rule 1, applied to the modules that carry §6.4's terms. A float here would not
    crash -- it would make `expected_closing` disagree with its own components by fractions
    of a paisa that compound, which is the failure this rule exists to prevent."""
    source = Path(path).read_text()

    assert "float(" not in source, path
    assert "sa.Float" not in source, path
    assert ": float" not in source, path


@pytest.mark.parametrize("path", _MONEY_PATH)
def test_no_offset_pagination_and_no_delete_route(path: str) -> None:
    """§9 and §3 rule 6. `OFFSET` causes duplicates and skips during a scroll, and there are
    no hard deletes on a financial table -- corrections append (§6.9)."""
    source = Path(path).read_text()

    assert ".offset(" not in source, path
    assert "@router.delete" not in source, path


def test_the_cash_equation_never_reads_a_cash_collection_row() -> None:
    """**§14's guardrail, pinned structurally.** §5.2 is explicit that the `cash` collection
    row is not a term in §6.4 but the independent observation the derived figure is checked
    against, and that summing the two double-counts the whole day's cash -- producing a
    result that looks entirely reasonable.

    Parsed per-function rather than grepped across the module, because
    `shift_cash_position` legitimately *does* read `declared_cash`: that is the comparison,
    and it lands in `gap`, never in `accountable_cash`. Grepping the file would confuse the
    two, which is exactly the confusion this test exists to prevent.
    """
    source = Path("app/services/cash.py").read_text()
    tree = ast.parse(source)
    functions = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }

    for name in ("expected_closing", "day_totals"):
        body = ast.get_source_segment(source, functions[name])
        assert "declared_cash" not in body, name
        assert "CollectionMode.cash" not in body, name


def test_expected_closing_subtracts_the_booked_shortfall() -> None:
    """§6.4's newest term, and the one whose absence is invisible.

    Without it a booked ₹500 is both the salesman's debt and cash the locker does not
    contain, and every count afterwards is wrong by exactly that amount. A value test proves
    today's arithmetic; this proves the *shape* survives a refactor that reorders the
    expression.
    """
    source = Path("app/services/cash.py").read_text()
    tree = ast.parse(source)
    functions = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }

    body = ast.get_source_segment(source, functions["expected_closing"])
    assert "- totals.shortfalls_booked" in body
    assert "- totals.bank_deposits_total" in body
    assert "+ totals.cash_credit_repayments" in body


def test_non_fuel_income_is_on_the_sales_side_of_the_equation() -> None:
    """§6.4's amendment. On the cash side, a card-paid oil sale understates derived cash by
    exactly its amount -- because the collections row already counted it.

    `cash_sales` is where `non_fuel_sales_total` must appear; `expected_closing` must not add
    it a second time.
    """
    source = Path("app/services/cash.py").read_text()
    tree = ast.parse(source)
    functions = {
        node.name: node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    assert "expected_closing" in functions

    tree_all = ast.parse(source)
    cash_sales = next(
        node
        for node in ast.walk(tree_all)
        if isinstance(node, ast.FunctionDef) and node.name == "cash_sales"
    )
    assert "non_fuel_sales_total" in ast.get_source_segment(source, cash_sales)

    expected = next(
        node
        for node in ast.walk(tree_all)
        if isinstance(node, ast.FunctionDef) and node.name == "expected_closing"
    )
    assert "non_fuel" not in ast.get_source_segment(source, expected)


def test_the_daily_summary_is_never_recomputed_on_read() -> None:
    """§5.2: the stored figure is the record of what the manager was told on the day. A read
    route that recomputed would destroy exactly that, and §13.16 makes the same argument for
    a reopened shift."""
    source = Path("app/api/v1/daily_summaries.py").read_text()
    tree = ast.parse(source)
    functions = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }

    for name in ("read_summary", "list_summaries", "update_summary"):
        body = ast.get_source_segment(source, functions[name])
        assert "day_totals" not in body, name
        assert "expected_closing=" not in body, name


# --- Phase 13: the reporting layer ---------------------------------------------
#
# The sibling of `test_the_daily_summary_is_never_recomputed_on_read` above, and it exists
# because Phase 13 introduced a module that DOES recompute -- deliberately, for the days that
# have no stored record (§13.20). That makes "which branch is allowed to compute" a rule that
# can now be broken by a one-line edit, where before it was structural.


def test_the_snapshot_branch_of_a_report_never_recomputes() -> None:
    """§13.20's whole guarantee, as source.

    `_from_snapshot` turns a stored `daily_cash_summaries` row into a response. Every figure
    in it must be a column read. The moment it calls `day_totals`, or `expected_closing`, or
    does arithmetic over the components, it has stopped reporting what the manager was told
    and started offering a second opinion wearing the record's clothes -- and §6.5 chains
    days, so the difference would propagate into every opening balance after it.

    The behavioural test (`test_a_snapshot_is_read_verbatim_even_after_a_price_revision`)
    proves it for one scenario. This proves it for every scenario, including the ones nobody
    wrote a fixture for.

    **The docstring is stripped before scanning**, and that is not a detail. On its first run
    this test failed against `_from_snapshot`'s own docstring, which says the function must
    not call `day_totals` -- the prose explaining the rule tripped the check enforcing it.
    That is the fourth time this codebase has hit that shape: Phase 10 and Phase 11 each
    recorded a version, and Phase 12's notes record the worse variant where a comment
    *silently satisfied* a search instead of breaking it.

    The lesson those three arrived at is the one applied here: the rule's prose lives in
    THIS docstring, in the test, and the scanner reads only executable source. Otherwise the
    fix a future reader reaches for is deleting the explanation.
    """
    source = Path("app/services/reporting.py").read_text()
    tree = ast.parse(source)
    functions = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }

    target = functions["_from_snapshot"]
    statements = target.body
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements = statements[1:]

    body = "\n".join(
        ast.get_source_segment(source, statement) or "" for statement in statements
    )

    assert "day_totals" not in body
    assert "expected_closing(" not in body
    assert "shift_sales" not in body
    # No arithmetic at all: every value is `summary.<column>`. A `+` here would mean a figure
    # is being derived rather than read, which is the same defect one operator smaller.
    assert not any(
        isinstance(node, ast.BinOp)
        for node in ast.walk(functions["_from_snapshot"])
    ), "_from_snapshot must read columns, never compute them"


@pytest.mark.parametrize(
    "path", ["app/api/v1/reports.py", "app/services/reporting.py"]
)
def test_the_reporting_layer_writes_nothing(path: str) -> None:
    """"It is a report" as a structural fact rather than an intention.

    The specific hazard is that a `computed` day looks exactly like a `daily_cash_summaries`
    row somebody could helpfully persist -- and doing so would silently promote an estimate
    into the record §5.2 says must never be recomputed, with §6.5 then chaining it forward.

    `audit.record` is included because a write that audits itself is still a write, and its
    absence here is also what keeps `test_audit_coverage.py` correctly silent about this
    router rather than accidentally so.
    """
    source = Path(path).read_text()

    for forbidden in ("db.add", "db.commit", "db.flush", "db.delete", "audit.record"):
        assert forbidden not in source, f"{path} must not write ({forbidden})"


def test_the_reports_router_has_no_write_verbs() -> None:
    """Read-only by declaration, not just by current contents.

    `test_audit_coverage.py` walks every `@router.post`/`@router.patch` demanding an
    `audit.record` call. This router has none, so that test is silent -- and this is what
    makes the silence deliberate: adding a write verb here fails HERE with a reason, rather
    than failing over there with a message about audit rows that would send somebody to add
    one instead of asking whether the endpoint should exist.
    """
    source = Path("app/api/v1/reports.py").read_text()

    for verb in ("@router.post", "@router.patch", "@router.put", "@router.delete"):
        assert verb not in source


def test_the_margin_catch_is_narrow_in_source_as_well_as_behaviour() -> None:
    """§13.21's `except` must test the code before swallowing.

    A bare `except AppError: pass`-shaped catch would report a genuine failure as "no
    commission entered" -- a lie that looks like a configuration note, and one nobody would
    investigate because the screen would be saying something entirely plausible.

    Asserted structurally because the behavioural version can only prove it for the one
    error code a test thought to raise.
    """
    source = Path("app/services/reporting.py").read_text()
    tree = ast.parse(source)

    handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
    assert handlers, "reporting.py should catch the two reference-data gaps"

    for handler in handlers:
        segment = ast.get_source_segment(source, handler)
        assert "exc.code" in segment, (
            "every except in reporting.py must inspect the error code and re-raise what it "
            f"does not recognise; this one does not:\n{segment}"
        )
        assert "raise" in segment, segment


def test_every_reporting_endpoint_is_reached_by_a_screen() -> None:
    """`test_frontend_assets.py` only proves that the string "/reports" appears somewhere.

    That is the right check for *router* coverage and too weak for this phase: it would pass
    with two of the three endpoints unbuilt. Each path is asserted by name here instead.

    **The per-path assertion is the point; the file it lands in is not.** Phase 15 moved the
    per-day report out of `reports.js` and into `days.js`, because `#/reports/{date}` and
    `#/daily-summaries/{date}` described one business date from two tables. Pinning the search
    to a single filename made this test fail for a reorganisation it has no opinion about --
    so it now searches the screens directory and keeps the assertion that actually guards
    something: that each of the three endpoints is called by name, somewhere a user can reach.

    Comments are **stripped first**, which `tests/test_frontend_assets.py` learned the hard
    way: prose describing an endpoint silently satisfies a raw text search, and that is the
    version of the failure nobody notices.
    """
    screens = sorted(Path("app/static/js/screens").glob("*.js"))
    assert screens, "no screens found -- did app/static/js/screens move?"

    source = "\n".join(_strip_comments(path.read_text()) for path in screens)

    for path in ("/reports/range", "/reports/variance-alerts", "/reports/daily/"):
        assert path in source, f"no screen calls {path}"


def _strip_comments(source: str) -> str:
    """Remove `//` and `/* */` comments. Crude on purpose -- it only has to stop prose from
    satisfying a search for an endpoint path, and a JavaScript parser is a dependency (§14)."""
    out: list[str] = []
    in_block = False
    for line in source.split("\n"):
        if in_block:
            if "*/" in line:
                line = line.split("*/", 1)[1]
                in_block = False
            else:
                continue
        if "/*" in line:
            head, rest = line.split("/*", 1)
            if "*/" in rest:
                line = head + rest.split("*/", 1)[1]
            else:
                line, in_block = head, True
        out.append(line.split("//", 1)[0])
    return "\n".join(out)


def test_the_shortfall_never_reaches_a_credit_table() -> None:
    """§13.14 and §14, structurally. Staff debt in a customer's ledger means "what does this
    customer owe me" -- the figure §14 says the owner checks first -- stops having an
    answer."""
    for path in ("app/services/shortfalls.py", "app/api/v1/shortfalls.py"):
        source = Path(path).read_text()
        assert "CreditSale" not in source, path
        assert "CreditCustomer" not in source, path
        assert "credit_customer_id" not in source, path


def test_the_salesman_is_never_taken_from_a_booking_payload() -> None:
    """§5.2: exactly one name carries the drawer, and it is read from the shift. A
    client-supplied value on a *booking* would let a typo put a debt on somebody who was not
    even working, with no second source of truth to catch it.

    A *settlement* legitimately names the salesman, because money can arrive on a later shift
    worked by somebody else -- so this is parsed per-class, not grepped.

    **Declared fields, not source text.** `ShortfallCreate` carries a comment saying "No
    `salesman_id`", and a text search would fail on the comment explaining the rule it is
    checking -- which would teach the next person to delete the comment.
    """
    source = Path("app/api/v1/shortfalls.py").read_text()
    tree = ast.parse(source)
    classes = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    }

    def _fields(name: str) -> set[str]:
        return {
            statement.target.id
            for statement in classes[name].body
            if isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
        }

    assert "salesman_id" not in _fields("ShortfallCreate")
    assert _fields("ShortfallCreate") >= {"amount", "reason"}
    assert "salesman_id" in _fields("SettlementCreate")


def test_every_money_creating_post_requires_an_idempotency_key() -> None:
    """§6.10: "This is not optional." Attendants use phones on patchy rural connectivity, and
    a retry after a timeout must not create a second ₹1,00,000 deposit.

    `daily_summaries.py` is the deliberate exception -- `UNIQUE (outlet_id, business_date)`
    makes it naturally idempotent, exactly as §6.10 argues for nozzle readings -- and this
    test asserts the exception rather than tolerating it silently.
    """
    for path in (
        "app/api/v1/non_fuel_sales.py",
        "app/api/v1/bank_deposits.py",
        "app/api/v1/shortfalls.py",
    ):
        source = Path(path).read_text()
        assert source.count("_require_key(idempotency_key)") >= 2, path

    summaries = Path("app/api/v1/daily_summaries.py").read_text()
    assert "Idempotency-Key" in summaries, "the exception must be explained, not silent"
    assert "idempotency.begin" not in summaries
