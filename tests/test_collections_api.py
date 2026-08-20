"""The collection endpoints (CLAUDE.md §5.2, §6.4).

Covers what a row *is* and what the routes refuse. §6.9's reversal path is
tests/test_reversals.py, §6.10's replay store is tests/test_idempotency.py, and §6.8's
close precondition is tests/test_shift_close_collections.py -- four concerns, four files,
because each of them fails for its own reasons.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.anyio

DAY = date(2026, 4, 6)


def _post(client: AsyncClient, shift_id: UUID, headers: dict[str, str], key: str, **body):
    return client.post(
        f"/api/v1/shifts/{shift_id}/collections",
        json=body,
        headers={**headers, "Idempotency-Key": key},
    )


def _count(engine: Engine, shift_id: UUID) -> int:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT count(*) FROM collections WHERE shift_id = :s").bindparams(
                s=shift_id
            )
        ).scalar_one()


# --- creating ----------------------------------------------------------------


async def test_a_collection_is_recorded_with_its_mode_and_amount(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_collections,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(
        client, shift, auth_headers(attendant), "k1", mode="cash", amount="60000.00"
    )

    assert response.status_code == 201
    body = response.json()
    assert body["mode"] == "cash"
    # A string, not a number: §3 rule 1 end to end. A JSON float here would be the same
    # binary-floating-point value the whole project exists to keep out.
    assert body["amount"] == "60000.00"
    assert body["reverses_id"] is None
    assert body["is_reversed"] is False


async def test_each_mode_is_recorded_separately(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_collections,
) -> None:
    """A day's takings arrive through several channels and each is its own fact."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)

    assert (await _post(client, shift, headers, "c", mode="cash", amount="60000.00")).status_code == 201
    assert (await _post(client, shift, headers, "u", mode="upi", amount="20000.00")).status_code == 201
    assert (
        await _post(
            client, shift, headers, "d", mode="card", amount="10000.00",
            reference="HDFC-batch-4471",
        )
    ).status_code == 201

    listing = await client.get(f"/api/v1/shifts/{shift}/collections", headers=headers)
    totals = listing.json()["totals_by_mode"]
    assert totals == {"cash": "60000.00", "upi": "20000.00", "card": "10000.00"}


async def test_a_second_live_row_for_the_same_mode_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_collections,
) -> None:
    """§5.2: one live row per mode, enforced in the service layer.

    This outlet has one card machine and one UPI QR and its register writes one lumped
    figure per mode, so a second live cash row is a mistake rather than a second genuine
    payment. It cannot be a unique constraint -- see migration 0006 -- so the check has to
    be here, and the refusal has to name PATCH as the way forward.
    """
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)

    await _post(client, shift, headers, "first", mode="cash", amount="60000.00")
    response = await _post(client, shift, headers, "second", mode="cash", amount="500.00")

    assert response.status_code == 409
    assert response.json()["code"] == "COLLECTION_ALREADY_EXISTS"
    assert _count(engine, shift) == 1


async def test_zero_cash_is_a_valid_declaration(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_collections,
) -> None:
    """§6.8: zero as an answer, never zero as an omission.

    On a day that genuinely took no cash the salesman declares ₹0 and that is a *record*.
    Refusing it would leave him no way to say so, and the shift would then be indis-
    tinguishable from one where somebody simply forgot -- which is where a shortfall hides.
    """
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(
        client, shift, auth_headers(attendant), "zero", mode="cash", amount="0.00"
    )

    assert response.status_code == 201
    assert response.json()["amount"] == "0.00"


