"""§8's permission matrix for collections, plus the structural assertions.

§8 puts it plainly: "Enforced server-side on every endpoint. Hiding a button is UX, not a
control." And: "Write a permission test for the attendant-touching-another-shift case
specifically."

Two axes, and they are separate. **Role** is a floor -- attendant < manager < admin, so
every check is a minimum-role comparison. **Ownership** is not a property of a role at all:
an attendant may write to a shift they hold and not to one they do not, at the same role.
`require_shift_access` composes the two, and this module asserts that nothing here
re-implements either of them.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.anyio

DAY = date(2026, 4, 10)


def _post(client: AsyncClient, shift_id, headers, key: str, **body):
    return client.post(
        f"/api/v1/shifts/{shift_id}/collections",
        json={"mode": "cash", "amount": "5000.00", **body},
        headers={**headers, "Idempotency-Key": key},
    )


# --- ownership ---------------------------------------------------------------


async def test_an_attendant_records_money_on_their_own_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_collections,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(client, shift, auth_headers(attendant), "own")

    assert response.status_code == 201


async def test_an_attendant_cannot_record_money_on_another_attendants_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """The case §8 names specifically.

    Cash is the figure a salesman is personally accountable for -- a shortfall is booked
    against his own name (§14) -- so writing one into somebody else's shift is not a
    permission slip-up, it is moving a liability onto another person.
    """
    mine = make_user("attendant")
    theirs = make_user("attendant")
    their_shift = make_shift(theirs, business_date=DAY, sequence=1)

    response = await _post(client, their_shift, auth_headers(mine), "not-mine")

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_YOUR_SHIFT"


async def test_an_attendant_cannot_read_another_attendants_collections(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
) -> None:
    mine = make_user("attendant")
    theirs = make_user("attendant")
    their_shift = make_shift(theirs, business_date=DAY, sequence=1)
    make_collection(their_shift, mode="cash", amount="60000.00")

    response = await client.get(
        f"/api/v1/shifts/{their_shift}/collections", headers=auth_headers(mine)
    )

    assert response.status_code == 403


async def test_a_manager_may_act_on_any_shift_at_the_outlet(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_collections,
) -> None:
    """Ownership applies to attendants only. Writing it unconditionally would lock managers
    out of the shifts they are specifically there to close."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(client, shift, auth_headers(manager), "mgr")

    assert response.status_code == 201


# --- role floors -------------------------------------------------------------


async def test_an_attendant_cannot_reverse_a_collection(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
) -> None:
    """Even on their own shift. A reversal cancels a recorded figure after the fact, which
    is a supervisory act -- and the person a shortfall would be booked against is the last
    person who should be able to withdraw the record of it."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    collection = make_collection(shift, mode="cash", amount="60000.00")

    response = await client.post(
        f"/api/v1/shifts/{shift}/collections/{collection}/reversals",
        json={"reason": "typed the wrong figure"},
        headers={**auth_headers(attendant), "Idempotency-Key": "att"},
    )

    assert response.status_code == 403


async def test_a_manager_may_reverse_on_a_closed_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
    clean_collections,
) -> None:
    """A closed shift is exactly where this route is needed.

    A miscount surfaces during reconciliation, which happens after close. Gating the
    reversal on `writable=True` would make the correction path unreachable on precisely the
    shifts that need one -- the same reasoning behind Phase 5's review route.
    """
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    collection = make_collection(shift, mode="cash", amount="60000.00")

    response = await client.post(
        f"/api/v1/shifts/{shift}/collections/{collection}/reversals",
        json={"reason": "recount at the locker found 58,000"},
        headers={**auth_headers(manager), "Idempotency-Key": "mgr-closed"},
    )

    assert response.status_code == 201


async def test_a_manager_cannot_reverse_on_a_locked_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
) -> None:
    """Locking is the point at which a day stops being anybody else's to change (§8: only
    an admin locks or finalises)."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="locked")
    collection = make_collection(shift, mode="cash", amount="60000.00")

    response = await client.post(
        f"/api/v1/shifts/{shift}/collections/{collection}/reversals",
        json={"reason": "recount at the locker found 58,000"},
        headers={**auth_headers(manager), "Idempotency-Key": "mgr-locked"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "LOCKED_SHIFT_REVERSAL_REQUIRES_ADMIN"


async def test_an_admin_may_reverse_on_a_locked_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
    engine,
    clean_collections,
) -> None:
    """§6.9 names locked shifts as needing the reversal path, and §5.2 says nothing
    referencing a locked shift may be *modified*. Both hold at once, because a reversal
    modifies nothing -- it appends. `app/api/deps.py`'s own SHIFT_LOCKED message already
    says corrections must be recorded as reversal entries."""
    from sqlalchemy import text

    admin = make_user("admin")
    shift = make_shift(admin, business_date=DAY, sequence=1, status="locked")
    collection = make_collection(shift, mode="cash", amount="60000.00")

    response = await client.post(
        f"/api/v1/shifts/{shift}/collections/{collection}/reversals",
        json={"reason": "recount at the locker found 58,000"},
        headers={**auth_headers(admin), "Idempotency-Key": "admin-locked"},
    )

    assert response.status_code == 201
    # The locked row itself is untouched -- appended to, never modified.
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT amount FROM collections WHERE id = :id").bindparams(
                id=collection
            )
        ).scalar_one() == __import__("decimal").Decimal("60000.00")


