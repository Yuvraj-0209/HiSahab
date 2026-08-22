"""GET /api/v1/attachments/{id}/url (CLAUDE.md §7.3).

The permission rule has two axes, per §8: role first (require_role, inside the
`resolve_outlet_from_attachment` dependency), then ownership (`may_read`, for an attendant
only). `make_expense`'s `attachment_id` kwarg (added in Step 3) links a raw-SQL expense to
an attachment without going through the Step 8 wiring, so these tests do not depend on it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.anyio

DAY = date(2026, 7, 20)

_REAL_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300"
    "0302020302020303030304030304050805050404050a070706"
    "080c0a0c0c0b0a0b0b0d0e12100d0e110e0b0b1016101113141"
    "51516150c0f171d17141d1114141400ffc9000b080001000101"
    "0100ffcc0006001005f0ffda0008010100003f00d2cf20ffd9"
)


async def _upload(
    client: AsyncClient, headers: dict[str, str], *, shift_id: UUID
) -> str:
    response = await client.post(
        "/api/v1/uploads/receipt",
        data={"shift_id": str(shift_id)},
        files={"file": ("r.jpg", _REAL_JPEG, "image/jpeg")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["attachment_id"]


# --- happy path + shape ------------------------------------------------------------


async def test_the_uploader_can_read_their_own_unlinked_upload(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    """Between §7.2's steps 5 and 6 the attachment is linked to nothing at all -- the
    uploader must still be able to see what they just uploaded."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    attachment_id = await _upload(client, auth_headers(attendant), shift_id=shift)

    response = await client.get(
        f"/api/v1/attachments/{attachment_id}/url", headers=auth_headers(attendant)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["url"]
    assert body["expires_in_seconds"] == 300


async def test_the_url_respects_signed_url_ttl_seconds_from_config(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    attachment_id = await _upload(client, auth_headers(attendant), shift_id=shift)

    response = await client.get(
        f"/api/v1/attachments/{attachment_id}/url", headers=auth_headers(attendant)
    )

    assert response.json()["expires_in_seconds"] == 300


# --- ownership (§7.3) ---------------------------------------------------------------


async def test_an_attendant_can_read_a_receipt_linked_to_their_own_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    attachment_id = await _upload(client, auth_headers(attendant), shift_id=shift)
    make_expense(shift, attachment_id=UUID(attachment_id))

    response = await client.get(
        f"/api/v1/attachments/{attachment_id}/url", headers=auth_headers(attendant)
    )

    assert response.status_code == 200


async def test_an_attendant_cannot_read_another_attendants_unlinked_upload(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    owner = make_user("attendant")
    other = make_user("attendant")
    shift = make_shift(owner, business_date=DAY, sequence=1)
    attachment_id = await _upload(client, auth_headers(owner), shift_id=shift)

    response = await client.get(
        f"/api/v1/attachments/{attachment_id}/url", headers=auth_headers(other)
    )

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_YOUR_ATTACHMENT"


async def test_an_attendant_cannot_read_a_receipt_linked_to_someone_elses_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    owner = make_user("attendant")
    other = make_user("attendant")
    shift = make_shift(owner, business_date=DAY, sequence=1)
    attachment_id = await _upload(client, auth_headers(owner), shift_id=shift)
    make_expense(shift, attachment_id=UUID(attachment_id))

    response = await client.get(
        f"/api/v1/attachments/{attachment_id}/url", headers=auth_headers(other)
    )

    assert response.status_code == 403


async def test_a_manager_may_read_any_attachment_at_their_outlet(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    attachment_id = await _upload(client, auth_headers(attendant), shift_id=shift)

    response = await client.get(
        f"/api/v1/attachments/{attachment_id}/url", headers=auth_headers(manager)
    )

    assert response.status_code == 200


async def test_an_admin_may_read_any_attachment_at_their_outlet(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    admin = make_user("admin")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    attachment_id = await _upload(client, auth_headers(attendant), shift_id=shift)

    response = await client.get(
        f"/api/v1/attachments/{attachment_id}/url", headers=auth_headers(admin)
    )

    assert response.status_code == 200


# --- existence / tenancy (§7.3) -----------------------------------------------------


async def test_an_unknown_attachment_id_is_a_404(
    client: AsyncClient, make_user: Callable[..., UUID], auth_headers
) -> None:
    attendant = make_user("attendant")

    response = await client.get(
        f"/api/v1/attachments/{uuid4()}/url", headers=auth_headers(attendant)
    )

    assert response.status_code == 404
    assert response.json()["code"] == "ATTACHMENT_NOT_FOUND"


async def test_an_attachment_from_another_outlet_is_404_not_403(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """Existence is not leaked across tenants -- the same posture §5.1's category lookup
    and §7.3 itself both take."""
    other_outlet = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO outlets (id, name) VALUES (:id, 'Second Outlet')").bindparams(
                id=other_outlet
            )
        )

    try:
        attendant = make_user("attendant")
        shift = make_shift(attendant, business_date=DAY, sequence=1)
        attachment_id = await _upload(client, auth_headers(attendant), shift_id=shift)

        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE attachments SET outlet_id = :outlet WHERE id = CAST(:id AS uuid)"
                ).bindparams(outlet=other_outlet, id=attachment_id)
            )

        response = await client.get(
            f"/api/v1/attachments/{attachment_id}/url", headers=auth_headers(attendant)
        )

        assert response.status_code == 404
        assert response.json()["code"] == "ATTACHMENT_NOT_FOUND"
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM attachments WHERE outlet_id = :id").bindparams(id=other_outlet))
            connection.execute(text("DELETE FROM outlets WHERE id = :id").bindparams(id=other_outlet))


async def test_a_user_with_no_membership_anywhere_still_gets_404_not_403(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """The deliberate deviation from `require_role`'s usual NOT_A_MEMBER 403 (see
    `app/api/v1/attachments.py::_attachment_access`). A user who is authenticated but has
    no outlet_memberships row at all must be indistinguishable, from the response alone,
    from one asking about an attachment that was never created."""
    from uuid import uuid4

    with engine.begin() as connection:
        stray_user = uuid4()
        connection.execute(
            text(
                "INSERT INTO user_profiles (id, full_name) VALUES (:id, 'No Membership')"
            ).bindparams(id=stray_user)
        )

    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    attachment_id = await _upload(client, auth_headers(attendant), shift_id=shift)

    try:
        response = await client.get(
            f"/api/v1/attachments/{attachment_id}/url", headers=auth_headers(stray_user)
        )
        assert response.status_code == 404
        assert response.json()["code"] == "ATTACHMENT_NOT_FOUND"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM user_profiles WHERE id = :id").bindparams(id=stray_user)
            )


async def test_an_unauthenticated_read_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    attachment_id = await _upload(client, auth_headers(attendant), shift_id=shift)

    response = await client.get(f"/api/v1/attachments/{attachment_id}/url")

    assert response.status_code == 401


async def test_the_private_bucket_is_never_reachable_without_a_signature(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    tmp_path: Path,
) -> None:
    """§7.3: 'The bucket is private. Never make it public.' LocalStorage's signed_url is a
    file:// URI, not an HTTP one -- there is no bare object URL to test against a live
    server for, so this proves the same property the way this backend can: the returned
    URL only resolves via the filesystem path this test controls, never as an anonymous
    HTTP request."""
    from app.api.deps import get_storage
    from app.main import create_app
    from app.services.storage import LocalStorage

    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    app = create_app()
    app.dependency_overrides[get_storage] = lambda: LocalStorage(root=tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        attachment_id = await _upload(client, auth_headers(attendant), shift_id=shift)
        response = await client.get(
            f"/api/v1/attachments/{attachment_id}/url", headers=auth_headers(attendant)
        )

    url = response.json()["url"]
    assert url.startswith("file://")
    assert Path(url.removeprefix("file://")).is_file()


# --- the linked-to-my-shift branch, uploaded by somebody else ----------------------
#
# P10 Step 0. `test_an_attendant_can_read_a_receipt_linked_to_their_own_shift` above
# uploads *as the attendant*, so `may_read` returns True at the `uploaded_by` branch and
# never reaches either join. It proves the uploader rule, not the linked-to-my-shift rule.
#
# The two tests below are the case §7.3's docstring actually describes -- "a manager
# entering the day on their behalf would have locked them out of their own paperwork" --
# and they are what makes both joins in `may_read` reachable. The credit-sale one was
# marked done on the Phase 9 checklist and never written; nothing in the suite hit the
# signed-url route with a credit sale at all.


async def test_an_attendant_reads_an_expense_receipt_a_manager_uploaded_for_them(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_expense: Callable[..., UUID],
    auth_headers,
) -> None:
    """The day is typed in after the fact (§4.7), often by somebody senior.

    The attendant did not upload this file, so only the expense join can let them in.
    """
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=date(2026, 7, 21), sequence=1)
    attachment_id = await _upload(client, auth_headers(manager), shift_id=shift)
    make_expense(shift, attachment_id=UUID(attachment_id))

    response = await client.get(
        f"/api/v1/attachments/{attachment_id}/url", headers=auth_headers(attendant)
    )

    assert response.status_code == 200
    assert response.json()["url"]


async def test_an_attendant_reads_a_credit_sale_receipt_a_manager_uploaded_for_them(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    make_credit_customer: Callable[..., UUID],
    make_credit_sale: Callable[..., UUID],
    auth_headers,
) -> None:
    """§7.3, and the whole reason Phase 9 widened `may_read` past expenses.

    Without the credit-sale join an attendant cannot open the udhaar slip for a sale on
    their own shift unless they personally uploaded it -- and `credit_sales.attachment_id`
    is NOT NULL, so every udhaar sale has one.
    """
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=date(2026, 7, 22), sequence=1)
    attachment_id = await _upload(client, auth_headers(manager), shift_id=shift)
    customer = make_credit_customer()
    make_credit_sale(shift, customer, UUID(attachment_id))

    response = await client.get(
        f"/api/v1/attachments/{attachment_id}/url", headers=auth_headers(attendant)
    )

    assert response.status_code == 200
    assert response.json()["url"]
