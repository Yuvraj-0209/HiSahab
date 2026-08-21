"""app/jobs/cleanup_attachments.py (CLAUDE.md §7.4, §10).

Calls main() directly with an argv list and an injected LocalStorage, rather than
shelling out -- see app/jobs/test_provision_user.py's own docstring for why.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from app.jobs.cleanup_attachments import main
from app.services.storage import LocalStorage

pytestmark = pytest.mark.anyio


def _make_attachment(
    engine: Engine,
    storage: LocalStorage,
    *,
    outlet_id,
    uploaded_by: UUID,
    created_at: datetime,
    linked_at: datetime | None,
    write_object: bool = True,
) -> tuple:
    from uuid import uuid4

    attachment_id = uuid4()
    storage_path = f"{outlet_id}/2026/08/03/{attachment_id}.jpg"
    if write_object:
        storage.upload(
            bucket="receipts", path=storage_path, data=b"x", content_type="image/jpeg"
        )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO attachments (id, outlet_id, bucket, storage_path, "
                "original_filename, mime_type, size_bytes, checksum_sha256, "
                "uploaded_by, linked_at, created_at) VALUES (:id, :outlet, 'receipts', "
                ":path, 'r.jpg', 'image/jpeg', 1, :checksum, :uploader, :linked, :created)"
            ).bindparams(
                id=attachment_id,
                outlet=outlet_id,
                path=storage_path,
                checksum="a" * 64,
                uploader=uploaded_by,
                linked=linked_at,
                created=created_at,
            )
        )
    return attachment_id, storage_path


@pytest.fixture
def local_storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(root=tmp_path)


def _exists(engine: Engine, attachment_id) -> bool:
    with engine.connect() as connection:
        return (
            connection.execute(
                text("SELECT 1 FROM attachments WHERE id = :id").bindparams(
                    id=attachment_id
                )
            ).first()
            is not None
        )


async def test_an_old_unlinked_attachment_is_deleted_row_and_object(
    make_user: Callable[..., UUID],
    engine: Engine,
    local_storage: LocalStorage,
) -> None:
    from app.core.config import get_settings

    attendant = make_user("attendant")
    old = datetime.now(tz=timezone.utc) - timedelta(hours=25)
    attachment_id, storage_path = _make_attachment(
        engine, local_storage,
        outlet_id=get_settings().DEFAULT_OUTLET_ID, uploaded_by=attendant,
        created_at=old, linked_at=None,
    )

    exit_code = main([], storage=local_storage)

    assert exit_code == 0
    assert not _exists(engine, attachment_id)
    assert not local_storage._resolve("receipts", storage_path).exists()


async def test_a_recent_unlinked_attachment_is_untouched(
    make_user: Callable[..., UUID],
    engine: Engine,
    local_storage: LocalStorage,
) -> None:
    from app.core.config import get_settings

    attendant = make_user("attendant")
    recent = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    attachment_id, storage_path = _make_attachment(
        engine, local_storage,
        outlet_id=get_settings().DEFAULT_OUTLET_ID, uploaded_by=attendant,
        created_at=recent, linked_at=None,
    )

    try:
        main([], storage=local_storage)

        assert _exists(engine, attachment_id)
        assert local_storage._resolve("receipts", storage_path).exists()
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM attachments WHERE id = :id").bindparams(
                    id=attachment_id
                )
            )


async def test_a_linked_attachment_is_never_deleted_regardless_of_age(
    make_user: Callable[..., UUID],
    engine: Engine,
    local_storage: LocalStorage,
) -> None:
    """The one case that would destroy evidence if it were ever wrong."""
    from app.core.config import get_settings

    attendant = make_user("attendant")
    ancient = datetime.now(tz=timezone.utc) - timedelta(days=400)
    attachment_id, storage_path = _make_attachment(
        engine, local_storage,
        outlet_id=get_settings().DEFAULT_OUTLET_ID, uploaded_by=attendant,
        created_at=ancient, linked_at=ancient,
    )

    try:
        main([], storage=local_storage)

        assert _exists(engine, attachment_id)
        assert local_storage._resolve("receipts", storage_path).exists()
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM attachments WHERE id = :id").bindparams(
                    id=attachment_id
                )
            )


async def test_dry_run_reports_a_count_and_deletes_nothing(
    make_user: Callable[..., UUID],
    engine: Engine,
    local_storage: LocalStorage,
    capsys: pytest.CaptureFixture,
) -> None:
    from app.core.config import get_settings

    attendant = make_user("attendant")
    old = datetime.now(tz=timezone.utc) - timedelta(hours=25)
    attachment_id, storage_path = _make_attachment(
        engine, local_storage,
        outlet_id=get_settings().DEFAULT_OUTLET_ID, uploaded_by=attendant,
        created_at=old, linked_at=None,
    )

    try:
        exit_code = main(["--dry-run"], storage=local_storage)

        assert exit_code == 0
        assert _exists(engine, attachment_id)
        assert local_storage._resolve("receipts", storage_path).exists()
        out = capsys.readouterr().out
        assert "would delete 1" in out
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM attachments WHERE id = :id").bindparams(
                    id=attachment_id
                )
            )


async def test_a_missing_storage_object_does_not_wedge_the_run(
    make_user: Callable[..., UUID],
    engine: Engine,
    local_storage: LocalStorage,
) -> None:
    """M10: LocalStorage.delete() is missing_ok=True, the local-filesystem shape of
    treating a 404 as success. A row whose object a previous run (or anything else)
    already removed must still be cleaned up, not left stuck forever."""
    from app.core.config import get_settings

    attendant = make_user("attendant")
    old = datetime.now(tz=timezone.utc) - timedelta(hours=25)
    attachment_id, storage_path = _make_attachment(
        engine, local_storage,
        outlet_id=get_settings().DEFAULT_OUTLET_ID, uploaded_by=attendant,
        created_at=old, linked_at=None, write_object=False,
    )
    assert not local_storage._resolve("receipts", storage_path).exists()

    exit_code = main([], storage=local_storage)

    assert exit_code == 0
    assert not _exists(engine, attachment_id)


async def test_running_it_twice_in_a_row_is_a_clean_no_op_the_second_time(
    make_user: Callable[..., UUID],
    engine: Engine,
    local_storage: LocalStorage,
    capsys: pytest.CaptureFixture,
) -> None:
    from app.core.config import get_settings

    attendant = make_user("attendant")
    old = datetime.now(tz=timezone.utc) - timedelta(hours=25)
    _make_attachment(
        engine, local_storage,
        outlet_id=get_settings().DEFAULT_OUTLET_ID, uploaded_by=attendant,
        created_at=old, linked_at=None,
    )

    main([], storage=local_storage)
    capsys.readouterr()  # discard first run's output
    exit_code = main([], storage=local_storage)

    assert exit_code == 0
    assert "deleted 0 unlinked attachment(s) (of 0 eligible)" in capsys.readouterr().out


async def test_a_storage_failure_is_skipped_loudly_and_the_run_continues(
    make_user: Callable[..., UUID],
    engine: Engine,
    local_storage: LocalStorage,
    capsys: pytest.CaptureFixture,
) -> None:
    """A genuine outage (not a missing object -- that's the 404-as-success test above)
    must not crash the whole sweep, and must not silently vanish either."""
    from app.core.config import get_settings
    from app.core.errors import AppError

    class _FailingStorage:
        def upload(self, **kwargs):  # pragma: no cover - unused here
            raise NotImplementedError

        def signed_url(self, **kwargs):  # pragma: no cover - unused here
            raise NotImplementedError

        def delete(self, **kwargs) -> None:
            raise AppError(502, "STORAGE_UNAVAILABLE", "simulated outage")

    attendant = make_user("attendant")
    old = datetime.now(tz=timezone.utc) - timedelta(hours=25)
    attachment_id, _ = _make_attachment(
        engine, local_storage,
        outlet_id=get_settings().DEFAULT_OUTLET_ID, uploaded_by=attendant,
        created_at=old, linked_at=None,
    )

    try:
        exit_code = main([], storage=_FailingStorage())

        assert exit_code == 0
        assert _exists(engine, attachment_id)  # the row survives for the next run
        err = capsys.readouterr().err
        assert "skip" in err
        assert "simulated outage" in err
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM attachments WHERE id = :id").bindparams(
                    id=attachment_id
                )
            )


async def test_with_no_storage_injected_it_builds_one_from_config(
    make_user: Callable[..., UUID], engine: Engine
) -> None:
    """The `storage=None` default path -- every other test injects a LocalStorage
    directly; this one proves main() still works when it has to build its own, the shape
    it actually runs in via cron."""
    from app.core.config import get_settings

    attendant = make_user("attendant")
    recent = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    # No object written to any real backend -- the row is recent, so nothing should try
    # to delete it, and the real (dev-fallback) storage backend is never touched.
    attachment_id, _ = _make_attachment(
        engine, LocalStorage(root=Path("/tmp/hisahab-unused-in-this-test")),
        outlet_id=get_settings().DEFAULT_OUTLET_ID, uploaded_by=attendant,
        created_at=recent, linked_at=None, write_object=False,
    )

    try:
        exit_code = main(["--dry-run"])

        assert exit_code == 0
        assert _exists(engine, attachment_id)
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM attachments WHERE id = :id").bindparams(
                    id=attachment_id
                )
            )


async def test_the_ttl_is_imported_not_redefined(local_storage: LocalStorage) -> None:
    """M9: the job and services/attachments.py::orphans must never be able to disagree
    about what "expired" means."""
    import app.jobs.cleanup_attachments as job_module
    from app.services.attachments import ORPHAN_TTL

    assert job_module.ORPHAN_TTL is ORPHAN_TTL
