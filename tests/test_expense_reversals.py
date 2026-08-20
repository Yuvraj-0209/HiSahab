"""§6.9's correction path for expenses, copied from tests/test_reversals.py.

The test that matters most is `test_the_original_row_is_left_byte_identical`: every other
property in this file could hold while the original was quietly edited, and that is
exactly the outcome §6.9 exists to forbid.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.anyio

DAY = date(2026, 4, 22)


def _reverse(client: AsyncClient, shift_id, expense_id, headers, key="rev", **body):
    return client.post(
        f"/api/v1/shifts/{shift_id}/expenses/{expense_id}/reversals",
        json={"reason": "amount was entered wrong", **body},
        headers={**headers, "Idempotency-Key": key},
    )


def _row(engine: Engine, expense_id: UUID) -> dict:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text("SELECT * FROM expenses WHERE id = :id").bindparams(id=expense_id)
            )
            .mappings()
            .one()
        )


def _net(engine: Engine, shift_id: UUID, category: str) -> Decimal:
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT coalesce(sum(amount), 0) FROM expenses "
                "WHERE shift_id = :s AND category = CAST(:c AS expense_category)"
            ).bindparams(s=shift_id, c=category)
        ).scalar_one()


async def test_the_original_row_is_left_byte_identical(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_expense(shift, category="maintenance", amount="5000.00")
    before = _row(engine, original)

    response = await _reverse(client, shift, original, auth_headers(manager))
    assert response.status_code == 201

    assert _row(engine, original) == before


async def test_the_reversal_carries_the_negated_amount_and_a_reason(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_expense(shift, category="maintenance", amount="5000.00")

    body = (await _reverse(client, shift, original, auth_headers(manager))).json()

    assert body["reversal"]["amount"] == "-5000.00"
    assert body["reversal"]["reverses_id"] == str(original)
    assert body["reversal"]["reversal_reason"] == "amount was entered wrong"
    assert body["original"]["is_reversed"] is True
    assert body["replacement"] is None


async def test_a_reversal_and_replacement_leave_the_correct_net_figure(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_expense(shift, category="maintenance", amount="5000.00")

    response = await _reverse(
        client, shift, original, auth_headers(manager), replacement_amount="4800.00"
    )

    assert response.status_code == 201
    assert response.json()["replacement"]["amount"] == "4800.00"
    assert _net(engine, shift, "maintenance") == Decimal("4800.00")


async def test_the_replacement_carries_the_originals_category_mode_and_description(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    """A correction is "the same expense, the right amount", not a new expense -- the
    reason for the change lives on the reversal row, where §6.9 already puts it."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_expense(
        shift, category="electricity", mode="bank_transfer",
        amount="8000.00", description="monthly board bill",
    )

    response = await _reverse(
        client, shift, original, auth_headers(manager), replacement_amount="7800.00"
    )

    replacement = response.json()["replacement"]
    assert replacement["category"] == "electricity"
    assert replacement["mode"] == "bank_transfer"
    assert replacement["description"] == "monthly board bill"


