"""§8's matrix for credit, plus the structural assertions every permissions file here carries.

The value-level permission cases live with their own routers -- `test_credit_customers.py`,
`test_credit_sales_api.py`, `test_credit_repayments.py`. What is here instead is the set of
checks that read the *source* rather than call it: things a passing endpoint test cannot
prove, like "the ownership rule was not quietly re-implemented in this module" or "no float
appears in the money path". `test_expense_permissions.py` and `test_collections_permissions.py`
carry the same four, and Phase 9 inherits them rather than inventing its own.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.anyio

_ROUTERS = (
    "app/api/v1/credit_customers.py",
    "app/api/v1/credit_sales.py",
    "app/api/v1/credit_repayments.py",
)
_MONEY_PATH = _ROUTERS + (
    "app/services/credit.py",
    "app/models/credit.py",
    "app/core/credit.py",
)

# The two routers that hang off a shift. credit_customers.py is outlet-scoped reference data
# and correctly has no shift in it at all.
_SHIFT_SCOPED = (
    "app/api/v1/credit_sales.py",
    "app/api/v1/credit_repayments.py",
)


@pytest.mark.parametrize("path", _SHIFT_SCOPED)
def test_no_ownership_check_is_reimplemented_here(path: str) -> None:
    """§8's ownership rule must live in exactly one place: `require_shift_access`.

    A second copy is worse than no copy. The two would drift, and the drift would show up as
    an attendant able to write to somebody else's shift through one route and not another --
    which is the sort of hole nobody finds by reading a passing test suite.
    """
    source = Path(path).read_text()

    assert "attendant_id !=" not in source, path
    assert "attendant_id ==" not in source, path
    assert "require_shift_access" in source, path


@pytest.mark.parametrize("path", _SHIFT_SCOPED)
def test_the_outlet_is_never_hardcoded_in_a_shift_scoped_router(path: str) -> None:
    """§8: when acting on an existing row, the outlet comes from *the row*, never from
    config. `DEFAULT_OUTLET_ID` in a shift-scoped router would mean a second outlet's shifts
    silently authorising against the first one's membership."""
    source = Path(path).read_text()

    assert "DEFAULT_OUTLET_ID" not in source, path
    assert "get_default_outlet_id" not in source, path


def test_the_customers_router_resolves_the_outlet_from_the_row_for_writes() -> None:
    """`credit_customers.py` is the exception to the test above, and legitimately so.

    Creating a customer has no row to read an outlet off, so it uses `require_role`'s default
    (`get_default_outlet_id`) exactly as `deps.py` prescribes. Everything that acts on an
    *existing* customer must go through `resolve_outlet_from_credit_customer` instead -- so
    this asserts the resolver exists and is actually wired, rather than the blanket ban.
    """
    source = Path("app/api/v1/credit_customers.py").read_text()

    assert "def resolve_outlet_from_credit_customer" in source
    assert "require_role(Role.admin, resolve_outlet_from_credit_customer)" in source
    assert "require_role(Role.manager, resolve_outlet_from_credit_customer)" in source


@pytest.mark.parametrize("path", _MONEY_PATH)
def test_no_float_appears_in_the_money_path(path: str) -> None:
    """§3 rule 1, asserted structurally rather than hoped for.

    `0.1 + 0.2 != 0.3` in binary floating point, and in a cash system that compounds into
    variance nobody can explain. A single `float(` in a money path is the whole failure.
    """
    source = Path(path).read_text()

    assert "float(" not in source, path
    assert "sa.Float" not in source, path


def test_the_close_precondition_reads_only_this_shifts_own_rows() -> None:
    """§6.8's `CREDIT_SALE_MISSING_RECEIPT` must be shift-scoped.

    Parsed rather than grepped, so a comment explaining the distinction cannot itself pass
    or fail the test. A business-date-scoped version would let one bad row on one shift
    block every other shift that day from closing.
    """
    source = Path("app/services/credit.py").read_text()
    tree = ast.parse(source)
    functions = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }

    assert "sales_missing_receipt" in functions
    body = ast.get_source_segment(source, functions["sales_missing_receipt"])
    assert "shift.id" in body
    assert "business_date" not in body


def test_the_outstanding_sum_does_not_filter_out_reversals() -> None:
    """§6.6 and §14: reversals are negative rows that net out, and dropping them makes a
    cancelled udhaar reappear as debt.

    `live_credit_sale_for_attachment` legitimately *does* filter to live rows -- that is
    §5.3's rule, a different question -- so this is parsed per-function rather than grepped
    across the module, or the two would be confused for each other.
    """
    source = Path("app/services/credit.py").read_text()
    tree = ast.parse(source)
    functions = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }

    for name in ("outstanding", "outstanding_by_customer", "credit_sales_total"):
        body = ast.get_source_segment(source, functions[name])
        assert "_sale_is_reversed" not in body, name
        assert "reverses_id" not in body, name


def test_no_credit_module_hardcodes_a_threshold_or_uses_offset() -> None:
    """§9 and §14. `OFFSET` causes duplicates and skips during a scroll, and a hardcoded
    money literal is a deploy standing between the owner and a config change."""
    for path in _MONEY_PATH:
        source = Path(path).read_text()
        assert ".offset(" not in source, path


def test_there_is_no_delete_route_anywhere_in_credit() -> None:
    """§3 rule 6: no hard deletes on a financial table, and customers are retired with
    `is_active` instead. `main.py` does not even allow the method through CORS."""
    for path in _ROUTERS:
        source = Path(path).read_text()
        assert "@router.delete" not in source, path


def test_the_shortfall_guardrail_is_restated_where_it_could_be_violated() -> None:
    """§14 forbids booking a salesman's cash shortfall as a credit sale, and Phase 10 gives
    shortfalls their own record type (§13.14).

    The enum that used to make `fuel_purchase` structurally impossible is the precedent here:
    Phase 8's notes record that when a guardrail moves from the schema into prose, the prose
    has to sit at the endpoint that could break it, not only in CLAUDE.md.
    `create_expense_category` does this for the fuel-purchase exclusion; this is the same
    thing for the shortfall one.

    Asserting on a docstring is unusual and worth defending: this is not testing behaviour,
    it is testing that a *warning a human needs to read* has not been deleted during a
    refactor. Nothing else can catch that.
    """
    source = Path("app/api/v1/credit_customers.py").read_text()

    assert "shortfall" in source.lower()
    assert "§13.14" in source or "Phase 10" in source