async def test_an_unauthenticated_request_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await client.get(f"/api/v1/shifts/{shift}/collections")

    assert response.status_code == 401


# --- structural assertions ---------------------------------------------------


def test_no_ownership_check_is_reimplemented_here() -> None:
    """§8's ownership rule must live in exactly one place.

    Phase 4 put it in `require_shift_access`. A second copy in this router would be a
    second place for it to go wrong, and the two would drift -- the more so because the
    rule is conditional on role, which is the part people forget.
    """
    source = Path("app/api/v1/collections.py").read_text()
    assert "attendant_id !=" not in source
    assert "attendant_id ==" not in source
    assert "require_shift_access" in source


def test_the_outlet_is_never_hardcoded_in_this_router() -> None:
    """§8: the question is always "does this user hold role R **at the outlet that owns
    this row**". Every check here resolves the outlet from the shift, via
    `require_shift_access`, so `DEFAULT_OUTLET_ID` has no business appearing."""
    source = Path("app/api/v1/collections.py").read_text()
    assert "DEFAULT_OUTLET_ID" not in source
    assert "get_default_outlet_id" not in source


def test_no_float_appears_in_the_money_path() -> None:
    """§3 rule 1, asserted structurally.

    A `float()` call anywhere on this path would not fail a value assertion -- Decimal
    compares equal to a float that happens to match -- so it has to be checked as text.
    """
    for path in (
        "app/api/v1/collections.py",
        "app/services/collections.py",
        "app/models/collection.py",
        "app/core/collections.py",
        "app/core/idempotency.py",
    ):
        source = Path(path).read_text()
        assert "float(" not in source, path
        assert "sa.Float" not in source, path


def test_the_close_precondition_never_prices_the_shift() -> None:
    """§6.8, and the reason it is worth asserting mechanically.

    `shift_sales` raises 409 NO_PRICE_FOR_DATE / NO_MARGIN_FOR_DATE when a rate is missing,
    and petrol and diesel margins have never been entered at this outlet. A close
    precondition that reached for it would make every petrol shift unclosable -- and the
    error would name a missing margin, which is a very confusing way to be told you cannot
    close the day.

    Parsed rather than grepped so that a comment mentioning `shift_sales` does not fail it.
    """
    tree = ast.parse(Path("app/services/collections.py").read_text())
    called = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    } | {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "shift_sales" not in called

    shifts_tree = ast.parse(Path("app/api/v1/shifts.py").read_text())
    shifts_called = {
        node.attr for node in ast.walk(shifts_tree) if isinstance(node, ast.Attribute)
    }
    assert "shift_sales" not in shifts_called


def test_the_credit_sale_precondition_is_still_only_a_comment() -> None:
    """§11: do not scaffold ahead. `credit_sales` does not exist until Phase 9, and an
    empty check that always passes is indistinguishable from a check that was forgotten.

    `UNREVIEWED_EXPENSES_EXIST` landed with Phase 7 -- see
    tests/test_shift_lock_expenses.py for its behaviour -- so it has moved out of this
    test and into the "no longer a comment" assertion below, alongside
    `MISSING_COLLECTIONS`."""
    tree = ast.parse(Path("app/api/v1/shifts.py").read_text())
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "CREDIT_SALE_MISSING_RECEIPT" not in literals
    # ...but MISSING_COLLECTIONS and UNREVIEWED_EXPENSES_EXIST are no longer comments.
    # Each landed with its phase.
    assert "MISSING_COLLECTIONS" in literals
    assert "UNREVIEWED_EXPENSES_EXIST" in literals