async def test_a_negative_amount_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_collections,
) -> None:
    """Money arriving is a positive number. The only negative row is a §6.9 reversal, and
    that is created by its own route, never by a client sending a minus sign."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _post(
        client, shift, auth_headers(attendant), "neg", mode="cash", amount="-500.00"
    )

    assert response.status_code == 422
    assert _count(engine, shift) == 0


async def test_the_amount_sign_rule_is_enforced_at_the_database_level(
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
) -> None:
    """Belt and braces (§6.6): a client can bypass JavaScript, but not a CHECK."""
    from sqlalchemy.exc import IntegrityError

    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    with pytest.raises(IntegrityError) as caught:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO collections (shift_id, mode, amount) VALUES "
                    "(:s, CAST('cash' AS collection_mode), -1.00)"
                ).bindparams(s=shift)
            )
    assert "ck_collections_amount_sign" in str(caught.value)


async def test_a_reversal_without_a_reason_is_refused_at_the_database_level(
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
) -> None:
    """§5.2 / §6.9: an unexplained negation is an unauditable one."""
    from sqlalchemy.exc import IntegrityError

    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    original = make_collection(shift, mode="cash", amount="500.00")

    with pytest.raises(IntegrityError) as caught:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO collections (shift_id, mode, amount, reverses_id) "
                    "VALUES (:s, CAST('cash' AS collection_mode), -500.00, :o)"
                ).bindparams(s=shift, o=original)
            )
    assert "ck_collections_reversal_has_reason" in str(caught.value)


async def test_a_row_cannot_reverse_itself(
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
) -> None:
    from sqlalchemy.exc import IntegrityError

    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    row_id = uuid4()

    with pytest.raises(IntegrityError) as caught:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO collections (id, shift_id, mode, amount, reverses_id, "
                    "reversal_reason) VALUES (:id, :s, CAST('cash' AS collection_mode), "
                    "0.00, :id, 'circular')"
                ).bindparams(id=row_id, s=shift)
            )
    assert "ck_collections_reversal_not_self" in str(caught.value)


async def test_a_collection_cannot_be_added_to_a_closed_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """§6.9. From here the correction path is a reversal, not a new row."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="closed")

    response = await _post(
        client, shift, auth_headers(manager), "closed", mode="cash", amount="10.00"
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_NOT_OPEN"
    assert _count(engine, shift) == 0


async def test_a_collection_cannot_be_added_to_a_locked_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """§10's immutability case: any write to a locked shift is a 409."""
    admin = make_user("admin")
    shift = make_shift(admin, business_date=DAY, sequence=1, status="locked")

    response = await _post(
        client, shift, auth_headers(admin), "locked", mode="cash", amount="10.00"
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_LOCKED"
    assert _count(engine, shift) == 0


# --- correcting while open ---------------------------------------------------


async def test_a_figure_can_be_corrected_while_the_shift_is_open(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_collections,
) -> None:
    """No reversal needed here: nothing has been reconciled against the figure yet.

    §6.9 governs closed and locked shifts. While a shift is open, typing a corrected
    number is the ordinary workflow -- the same reasoning that keeps an append-only trigger
    off this table.
    """
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)

    created = await _post(client, shift, headers, "k", mode="cash", amount="60000.00")
    collection_id = created.json()["id"]

    response = await client.patch(
        f"/api/v1/shifts/{shift}/collections/{collection_id}",
        json={"amount": "58000.00"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["amount"] == "58000.00"
    # Corrected in place, not appended -- there is still exactly one row.
    assert _count(engine, shift) == 1


async def test_a_patch_records_the_old_and_new_values_in_the_audit_log(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_collections,
) -> None:
    """§5.3: `created_by` says who last touched a row; only the audit log says what it was."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)

    created = await _post(client, shift, headers, "k", mode="cash", amount="60000.00")
    collection_id = UUID(created.json()["id"])
    await client.patch(
        f"/api/v1/shifts/{shift}/collections/{collection_id}",
        json={"amount": "58000.00"},
        headers=headers,
    )

    with engine.connect() as connection:
        old, new = connection.execute(
            text(
                "SELECT old_values, new_values FROM audit_logs WHERE table_name = "
                "'collections' AND record_id = :id AND action = 'update'"
            ).bindparams(id=collection_id)
        ).one()

    # Strings, not floats: app/services/audit.py stringifies Decimal so the scale survives.
    assert old["amount"] == "60000.00"
    assert new["amount"] == "58000.00"


async def test_a_patch_cannot_change_the_mode(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_collections,
) -> None:
    """Changing which channel money arrived through is a different row, not a correction --
    and allowing it would let a PATCH walk around the one-live-row-per-mode rule."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)

    created = await _post(client, shift, headers, "k", mode="cash", amount="60000.00")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/collections/{created.json()['id']}",
        json={"mode": "upi"},
        headers=headers,
    )

    assert response.status_code == 422


async def test_an_empty_patch_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    collection = make_collection(shift, mode="cash", amount="60000.00")

    response = await client.patch(
        f"/api/v1/shifts/{shift}/collections/{collection}",
        json={},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "NO_FIELDS_TO_UPDATE"


async def test_patching_a_collection_from_another_shift_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
) -> None:
    """409 rather than 404: the row exists and the caller may well be allowed to see it,
    but the URL asserts a parent-child relationship that is not true. Reporting "not
    found" would send somebody hunting for a row that is sitting right there."""
    manager = make_user("manager")
    mine = make_shift(manager, business_date=DAY, sequence=1)
    theirs = make_shift(manager, business_date=DAY, sequence=2, status="closed")
    elsewhere = make_collection(theirs, mode="cash", amount="1.00")

    response = await client.patch(
        f"/api/v1/shifts/{mine}/collections/{elsewhere}",
        json={"amount": "2.00"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "COLLECTION_NOT_IN_SHIFT"


async def test_patching_an_unknown_collection_is_a_404(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/collections/{uuid4()}",
        json={"amount": "2.00"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "COLLECTION_NOT_FOUND"


# --- reading back ------------------------------------------------------------


async def test_declared_cash_is_null_when_nobody_has_declared(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
) -> None:
    """`null` and `"0.00"` are different answers and the API keeps them apart.

    Collapsing them would let a forgotten entry look exactly like a genuinely cashless day,
    and a shortfall would disappear into that gap -- the same failure §4.7 describes for an
    assumed opening reading.
    """
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    make_collection(shift, mode="upi", amount="20000.00")

    response = await client.get(
        f"/api/v1/shifts/{shift}/collections", headers=auth_headers(attendant)
    )

    assert response.json()["declared_cash"] is None


async def test_declared_cash_is_zero_when_somebody_declared_zero(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    make_collection(shift, mode="cash", amount="0.00")

    response = await client.get(
        f"/api/v1/shifts/{shift}/collections", headers=auth_headers(attendant)
    )

    assert response.json()["declared_cash"] == "0.00"


async def test_the_payload_says_what_declared_cash_is_not(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
) -> None:
    """§13.7's lesson, applied to §6.4: the label travels in the payload, not in the HTML.

    §2 requires a mobile app to do everything the web page can against identical endpoints,
    so a caveat that lives only in a rendered template is a caveat the mobile client does
    not have. Declared cash is the salesman's own figure, not a derived one, and adding it
    to §6.4's derived `cash_sales` double-counts the whole day.
    """
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    make_collection(shift, mode="cash", amount="60000.00")

    body = (
        await client.get(
            f"/api/v1/shifts/{shift}/collections", headers=auth_headers(attendant)
        )
    ).json()

    assert "declared" in body["cash_basis"]
    assert "§6.4" in body["cash_basis"] or "6.4" in body["cash_basis"]


async def test_the_listing_is_empty_for_a_shift_with_no_collections(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    body = (
        await client.get(
            f"/api/v1/shifts/{shift}/collections", headers=auth_headers(attendant)
        )
    ).json()

    assert body["items"] == []
    assert body["totals_by_mode"] == {}
    assert body["declared_cash"] is None
    assert body["truncated"] is False


async def test_totals_are_decimal_all_the_way_through(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
) -> None:
    """§3 rule 1: 0.1 + 0.2 != 0.3 in binary floating point.

    Three amounts chosen because their float sum is 0.30000000000000004. Summed as Decimal
    they are exactly 0.30, and the wire format is a string so nothing downstream can undo
    that by parsing it as a double.
    """
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    make_collection(shift, mode="cash", amount="0.10")
    make_collection(shift, mode="upi", amount="0.20")

    body = (
        await client.get(
            f"/api/v1/shifts/{shift}/collections", headers=auth_headers(attendant)
        )
    ).json()

    assert Decimal(body["totals_by_mode"]["cash"]) + Decimal(
        body["totals_by_mode"]["upi"]
    ) == Decimal("0.30")


async def test_an_explicit_null_in_a_patch_is_ignored_rather_than_clearing_a_field(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    clean_collections,
) -> None:
    """Matches `readings.py`. A client that omits a field and one that sends `null` both
    mean "leave it alone" far more often than they mean "erase it" -- and erasing a
    settlement reference nobody asked to erase makes a card batch unreconcilable."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    headers = auth_headers(attendant)

    created = await _post(
        client, shift, headers, "k", mode="card", amount="10000.00",
        reference="HDFC-batch-4471",
    )
    collection_id = created.json()["id"]

    response = await client.patch(
        f"/api/v1/shifts/{shift}/collections/{collection_id}",
        json={"amount": "9000.00", "reference": None},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["amount"] == "9000.00"
    assert response.json()["reference"] == "HDFC-batch-4471"


# --- two live rows for one mode (Phase 7 Step 0) ------------------------------


async def test_two_live_rows_for_one_mode_are_refused_by_name_not_by_a_500(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_collections,
) -> None:
    """§5.2's one-live-row rule has no unique constraint, so this state is reachable.

    Two concurrent POSTs under READ COMMITTED both pass the live-row probe and both
    insert; idempotency only deduplicates the *same* key, and two genuine requests carry
    different ones. `live_collection_for_mode` used to end in `scalar_one_or_none()`, so
    the second row turned every later call into `MultipleResultsFound` -- a 500 from GET,
    from the next POST, and from §6.8's close precondition. The shift became unreadable
    *and* unclosable, recoverable only by direct SQL.

    The system will not pick between two figures that are both real money, so it refuses
    and names both. That is what makes the refusal actionable rather than merely loud.
    """
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="open")
    first = make_collection(shift, mode="cash", amount="60000.00")
    second = make_collection(shift, mode="cash", amount="58000.00")

    response = await client.get(
        f"/api/v1/shifts/{shift}/collections", headers=auth_headers(manager)
    )

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "DUPLICATE_LIVE_COLLECTIONS"
    # Both rows named, with their amounts -- the caller can act on the error alone.
    assert str(first) in body["detail"] and str(second) in body["detail"]
    assert "60000.00" in body["detail"] and "58000.00" in body["detail"]


async def test_the_reversal_route_still_works_while_a_mode_has_two_live_rows(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_collections,
) -> None:
    """The fix path must stay open while everything else is held shut.

    `reverse_collection` loads its row by id and never calls `live_collection_for_mode`,
    so the 409 above names two rows *and* the caller can immediately act on that name.
    Reversing one restores the shift -- no direct SQL, no support ticket.
    """
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="open")
    make_collection(shift, mode="cash", amount="60000.00")
    duplicate = make_collection(shift, mode="cash", amount="58000.00")

    reversal = await client.post(
        f"/api/v1/shifts/{shift}/collections/{duplicate}/reversals",
        json={"reason": "duplicate entry from a retried request"},
        headers={**auth_headers(manager), "Idempotency-Key": "unwedge"},
    )
    assert reversal.status_code == 201

    after = await client.get(
        f"/api/v1/shifts/{shift}/collections", headers=auth_headers(manager)
    )
    assert after.status_code == 200
    assert after.json()["declared_cash"] == "60000.00"


async def test_a_blank_reversal_reason_is_refused_at_the_database_level(
    engine: Engine,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_collection: Callable[..., UUID],
) -> None:
    """0007 strengthened `ck_collections_reversal_has_reason` past NOT NULL.

    The sibling above proves a NULL reason is refused; that was all the constraint ever
    checked, so an empty string sailed through it -- and the API produced exactly that,
    because `Field(min_length=3)` measured "   " before the handler stripped it. §6.6's
    belt-and-braces rule applies here for the same reason it applies to the receipt FK:
    `services/collections.py::reverse` is reachable from a management command with no
    Pydantic anywhere in the picture.
    """
    from sqlalchemy.exc import IntegrityError

    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    original = make_collection(shift, mode="cash", amount="500.00")

    for blank in ("", "   ", "\t"):
        with pytest.raises(IntegrityError) as caught:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO collections "
                        "(shift_id, mode, amount, reverses_id, reversal_reason) "
                        "VALUES (:s, CAST('cash' AS collection_mode), -500.00, :o, :r)"
                    ).bindparams(s=shift, o=original, r=blank)
                )
        assert "ck_collections_reversal_has_reason" in str(caught.value)
