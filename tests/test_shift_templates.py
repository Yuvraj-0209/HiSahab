"""Shift templates (CLAUDE.md §5.1, §4.7, §8).

The table exists so that nobody retypes 06:00 and 22:00 every morning, because days are
typed in after the fact and `started_at` is the field that decides which day's fuel rate a
whole shift is valued at (§6.3).

The rule these tests defend hardest is the one that is easy to break by accident: a template
supplies a *default at creation* and is never read again. If editing a template ever
revalued a shift that already traded, the system would silently restate history.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text


@pytest.fixture
def clean_templates(engine: Engine):
    """Remove templates a test created, leaving migration 0004's seeded row alone.

    Keyed on `created_by IS NOT NULL`: the seeded row is system-created and has none, the
    same marker 0001's outlet and 0003's fuel types use.
    """
    yield
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM outlet_shift_templates WHERE created_by IS NOT NULL")
        )


async def test_the_seeded_template_is_this_outlets_trading_window(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """§4.7: 06:00-22:00. A confirmed fact about the outlet, not the kind of guess 0003
    deliberately refused to seed."""
    attendant = make_user("attendant")

    response = await client.get("/api/v1/shift-templates", headers=auth_headers(attendant))

    assert response.status_code == 200
    templates = response.json()
    assert len(templates) == 1
    assert templates[0]["sequence"] == 1
    assert templates[0]["starts_at_local"] == "06:00:00"
    assert templates[0]["ends_at_local"] == "22:00:00"
    # Does not cross midnight, unlike a 24-hour outlet's night shift.
    assert templates[0]["crosses_midnight"] is False


async def test_an_admin_can_describe_a_three_shift_outlet(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_templates,
) -> None:
    """The SaaS case §4.7 exists to serve: a 24-hour station on three 8-hour shifts.

    Nothing forces the templates to tile the day or be contiguous -- an outlet that shuts
    overnight has a deliberate gap, and refusing that would refuse the outlet this software
    was written for.
    """
    admin = make_user("admin")

    for sequence, label, starts, ends in (
        (2, "Afternoon", "14:00:00", "22:00:00"),
        (3, "Night", "22:00:00", "06:00:00"),
    ):
        response = await client.post(
            "/api/v1/shift-templates",
            json={
                "sequence": sequence,
                "label": label,
                "starts_at_local": starts,
                "ends_at_local": ends,
            },
            headers=auth_headers(admin),
        )
        assert response.status_code == 201, response.json()

    listed = await client.get("/api/v1/shift-templates", headers=auth_headers(admin))
    assert [t["sequence"] for t in listed.json()] == [1, 2, 3]
    assert [t["crosses_midnight"] for t in listed.json()] == [False, False, True]


async def test_a_duplicate_sequence_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_templates,
) -> None:
    """Caught before the unique constraint so it surfaces as a 409, never a 500."""
    admin = make_user("admin")

    response = await client.post(
        "/api/v1/shift-templates",
        json={
            "sequence": 1,
            "label": "Also shift one",
            "starts_at_local": "07:00:00",
            "ends_at_local": "21:00:00",
        },
        headers=auth_headers(admin),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_TEMPLATE_SEQUENCE_EXISTS"


async def test_a_zero_length_template_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_templates,
) -> None:
    """Equal times are ambiguous rather than wrong-looking: they could mean "no time at
    all" or "a full 24 hours", and `template_window` would resolve them to the former."""
    admin = make_user("admin")

    response = await client.post(
        "/api/v1/shift-templates",
        json={
            "sequence": 4,
            "label": "Nothing",
            "starts_at_local": "09:00:00",
            "ends_at_local": "09:00:00",
        },
        headers=auth_headers(admin),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "SHIFT_TEMPLATE_ZERO_LENGTH"


@pytest.mark.parametrize("role", ["attendant", "manager"])
async def test_only_an_admin_may_add_a_template(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers, role: str
) -> None:
    """§8 groups this with "manage users, nozzles, customers" -- admin only."""
    actor = make_user(role)

    response = await client.post(
        "/api/v1/shift-templates",
        json={
            "sequence": 5,
            "label": "Sneaky",
            "starts_at_local": "01:00:00",
            "ends_at_local": "02:00:00",
        },
        headers=auth_headers(actor),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


async def test_an_attendant_may_read_templates(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """Attendant floor on the read: they are the ones opening shifts from these."""
    attendant = make_user("attendant")

    response = await client.get("/api/v1/shift-templates", headers=auth_headers(attendant))

    assert response.status_code == 200


async def test_the_sequence_cannot_be_renumbered(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_templates,
) -> None:
    """`sequence` is the template's identity -- shifts are matched to a template by it --
    so renumbering would quietly repoint future shifts at a different pattern.

    Omitted from the update schema with extra="forbid", so it is a 422 rather than a silent
    no-op that leaves the admin believing it applied."""
    admin = make_user("admin")
    created = await client.post(
        "/api/v1/shift-templates",
        json={
            "sequence": 6,
            "label": "Relief",
            "starts_at_local": "12:00:00",
            "ends_at_local": "16:00:00",
        },
        headers=auth_headers(admin),
    )

    response = await client.patch(
        f"/api/v1/shift-templates/{created.json()['id']}",
        json={"sequence": 7},
        headers=auth_headers(admin),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_a_template_can_be_retimed_and_retired(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_templates,
) -> None:
    admin = make_user("admin")
    created = await client.post(
        "/api/v1/shift-templates",
        json={
            "sequence": 8,
            "label": "Relief",
            "starts_at_local": "12:00:00",
            "ends_at_local": "16:00:00",
        },
        headers=auth_headers(admin),
    )
    template_id = created.json()["id"]

    retimed = await client.patch(
        f"/api/v1/shift-templates/{template_id}",
        json={"ends_at_local": "17:30:00", "is_active": False},
        headers=auth_headers(admin),
    )

    assert retimed.status_code == 200
    assert retimed.json()["ends_at_local"] == "17:30:00"
    assert retimed.json()["is_active"] is False

    # Retired templates are hidden by default but not deleted (§3 rule 6).
    default_list = await client.get(
        "/api/v1/shift-templates", headers=auth_headers(admin)
    )
    with_inactive = await client.get(
        "/api/v1/shift-templates?include_inactive=true", headers=auth_headers(admin)
    )
    assert template_id not in {t["id"] for t in default_list.json()}
    assert template_id in {t["id"] for t in with_inactive.json()}


async def test_editing_a_template_does_not_revalue_a_shift_that_already_traded(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_shifts,
    engine: Engine,
) -> None:
    """The rule this table is most likely to have broken by accident.

    §6.3 values a whole shift's fuel at the rate effective at its own `started_at`. If the
    times were read back through the template instead of stored on the shift, changing the
    template would restate what a past day's fuel was worth -- silently, with no error and
    no audit entry. That is precisely the plausible-but-wrong number CLAUDE.md exists to
    prevent.
    """
    admin = make_user("admin")
    attendant = make_user("attendant")

    opened = await client.post(
        "/api/v1/shifts",
        json={"business_date": "2026-07-01"},
        headers=auth_headers(attendant),
    )
    shift_id = opened.json()["id"]
    original_start = opened.json()["started_at"]

    seeded = (
        await client.get("/api/v1/shift-templates", headers=auth_headers(admin))
    ).json()[0]
    await client.patch(
        f"/api/v1/shift-templates/{seeded['id']}",
        json={"starts_at_local": "05:00:00"},
        headers=auth_headers(admin),
    )

    after = await client.get(f"/api/v1/shifts/{shift_id}", headers=auth_headers(admin))

    assert after.json()["started_at"] == original_start

    # Restore the seeded window for the rest of the run -- 0004 seeded it, so no fixture
    # owns it.
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE outlet_shift_templates SET starts_at_local = '06:00:00' "
                "WHERE created_by IS NULL"
            )
        )


async def test_an_unknown_template_is_a_404(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """404 lives in the outlet resolver, so a missing row never reports as a 403."""
    admin = make_user("admin")

    response = await client.patch(
        f"/api/v1/shift-templates/{uuid4()}",
        json={"label": "Ghost"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "SHIFT_TEMPLATE_NOT_FOUND"


async def test_an_empty_patch_is_refused(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """A no-op that returns 200 leaves the admin believing something changed."""
    admin = make_user("admin")
    seeded = (
        await client.get("/api/v1/shift-templates", headers=auth_headers(admin))
    ).json()[0]

    response = await client.patch(
        f"/api/v1/shift-templates/{seeded['id']}", json={}, headers=auth_headers(admin)
    )

    assert response.status_code == 422
    assert response.json()["code"] == "NO_FIELDS_TO_UPDATE"


async def test_an_explicit_null_is_ignored_rather_than_clearing_a_field(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_templates,
) -> None:
    """`label: null` means "not supplied", not "erase it" -- these columns are NOT NULL,
    so writing the null through would be an IntegrityError surfacing as a 500."""
    admin = make_user("admin")
    created = await client.post(
        "/api/v1/shift-templates",
        json={
            "sequence": 9,
            "label": "Relief",
            "starts_at_local": "12:00:00",
            "ends_at_local": "16:00:00",
        },
        headers=auth_headers(admin),
    )

    response = await client.patch(
        f"/api/v1/shift-templates/{created.json()['id']}",
        json={"label": None, "is_active": False},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    assert response.json()["label"] == "Relief"
    assert response.json()["is_active"] is False


async def test_a_patch_cannot_make_a_template_zero_length(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_templates,
) -> None:
    """The same guard as on create. Checked after the fields are applied, because either
    one alone can collide with the other's existing value."""
    admin = make_user("admin")
    created = await client.post(
        "/api/v1/shift-templates",
        json={
            "sequence": 10,
            "label": "Relief",
            "starts_at_local": "12:00:00",
            "ends_at_local": "16:00:00",
        },
        headers=auth_headers(admin),
    )

    response = await client.patch(
        f"/api/v1/shift-templates/{created.json()['id']}",
        json={"ends_at_local": "12:00:00"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "SHIFT_TEMPLATE_ZERO_LENGTH"
