"""Who may touch a reading (CLAUDE.md §8).

§8 is explicit that ownership is a **separate axis** from role: an attendant may write to a
shift they hold and not to one they do not, at the same role. Phase 4 built
`require_shift_access` for exactly this, and the most important assertion in this file is a
negative one -- `test_no_ownership_check_is_reimplemented_here` -- because a second copy of
that rule is a second place for it to be wrong.

Four floors, one per capability:

* **attendant** -- enter and correct readings, on their own shift only
* **manager** -- clear review flags, read the sales figures
* **admin** -- anchor a new meter, enter a manual quantity after a reset
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

DAY = date(2026, 3, 10)


def _post(client: AsyncClient, shift_id: UUID, headers: dict[str, str], **body: object):
    payload: dict[str, object] = {"opening_confirmed": True}
    payload.update(body)
    return client.post(
        f"/api/v1/shifts/{shift_id}/readings", json=payload, headers=headers
    )


# --- ownership (§8) ----------------------------------------------------------


async def test_an_attendant_cannot_write_to_another_attendants_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§10 names this case specifically, and §8 requires a test for it by name."""
    owner = make_user("attendant")
    intruder = make_user("attendant")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    previous = make_shift(owner, business_date=DAY, sequence=1, status="closed")
    make_reading(previous, nozzle, closing_reading="1500.00")
    shift = make_shift(owner, business_date=date(2026, 3, 11), sequence=1)

    response = await _post(
        client, shift, auth_headers(intruder), nozzle_id=str(nozzle),
        closing_reading="1800.00",
    )

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_YOUR_SHIFT"


async def test_an_attendant_writes_to_their_own_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    previous = make_shift(attendant, business_date=DAY, sequence=1, status="closed")
    make_reading(previous, nozzle, closing_reading="1500.00")
    shift = make_shift(attendant, business_date=date(2026, 3, 11), sequence=1)

    response = await _post(
        client, shift, auth_headers(attendant), nozzle_id=str(nozzle),
        closing_reading="1800.00",
    )

    assert response.status_code == 201


