"""POST /api/v1/uploads/receipt (CLAUDE.md §7.1, §7.2, §10).

Content-sniffing and size cases are already fully covered in tests/test_uploads_core.py
against the pure `validate()` function -- this file covers what only the real endpoint can:
authorisation, storage-path shape, and what actually lands in the database.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.anyio

DAY = date(2026, 7, 9)

_REAL_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300"
    "0302020302020303030304030304050805050404050a070706"
    "080c0a0c0c0b0a0b0b0d0e12100d0e110e0b0b1016101113141"
    "51516150c0f171d17141d1114141400ffc9000b080001000101"
    "0100ffcc0006001005f0ffda0008010100003f00d2cf20ffd9"
)
_PDF_LIKE = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + b"\x00" * 32
_HEIC_LIKE = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00" + b"\x00" * 16


def _upload(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    shift_id: UUID,
    data: bytes,
    filename: str = "receipt.jpg",
    content_type: str = "image/jpeg",
):
    return client.post(
        "/api/v1/uploads/receipt",
        data={"shift_id": str(shift_id)},
        files={"file": (filename, data, content_type)},
        headers=headers,
    )


def _attachment_row(engine: Engine, attachment_id: str) -> dict | None:
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT * FROM attachments WHERE id = CAST(:id AS uuid)").bindparams(
                id=attachment_id
            )
        ).mappings().first()
    return dict(row) if row else None


# --- happy path ------------------------------------------------------------------


async def test_a_valid_jpeg_is_accepted_and_returns_an_attachment_id(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _upload(
        client, auth_headers(attendant), shift_id=shift, data=_REAL_JPEG
    )

    assert response.status_code == 201
    attachment_id = response.json()["attachment_id"]
    row = _attachment_row(engine, attachment_id)
    assert row is not None
    assert row["mime_type"] == "image/jpeg"
    assert row["linked_at"] is None


async def test_the_storage_path_uses_the_shifts_business_date_not_today(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """§7.2, §4.7: the day is typed in after the fact, so "today" would file a receipt
    under a date the register never mentions. DAY here is deliberately not today."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _upload(
        client, auth_headers(attendant), shift_id=shift, data=_REAL_JPEG
    )

    row = _attachment_row(engine, response.json()["attachment_id"])
    assert row["storage_path"].split("/")[1:4] == ["2026", "07", "09"]


