"""app/services/attachments.py: the one path an HTTP-level test cannot reach.

`create()`'s M4 case -- a commit failure *after* the storage upload already succeeded --
needs the database to fail at exactly that instant, which no amount of HTTP-level setup can
provoke on demand. Exercised directly against the service function with a stubbed session
and a real (in-memory) storage backend instead.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.core.uploads import ValidatedUpload
from app.services import attachments as attachment_service


class _SucceedingStorage:
    def __init__(self) -> None:
        self.uploaded: list[tuple[str, str]] = []

    def upload(self, *, bucket, path, data, content_type) -> None:
        self.uploaded.append((bucket, path))

    def signed_url(self, **kwargs):  # pragma: no cover - unused here
        raise NotImplementedError

    def delete(self, **kwargs):  # pragma: no cover - unused here
        raise NotImplementedError


def test_a_commit_failure_after_a_successful_upload_is_logged_as_an_orphan(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M4: the one remaining window in create()'s ordering. The object really did land in
    storage; nothing in the database will ever point at it unless someone reads this log
    line and reclaims it by hand."""
    storage = _SucceedingStorage()
    db = MagicMock()
    db.commit.side_effect = RuntimeError("database went away")

    validated = ValidatedUpload(
        mime_type="image/jpeg",
        extension="jpg",
        size_bytes=5,
        checksum_sha256="a" * 64,
    )

    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError):
            attachment_service.create(
                db,
                storage=storage,
                bucket="receipts",
                outlet_id=uuid4(),
                business_date=date(2026, 8, 3),
                uploaded_by=uuid4(),
                original_filename="r.jpg",
                validated=validated,
                data=b"hello",
            )

    assert storage.uploaded, "the upload must have been attempted before commit"
    assert any(
        record.message == "orphan_storage_object" for record in caplog.records
    )
    orphan_record = next(
        r for r in caplog.records if r.message == "orphan_storage_object"
    )
    assert orphan_record.bucket == "receipts"
    assert orphan_record.storage_path == storage.uploaded[0][1]
    # db.rollback() must NOT have been called on this path -- the transaction is already
    # dead from the failed commit, and calling rollback again is not what M4 asks for; the
    # row-flush stays uncommitted regardless once commit() itself has failed.
    db.rollback.assert_not_called()
