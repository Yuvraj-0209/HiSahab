"""Every write endpoint records an audit row (CLAUDE.md §5.3, §11, §14).

**This is the deliverable of Phase 11.** The twelve `audit.record` calls it was built around
were an afternoon's work; the reason the gap existed for seven phases is that *nothing failed
when it was missing*. §11's own text called the retrofit "genuinely optional" while three of
the seven tables it forgot were gating money rules.

Nothing would fail in Phase 12 or 13 either. So the rule is enforced structurally, the way
§6.9 records this codebase already learned to do:

> Every one of them brings a `uq_<table>_reverses_id` ... **and every one of them must have an
> entry in `app/core/errors.py::_CONSTRAINT_ERRORS`** ... This has now been forgotten twice,
> which is why `tests/test_errors.py` asserts it structurally against `pg_constraint` rather
> than trusting anyone to remember.

Same shape, one layer up. A new router with an unaudited `POST` or `PATCH` fails the suite
rather than the review.

## Why an AST walk rather than a grep

Phase 10 recorded what happens when a structural test is written as a text search: a test
asserting `salesman_id` never appears in a booking payload tripped over the *comment* saying
"No `salesman_id`. Read from `shifts.attendant_id`" -- the comment documenting the very rule
it was checking. Its note is worth repeating, because it applies to this file exactly:

> Left as a text search, it would have taught the next person to delete the comment.

So this parses the module and inspects real call nodes. A router may explain `audit.record`
in prose all it likes; only a call counts.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_ROUTERS_DIR = pathlib.Path(__file__).resolve().parents[1] / "app" / "api" / "v1"

# HTTP methods that create or change a row. `GET` reads, and §5.3 scopes the trail to
# changes -- auditing reads would multiply the row count by an order of magnitude for a
# control nobody asked for. `DELETE` is unused everywhere (§3 rule 6, §9).
_WRITE_DECORATORS = {"post", "patch", "put", "delete"}

# The one endpoint that legitimately writes without auditing.
#
# An attachment is not a business row. §6.10 already makes this argument for idempotency --
# "a retried upload creates a second row the client simply does not use, and §7.4's sweep
# reclaims it within 24 hours" -- and auditing a row that is *designed to be swept* records a
# fact about garbage. The money event is the **link**, when a business row claims the
# attachment, and `expenses.py` and `credit_sales.py` already audit that.
#
# Additions here need a reason in this comment, not just an entry. Deleting the assertion
# instead is what §14 forbids by name.
_EXEMPT: dict[str, set[str]] = {
    "uploads.py": {"upload_receipt"},
}

# A floor, so a router silently dropped from discovery fails loudly instead of the suite
# passing over a shrinking surface. Deliberately below the current count -- this is a
# tripwire against collapse, not a figure to update on every new endpoint.
_MINIMUM_AUDITED_WRITE_ENDPOINTS = 40


def _write_endpoints(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every function decorated with a router write method, at module level."""
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            # Matches `@router.post(...)` / `@router.patch(...)`.
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in _WRITE_DECORATORS
            ):
                found.append(node)
                break
    return found


def _calls_audit_record(node: ast.AST) -> bool:
    """Whether this function's body contains a real `audit.record(...)` call.

    A call node, never a text match: a docstring or comment mentioning `audit.record` must
    not satisfy this, or the test would reward describing the rule over following it.
    """
    for inner in ast.walk(node):
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "record"
            and isinstance(inner.func.value, ast.Name)
            and inner.func.value.id == "audit"
        ):
            return True
    return False


def _router_modules() -> list[pathlib.Path]:
    """Every router module, found by listing the directory.

    Discovered rather than hardcoded, so a router added in Phase 12 or 13 is covered without
    anybody remembering to edit this list -- which is the failure mode the whole file exists
    to prevent, and a hardcoded list would reintroduce it verbatim.
    """
    return sorted(
        path
        for path in _ROUTERS_DIR.glob("*.py")
        if path.name not in {"__init__.py", "router.py"}
    )