async def test_a_manager_writes_to_any_shift_at_the_outlet(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§8: ownership constrains attendants only. Writing it unconditionally would lock
    managers out of the shifts they are specifically there to close."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    previous = make_shift(attendant, business_date=DAY, sequence=1, status="closed")
    make_reading(previous, nozzle, closing_reading="1500.00")
    shift = make_shift(attendant, business_date=date(2026, 3, 11), sequence=1)

    response = await _post(
        client, shift, auth_headers(manager), nozzle_id=str(nozzle),
        closing_reading="1800.00",
    )

    assert response.status_code == 201


async def test_an_attendant_cannot_read_another_attendants_worksheet(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    owner = make_user("attendant")
    intruder = make_user("attendant")
    shift = make_shift(owner, business_date=DAY, sequence=1)

    response = await client.get(
        f"/api/v1/shifts/{shift}/readings", headers=auth_headers(intruder)
    )

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_YOUR_SHIFT"


# --- role floors (§8) --------------------------------------------------------


async def test_an_attendant_cannot_override_a_quantity(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§8's "Override credit limit / manual litres" row: admin only.

    Without this, the person whose cash the shift is measured against could state the
    quantity it is measured by.
    """
    attendant = make_user("attendant")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, meter_reset_occurred=True)

    response = await client.post(
        f"/api/v1/shifts/{shift}/readings/{nozzle}/override",
        json={"manual_quantity_override": "999.000", "override_reason": "trust me"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


async def test_a_manager_cannot_override_a_quantity(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """The boundary case: roles are hierarchical, so the floor has to be `admin` exactly
    rather than "not an attendant"."""
    manager = make_user("manager")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(manager, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, meter_reset_occurred=True)

    response = await client.post(
        f"/api/v1/shifts/{shift}/readings/{nozzle}/override",
        json={"manual_quantity_override": "312.500", "override_reason": "meter swapped"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 403


async def test_an_attendant_cannot_clear_their_own_review_flag(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """The one that would make §4.7's whole mechanism pointless.

    A flag the flagged person can clear is not a control. §4.7 raises the mismatch so that
    somebody *else* looks at it before a shortfall lands on a salesman's name.
    """
    attendant = make_user("attendant")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    make_reading(shift, nozzle, requires_review=True)

    response = await client.patch(
        f"/api/v1/shifts/{shift}/readings/{nozzle}/review",
        json={"review_note": "it's fine, honestly"},
        headers=auth_headers(attendant),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


async def test_an_attendant_cannot_read_the_sales_figures(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """§8: attendants may read their own shift but not reports. This is the first endpoint
    that is genuinely a report."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await client.get(
        f"/api/v1/shifts/{shift}/sales", headers=auth_headers(attendant)
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


# --- §6.9 immutability -------------------------------------------------------


async def test_a_closed_shift_refuses_a_new_reading_with_a_recoverable_code(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """SHIFT_NOT_OPEN and SHIFT_LOCKED are separate codes on purpose: one is recoverable by
    an admin's reopen (§6.8) and the other never is, and a client should be able to tell
    the user which without parsing prose."""
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(admin, business_date=DAY, sequence=1, status="closed")

    response = await _post(
        client, shift, auth_headers(admin), nozzle_id=str(nozzle),
        opening_reading="1000.00",
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_NOT_OPEN"


async def test_a_locked_shift_refuses_every_write(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_nozzle: Callable[..., UUID],
    make_reading: Callable[..., UUID],
    fuel_type_ids: dict[str, UUID],
    auth_headers,
) -> None:
    """§10's *Immutability* block: any write to a locked shift is a 409. All four routes."""
    admin = make_user("admin")
    nozzle = make_nozzle(fuel_type_ids["PETROL"])
    shift = make_shift(admin, business_date=DAY, sequence=1, status="locked")
    make_reading(shift, nozzle, requires_review=True)
    headers = auth_headers(admin)

    create = await _post(
        client, shift, headers, nozzle_id=str(nozzle), opening_reading="1000.00"
    )
    patch = await client.patch(
        f"/api/v1/shifts/{shift}/readings/{nozzle}",
        json={"closing_reading": "1600.00"},
        headers=headers,
    )
    override = await client.post(
        f"/api/v1/shifts/{shift}/readings/{nozzle}/override",
        json={"manual_quantity_override": "10.000", "override_reason": "no chance"},
        headers=headers,
    )
    review = await client.patch(
        f"/api/v1/shifts/{shift}/readings/{nozzle}/review",
        json={"review_note": "no chance"},
        headers=headers,
    )

    for response in (create, patch, override, review):
        assert response.status_code == 409, response.json()
        assert response.json()["code"] == "SHIFT_LOCKED"


async def test_reading_a_locked_shift_is_still_allowed(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """Immutable is not invisible. A locked shift is precisely what an audit reads."""
    manager = make_user("manager")
    shift = make_shift(manager, business_date=DAY, sequence=1, status="locked")

    worksheet = await client.get(
        f"/api/v1/shifts/{shift}/readings", headers=auth_headers(manager)
    )
    sales = await client.get(
        f"/api/v1/shifts/{shift}/sales", headers=auth_headers(manager)
    )

    assert worksheet.status_code == 200
    assert sales.status_code == 200


# --- structural assertions ---------------------------------------------------


def test_no_ownership_check_is_reimplemented_here() -> None:
    """§8's ownership rule must live in exactly one place.

    Phase 4 put it in `require_shift_access`. A second copy in this router would be a
    second place for it to go wrong, and the two would drift -- the more so because the
    rule is conditional on role, which is the part people forget.

    A grep rather than a mock: this asserts the property that actually matters (the
    comparison does not appear in this module) rather than that some call was made.
    """
    source = Path("app/api/v1/readings.py").read_text()
    assert "attendant_id !=" not in source
    assert "attendant_id ==" not in source
    assert "require_shift_access" in source


def test_the_flow_ceiling_never_reads_the_config_constant() -> None:
    """§14: "Do not read `MAX_FLOW_RATE_LPM` in the §6.2 guard".

    It seeds `fuel_types.max_flow_rate_per_minute` for litre fuels in migration 0003 and
    nothing else. Reading it in the guard would reimpose the single global
    litres-per-minute figure §4.5 rules out, and would reject every real CBG sale -- while
    passing every test that only used petrol.

    Parsed rather than grepped, because these modules *mention* the constant in comments
    warning against exactly this. A plain substring search would fail on the warning and
    pass if somebody deleted the warning and added the bug.
    """
    for path in (
        "app/services/sales.py",
        "app/services/readings.py",
        "app/api/v1/readings.py",
    ):
        tree = ast.parse(Path(path).read_text())
        referenced = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        assert "MAX_FLOW_RATE_LPM" not in referenced, path


def test_no_float_appears_in_the_money_path() -> None:
    """§3 rule 1, asserted structurally.

    A `float()` call anywhere on this path would not fail a value assertion -- Decimal
    compares equal to a float that happens to match -- so it has to be checked as text.
    """
    for path in (
        "app/services/sales.py",
        "app/services/readings.py",
        "app/api/v1/readings.py",
        "app/models/reading.py",
    ):
        source = Path(path).read_text()
        assert "float(" not in source, path
        assert "sa.Float" not in source, path
