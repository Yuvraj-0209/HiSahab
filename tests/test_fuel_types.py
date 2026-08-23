"""Fuel types over HTTP (CLAUDE.md §4.5, §5.1, §8).

Two properties matter here and neither is obvious from the endpoint's shape:

* the unit is **always** returned, so no client ever has to assume litres; and
* `code` and `unit_of_measure` are **refused**, not ignored, on PATCH.

The second is the one that would hurt. An admin who "changed" a fuel from litres to
kilograms and received a 200 back would reasonably believe it had worked.
"""

from __future__ import annotations

from decimal import Decimal

import pytest


async def test_seeded_fuels_are_listed_with_their_units(
    client, make_user, auth_headers
) -> None:
    response = await client.get(
        "/api/v1/fuel-types", headers=auth_headers(make_user("attendant"))
    )

    assert response.status_code == 200
    units = {row["code"]: row["unit_of_measure"] for row in response.json()}
    assert units == {
        "PETROL": "litre",
        "DIESEL": "litre",
        "PREMIUM_PETROL": "litre",
        "CBG": "kilogram",
    }


async def test_cbg_carries_a_different_flow_ceiling_than_petrol(
    client, make_user, auth_headers
) -> None:
    """§6.2's ceiling is per fuel. Served to clients so a UI can validate before posting."""
    response = await client.get(
        "/api/v1/fuel-types", headers=auth_headers(make_user("attendant"))
    )

    rates = {r["code"]: Decimal(r["max_flow_rate_per_minute"]) for r in response.json()}
    assert rates["CBG"] < rates["PETROL"]


async def test_an_admin_can_add_a_product_without_a_migration(
    client, make_user, auth_headers, engine
) -> None:
    """The XP-95 case. Adding a product you already sell is data entry, not a deploy."""
    from sqlalchemy import text

    response = await client.post(
        "/api/v1/fuel-types",
        headers=auth_headers(make_user("admin")),
        json={
            "code": "XP95",
            "display_name": "XP-95",
            "unit_of_measure": "litre",
            "max_flow_rate_per_minute": "60.000",
        },
    )
    try:
        assert response.status_code == 201
        assert response.json()["code"] == "XP95"
        assert response.json()["is_active"] is True
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM fuel_types WHERE code = 'XP95'"))


async def test_a_new_fuel_can_immediately_have_a_nozzle_attached(
    client, make_user, auth_headers, engine
) -> None:
    """End to end: the point of making fuel types writable at all."""
    from sqlalchemy import text

    headers = auth_headers(make_user("admin"))
    created = await client.post(
        "/api/v1/fuel-types",
        headers=headers,
        json={
            "code": "EXTRAGREEN",
            "display_name": "Extra Green",
            "unit_of_measure": "litre",
            "max_flow_rate_per_minute": "55.000",
        },
    )
    try:
        assert created.status_code == 201
        nozzle = await client.post(
            "/api/v1/nozzles",
            headers=headers,
            json={
                "label": "DU-9/N-1",
                "dispenser_label": "DU-9",
                "fuel_type_id": created.json()["id"],
                "totalizer_max_value": "999999.99",
                "meter_installed_at": "2026-01-01T00:00:00+05:30",
            },
        )
        assert nozzle.status_code == 201
        assert nozzle.json()["fuel_type_code"] == "EXTRAGREEN"
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM nozzles WHERE label = 'DU-9/N-1'"))
            connection.execute(text("DELETE FROM fuel_types WHERE code = 'EXTRAGREEN'"))


async def test_the_code_is_normalised_so_one_fuel_cannot_become_two(
    client, make_user, auth_headers, engine
) -> None:
    from sqlalchemy import text

    response = await client.post(
        "/api/v1/fuel-types",
        headers=auth_headers(make_user("admin")),
        json={
            "code": "  lowercase_fuel  ",
            "display_name": "Mixed Case",
            "unit_of_measure": "litre",
            "max_flow_rate_per_minute": "60.000",
        },
    )
    try:
        assert response.json()["code"] == "LOWERCASE_FUEL"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM fuel_types WHERE code = 'LOWERCASE_FUEL'")
            )


