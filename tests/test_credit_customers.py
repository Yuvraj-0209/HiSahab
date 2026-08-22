"""Credit customers over HTTP (CLAUDE.md §5.1, §6.6, §8).

The two things worth the most attention here are the split response shape -- attendants get
a list with no phone, no limit and no balance -- and the `(outlet_id, phone)` rule, which is
what stops one customer becoming two ledgers and §6.6's limit never firing against either.
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

DAY = date(2026, 9, 18)


async def _create(client: AsyncClient, headers: dict[str, str], **body):
    payload = {"name": "Ramesh Kumar", "phone": "9876543210", **body}
    return await client.post("/api/v1/credit-customers", json=payload, headers=headers)


# --- creation ----------------------------------------------------------------


async def test_an_admin_can_create_a_customer(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    admin = make_user("admin")

    response = await _create(client, auth_headers(admin), credit_limit="20000.00")

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Ramesh Kumar"
    assert body["credit_limit"] == "20000.00"
    assert body["is_active"] is True
    assert body["outstanding"] == "0.00"


async def test_a_customer_may_be_created_with_no_limit(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """Null means unlimited (§6.6), and must be distinguishable from zero."""
    admin = make_user("admin")

    response = await _create(client, auth_headers(admin))

    assert response.status_code == 201
    assert response.json()["credit_limit"] is None


async def test_a_zero_limit_is_kept_as_zero_not_turned_into_null(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """Zero is a real answer -- on the books, no further udhaar -- and §14 forbids conflating
    it with "no limit"."""
    admin = make_user("admin")

    response = await _create(client, auth_headers(admin), credit_limit="0.00")

    assert response.status_code == 201
    assert response.json()["credit_limit"] == "0.00"


async def test_a_duplicate_phone_at_one_outlet_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """§5.1: one customer, one ledger. Two rows would split a balance in half and §6.6's
    limit would never fire against either."""
    admin = make_user("admin")
    headers = auth_headers(admin)
    first = await _create(client, headers, phone="9998887770")
    assert first.status_code == 201

    second = await _create(client, headers, name="Someone Else", phone="9998887770")

    assert second.status_code == 409
    assert second.json()["code"] == "CREDIT_CUSTOMER_PHONE_EXISTS"


async def test_the_same_phone_at_a_different_outlet_is_fine(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    clean_credit,
) -> None:
    """The constraint is outlet-scoped (§5.0): the same fleet operator may hold accounts at
    two pumps."""
    other_outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outlets (id, name) VALUES (:id, 'Second Outlet')"
            ).bindparams(id=other_outlet)
        )

    try:
        make_credit_customer(phone="9111222333", outlet_id=other_outlet)
        admin = make_user("admin")

        response = await _create(client, auth_headers(admin), phone="9111222333")

        assert response.status_code == 201
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM credit_customers WHERE outlet_id = :id").bindparams(
                    id=other_outlet
                )
            )
            connection.execute(
                text("DELETE FROM outlets WHERE id = :id").bindparams(id=other_outlet)
            )


async def test_a_blank_name_is_a_422_not_a_500(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """`min_length=1` alone passes "   ", which would then hit the database's own non-blank
    CHECK and surface as an opaque 500. Migration 0007 had to fix this exact shape for
    reversal reasons; the validator applies the lesson on the way in."""
    admin = make_user("admin")

    response = await _create(client, auth_headers(admin), name="   ")

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_vehicle_numbers_are_normalised_so_one_vehicle_is_not_three(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """`MH 12 AB 1234`, `mh12-ab-1234` and `MH12AB1234` are one vehicle."""
    admin = make_user("admin")

    response = await _create(
        client,
        auth_headers(admin),
        vehicle_numbers=["MH 12 AB 1234", "mh12-ab-1234", "MH12AB1234"],
    )

    assert response.status_code == 201
    assert response.json()["vehicle_numbers"] == ["MH12AB1234"]


async def test_an_empty_vehicle_list_becomes_null(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
    clean_credit,
) -> None:
    """"No vehicles recorded" and "an empty array" are one fact, not two states."""
    admin = make_user("admin")

    response = await _create(client, auth_headers(admin), vehicle_numbers=[])

    assert response.status_code == 201
    assert response.json()["vehicle_numbers"] is None


# --- the split response shape (§8) -------------------------------------------


async def test_an_attendant_listing_customers_sees_no_phone_limit_or_balance(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    """§8's decision, asserted on the *keys* rather than the values -- a null phone would
    pass a value check while still shipping the field in the schema."""
    attendant = make_user("attendant")
    make_credit_customer(name="Visible", credit_limit="5000.00")

    response = await client.get(
        "/api/v1/credit-customers", headers=auth_headers(attendant)
    )

    assert response.status_code == 200
    row = next(r for r in response.json() if r["name"] == "Visible")
    assert set(row) == {"id", "name", "vehicle_numbers", "is_active"}


async def test_a_manager_listing_customers_sees_the_same_lean_shape(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    """The list endpoint does not change shape by role. One payload that is sometimes safe
    and sometimes not is the thing that goes wrong quietly."""
    manager = make_user("manager")
    make_credit_customer(name="Also Visible")

    response = await client.get(
        "/api/v1/credit-customers", headers=auth_headers(manager)
    )

    row = next(r for r in response.json() if r["name"] == "Also Visible")
    assert set(row) == {"id", "name", "vehicle_numbers", "is_active"}


async def test_an_attendant_cannot_read_one_customers_detail(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    customer = make_credit_customer(name="Private")

    response = await client.get(
        f"/api/v1/credit-customers/{customer}", headers=auth_headers(attendant)
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


async def test_a_manager_reading_one_customer_gets_the_balance(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Owing", credit_limit="9000.00")
    make_credit_sale(shift, customer, make_attachment(attendant), amount="3400.00")

    response = await client.get(
        f"/api/v1/credit-customers/{customer}", headers=auth_headers(manager)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outstanding"] == "3400.00"
    assert body["phone"]
    assert body["credit_limit"] == "9000.00"


# --- the outstanding report --------------------------------------------------


async def test_the_outstanding_report_hides_settled_customers_by_default(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """A chase list of people who owe nothing is noise on the one screen where the answer
    matters."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    owing = make_credit_customer(name="Owes Money")
    settled = make_credit_customer(name="Owes Nothing")
    make_credit_sale(shift, owing, make_attachment(attendant), amount="1500.00")

    response = await client.get(
        "/api/v1/credit-customers/outstanding", headers=auth_headers(manager)
    )

    assert response.status_code == 200
    ids = {row["id"] for row in response.json()}
    assert str(owing) in ids
    assert str(settled) not in ids


