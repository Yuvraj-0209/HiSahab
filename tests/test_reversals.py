"""§6.9's correction path, on the first table it can apply to.

    "Corrections create a reversal entry: a new row with the negated amount, a
    `reverses_id` FK to the original, and a mandatory reason. Both rows remain visible.
    This is how double-entry accounting has worked for 600 years and it is the only way to
    answer 'who changed this, when, and what was it before'."

`nozzle_readings` never faced this -- it carries meter values, not rupees -- so the shape
built here is the one `expenses` (Phase 7) and `credit_sales` (Phase 9) will copy.

The test that matters most is `test_the_original_row_is_left_byte_identical`. Every other
property in this file could hold while the original was quietly edited, and an edited
original is exactly the outcome §6.9 exists to forbid.
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

DAY = date(2026, 4, 8)


def _reverse(client: AsyncClient, shift_id, collection_id, headers, key="rev", **body):
    return client.post(
        f"/api/v1/shifts/{shift_id}/collections/{collection_id}/reversals",
        json={"reason": "cash miscounted at close", **body},
        headers={**headers, "Idempotency-Key": key},
    )


def _row(engine: Engine, collection_id: UUID) -> dict:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text("SELECT * FROM collections WHERE id = :id").bindparams(
                    id=collection_id
                )
            )
            .mappings()
            .one()
        )


def _net(engine: Engine, shift_id: UUID, mode: str) -> Decimal:
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT coalesce(sum(amount), 0) FROM collections "
                "WHERE shift_id = :s AND mode = CAST(:m AS collection_mode)"
            ).bindparams(s=shift_id, m=mode)
        ).scalar_one()


async def test_the_original_row_is_left_byte_identical(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_collections,
) -> None:
    """The whole of §6.9 in one assertion.

    Every column is compared, not just `amount`, because "we only changed a flag" is how a
    row starts being edited. If a later refactor adds a `reversed_at` marker -- the obvious
    way to make a unique constraint work again -- this test is what refuses it.
    """
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_collection(shift, mode="cash", amount="60000.00", reference="locker")
    before = _row(engine, original)

    response = await _reverse(client, shift, original, auth_headers(manager))
    assert response.status_code == 201

    assert _row(engine, original) == before


async def test_the_reversal_carries_the_negated_amount_and_a_reason(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
    clean_collections,
) -> None:
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_collection(shift, mode="cash", amount="60000.00")

    body = (await _reverse(client, shift, original, auth_headers(manager))).json()

    assert body["reversal"]["amount"] == "-60000.00"
    assert body["reversal"]["reverses_id"] == str(original)
    assert body["reversal"]["reversal_reason"] == "cash miscounted at close"
    assert body["original"]["is_reversed"] is True
    assert body["replacement"] is None


async def test_a_reversal_and_replacement_leave_the_correct_net_figure(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_collections,
) -> None:
    """Monday's ₹60,000 stays; the day nets to ₹58,000; three rows tell the whole story."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_collection(shift, mode="cash", amount="60000.00")

    response = await _reverse(
        client, shift, original, auth_headers(manager), replacement_amount="58000.00"
    )

    assert response.status_code == 201
    assert response.json()["replacement"]["amount"] == "58000.00"
    assert _net(engine, shift, "cash") == Decimal("58000.00")


async def test_the_replacement_becomes_the_live_row(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
    clean_collections,
) -> None:
    """After a correction, `declared_cash` reports the corrected figure and not the old one.

    This is what makes the one-live-row-per-mode rule survive a reversal: the original is
    no longer live because something points at it, and the replacement takes its place.
    """
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_collection(shift, mode="cash", amount="60000.00")

    await _reverse(
        client, shift, original, auth_headers(manager), replacement_amount="58000.00"
    )
    body = (
        await client.get(
            f"/api/v1/shifts/{shift}/collections", headers=auth_headers(manager)
        )
    ).json()

    assert body["declared_cash"] == "58000.00"
    # §6.9: "Both rows remain visible." Three rows, none hidden.
    assert len(body["items"]) == 3


async def test_the_mode_is_free_again_after_a_bare_reversal(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
    clean_collections,
) -> None:
    """A reversal with no replacement leaves the mode with no live row, so a fresh POST
    works. Without this the only correction path on a reopened shift would be a dead end."""
    admin = make_user("admin")
    shift = make_shift(admin, business_date=DAY, sequence=1)
    original = make_collection(shift, mode="cash", amount="60000.00")
    headers = auth_headers(admin)

    await _reverse(client, shift, original, headers)
    response = await client.post(
        f"/api/v1/shifts/{shift}/collections",
        json={"mode": "cash", "amount": "58000.00"},
        headers={**headers, "Idempotency-Key": "fresh"},
    )

    assert response.status_code == 201