async def test_a_duplicate_code_is_a_conflict(client, make_user, auth_headers) -> None:
    response = await client.post(
        "/api/v1/fuel-types",
        headers=auth_headers(make_user("admin")),
        json={
            "code": "PETROL",
            "display_name": "Petrol Again",
            "unit_of_measure": "litre",
            "max_flow_rate_per_minute": "60.000",
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "FUEL_TYPE_CODE_EXISTS"


@pytest.mark.parametrize("role", ["attendant", "manager"])
async def test_only_an_admin_may_add_a_fuel_type(
    client, make_user, auth_headers, role: str
) -> None:
    """§8's new "Manage fuel types" row sits above the manager floor."""
    response = await client.post(
        "/api/v1/fuel-types",
        headers=auth_headers(make_user(role)),
        json={
            "code": "SNEAKY",
            "display_name": "Sneaky",
            "unit_of_measure": "litre",
            "max_flow_rate_per_minute": "60.000",
        },
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


@pytest.mark.parametrize(
    "field, value",
    [("code", "RENAMED"), ("unit_of_measure", "kilogram")],
)
async def test_immutable_fields_are_refused_not_ignored(
    client, make_user, auth_headers, fuel_type_ids, engine, field: str, value: str
) -> None:
    """422, and the stored value untouched.

    Changing a unit would reinterpret every quantity ever recorded against this fuel --
    litres read as kilograms, every historical sale value wrong, and no error anywhere.
    A silent no-op would be worse than the change itself, because the admin would believe
    it had taken effect.
    """
    from sqlalchemy import text

    petrol_id = fuel_type_ids["PETROL"]
    response = await client.patch(
        f"/api/v1/fuel-types/{petrol_id}",
        headers=auth_headers(make_user("admin")),
        json={field: value},
    )

    assert response.status_code == 422
    with engine.connect() as connection:
        stored = connection.execute(
            text(
                "SELECT code, unit_of_measure FROM fuel_types WHERE id = :id"
            ).bindparams(id=petrol_id)
        ).one()
    assert stored == ("PETROL", "litre")


async def test_the_mutable_fields_can_be_changed(
    client, make_user, auth_headers, make_fuel_type
) -> None:
    fuel_type_id = make_fuel_type("MUTABLE", display_name="Before")

    response = await client.patch(
        f"/api/v1/fuel-types/{fuel_type_id}",
        headers=auth_headers(make_user("admin")),
        json={"display_name": "After", "is_active": False},
    )

    assert response.status_code == 200
    assert response.json()["display_name"] == "After"
    assert response.json()["is_active"] is False


async def test_a_deactivated_fuel_is_hidden_but_not_deleted(
    client, make_user, auth_headers, make_fuel_type
) -> None:
    """§3 rule 6 -- no hard deletes. History that references it stays readable."""
    make_fuel_type("RETIRED", is_active=False)
    headers = auth_headers(make_user("attendant"))

    default = await client.get("/api/v1/fuel-types", headers=headers)
    including = await client.get(
        "/api/v1/fuel-types?include_inactive=true", headers=headers
    )

    assert "RETIRED" not in {row["code"] for row in default.json()}
    assert "RETIRED" in {row["code"] for row in including.json()}


async def test_an_empty_patch_is_rejected(
    client, make_user, auth_headers, fuel_type_ids
) -> None:
    response = await client.patch(
        f"/api/v1/fuel-types/{fuel_type_ids['DIESEL']}",
        headers=auth_headers(make_user("admin")),
        json={},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "NO_FIELDS_TO_UPDATE"


async def test_reading_fuel_types_requires_a_token(client) -> None:
    response = await client.get("/api/v1/fuel-types")

    assert response.status_code == 401
    assert response.json()["code"] == "NOT_AUTHENTICATED"


# --- coverage gaps found auditing Phase 3 (Phase 11 Step 0) -------------------
#
# These branches were reachable and untested. The four Phase 3 routers predate the
# 100%-coverage habit that Phases 5-10 hold to; the tests land here, with the phase that
# was editing these files anyway, rather than as a separate sweep.


async def test_patching_a_fuel_type_that_does_not_exist_is_a_404(
    client, make_user, auth_headers
) -> None:
    """Unlike `nozzles`, `fuel_types` has no outlet resolver to 404 first.

    `nozzles.py`'s equivalent branch carries `# pragma: no cover - the resolver above already
    404s`, because a nozzle PATCH authorises through `resolve_outlet_from_nozzle`. A fuel type
    is global reference data (§5.0) with nothing to resolve, so this check is the only one and
    it is genuinely reachable.
    """
    from uuid import uuid4

    response = await client.patch(
        f"/api/v1/fuel-types/{uuid4()}",
        headers=auth_headers(make_user("admin")),
        json={"display_name": "Nothing"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "FUEL_TYPE_NOT_FOUND"


async def test_an_explicit_null_leaves_a_field_alone(
    client, make_user, auth_headers, make_fuel_type
) -> None:
    """`exclude_unset` keeps "not mentioned" and "explicitly null" apart, and the loop then
    skips the null rather than writing it.

    Worth pinning because the alternative -- letting a null through to `setattr` -- would
    violate the column's NOT NULL and surface as a 500 on a request that looks reasonable.
    """
    fuel_type_id = make_fuel_type("NULLPATCH", display_name="Keep Me")

    response = await client.patch(
        f"/api/v1/fuel-types/{fuel_type_id}",
        headers=auth_headers(make_user("admin")),
        json={"display_name": None, "is_active": False},
    )

    assert response.status_code == 200
    # The null was skipped; the real change alongside it still landed.
    assert response.json()["display_name"] == "Keep Me"
    assert response.json()["is_active"] is False