async def test_the_outstanding_report_can_include_settled_customers(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    settled = make_credit_customer(name="Nothing Owed")

    response = await client.get(
        "/api/v1/credit-customers/outstanding?include_settled=true",
        headers=auth_headers(manager),
    )

    ids = {row["id"] for row in response.json()}
    assert str(settled) in ids


async def test_the_outstanding_report_sorts_the_largest_debt_first(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    """And a customer in credit (§6.6's negative balance) sorts to the bottom, which is
    where they belong on a chase list."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    small = make_credit_customer(name="Small Debt")
    large = make_credit_customer(name="Large Debt")
    in_credit = make_credit_customer(name="Paid Ahead")
    make_credit_sale(shift, small, make_attachment(attendant), amount="500.00")
    make_credit_sale(shift, large, make_attachment(attendant), amount="9000.00")
    make_credit_repayment(shift, in_credit, amount="200.00")

    response = await client.get(
        "/api/v1/credit-customers/outstanding", headers=auth_headers(manager)
    )

    rows = response.json()
    order = [row["id"] for row in rows]
    assert order.index(str(large)) < order.index(str(small))
    assert order.index(str(small)) < order.index(str(in_credit))
    assert Decimal(rows[order.index(str(in_credit))]["outstanding"]) < 0


async def test_an_attendant_cannot_read_the_outstanding_report(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    attendant = make_user("attendant")

    response = await client.get(
        "/api/v1/credit-customers/outstanding", headers=auth_headers(attendant)
    )

    assert response.status_code == 403


async def test_outstanding_is_not_parsed_as_a_customer_id(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    """The route-ordering hazard, pinned. If `/credit-customers/{customer_id}` were declared
    first, "outstanding" would parse as a UUID and 422."""
    manager = make_user("manager")

    response = await client.get(
        "/api/v1/credit-customers/outstanding", headers=auth_headers(manager)
    )

    assert response.status_code == 200
    assert isinstance(response.json(), list)


# --- editing and retiring ----------------------------------------------------


async def test_an_admin_can_edit_a_customer(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    admin = make_user("admin")
    customer = make_credit_customer(name="Old Name")

    response = await client.patch(
        f"/api/v1/credit-customers/{customer}",
        json={"name": "New Name", "credit_limit": "12000.00"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "New Name"
    assert body["credit_limit"] == "12000.00"


async def test_a_phone_may_be_changed_unlike_a_category_code(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    """A phone identifies a person so they are not created twice; it carries no historical
    meaning the way `expense_categories.code` does, and people change numbers."""
    admin = make_user("admin")
    customer = make_credit_customer(phone="9000000001")

    response = await client.patch(
        f"/api/v1/credit-customers/{customer}",
        json={"phone": "9000000002"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    assert response.json()["phone"] == "9000000002"


async def test_a_phone_cannot_be_changed_onto_another_customers(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    admin = make_user("admin")
    make_credit_customer(phone="9000000010")
    other = make_credit_customer(phone="9000000011")

    response = await client.patch(
        f"/api/v1/credit-customers/{other}",
        json={"phone": "9000000010"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CREDIT_CUSTOMER_PHONE_EXISTS"


async def test_setting_a_customers_own_phone_again_is_not_a_conflict(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    """The duplicate check excludes the row being edited, or every PATCH that echoed the
    existing phone back would 409 against itself."""
    admin = make_user("admin")
    customer = make_credit_customer(phone="9000000020")

    response = await client.patch(
        f"/api/v1/credit-customers/{customer}",
        json={"phone": "9000000020", "name": "Renamed"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200


async def test_an_empty_patch_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    admin = make_user("admin")
    customer = make_credit_customer()

    response = await client.patch(
        f"/api/v1/credit-customers/{customer}", json={}, headers=auth_headers(admin)
    )

    assert response.status_code == 422
    assert response.json()["code"] == "NO_FIELDS_TO_UPDATE"


async def test_an_unknown_field_is_refused_rather_than_silently_ignored(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    """An admin who "edited" something and got a 200 back would reasonably believe it
    worked."""
    admin = make_user("admin")
    customer = make_credit_customer()

    response = await client.patch(
        f"/api/v1/credit-customers/{customer}",
        json={"outstanding": "0.00"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 422


async def test_a_customer_is_retired_by_deactivating_never_deleting(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """§3 rule 6. There is no DELETE route at all, and the row survives deactivation."""
    admin = make_user("admin")
    customer = make_credit_customer(name="Retiring")

    response = await client.patch(
        f"/api/v1/credit-customers/{customer}",
        json={"is_active": False},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    assert response.json()["is_active"] is False
    with engine.connect() as connection:
        still_there = connection.execute(
            text("SELECT count(*) FROM credit_customers WHERE id = :id").bindparams(
                id=customer
            )
        ).scalar_one()
    assert still_there == 1


async def test_a_deactivated_customer_is_hidden_from_the_list_but_findable(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    retired = make_credit_customer(name="Retired", is_active=False)

    default = await client.get(
        "/api/v1/credit-customers", headers=auth_headers(attendant)
    )
    included = await client.get(
        "/api/v1/credit-customers?include_inactive=true",
        headers=auth_headers(attendant),
    )

    assert str(retired) not in {row["id"] for row in default.json()}
    assert str(retired) in {row["id"] for row in included.json()}


# --- permissions and tenancy -------------------------------------------------


async def test_a_manager_cannot_create_a_customer(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers, clean_credit
) -> None:
    """§8: managing customers is admin-only, alongside users and nozzles."""
    manager = make_user("manager")

    response = await _create(client, auth_headers(manager))

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


async def test_a_manager_cannot_edit_a_customer(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    manager = make_user("manager")
    customer = make_credit_customer()

    response = await client.patch(
        f"/api/v1/credit-customers/{customer}",
        json={"name": "Nope"},
        headers=auth_headers(manager),
    )

    assert response.status_code == 403


async def test_an_unknown_customer_id_is_a_404(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    manager = make_user("manager")

    response = await client.get(
        f"/api/v1/credit-customers/{uuid4()}", headers=auth_headers(manager)
    )

    assert response.status_code == 404
    assert response.json()["code"] == "CREDIT_CUSTOMER_NOT_FOUND"


async def test_a_customer_at_another_outlet_is_403_not_found_here(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """The deliberate divergence from §7.3's attachments, pinned so a future change is a
    decision rather than a drift.

    Attachments return 404 across tenants because a receipt photo can carry anything. A
    customer record is manager-gated already -- an attendant cannot read one at all -- so
    this keeps the ordinary `require_role` posture every other outlet-scoped resource uses.
    The Phase 9 plan flags it for the owner as the looser of two defensible options.
    """
    other_outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outlets (id, name) VALUES (:id, 'Second Outlet')"
            ).bindparams(id=other_outlet)
        )

    try:
        elsewhere = make_credit_customer(outlet_id=other_outlet)
        manager = make_user("manager")

        response = await client.get(
            f"/api/v1/credit-customers/{elsewhere}", headers=auth_headers(manager)
        )

        assert response.status_code == 403
        assert response.json()["code"] == "NOT_A_MEMBER"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM credit_customers WHERE outlet_id = :id").bindparams(
                    id=other_outlet
                )
            )
            connection.execute(
                text("DELETE FROM outlets WHERE id = :id").bindparams(id=other_outlet)
            )


async def test_the_list_is_scoped_to_the_callers_outlet(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    other_outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outlets (id, name) VALUES (:id, 'Second Outlet')"
            ).bindparams(id=other_outlet)
        )

    try:
        theirs = make_credit_customer(name="Theirs", outlet_id=other_outlet)
        mine = make_credit_customer(name="Mine")
        attendant = make_user("attendant")

        response = await client.get(
            "/api/v1/credit-customers", headers=auth_headers(attendant)
        )

        ids = {row["id"] for row in response.json()}
        assert str(mine) in ids
        assert str(theirs) not in ids
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM credit_customers WHERE outlet_id = :id").bindparams(
                    id=other_outlet
                )
            )
            connection.execute(
                text("DELETE FROM outlets WHERE id = :id").bindparams(id=other_outlet)
            )


async def test_an_unauthenticated_request_is_refused(client: AsyncClient) -> None:
    response = await client.get("/api/v1/credit-customers")

    assert response.status_code == 401


# --- what an explicit null means on PATCH ------------------------------------


async def test_a_credit_limit_can_be_cleared_back_to_unlimited(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    """The deliberate divergence from `expense_categories.py`, which skips every explicit
    null on PATCH.

    That is right for a table whose editable columns are all NOT NULL and wrong here: null
    IS the meaningful value for `credit_limit` -- it is §6.6's "no limit" -- and lifting a
    cap from a customer who has earned unlimited trust is a real operation. A blanket skip
    would make a limit, once set, impossible to remove.
    """
    admin = make_user("admin")
    customer = make_credit_customer(credit_limit="5000.00")

    response = await client.patch(
        f"/api/v1/credit-customers/{customer}",
        json={"credit_limit": None},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    assert response.json()["credit_limit"] is None


async def test_vehicle_numbers_can_be_cleared(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    admin = make_user("admin")
    customer = make_credit_customer(vehicle_numbers=["MH12AB1234"])

    response = await client.patch(
        f"/api/v1/credit-customers/{customer}",
        json={"vehicle_numbers": None},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    assert response.json()["vehicle_numbers"] is None


async def test_vehicle_numbers_are_normalised_on_patch_too(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    """Normalisation on create and not on edit would let the second spelling in through the
    back door."""
    admin = make_user("admin")
    customer = make_credit_customer()

    response = await client.patch(
        f"/api/v1/credit-customers/{customer}",
        json={"vehicle_numbers": ["ka 05 mn 7788", "KA05MN7788"]},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    assert response.json()["vehicle_numbers"] == ["KA05MN7788"]


async def test_a_null_name_is_ignored_rather_than_reaching_the_not_null_column(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    """`name` is NOT NULL, so applying an explicit null would surface as an opaque 500
    rather than a field error. Skipped, exactly as Phase 8 skips every null."""
    admin = make_user("admin")
    customer = make_credit_customer(name="Keeps This Name")

    response = await client.patch(
        f"/api/v1/credit-customers/{customer}",
        json={"name": None, "credit_limit": "100.00"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Keeps This Name"
    assert body["credit_limit"] == "100.00"


async def test_a_blank_name_on_patch_is_a_422(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    admin = make_user("admin")
    customer = make_credit_customer()

    response = await client.patch(
        f"/api/v1/credit-customers/{customer}",
        json={"name": "  "},
        headers=auth_headers(admin),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_a_null_phone_does_not_trip_the_duplicate_check(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    """Regression guard: the duplicate-phone check reads `changes["phone"]`, and an explicit
    null must not be compared against another customer's number or used as one."""
    admin = make_user("admin")
    make_credit_customer(phone="9000000030")
    customer = make_credit_customer(phone="9000000031")

    response = await client.patch(
        f"/api/v1/credit-customers/{customer}",
        json={"phone": None, "name": "Still Fine"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    assert response.json()["phone"] == "9000000031"


# --- the ledger --------------------------------------------------------------


async def test_the_ledger_interleaves_sales_and_repayments_newest_first(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    """The evidence behind §6.6's outstanding figure. A UNION ALL rather than two requests
    the client merges, because interleaving the newest 50 of each table gives the newest 50
    overall only by luck."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Has History")
    make_credit_sale(shift, customer, make_attachment(attendant), amount="2000.00")
    make_credit_repayment(shift, customer, amount="500.00")

    response = await client.get(
        f"/api/v1/credit-customers/{customer}/ledger", headers=auth_headers(manager)
    )

    assert response.status_code == 200
    kinds = {row["kind"] for row in response.json()["items"]}
    assert kinds == {"sale", "repayment"}


async def test_the_ledgers_balance_deltas_sum_to_the_outstanding_figure(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    """The check that makes the ledger worth having: line by line, it must add up to the
    number on the customer's detail page. A balance nobody can take apart is one the owner
    has to trust rather than verify, and §14 calls a plausible-but-wrong figure this
    project's primary failure mode.

    Deliberately includes a reversed sale AND a reversed repayment, so the two sign rules
    are exercised together rather than one at a time.
    """
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer(name="Complicated")
    attachment = make_attachment(attendant)

    make_credit_sale(shift, customer, attachment, amount="5000.00")
    cancelled = make_credit_sale(
        shift, customer, make_attachment(attendant), amount="900.00"
    )
    make_credit_sale(
        shift, customer, make_attachment(attendant), amount="-900.00",
        reverses_id=cancelled, reversal_reason="wrong customer",
    )
    make_credit_repayment(shift, customer, amount="1200.00")
    bounced = make_credit_repayment(shift, customer, amount="800.00")
    make_credit_repayment(
        shift, customer, amount="-800.00", reverses_id=bounced,
        reversal_reason="cheque bounced",
    )

    ledger = await client.get(
        f"/api/v1/credit-customers/{customer}/ledger", headers=auth_headers(manager)
    )
    detail = await client.get(
        f"/api/v1/credit-customers/{customer}", headers=auth_headers(manager)
    )

    total = sum(
        (Decimal(row["balance_delta"]) for row in ledger.json()["items"]),
        Decimal("0.00"),
    )
    assert total == Decimal(detail.json()["outstanding"])
    # 5000 - 900 + 900 (the cancelled sale nets out) - 1200 - 800 + 800 = 3800
    assert total == Decimal("3800.00")


async def test_a_repayments_balance_delta_is_the_negation_of_its_amount(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_repayment: Callable[..., UUID],
    auth_headers,
) -> None:
    """`amount` is what a human finds on the slip; `balance_delta` is what §6.6's sum added.
    Both are returned because deriving one from the other requires knowing the rule."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer()
    make_credit_repayment(shift, customer, amount="750.00")

    response = await client.get(
        f"/api/v1/credit-customers/{customer}/ledger", headers=auth_headers(manager)
    )

    row = response.json()["items"][0]
    assert row["amount"] == "750.00"
    assert row["balance_delta"] == "-750.00"


async def test_reversals_appear_as_their_own_lines(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """§6.9: both rows remain visible. A customer disputing a bill is entitled to see that a
    charge was raised and cancelled, rather than an account that never mentions it."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer()
    attachment = make_attachment(attendant)
    original = make_credit_sale(shift, customer, attachment, amount="600.00")
    make_credit_sale(
        shift, customer, attachment, amount="-600.00",
        reverses_id=original, reversal_reason="cancelled",
    )

    response = await client.get(
        f"/api/v1/credit-customers/{customer}/ledger", headers=auth_headers(manager)
    )

    items = response.json()["items"]
    assert len(items) == 2
    assert sum(1 for row in items if row["is_reversal"]) == 1


async def test_the_ledger_is_cursor_paginated(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """§9 forbids offset pagination -- inserts during a scroll cause duplicates and skips.
    A ledger grows forever, unlike the customer list, so it gets a real cursor."""
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer()
    for _ in range(5):
        make_credit_sale(
            shift, customer, make_attachment(attendant), amount="100.00"
        )

    first = await client.get(
        f"/api/v1/credit-customers/{customer}/ledger?limit=2",
        headers=auth_headers(manager),
    )
    body = first.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None

    second = await client.get(
        f"/api/v1/credit-customers/{customer}/ledger?limit=2&cursor={body['next_cursor']}",
        headers=auth_headers(manager),
    )

    first_ids = {row["id"] for row in body["items"]}
    second_ids = {row["id"] for row in second.json()["items"]}
    assert first_ids.isdisjoint(second_ids)


async def test_the_last_page_has_no_cursor(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    customer = make_credit_customer()
    make_credit_sale(shift, customer, make_attachment(attendant), amount="100.00")

    response = await client.get(
        f"/api/v1/credit-customers/{customer}/ledger", headers=auth_headers(manager)
    )

    assert response.json()["next_cursor"] is None


async def test_a_bad_cursor_is_a_400(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    """Never a silent restart from the top -- that would look like working pagination while
    quietly re-showing rows the reader already passed."""
    manager = make_user("manager")
    customer = make_credit_customer()

    response = await client.get(
        f"/api/v1/credit-customers/{customer}/ledger?cursor=not-a-cursor",
        headers=auth_headers(manager),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_CURSOR"


async def test_an_attendant_cannot_read_a_ledger(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    customer = make_credit_customer()

    response = await client.get(
        f"/api/v1/credit-customers/{customer}/ledger", headers=auth_headers(attendant)
    )

    assert response.status_code == 403


async def test_one_customers_ledger_shows_only_their_own_rows(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_attachment: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    mine = make_credit_customer(name="Mine")
    theirs = make_credit_customer(name="Theirs")
    make_credit_sale(shift, mine, make_attachment(attendant), amount="100.00")
    make_credit_sale(shift, theirs, make_attachment(attendant), amount="200.00")

    response = await client.get(
        f"/api/v1/credit-customers/{mine}/ledger", headers=auth_headers(manager)
    )

    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["amount"] == "100.00"