def test_every_write_endpoint_records_an_audit_row() -> None:
    """The rule §14 states, enforced.

    Failure names the exact endpoint, so the fix is obvious from the message alone rather
    than requiring somebody to rediscover this file's purpose.
    """
    unaudited: list[str] = []

    for path in _router_modules():
        tree = ast.parse(path.read_text())
        exempt = _EXEMPT.get(path.name, set())
        for endpoint in _write_endpoints(tree):
            if endpoint.name in exempt:
                continue
            if not _calls_audit_record(endpoint):
                unaudited.append(f"{path.name}::{endpoint.name}")

    assert not unaudited, (
        "These write endpoints change a row without recording who did it (CLAUDE.md §5.3, "
        "§14). Add an `audit.record(...)` call staged in the same transaction as the change "
        "-- see app/api/v1/credit_sales.py for the insert and update shapes. If an endpoint "
        "genuinely should not be audited, add it to _EXEMPT in this file **with a reason**, "
        f"never by weakening the assertion: {unaudited}"
    )


def test_the_router_scan_actually_finds_endpoints() -> None:
    """Guards the test above against passing vacuously.

    tests/test_routes.py records this exact trap being sprung once already: its first test
    enumerated `app.routes`, which FastAPI does not flatten, so it was "passing *vacuously*,
    seeing nothing but the framework paths it already excluded". A discovery-based structural
    test is worth nothing until something proves discovery works.
    """
    total = sum(
        len(_write_endpoints(ast.parse(path.read_text())))
        for path in _router_modules()
    )

    assert total >= _MINIMUM_AUDITED_WRITE_ENDPOINTS, (
        f"Only {total} write endpoints found across {len(_router_modules())} router "
        "modules. Either discovery broke, or the API surface collapsed -- both are bugs."
    )


def test_the_exemption_list_names_endpoints_that_exist() -> None:
    """An exemption for a function that no longer exists is a stale licence.

    It would sit here silently, and the day somebody adds a *new* function with that name --
    say a second upload route -- it would be exempted for a reason nobody intended.
    """
    for module_name, exempt_names in _EXEMPT.items():
        path = _ROUTERS_DIR / module_name
        assert path.exists(), f"_EXEMPT names a module that no longer exists: {module_name}"

        declared = {
            endpoint.name for endpoint in _write_endpoints(ast.parse(path.read_text()))
        }
        missing = exempt_names - declared
        assert not missing, (
            f"_EXEMPT[{module_name!r}] names endpoints that no longer exist: {missing}. "
            "Remove the entry rather than leaving a licence nothing claims."
        )


def test_the_check_is_a_syntax_walk_not_a_text_search() -> None:
    """A router that only *mentions* `audit.record` in prose must still fail.

    Phase 10's lesson, pinned: a structural test written as a text search tripped over the
    comment documenting the rule it was checking, and "left as a text search, it would have
    taught the next person to delete the comment".

    Built as a synthetic module rather than by touching a real one, so the proof costs
    nothing and cannot leave the tree dirty.
    """
    prose_only = ast.parse(
        '''
@router.post("/things")
def create_thing():
    """This endpoint deliberately does not call audit.record -- see the note below."""
    # audit.record(db, ...) would go here
    thing = "audit.record"
    return thing
'''
    )
    endpoints = _write_endpoints(prose_only)
    assert len(endpoints) == 1
    assert not _calls_audit_record(endpoints[0])

    real_call = ast.parse(
        '''
@router.patch("/things/{thing_id}")
def update_thing():
    audit.record(db, table_name="things")
    return None
'''
    )
    assert _calls_audit_record(_write_endpoints(real_call)[0])


@pytest.mark.parametrize(
    "module_name",
    [
        "fuel_types.py",
        "nozzles.py",
        "fuel_prices.py",
        "fuel_margins.py",
        "expense_categories.py",
        "credit_customers.py",
        "shift_templates.py",
    ],
)
def test_the_seven_retrofitted_routers_are_audited(module_name: str) -> None:
    """Named explicitly, not just covered by the sweep above.

    The sweep would still pass if one of these routers were deleted, and §11's history is
    precisely a list of tables that were forgotten. Naming them means a regression says
    *which* one.
    """
    path = _ROUTERS_DIR / module_name
    endpoints = _write_endpoints(ast.parse(path.read_text()))

    assert endpoints, f"{module_name} has no write endpoints -- did it get renamed?"
    for endpoint in endpoints:
        assert _calls_audit_record(endpoint), f"{module_name}::{endpoint.name}"