async def test_replacement_paid_to_may_override_the_originals(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_expense(
        shift, category="maintenance", amount="500.00", paid_to="Old Vendor"
    )

    response = await _reverse(
        client, shift, original, auth_headers(manager),
        replacement_amount="500.00", replacement_paid_to="New Vendor",
    )

    assert response.json()["replacement"]["paid_to"] == "New Vendor"


async def test_a_row_cannot_be_reversed_twice(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_expense(shift, amount="500.00")

    first = await _reverse(client, shift, original, auth_headers(manager), key="first")
    second = await _reverse(client, shift, original, auth_headers(manager), key="second")

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["code"] == "ALREADY_REVERSED"


async def test_a_reversal_cannot_itself_be_reversed(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_expense(shift, amount="500.00")
    reversal_id = (
        await _reverse(client, shift, original, auth_headers(manager))
    ).json()["reversal"]["id"]

    response = await _reverse(
        client, shift, UUID(reversal_id), auth_headers(manager), key="double"
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CANNOT_REVERSE_A_REVERSAL"


async def test_a_reversal_cannot_be_edited(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    clean_expenses,
) -> None:
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    original = make_expense(shift, amount="500.00")
    reversal_id = (
        await _reverse(client, shift, original, auth_headers(manager))
    ).json()["reversal"]["id"]

    response = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{reversal_id}",
        json={"amount": "1.00"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CANNOT_EDIT_A_REVERSAL"


async def test_a_reversed_expense_cannot_be_patched(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    """The exact defect Phase 7 Step 0 found on `collections`, built correctly here from
    day one: PATCH must refuse a row that *has been* reversed, not only one that *is* a
    reversal -- a reversal is legal on an open shift too, so `writable=True` alone does
    not close the gap."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="open")
    original = make_expense(shift, category="maintenance", amount="500.00")

    reversal = await _reverse(client, shift, original, auth_headers(manager))
    assert reversal.status_code == 201

    response = await client.patch(
        f"/api/v1/shifts/{shift}/expenses/{original}",
        json={"amount": "400.00"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "EXPENSE_ALREADY_REVERSED"
    assert _net(engine, shift, "maintenance") == Decimal("0.00")


@pytest.mark.parametrize("reason", ["", "  ", "ab", "\t\n "])
async def test_a_reversal_without_a_real_reason_is_refused(
    reason: str,
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    """§6.9 says "a mandatory reason", and whitespace is not one. `StringConstraints`
    strips before the length check, unlike collections' original Field(min_length=3) --
    the Phase 7 Step 0 lesson applied here from the start rather than retrofitted."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_expense(shift, amount="500.00")

    response = await client.post(
        f"/api/v1/shifts/{shift}/expenses/{original}/reversals",
        json={"reason": reason},
        headers={**auth_headers(manager), "Idempotency-Key": f"k{len(reason)}"},
    )

    assert response.status_code == 422
    assert _net(engine, shift, "maintenance") == Decimal("500.00")


async def test_a_reversal_is_written_to_the_audit_log_as_a_reversal(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_expense(shift, amount="500.00")

    reversal_id = (
        await _reverse(client, shift, original, auth_headers(manager))
    ).json()["reversal"]["id"]

    with engine.connect() as connection:
        action, old, new = connection.execute(
            text(
                "SELECT action, old_values, new_values FROM audit_logs "
                "WHERE table_name = 'expenses' AND record_id = :id"
            ).bindparams(id=UUID(reversal_id))
        ).one()

    assert action == "reversal"
    assert old["amount"] == "500.00"
    assert new["amount"] == "-500.00"
    assert new["reason"] == "amount was entered wrong"


async def test_the_amount_boundary_is_refused_before_the_handler_runs(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    """§3 rule 1: NUMERIC(12,2) is declared at the boundary as well as in the column."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_expense(shift, amount="500.00")

    response = await _reverse(
        client, shift, original, auth_headers(manager),
        replacement_amount="99999999999.00",
    )

    assert response.status_code == 422
    assert _net(engine, shift, "maintenance") == Decimal("500.00")


async def test_a_blank_reversal_reason_is_refused_at_the_database_level(
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
) -> None:
    """§6.6's belt-and-braces rule: `services/expenses.py::reverse` is reachable from a
    management command with no Pydantic anywhere in the picture. Built strong from day
    one -- 0007 had to retrofit exactly this onto `collections` after the fact."""
    from sqlalchemy.exc import IntegrityError

    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    original = make_expense(shift, amount="500.00")

    for blank in ("", "   ", "\t"):
        with pytest.raises(IntegrityError) as caught:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO expenses "
                        "(shift_id, category, mode, amount, description, reverses_id, "
                        "reversal_reason) VALUES (:s, "
                        "CAST('maintenance' AS expense_category), "
                        "CAST('cash' AS expense_mode), -500.00, 'reversal', :o, :r)"
                    ).bindparams(s=shift, o=original, r=blank)
                )
        assert "ck_expenses_reversal_has_reason" in str(caught.value)


async def test_a_row_cannot_reverse_itself(
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
) -> None:
    from uuid import uuid4

    from sqlalchemy.exc import IntegrityError

    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1)
    row_id = uuid4()

    with pytest.raises(IntegrityError) as caught:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO expenses (id, shift_id, category, mode, amount, "
                    "description, reverses_id, reversal_reason) VALUES (:id, :s, "
                    "CAST('maintenance' AS expense_category), "
                    "CAST('cash' AS expense_mode), -0.01, 'circular', :id, 'circular')"
                ).bindparams(id=row_id, s=shift)
            )
    assert "ck_expenses_reversal_not_self" in str(caught.value)


async def test_a_failure_after_the_reversal_rolls_the_whole_thing_back(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    clean_expenses,
) -> None:
    """A half-applied correction is worse than no correction at all -- mirrors
    tests/test_reversals.py's collections version exactly. The failure is injected rather
    than provoked, for the same reason: every reachable input that could fail is refused
    earlier, by Pydantic or by a CHECK."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_expense(shift, category="maintenance", amount="500.00")

    import app.api.v1.expenses as expenses_module

    def _explode(*args, **kwargs):
        raise RuntimeError("storage went away mid-correction")

    monkeypatch.setattr(expenses_module.audit, "record", _explode)

    with pytest.raises(RuntimeError):
        await _reverse(
            client, shift, original, auth_headers(manager),
            replacement_amount="480.00",
        )

    assert _net(engine, shift, "maintenance") == Decimal("500.00")
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM expenses WHERE shift_id = :s").bindparams(s=shift)
        ).scalar_one() == 1


async def test_the_reversal_route_is_idempotent(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_expenses,
) -> None:
    """Same key twice replays the stored response and creates nothing a second time."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_expense(shift, amount="500.00")
    headers = auth_headers(manager)

    first = await _reverse(client, shift, original, headers, key="idem-rev")
    second = await _reverse(client, shift, original, headers, key="idem-rev")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM expenses WHERE shift_id = :s").bindparams(s=shift)
        ).scalar_one()
    assert count == 2  # original + one reversal, not two
