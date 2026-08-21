"""app/services/storage.py: the StorageBackend protocol, both implementations.

`SupabaseStorage` is exercised against `httpx.MockTransport` -- no real network call is
ever made, matching the rest of this suite's fully-offline posture. `LocalStorage` is
exercised against a real `tmp_path`, since it IS the filesystem.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.core.errors import AppError
from app.services.storage import LocalStorage, SupabaseStorage, build_storage


def _client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://project.supabase.co/storage/v1",
        transport=httpx.MockTransport(handler),
    )


# --- SupabaseStorage -----------------------------------------------------------


def test_upload_posts_the_bytes_with_the_declared_content_type() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers["content-type"]
        seen["body"] = request.content
        return httpx.Response(200)

    storage = SupabaseStorage(
        base_url="https://project.supabase.co",
        service_key="k",
        client=_client(handler),
    )
    storage.upload(
        bucket="receipts", path="a/b/c.jpg", data=b"bytes", content_type="image/jpeg"
    )

    assert seen["method"] == "POST"
    assert seen["url"].endswith("/object/receipts/a/b/c.jpg")
    assert seen["content_type"] == "image/jpeg"
    assert seen["body"] == b"bytes"


def test_upload_carries_the_bearer_and_apikey_headers() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["apikey"] = request.headers["apikey"]
        return httpx.Response(200)

    storage = SupabaseStorage(
        base_url="https://project.supabase.co",
        service_key="the-service-key",
        client=_client(handler),
    )
    storage.upload(bucket="receipts", path="x.jpg", data=b"x", content_type="image/jpeg")

    assert seen["auth"] == "Bearer the-service-key"
    assert seen["apikey"] == "the-service-key"


def test_upload_failure_status_raises_storage_unavailable() -> None:
    storage = SupabaseStorage(
        base_url="https://project.supabase.co",
        service_key="k",
        client=_client(lambda request: httpx.Response(500)),
    )

    with pytest.raises(AppError) as caught:
        storage.upload(bucket="receipts", path="x.jpg", data=b"x", content_type="image/jpeg")

    assert caught.value.status_code == 502
    assert caught.value.code == "STORAGE_UNAVAILABLE"


def test_upload_network_failure_raises_storage_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    storage = SupabaseStorage(
        base_url="https://project.supabase.co", service_key="k", client=_client(handler)
    )

    with pytest.raises(AppError) as caught:
        storage.upload(bucket="receipts", path="x.jpg", data=b"x", content_type="image/jpeg")

    assert caught.value.code == "STORAGE_UNAVAILABLE"


def test_signed_url_joins_the_returned_fragment_onto_the_base_url() -> None:
    """Supabase returns a path fragment, not a full URL -- this is the one piece of parsing
    logic in the whole module, and the one most likely to silently break against a real
    API response shape."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url).endswith("/object/sign/receipts/a/b.jpg")
        return httpx.Response(
            200, json={"signedURL": "/object/sign/receipts/a/b.jpg?token=abc123"}
        )

    storage = SupabaseStorage(
        base_url="https://project.supabase.co", service_key="k", client=_client(handler)
    )
    url = storage.signed_url(bucket="receipts", path="a/b.jpg", ttl_seconds=300)

    assert url == (
        "https://project.supabase.co/storage/v1/object/sign/receipts/a/b.jpg?token=abc123"
    )


def test_signed_url_sends_the_requested_ttl() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return httpx.Response(200, json={"signedURL": "/x?token=y"})

    storage = SupabaseStorage(
        base_url="https://project.supabase.co", service_key="k", client=_client(handler)
    )
    storage.signed_url(bucket="receipts", path="a.jpg", ttl_seconds=300)

    assert b'"expiresIn":300' in seen["body"]


def test_signed_url_failure_raises_storage_unavailable() -> None:
    storage = SupabaseStorage(
        base_url="https://project.supabase.co",
        service_key="k",
        client=_client(lambda request: httpx.Response(404)),
    )

    with pytest.raises(AppError) as caught:
        storage.signed_url(bucket="receipts", path="gone.jpg", ttl_seconds=300)

    assert caught.value.code == "STORAGE_UNAVAILABLE"


def test_signed_url_network_failure_raises_storage_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    storage = SupabaseStorage(
        base_url="https://project.supabase.co", service_key="k", client=_client(handler)
    )

    with pytest.raises(AppError) as caught:
        storage.signed_url(bucket="receipts", path="a.jpg", ttl_seconds=300)

    assert caught.value.code == "STORAGE_UNAVAILABLE"


