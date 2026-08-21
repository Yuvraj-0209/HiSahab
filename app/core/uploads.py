"""Content validation for uploaded receipts (CLAUDE.md §7.2).

Pure functions over `bytes` -- no I/O, no FastAPI, no database, no storage client. The
router (`app/api/v1/uploads.py`) reads the file into memory in bounded chunks (so a
`Content-Length` lie cannot exhaust memory before this module ever runs) and handles
everything downstream of a successful validation. Keeping this module I/O-free is what
makes it the most heavily tested part of the phase: every case here is testable with no
server, no database and no bucket.

## Content sniffing, not the extension or the client's `Content-Type` (§7.2)

Renaming `evil.exe` to `receipt.jpg` takes two seconds. The three-way split here -- sniff
the magic bytes, ignore what the client claims, and give HEIC its own message -- is what a
fake extension and a fake `Content-Type` both fail to get past.

## Why HEIC gets a bespoke path instead of falling into "unsupported"

iPhones shoot HEIC by default. §7.2 predicts this will be the #1 support complaint, and a
generic "invalid file type" error would waste hours of a non-technical user's time guessing
what "the file type" even means. Detecting HEIC specifically and naming the "Most
Compatible" camera setting turns an opaque rejection into a fix the user can make in ten
seconds without contacting anyone.

**HEIC is checked only after JPEG and PNG have both failed to match**, never before --
otherwise a genuine JPEG that happened to share some early byte pattern could misroute into
the wrong error. In practice this never collides (JPEG and PNG magic bytes are
unambiguous), but the ordering is the safe direction regardless.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.core.errors import AppError

# Mirrors ck_attachments_mime_type_allowed (migration 0011) exactly. The two must never
# drift -- if they did, the API could accept something the database refuses, and a create
# would fail with an opaque 500 instead of a 422 naming the field.
ALLOWED_MIME_TYPES: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
}

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# ISOBMFF ("ftyp" box) major brands that Apple's Camera app and HEIF encoders actually
# produce. Not exhaustive of every HEIF brand that has ever been defined -- exhaustive of
# the ones a phone camera in the wild produces, which is what a support ticket looks like.
_HEIC_BRANDS = frozenset(
    {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1"}
)


@dataclass(frozen=True)
class ValidatedUpload:
    """What survives §7.2's gate: a real content type, a size, and a checksum.

    Nothing here has touched storage or the database -- both come after this, in
    `app/services/attachments.py`.
    """

    mime_type: str
    extension: str
    size_bytes: int
    checksum_sha256: str


def _sniff_mime_type(data: bytes) -> str | None:
    """The magic-byte check. `None` means "not a type we recognise" -- HEIC included,
    deliberately, so the caller can give it its own message instead of the generic one."""
    if data.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    if data.startswith(_PNG_MAGIC):
        return "image/png"
    return None


def _is_heic(data: bytes) -> bool:
    """ISOBMFF's `ftyp` box sits at byte 4; the four-byte brand that follows says what kind
    of ISOBMFF file this is. Neither JPEG nor PNG has this structure, so this only runs
    after both have already failed to match."""
    if len(data) < 12 or data[4:8] != b"ftyp":
        return False
    return data[8:12] in _HEIC_BRANDS


def validate(data: bytes, *, max_bytes: int) -> ValidatedUpload:
    """§7.2's gate, in the order the spec lists it: size, then content, then checksum.

    Raises `AppError` for every refusal case -- `EMPTY_FILE`, `FILE_TOO_LARGE` (413),
    `HEIC_NOT_SUPPORTED`, `UNSUPPORTED_FILE_TYPE` (422) -- so the router calls this once and
    lets the exception handler produce the §3 rule 10 envelope. Nothing is written anywhere
    by this function; a caller that gets an `AppError` has left no trace of the attempt.
    """
    size = len(data)

    if size == 0:
        raise AppError(
            status_code=422,
            code="EMPTY_FILE",
            detail="The uploaded file is empty.",
        )

    if size > max_bytes:
        raise AppError(
            status_code=413,
            code="FILE_TOO_LARGE",
            detail=(
                f"The file is {size} bytes, over the {max_bytes}-byte limit "
                "(MAX_UPLOAD_BYTES)."
            ),
        )

    mime_type = _sniff_mime_type(data)

    if mime_type is None:
        if _is_heic(data):
            raise AppError(
                status_code=422,
                code="HEIC_NOT_SUPPORTED",
                detail=(
                    "This looks like an HEIC/HEIF photo. iPhones shoot this format by "
                    "default. Please change Settings > Camera > Formats to 'Most "
                    "Compatible' and take the photo again, or upload a JPEG/PNG instead."
                ),
            )
        raise AppError(
            status_code=422,
            code="UNSUPPORTED_FILE_TYPE",
            detail="Only JPEG and PNG images are accepted.",
        )

    return ValidatedUpload(
        mime_type=mime_type,
        extension=ALLOWED_MIME_TYPES[mime_type],
        size_bytes=size,
        checksum_sha256=hashlib.sha256(data).hexdigest(),
    )
