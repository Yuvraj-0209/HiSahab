"""Nozzles over HTTP (CLAUDE.md §4.3, §5.1, §8).

The immutability tests are the important ones. `fuel_type_id` and `totalizer_max_value`
feed §6.2 and §6.3, which recompute historical sales on read -- so allowing either to
change would silently revalue every shift that nozzle ever served.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

_VALID = {
    "label": "DU-1/N-1",
    "dispenser_label": "DU-1",
    "totalizer_max_value": "999999.99",
    "meter_installed_at": "2026-01-01T00:00:00+05:30",
}


async def test_an_admin_can_register_a_nozzle(
    client, make_user, auth_headers, fuel_type_ids, engine
) -> None:
    from sqlalchemy import text

    response = await client.post(
        "/api/v1/nozzles",
        headers=auth_headers(make_user("admin")),
        json={**_VALID, "fuel_type_id": str(fuel_type_ids["PETROL"])},
    )
    try:
        assert response.status_code == 201
        body = response.json()
        assert body["label"] == "DU-1/N-1"
        assert body["fuel_type_code"] == "PETROL"
        assert body["unit_of_measure"] == "litre"
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM nozzles WHERE label = 'DU-1/N-1'"))


async def test_a_cbg_nozzle_reports_kilograms(
    client, make_user, auth_headers, fuel_type_ids, engine
) -> None:
    """§4.5 end to end -- the unit abstraction reaching an actual HTTP response.

    A client rendering a totalizer reading for this nozzle must be told it counts
    kilograms; nothing about the endpoint's shape would otherwise reveal it.
    """
    from sqlalchemy import text

    response = await client.post(
        "/api/v1/nozzles",
        headers=auth_headers(make_user("admin")),
        json={
            **_VALID,
            "label": "CBG-1/N-1",
            "dispenser_label": "CBG-1",
            "fuel_type_id": str(fuel_type_ids["CBG"]),
        },
    )
    try:
        assert response.status_code == 201
        assert response.json()["unit_of_measure"] == "kilogram"
        assert response.json()["fuel_type_code"] == "CBG"
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM nozzles WHERE label = 'CBG-1/N-1'"))


async def test_a_duplicate_label_at_the_same_outlet_is_a_conflict(
    client, make_user, auth_headers, fuel_type_ids, make_nozzle
) -> None:
    make_nozzle(fuel_type_ids["PETROL"], label="DU-2/N-1")

    response = await client.post(
        "/api/v1/nozzles",
        headers=auth_headers(make_user("admin")),
        json={
            **_VALID,
            "label": "DU-2/N-1",
            "fuel_type_id": str(fuel_type_ids["PETROL"]),
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "NOZZLE_LABEL_EXISTS"


async def test_an_unknown_fuel_type_is_refused(
    client, make_user, auth_headers
) -> None:
    response = await client.post(
        "/api/v1/nozzles",
        headers=auth_headers(make_user("admin")),
        json={**_VALID, "fuel_type_id": str(uuid4())},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "FUEL_TYPE_NOT_FOUND"


async def test_an_inactive_fuel_type_is_refused(
    client, make_user, auth_headers, make_fuel_type
) -> None:
    """You cannot attach a meter to a product the outlet has stopped selling."""
    retired = make_fuel_type("RETIRED_FUEL", is_active=False)

    response = await client.post(
        "/api/v1/nozzles",
        headers=auth_headers(make_user("admin")),
        json={**_VALID, "label": "DU-8/N-1", "fuel_type_id": str(retired)},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "FUEL_TYPE_NOT_FOUND"


@pytest.mark.parametrize("role", ["attendant", "manager"])
async def test_only_an_admin_may_register_a_nozzle(
    client, make_user, auth_headers, fuel_type_ids, role: str
) -> None:
    """§8: "Manage users, nozzles, customers" is admin-only."""
    response = await client.post(
        "/api/v1/nozzles",
        headers=auth_headers(make_user(role)),
        json={**_VALID, "fuel_type_id": str(fuel_type_ids["PETROL"])},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


@pytest.mark.parametrize(
    "field, value",
    [("fuel_type_id", None), ("totalizer_max_value", "500000.00")],
)
async def test_immutable_fields_are_refused_not_ignored(
    client, make_user, auth_headers, fuel_type_ids, make_nozzle, engine, field, value
) -> None:
    """A rewired or re-metered nozzle is a new row, not an edit.

    Repointing the fuel would revalue every past shift at the wrong rate; changing the
    ceiling would change what a past rollover meant (§6.2).
    """
    from sqlalchemy import text

    nozzle_id = make_nozzle(fuel_type_ids["PETROL"], label="DU-3/N-1")
    payload = {field: str(fuel_type_ids["DIESEL"]) if value is None else value}

    response = await client.patch(
        f"/api/v1/nozzles/{nozzle_id}",
        headers=auth_headers(make_user("admin")),
        json=payload,
    )

    assert response.status_code == 422
    with engine.connect() as connection:
        stored = connection.execute(
            text(
                "SELECT fuel_type_id, totalizer_max_value FROM nozzles WHERE id = :id"
            ).bindparams(id=nozzle_id)
        ).one()
    assert stored[0] == fuel_type_ids["PETROL"]
    assert str(stored[1]) == "999999.99"


async def test_the_mutable_fields_can_be_changed(
    client, make_user, auth_headers, fuel_type_ids, make_nozzle
) -> None:
    nozzle_id = make_nozzle(fuel_type_ids["DIESEL"], label="DU-4/N-1")

    response = await client.patch(
        f"/api/v1/nozzles/{nozzle_id}",
        headers=auth_headers(make_user("admin")),
        json={"label": "DU-4/N-2", "is_active": False},
    )

    assert response.status_code == 200
    assert response.json()["label"] == "DU-4/N-2"
    assert response.json()["is_active"] is False


async def test_patching_an_unknown_nozzle_is_a_404_not_a_403(
    client, make_user, auth_headers
) -> None:
    """The row-scoped outlet resolver runs before the role check, so a missing nozzle
    reports as missing rather than as a permission failure."""
    response = await client.patch(
        f"/api/v1/nozzles/{uuid4()}",
        headers=auth_headers(make_user("admin")),
        json={"label": "Whatever"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "NOZZLE_NOT_FOUND"


async def test_inactive_nozzles_are_hidden_by_default(
    client, make_user, auth_headers, fuel_type_ids, make_nozzle
) -> None:
    make_nozzle(fuel_type_ids["PETROL"], label="DU-5/N-1", is_active=False)
    headers = auth_headers(make_user("attendant"))

    default = await client.get("/api/v1/nozzles", headers=headers)
    including = await client.get("/api/v1/nozzles?include_inactive=true", headers=headers)

    assert "DU-5/N-1" not in {row["label"] for row in default.json()}
    assert "DU-5/N-1" in {row["label"] for row in including.json()}


async def test_listing_nozzles_requires_a_token(client) -> None:
    response = await client.get("/api/v1/nozzles")

    assert response.status_code == 401
