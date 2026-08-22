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
)
_MONEY_PATH = _ROUTERS + (
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
