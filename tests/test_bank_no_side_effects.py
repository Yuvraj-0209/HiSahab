"""§14's rule, pinned structurally: a statement line writes no business row but a repayment.

An `ast` walk over the bank modules, in the same construction and for the same reason as
`tests/test_audit_coverage.py`. Phase 11's notes put it plainly -- the audit gap survived
seven phases *"because nothing failed when it was missing"*. A rule that lives only in a
docstring is the same shape: the code drifts, every test still passes, and the damage is a
figure nobody can trace back.

## What is actually being protected

§12 forbids recording an IOCL payment as an expense, because §6.4 would invent a daily cash
shortage that never happened. §14 extends that: **no statement line may create any business
row except a confirmed `credit_repayment`.** A `bank_deposits` row needs a `shift_id`, so
inventing one would move a closed day's expected cash on the strength of an uploaded file.

The failure mode is what makes this worth an `ast` walk rather than a code review. Somebody
adding "helpfully create the missing deposit" to the reconciliation would be *trying to be
useful*, the tests would pass, and a month of expected-cash figures would quietly move.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[1] / "app"

# The modules this rule governs: everything Phase 20 added that can reach a Session.
_BANK_MODULES = (
    _APP / "services" / "bank.py",
    _APP / "api" / "v1" / "bank_statements.py",
    _APP / "core" / "bank_statements.py",
)

# Models whose construction means money moved somewhere §6.4 can see. `CreditRepayment` is
# deliberately absent: creating one is the single write this phase is allowed to make.
_FORBIDDEN_MODELS = frozenset(
    {
        "BankDeposit",
        "DailyCashSummary",
        "Expense",
        "Collection",
        "NonFuelSale",
        "SalesmanShortfall",
        "SalesmanShortfallSettlement",
        "CreditSale",
    }
)

# Tables the same rule covers, for a raw-SQL route around the ORM.
_FORBIDDEN_TABLES = frozenset(
    {
        "bank_deposits",
        "daily_cash_summaries",
        "expenses",
        "collections",
        "non_fuel_sales",
        "salesman_shortfalls",
        "credit_sales",
    }
)


def _module_trees() -> list[tuple[Path, ast.Module]]:
    trees = []
    for path in _BANK_MODULES:
        assert path.exists(), f"{path} moved -- update this test rather than deleting it"
        trees.append((path, ast.parse(path.read_text())))
    return trees


def test_the_bank_modules_exist_and_parse() -> None:
    """A meta-test: an empty scan must fail loudly rather than pass vacuously."""
    trees = _module_trees()

    assert len(trees) == 3


def test_no_bank_module_constructs_a_cash_engine_row() -> None:
    """The rule itself. `BankDeposit(...)` anywhere in these modules fails here.

    Constructing the model is the thing caught, not adding it to a session, because the
    construction is the part a reviewer's eye skims past -- `db.add` at least looks like a
    write.
    """
    offenders: list[str] = []

    for path, tree in _module_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in _FORBIDDEN_MODELS:
                    offenders.append(f"{path.name}:{node.lineno}: {node.func.id}(...)")

    assert offenders == [], (
        "a bank module constructs a business row it must never create: "
        f"{offenders}. §14: reading a statement must not move money. A deposit needs a "
        "shift_id, so inventing one moves §6.4's expected cash for a day already closed."
    )


def test_no_bank_module_writes_those_tables_in_raw_sql() -> None:
    """The same rule, for a route around the ORM.

    The service reads `bank_deposits` legitimately -- that is the whole point of the deposit
    reconciliation -- so this looks for the *verbs*, not the table names alone.
    """
    offenders: list[str] = []

    for path, tree in _module_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            sql = node.value.lower()
            if not any(
                verb in sql for verb in ("insert into", "update ", "delete from")
            ):
                continue
            for table in _FORBIDDEN_TABLES:
                if table in sql:
                    offenders.append(f"{path.name}:{node.lineno}: writes {table}")

    assert offenders == [], f"raw SQL writing a forbidden table: {offenders}"


def test_only_the_confirm_endpoint_creates_a_repayment() -> None:
    """The one permitted write, confined to one function.

    Not a style rule: a second path into `credit_repayments` would be a second place the
    opening-balance guard and the future-date check could be forgotten, and §6.9's correction
    path only reaches rows the ordinary routes created.
    """
    path = _APP / "api" / "v1" / "bank_statements.py"
    tree = ast.parse(path.read_text())

    creating_functions: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "CreditRepayment"
            ):
                creating_functions.append(node.name)
                break

    assert creating_functions == ["confirm_repayments"], (
        "exactly one function may create a CreditRepayment from a statement line; "
        f"found: {creating_functions}"
    )


@pytest.mark.parametrize(
    "forbidden",
    sorted(_FORBIDDEN_MODELS),
)
def test_the_forbidden_list_names_real_models(forbidden: str) -> None:
    """A guard against the guard rotting: a typo here would silently protect nothing."""
    import app.models as models

    assert hasattr(models, forbidden), (
        f"{forbidden} is not a real model -- this test would protect nothing. Renamed?"
    )