async def test_a_row_cannot_be_reversed_twice(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_collections,
) -> None:
    """Two corrections each negating the same ₹60,000 would take the day ₹60,000 the wrong
    way -- and it would look like arithmetic rather than a mistake."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_collection(shift, mode="cash", amount="60000.00")
    headers = auth_headers(manager)

    await _reverse(client, shift, original, headers, key="one")
    response = await _reverse(client, shift, original, headers, key="two")

    assert response.status_code == 409
    assert response.json()["code"] == "ALREADY_REVERSED"
    assert _net(engine, shift, "cash") == Decimal("0.00")


async def test_a_reversal_cannot_itself_be_reversed(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
    clean_collections,
) -> None:
    """Negating a negation is arithmetic that reads as an undo, and an undo is the one
    thing §6.9 refuses. Record the correct figure as a new collection instead."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_collection(shift, mode="cash", amount="60000.00")
    headers = auth_headers(manager)

    reversal_id = (await _reverse(client, shift, original, headers, key="one")).json()[
        "reversal"
    ]["id"]
    response = await _reverse(client, shift, reversal_id, headers, key="two")

    assert response.status_code == 409
    assert response.json()["code"] == "CANNOT_REVERSE_A_REVERSAL"


async def test_a_reversal_cannot_be_edited(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
    clean_collections,
) -> None:
    """Editing a reversal would rewrite the correction itself, which is the same failure
    one level up."""
    admin = make_user("admin")
    shift = make_shift(admin, business_date=DAY, sequence=1)
    original = make_collection(shift, mode="cash", amount="60000.00")
    headers = auth_headers(admin)

    reversal_id = (await _reverse(client, shift, original, headers)).json()["reversal"]["id"]
    response = await client.patch(
        f"/api/v1/shifts/{shift}/collections/{reversal_id}",
        json={"amount": "1.00"},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CANNOT_EDIT_A_REVERSAL"


@pytest.mark.parametrize("reason", ["", "  ", "ab"])
async def test_a_reversal_without_a_real_reason_is_refused(
    reason: str,
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_collections,
) -> None:
    """§6.9 says "a mandatory reason", and whitespace is not one.

    A reason field that accepts " " is a reason field nobody fills in, and the audit trail
    then records that somebody cancelled ₹60,000 for no stated cause.
    """
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_collection(shift, mode="cash", amount="60000.00")

    response = await client.post(
        f"/api/v1/shifts/{shift}/collections/{original}/reversals",
        json={"reason": reason},
        headers={**auth_headers(manager), "Idempotency-Key": f"k{len(reason)}"},
    )

    assert response.status_code == 422
    assert _net(engine, shift, "cash") == Decimal("60000.00")


async def test_a_reversal_is_written_to_the_audit_log_as_a_reversal(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_collections,
) -> None:
    """§5.3's `action` enum has a `reversal` member; this is the first thing to use it."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_collection(shift, mode="cash", amount="60000.00")

    reversal_id = (
        await _reverse(client, shift, original, auth_headers(manager))
    ).json()["reversal"]["id"]

    with engine.connect() as connection:
        action, old, new = connection.execute(
            text(
                "SELECT action, old_values, new_values FROM audit_logs "
                "WHERE table_name = 'collections' AND record_id = :id"
            ).bindparams(id=UUID(reversal_id))
        ).one()

    assert action == "reversal"
    assert old["amount"] == "60000.00"
    assert new["amount"] == "-60000.00"
    assert new["reason"] == "cash miscounted at close"


async def test_a_failure_after_the_reversal_rolls_the_whole_thing_back(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    clean_collections,
) -> None:
    """A half-applied correction is worse than no correction at all.

    The reversal and its replacement are two INSERTs and one commit. If the second fails,
    the first must not stand -- otherwise the money is gone from the system with nothing
    replacing it, and the shift silently reads as ₹0 cash. That is a wrong number with no
    error attached, which is the failure mode this whole project is organised against.

    The failure is injected rather than provoked, because no reachable input fails at that
    exact point: every value a client can send is refused earlier by `condecimal` or by a
    CHECK. What is being asserted is real all the same -- that the handler's rollback
    covers work already flushed, not merely work not yet attempted.
    """
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_collection(shift, mode="cash", amount="60000.00")

    from app.api.v1 import collections as collections_api

    def _explode(*args, **kwargs):
        raise RuntimeError("storage went away mid-correction")

    monkeypatch.setattr(collections_api.audit, "record", _explode)

    with pytest.raises(RuntimeError):
        await _reverse(
            client, shift, original, auth_headers(manager),
            replacement_amount="58000.00",
        )

    # Nothing was appended: the original stands alone, exactly as it was.
    assert _net(engine, shift, "cash") == Decimal("60000.00")
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM collections WHERE shift_id = :s").bindparams(
                s=shift
            )
        ).scalar_one() == 1


async def test_an_over_long_amount_is_refused_at_the_boundary(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_collections,
) -> None:
    """§3 rule 1: NUMERIC(12,2) is declared at the boundary as well as in the column.

    A figure too large for the column is a 422 naming the field, not a 500 from psycopg
    several layers down.
    """
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")
    original = make_collection(shift, mode="cash", amount="60000.00")

    response = await client.post(
        f"/api/v1/shifts/{shift}/collections/{original}/reversals",
        json={"reason": "retyping the figure", "replacement_amount": "99999999999.00"},
        headers={**auth_headers(manager), "Idempotency-Key": "toolong"},
    )

    assert response.status_code == 422
    assert _net(engine, shift, "cash") == Decimal("60000.00")
