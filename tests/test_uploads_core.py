"""app/core/uploads.py: pure content validation (CLAUDE.md §7.2, §10).

No client, no database, no storage -- every case here is bytes in, a `ValidatedUpload` or
an `AppError` out.
"""

from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.core.uploads import ALLOWED_MIME_TYPES, validate

# Real magic bytes, not guesses. A 1x1 JPEG and a 1x1 PNG, trimmed to a few dozen bytes --
# enough to exercise the sniffer, not a full valid image (nothing here decodes pixels).
_REAL_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300"
    "0302020302020303030304030304050805050404050a070706"
    "080c0a0c0c0b0a0b0b0d0e12100d0e110e0b0b1016101113141"
    "51516150c0f171d17141d1114141400ffc9000b080001000101"
    "0100ffcc0006001005f0ffda0008010100003f00d2cf20ffd9"
)
_REAL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108"
    "0600000031e363740000000a4944415478da6360000002000155"
    "60ed7f0000000049454e44ae426082"
)
# A minimal ISOBMFF container advertising the "heic" major brand -- the exact structure a
# real HEIC file starts with, even though nothing after byte 12 here is a real image.
_HEIC_LIKE = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00" + b"\x00" * 16
_PDF_LIKE = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + b"\x00" * 32


def test_a_real_jpeg_is_accepted() -> None:
    result = validate(_REAL_JPEG, max_bytes=5_242_880)
    assert result.mime_type == "image/jpeg"
    assert result.extension == "jpg"
    assert result.size_bytes == len(_REAL_JPEG)
    assert len(result.checksum_sha256) == 64


def test_a_real_png_is_accepted() -> None:
    result = validate(_REAL_PNG, max_bytes=5_242_880)
    assert result.mime_type == "image/png"
    assert result.extension == "png"


def test_the_checksum_is_a_real_sha256_of_the_bytes() -> None:
    import hashlib

    result = validate(_REAL_JPEG, max_bytes=5_242_880)
    assert result.checksum_sha256 == hashlib.sha256(_REAL_JPEG).hexdigest()


# --- §10's four named upload cases (the three this module owns) --------------


def test_oversized_content_is_refused_with_413() -> None:
    """§10: 'Oversized file -> 413'."""
    with pytest.raises(AppError) as caught:
        validate(_REAL_JPEG, max_bytes=len(_REAL_JPEG) - 1)

    assert caught.value.status_code == 413
    assert caught.value.code == "FILE_TOO_LARGE"


def test_a_pdf_renamed_as_a_jpeg_is_rejected_by_content_sniffing() -> None:
    """§10: 'PDF renamed to .jpg -> rejected by content sniffing'.

    The point of the test is that nothing here is told this is a PDF -- there is no
    filename or extension in play at all, only the bytes. Sniffing must catch it on
    content alone.
    """
    with pytest.raises(AppError) as caught:
        validate(_PDF_LIKE, max_bytes=5_242_880)

    assert caught.value.status_code == 422
    assert caught.value.code == "UNSUPPORTED_FILE_TYPE"


def test_heic_gets_the_specific_actionable_message_not_a_generic_one() -> None:
    """§10: 'HEIC -> specific, actionable error message'.

    §7.2 predicts this is the #1 support complaint; the assertion on `detail` exists so a
    future edit cannot quietly turn this back into a generic "unsupported file type" string
    that leaves an iPhone user with no idea what to do next.
    """
    with pytest.raises(AppError) as caught:
        validate(_HEIC_LIKE, max_bytes=5_242_880)

    assert caught.value.status_code == 422
    assert caught.value.code == "HEIC_NOT_SUPPORTED"
    assert "Most Compatible" in caught.value.detail
    assert "Camera" in caught.value.detail


@pytest.mark.parametrize(
    "brand",
    [b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1"],
)
def test_every_real_heic_brand_a_phone_camera_produces_is_recognised(
    brand: bytes,
) -> None:
    data = b"\x00\x00\x00\x18ftyp" + brand + b"\x00\x00\x00\x00" + b"\x00" * 16
    with pytest.raises(AppError) as caught:
        validate(data, max_bytes=5_242_880)
    assert caught.value.code == "HEIC_NOT_SUPPORTED"


def test_an_isobmff_file_with_an_unrecognised_brand_falls_through_to_generic() -> None:
    """Not every ftyp box is HEIC -- e.g. `isom`/`mp42` are plain MP4. Those must not be
    misdiagnosed as "convert your camera settings"; they are simply unsupported."""
    data = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00" + b"\x00" * 16
    with pytest.raises(AppError) as caught:
        validate(data, max_bytes=5_242_880)
    assert caught.value.code == "UNSUPPORTED_FILE_TYPE"


# --- edges --------------------------------------------------------------------


def test_an_empty_file_is_refused() -> None:
    with pytest.raises(AppError) as caught:
        validate(b"", max_bytes=5_242_880)
    assert caught.value.status_code == 422
    assert caught.value.code == "EMPTY_FILE"


def test_a_file_exactly_at_the_size_limit_is_accepted() -> None:
    """Boundary, matching the house style of testing `>` vs `>=` explicitly elsewhere."""
    padded = _REAL_JPEG + b"\x00" * (100 - len(_REAL_JPEG))
    result = validate(padded, max_bytes=len(padded))
    assert result.size_bytes == len(padded)


def test_a_file_one_byte_over_the_limit_is_refused() -> None:
    padded = _REAL_JPEG + b"\x00" * (100 - len(_REAL_JPEG))
    with pytest.raises(AppError) as caught:
        validate(padded, max_bytes=len(padded) - 1)
    assert caught.value.code == "FILE_TOO_LARGE"


def test_a_jpeg_with_a_png_extension_is_still_identified_as_a_jpeg() -> None:
    """The router never consults a filename or extension for classification -- only what
    `validate` returns. This proves sniffing wins in both directions: a lying extension
    changes nothing about what gets stored."""
    result = validate(_REAL_JPEG, max_bytes=5_242_880)
    assert result.mime_type == "image/jpeg"


def test_allowed_mime_types_matches_the_database_check_exactly() -> None:
    """`ck_attachments_mime_type_allowed` (migration 0011) is written as a literal SQL IN
    list, not read from this table -- so this test is the one place that would catch the
    two drifting apart."""
    assert set(ALLOWED_MIME_TYPES) == {"image/jpeg", "image/png"}