def test_delete_of_an_existing_object_succeeds() -> None:
    storage = SupabaseStorage(
        base_url="https://project.supabase.co",
        service_key="k",
        client=_client(lambda request: httpx.Response(200)),
    )
    storage.delete(bucket="receipts", paths=["a.jpg"])  # must not raise


def test_delete_treats_a_404_as_success() -> None:
    """M10: an object already gone must not wedge the §7.4 sweep. The next run has to be
    able to finish deleting the *row* even if a previous run already removed the object."""
    storage = SupabaseStorage(
        base_url="https://project.supabase.co",
        service_key="k",
        client=_client(lambda request: httpx.Response(404)),
    )
    storage.delete(bucket="receipts", paths=["already-gone.jpg"])  # must not raise


def test_delete_of_several_paths_issues_one_request_per_path() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    storage = SupabaseStorage(
        base_url="https://project.supabase.co", service_key="k", client=_client(handler)
    )
    storage.delete(bucket="receipts", paths=["a.jpg", "b.jpg", "c.jpg"])

    assert len(seen) == 3
    assert seen[0].endswith("/object/receipts/a.jpg")
    assert seen[2].endswith("/object/receipts/c.jpg")


def test_delete_a_non_404_failure_raises_storage_unavailable() -> None:
    storage = SupabaseStorage(
        base_url="https://project.supabase.co",
        service_key="k",
        client=_client(lambda request: httpx.Response(500)),
    )

    with pytest.raises(AppError) as caught:
        storage.delete(bucket="receipts", paths=["a.jpg"])

    assert caught.value.code == "STORAGE_UNAVAILABLE"


def test_delete_network_failure_raises_storage_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    storage = SupabaseStorage(
        base_url="https://project.supabase.co", service_key="k", client=_client(handler)
    )

    with pytest.raises(AppError):
        storage.delete(bucket="receipts", paths=["a.jpg"])


# --- LocalStorage ---------------------------------------------------------------


def test_local_storage_round_trips_bytes(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path)
    storage.upload(bucket="receipts", path="a/b/c.jpg", data=b"hello", content_type="image/jpeg")

    written = tmp_path / "receipts" / "a" / "b" / "c.jpg"
    assert written.read_bytes() == b"hello"


def test_local_storage_signed_url_is_a_file_uri(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path)
    storage.upload(bucket="receipts", path="a.jpg", data=b"x", content_type="image/jpeg")

    url = storage.signed_url(bucket="receipts", path="a.jpg", ttl_seconds=300)

    assert url.startswith("file://")
    assert url.endswith("a.jpg")


def test_local_storage_delete_removes_the_file(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path)
    storage.upload(bucket="receipts", path="a.jpg", data=b"x", content_type="image/jpeg")

    storage.delete(bucket="receipts", paths=["a.jpg"])

    assert not (tmp_path / "receipts" / "a.jpg").exists()


def test_local_storage_delete_of_a_missing_file_does_not_raise(tmp_path: Path) -> None:
    """The local-filesystem shape of M10: 404-as-success, expressed as missing_ok=True."""
    storage = LocalStorage(root=tmp_path)
    storage.delete(bucket="receipts", paths=["never-existed.jpg"])  # must not raise


def test_local_storage_creates_its_root_directory(tmp_path: Path) -> None:
    root = tmp_path / "does" / "not" / "exist" / "yet"
    LocalStorage(root=root)
    assert root.is_dir()


# --- build_storage selection -----------------------------------------------------


def test_build_storage_picks_supabase_when_both_settings_are_present() -> None:
    from types import SimpleNamespace

    settings = SimpleNamespace(
        SUPABASE_URL="https://project.supabase.co", SUPABASE_SERVICE_KEY="k"
    )
    assert isinstance(build_storage(settings), SupabaseStorage)


def test_build_storage_falls_back_to_local_when_unconfigured() -> None:
    from types import SimpleNamespace

    settings = SimpleNamespace(SUPABASE_URL=None, SUPABASE_SERVICE_KEY=None)
    assert isinstance(build_storage(settings), LocalStorage)


def test_build_storage_is_cached_across_calls_with_the_same_settings() -> None:
    from types import SimpleNamespace

    settings = SimpleNamespace(SUPABASE_URL=None, SUPABASE_SERVICE_KEY=None)
    assert build_storage(settings) is build_storage(settings)