async def test_the_bucket_name_never_appears_inside_storage_path(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _upload(
        client, auth_headers(attendant), shift_id=shift, data=_REAL_JPEG
    )

    row = _attachment_row(engine, response.json()["attachment_id"])
    assert row["bucket"] == "receipts"
    assert "receipts" not in row["storage_path"]


@pytest.mark.parametrize("filename", ["../../etc/passwd.jpg", "my receipt (1).jpg"])
async def test_the_client_supplied_filename_appears_nowhere_in_the_storage_path(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    filename: str,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _upload(
        client, auth_headers(attendant), shift_id=shift, data=_REAL_JPEG, filename=filename
    )

    assert response.status_code == 201
    row = _attachment_row(engine, response.json()["attachment_id"])
    assert "etc" not in row["storage_path"]
    assert "passwd" not in row["storage_path"]
    assert " " not in row["storage_path"]
    assert "(" not in row["storage_path"]
    # Kept only as the display label.
    assert row["original_filename"] == filename


async def test_the_checksum_matches_an_independent_sha256_of_the_bytes(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    import hashlib

    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _upload(
        client, auth_headers(attendant), shift_id=shift, data=_REAL_JPEG
    )

    row = _attachment_row(engine, response.json()["attachment_id"])
    assert row["checksum_sha256"] == hashlib.sha256(_REAL_JPEG).hexdigest()


async def test_a_jpeg_uploaded_with_a_png_extension_is_still_stored_as_a_jpeg(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    """Sniffing wins in both directions -- the client's claimed extension and Content-Type
    are both ignored (M2)."""
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _upload(
        client,
        auth_headers(attendant),
        shift_id=shift,
        data=_REAL_JPEG,
        filename="receipt.png",
        content_type="image/png",
    )

    assert response.status_code == 201
    row = _attachment_row(engine, response.json()["attachment_id"])
    assert row["mime_type"] == "image/jpeg"
    assert row["storage_path"].endswith(".jpg")


# --- §10's named cases -----------------------------------------------------------


async def test_oversized_file_is_413_and_nothing_is_written(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    oversized = _REAL_JPEG + b"\x00" * (5_242_880 + 1)

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MAX_UPLOAD_BYTES", "5242880")
        response = await _upload(
            client, auth_headers(attendant), shift_id=shift, data=oversized
        )

    assert response.status_code == 413
    assert response.json()["code"] == "FILE_TOO_LARGE"
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM attachments WHERE outlet_id IS NOT NULL")
        ).scalar_one()
    # No attachment created by THIS test -- other tests may have left rows, so assert on
    # absence of a matching size instead of a global zero count.
    with engine.connect() as connection:
        matching = connection.execute(
            text("SELECT count(*) FROM attachments WHERE size_bytes > 5242880")
        ).scalar_one()
    assert matching == 0


async def test_a_pdf_renamed_to_jpg_is_rejected_by_content_sniffing(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _upload(
        client, auth_headers(attendant), shift_id=shift, data=_PDF_LIKE,
        filename="receipt.jpg", content_type="image/jpeg",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "UNSUPPORTED_FILE_TYPE"


async def test_heic_gets_the_specific_actionable_message(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _upload(
        client, auth_headers(attendant), shift_id=shift, data=_HEIC_LIKE,
        filename="IMG_0001.HEIC", content_type="image/heic",
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "HEIC_NOT_SUPPORTED"
    assert "Most Compatible" in body["detail"]


# --- authorisation (§8) ------------------------------------------------------------


async def test_upload_against_a_closed_shift_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1, status="closed")

    response = await _upload(
        client, auth_headers(attendant), shift_id=shift, data=_REAL_JPEG
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_NOT_OPEN"


async def test_upload_against_a_locked_shift_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1, status="locked")

    response = await _upload(
        client, auth_headers(attendant), shift_id=shift, data=_REAL_JPEG
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SHIFT_LOCKED"


async def test_an_attendant_cannot_upload_against_another_attendants_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    owner = make_user("attendant")
    other = make_user("attendant")
    shift = make_shift(owner, business_date=DAY, sequence=1)

    response = await _upload(
        client, auth_headers(other), shift_id=shift, data=_REAL_JPEG
    )

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_YOUR_SHIFT"


async def test_a_manager_may_upload_against_any_attendants_shift(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    manager = make_user("manager")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _upload(
        client, auth_headers(manager), shift_id=shift, data=_REAL_JPEG
    )

    assert response.status_code == 201


async def test_a_user_with_no_membership_at_the_shifts_outlet_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)
    outsider = make_user("attendant", with_membership=False)

    response = await _upload(
        client, auth_headers(outsider), shift_id=shift, data=_REAL_JPEG
    )

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_A_MEMBER"


async def test_an_inactive_membership_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant", membership_active=False)
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await _upload(
        client, auth_headers(attendant), shift_id=shift, data=_REAL_JPEG
    )

    assert response.status_code == 403
    assert response.json()["code"] == "MEMBERSHIP_INACTIVE"


async def test_a_nonexistent_shift_is_a_404(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    auth_headers,
) -> None:
    attendant = make_user("attendant")

    response = await _upload(
        client, auth_headers(attendant), shift_id=uuid4(), data=_REAL_JPEG
    )

    assert response.status_code == 404
    assert response.json()["code"] == "SHIFT_NOT_FOUND"


async def test_an_unauthenticated_upload_is_refused(
    client: AsyncClient,
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
) -> None:
    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    response = await client.post(
        "/api/v1/uploads/receipt",
        data={"shift_id": str(shift)},
        files={"file": ("r.jpg", _REAL_JPEG, "image/jpeg")},
    )

    assert response.status_code == 401


# --- storage failure (M3) ----------------------------------------------------------


async def test_storage_failure_returns_502_and_leaves_no_attachment_row(
    make_user: Callable[..., UUID],
    make_shift: Callable[..., UUID],
    auth_headers,
    engine: Engine,
    tmp_path: Path,
) -> None:
    """M3: the row is flushed before the storage write is attempted, so a failed upload
    must roll back cleanly -- no attachments row left pointing at an object that was never
    written."""
    from httpx import ASGITransport, AsyncClient as _AsyncClient

    from app.api.deps import get_storage
    from app.core.errors import AppError
    from app.main import create_app

    class _ExplodingStorage:
        def upload(self, **kwargs):
            raise AppError(502, "STORAGE_UNAVAILABLE", "simulated outage")

        def signed_url(self, **kwargs):  # pragma: no cover - unused here
            raise NotImplementedError

        def delete(self, **kwargs):  # pragma: no cover - unused here
            raise NotImplementedError

    attendant = make_user("attendant")
    shift = make_shift(attendant, business_date=DAY, sequence=1)

    app = create_app()
    app.dependency_overrides[get_storage] = lambda: _ExplodingStorage()
    transport = ASGITransport(app=app)

    before = engine.connect().execute(text("SELECT count(*) FROM attachments")).scalar_one()

    async with _AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _upload(
            client, auth_headers(attendant), shift_id=shift, data=_REAL_JPEG
        )

    assert response.status_code == 502
    assert response.json()["code"] == "STORAGE_UNAVAILABLE"

    after = engine.connect().execute(text("SELECT count(*) FROM attachments")).scalar_one()
    assert after == before
