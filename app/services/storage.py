"""Object storage, behind a three-method protocol (CLAUDE.md §7.1, §13.4, §16).

§7.1 chooses proxy-through-FastAPI over presigned direct upload for V1 -- simpler,
synchronous, easier to reason about for a developer learning backend basics. That choice is
what makes this module possible: the server always holds the bytes before anything is
written, so `upload`/`signed_url`/`delete` is the entire surface any caller ever needs.

## Why a hand-rolled protocol instead of the Supabase SDK

Three REST calls behind a small interface is more boring and more testable than a client
library, and §14 says ask before adding a dependency. `SupabaseStorage` below is the whole
implementation -- there is nothing else to import.

## Two implementations, selected once by `build_storage`

`SupabaseStorage` is what runs in production. `LocalStorage` is what runs when
`SUPABASE_URL`/`SUPABASE_SERVICE_KEY` are unset (local dev without a Supabase project) and
is what the test suite injects via a dependency override -- `tests/conftest.py` is fully
offline and must never reach a real Supabase project, the same posture `SUPABASE_JWT_SECRET`
already takes for auth.

## Placement

`app/services/__init__.py` requires everything here to be "callable from a router, a test,
or a management command without touching FastAPI" -- so this module exposes a plain
`build_storage(settings) -> StorageBackend` factory with no FastAPI import anywhere in it.
The FastAPI `get_storage()` dependency that wraps it lives in `app/api/deps.py`, and
`app/jobs/cleanup_attachments.py` calls `build_storage` directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from tempfile import gettempdir
from typing import Protocol

import httpx

from app.core.config import Settings
from app.core.errors import AppError


class StorageBackend(Protocol):
    """What every caller in this codebase needs from object storage, and nothing more."""

    def upload(
        self, *, bucket: str, path: str, data: bytes, content_type: str
    ) -> None: ...

    def signed_url(self, *, bucket: str, path: str, ttl_seconds: int) -> str: ...

    def delete(self, *, bucket: str, paths: Sequence[str]) -> None: ...


def _unavailable(action: str, status_code: int | None = None) -> AppError:
    """A dependency being down is not a bug -- the client should know it can retry, which a
    bare 500 does not communicate. One helper so the three call sites cannot drift on the
    code or the status."""
    suffix = f" (storage returned {status_code})" if status_code is not None else ""
    return AppError(
        status_code=502,
        code="STORAGE_UNAVAILABLE",
        detail=f"Could not {action} to object storage right now{suffix}. Please retry.",
    )


class SupabaseStorage:
    """Three REST calls against the Supabase Storage API. No SDK -- see the module
    docstring for why.

    A `httpx.Client` may be injected for testing (built against `httpx.MockTransport`, so
    no real network call is ever made in the suite); production leaves it unset and gets a
    real client scoped to the Storage base URL.
    """

    def __init__(
        self,
        *,
        base_url: str,
        service_key: str,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._client = client or httpx.Client(
            base_url=f"{base_url.rstrip('/')}/storage/v1", timeout=timeout
        )
        # Applied explicitly per request rather than as client-level defaults, so the same
        # header-setting code runs whether or not a client was injected for testing --
        # baking them into a client built inside this constructor would make them
        # untestable the moment a caller (a test) supplies its own client.
        self._auth_headers = {
            "Authorization": f"Bearer {service_key}",
            "apikey": service_key,
        }

    def upload(self, *, bucket: str, path: str, data: bytes, content_type: str) -> None:
        try:
            response = self._client.post(
                f"/object/{bucket}/{path}",
                content=data,
                headers={**self._auth_headers, "Content-Type": content_type},
            )
        except httpx.HTTPError as exc:
            raise _unavailable("upload") from exc
        if response.status_code >= 400:
            raise _unavailable("upload", response.status_code)

    def signed_url(self, *, bucket: str, path: str, ttl_seconds: int) -> str:
        try:
            response = self._client.post(
                f"/object/sign/{bucket}/{path}",
                json={"expiresIn": ttl_seconds},
                headers=self._auth_headers,
            )
        except httpx.HTTPError as exc:
            raise _unavailable("create a signed URL for") from exc
        if response.status_code >= 400:
            raise _unavailable("create a signed URL for", response.status_code)

        # Supabase returns a path fragment ("/object/sign/{bucket}/{path}?token=..."), not
        # a full URL -- it has to be joined onto this client's own base URL to be usable.
        # `httpx.URL.__str__` always ends in "/", so the naive f-string version produces
        # ".../v1//object/..." -- caught by test_signed_url_joins_the_returned_fragment_
        # onto_the_base_url, which is why this strips it explicitly rather than trusting
        # string concatenation.
        signed_path = response.json()["signedURL"]
        return f"{str(self._client.base_url).rstrip('/')}{signed_path}"

    def delete(self, *, bucket: str, paths: Sequence[str]) -> None:
        for path in paths:
            try:
                response = self._client.delete(
                    f"/object/{bucket}/{path}", headers=self._auth_headers
                )
            except httpx.HTTPError as exc:
                raise _unavailable("delete from") from exc
            # §7.4, M10: a 404 means the object is already gone -- success, not failure.
            # The row-delete that follows this call can fail independently of the object
            # delete, and the next sweep run must not wedge on a row whose object a prior
            # run already removed.
            if response.status_code >= 400 and response.status_code != 404:
                raise _unavailable("delete from", response.status_code)


class LocalStorage:
    """A directory on disk. Runs when Supabase Storage is not configured, and is what the
    test suite injects so it never touches a real bucket.

    **Signed URLs here are not real signed URLs.** There is no way to serve a private file
    over HTTP without a running static file server, and adding one for a dev-only backend
    would be exactly the kind of scaffolding §11 warns against -- this project's FastAPI
    app never serves anything but JSON (§2). `signed_url` returns a `file://` URI instead:
    enough to prove the plumbing end-to-end locally, and never reachable in production,
    where `SupabaseStorage` is what actually runs.
    """

    def __init__(self, *, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, bucket: str, path: str) -> Path:
        return self._root / bucket / path

    def upload(self, *, bucket: str, path: str, data: bytes, content_type: str) -> None:
        target = self._resolve(bucket, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def signed_url(self, *, bucket: str, path: str, ttl_seconds: int) -> str:
        return self._resolve(bucket, path).as_uri()

    def delete(self, *, bucket: str, paths: Sequence[str]) -> None:
        for path in paths:
            # missing_ok=True is the local-filesystem shape of M10's "404 is success".
            self._resolve(bucket, path).unlink(missing_ok=True)


@lru_cache
def _cached_storage(
    supabase_url: str | None, service_key: str | None
) -> StorageBackend:
    """Cached the same way `get_settings()` is -- one long-lived `SupabaseStorage` (and
    its one `httpx.Client`) per process, not a new TCP-connecting client on every request.
    Keyed on the two values that decide *which* backend, not on the whole `Settings`
    object, which is not guaranteed hashable.
    """
    if supabase_url and service_key:
        return SupabaseStorage(base_url=supabase_url, service_key=service_key)
    # Dev-only fallback. A fixed, machine-local temp directory -- not committed, not
    # shared, gone on reboot. Never reachable in production: the prod config validator
    # (app/core/config.py) already requires SUPABASE_URL/SUPABASE_JWT_SECRET when
    # ENV=prod, and Storage config is expected to travel alongside them operationally.
    return LocalStorage(root=Path(gettempdir()) / "hisahab-local-storage")


def build_storage(settings: Settings) -> StorageBackend:
    """The selection point (D4/D6): Supabase when configured, local disk otherwise."""
    return _cached_storage(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
