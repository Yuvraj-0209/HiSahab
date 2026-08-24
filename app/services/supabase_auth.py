"""Identity provisioning, behind a two-method protocol (CLAUDE.md §5.1, §13.25, §13.26).

Supabase Auth owns `auth.users`; this application owns the *profile* and the *membership*
(§5.1). Until Phase 14 the two halves were joined by hand -- create the person in the
Supabase dashboard, copy their UUID, run `app/jobs/provision_user.py`. This module is the
first thing in the codebase that writes the Supabase half itself, so that
`POST /api/v1/users` can do both in one action.

## Why a hand-rolled protocol instead of the Supabase SDK

The same argument `app/services/storage.py` makes, and this module is deliberately its twin
in shape: two REST calls behind a small interface is more boring and more testable than a
client library, and §14 says ask before adding a dependency. `SupabaseAuth` below is the
whole implementation -- there is nothing else to import.

## Why only two methods

`create_user` is what Phase 14 needs. `delete_user` exists **solely** as the compensating
action for a failed database write (§13.25) -- it is not exposed through any endpoint and
must not become one, because §3 rule 6 forbids hard-deleting a person and roughly fifteen
tables hold non-cascading foreign keys to `user_profiles.id`. The only row it may ever
remove is an auth user created seconds earlier whose profile never landed.

## Two implementations, selected once by `build_auth`

`SupabaseAuth` is what runs in production. `LocalAuth` runs when
`SUPABASE_URL`/`SUPABASE_SERVICE_KEY` are unset and is what the test suite injects via a
dependency override -- `tests/conftest.py` is fully offline and must never reach a real
Supabase project.

## Placement

`app/services/__init__.py` requires everything here to be callable without touching
FastAPI, so this module exposes a plain `build_auth(settings) -> AuthBackend` factory. The
`get_auth()` dependency that wraps it lives in `app/api/deps.py`.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Protocol
from uuid import UUID, uuid4

import httpx

from app.core.config import Settings
from app.core.errors import AppError

logger = logging.getLogger(__name__)


class AuthBackend(Protocol):
    """What this codebase needs from an identity provider, and nothing more."""

    def create_user(self, *, email: str, password: str) -> UUID: ...

    def delete_user(self, *, user_id: UUID) -> None: ...


def _unavailable(action: str, status_code: int | None = None) -> AppError:
    """The `STORAGE_UNAVAILABLE` shape, one dependency over: a provider being down is not a
    bug, and the client should know it can retry -- which a bare 500 does not communicate.
    One helper so the call sites cannot drift on the code or the status."""
    suffix = f" (auth returned {status_code})" if status_code is not None else ""
    return AppError(
        status_code=502,
        code="AUTH_PROVIDER_UNAVAILABLE",
        detail=(
            f"Could not {action} with the identity provider right now{suffix}. "
            "Please retry."
        ),
    )


def _user_exists() -> AppError:
    """409, and the detail names the repair.

    This is the one create in the codebase with no local pre-check, because the unique key
    is the email and §5.1 refuses to mirror it -- Supabase is the only authority (§13.26).
    So an admin can hit this with a real account they cannot attach, and the message has to
    tell them how to finish the job rather than merely refusing.
    """
    return AppError(
        status_code=409,
        code="AUTH_USER_EXISTS",
        detail=(
            "That email already has a Supabase account. If they should have access here, "
            "run: python -m app.jobs.provision_user --user-id <their uuid> "
            "--full-name '<name>' --role <role>"
        ),
    )


# Substrings GoTrue uses when refusing a duplicate. Matched case-insensitively against the
# response body, because the wire format carries the distinction and the status code does
# not: a duplicate email and a too-short password are both 422 there, and they are a 409 and
# a 422 here. Deliberately a short list of *substrings* rather than a parsed error code --
# GoTrue's `error_code` field is newer than some self-hosted deployments, and a missing key
# would silently reclassify every duplicate as a validation failure.
_DUPLICATE_MARKERS = ("already been registered", "already exists", "already registered")


class SupabaseAuth:
    """Two REST calls against the Supabase Auth admin API. No SDK -- see the module
    docstring for why.

    A `httpx.Client` may be injected for testing (built against `httpx.MockTransport`, so
    no real network call is ever made in the suite); production leaves it unset and gets a
    real client scoped to the Auth base URL.
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
            base_url=f"{base_url.rstrip('/')}/auth/v1", timeout=timeout
        )
        # Applied explicitly per request rather than as client-level defaults, for the
        # reason app/services/storage.py gives: baking them into a client built inside this
        # constructor would make them untestable the moment a caller supplies its own.
        self._auth_headers = {
            "Authorization": f"Bearer {service_key}",
            "apikey": service_key,
        }

    def create_user(self, *, email: str, password: str) -> UUID:
        try:
            response = self._client.post(
                "/admin/users",
                # `email_confirm: true` so the person can sign in immediately. The
                # alternative is a confirmation email, which needs SMTP configured in the
                # Supabase project -- one more thing that has to be working before an admin
                # can hand a new salesman his login.
                json={"email": email, "password": password, "email_confirm": True},
                headers=self._auth_headers,
            )
        except httpx.HTTPError as exc:
            raise _unavailable("create an account") from exc

        if response.status_code >= 500:
            raise _unavailable("create an account", response.status_code)

        if response.status_code >= 400:
            body = response.text.lower()
            if any(marker in body for marker in _DUPLICATE_MARKERS):
                raise _user_exists()
            # Anything else 4xx is the payload's fault -- overwhelmingly a password below
            # the project's minimum length. 422 rather than 502: retrying unchanged will
            # not help, which is exactly the 4xx/5xx distinction §9 draws.
            raise AppError(
                status_code=422,
                code="AUTH_USER_REJECTED",
                detail=(
                    "The identity provider refused these details "
                    f"(it returned {response.status_code}). The usual cause is a password "
                    "shorter than the project's minimum."
                ),
            )

        # Two shapes in the wild: GoTrue returns the user object at the top level, and some
        # versions wrap it in {"user": {...}}. Handling both is three lines and cheaper than
        # discovering the difference against a live project.
        payload = response.json()
        raw_id = payload.get("id") or payload.get("user", {}).get("id")
        if not raw_id:
            # A 2xx with no id is a contract violation, not a business outcome. 502 keeps it
            # in the "the dependency misbehaved" bucket rather than blaming the caller.
            raise _unavailable("read back the account", response.status_code)
        return UUID(str(raw_id))

    def delete_user(self, *, user_id: UUID) -> None:
        try:
            response = self._client.delete(
                f"/admin/users/{user_id}", headers=self._auth_headers
            )
        except httpx.HTTPError as exc:
            raise _unavailable("remove an account") from exc
        # 404 is success, the same reasoning storage.delete uses: this only ever runs as
        # §13.25's compensation, and an account that is already gone is the outcome wanted.
        if response.status_code >= 400 and response.status_code != 404:
            raise _unavailable("remove an account", response.status_code)


class LocalAuth:
    """An in-memory dictionary. Runs when Supabase Auth is not configured, and is what the
    test suite injects so it never touches a real project.

    **These are not real accounts.** Nothing here can issue a token, so a user created
    through this backend cannot actually sign in -- local development still needs a real
    Supabase project to reach a login screen. What it does provide is the one behaviour the
    endpoint's logic depends on: a stable id, and a refusal on a duplicate email. That is
    enough to exercise every branch of `POST /users` offline, which is the point.
    """

    def __init__(self) -> None:
        self._by_email: dict[str, UUID] = {}

    def create_user(self, *, email: str, password: str) -> UUID:
        key = email.strip().lower()
        if key in self._by_email:
            raise _user_exists()
        user_id = uuid4()
        self._by_email[key] = user_id
        return user_id

    def delete_user(self, *, user_id: UUID) -> None:
        for email, existing in list(self._by_email.items()):
            if existing == user_id:
                del self._by_email[email]


@lru_cache
def _cached_auth(supabase_url: str | None, service_key: str | None) -> AuthBackend:
    """Cached the same way `_cached_storage` is -- one long-lived client per process, not a
    new TCP-connecting one on every request. Keyed on the two values that decide *which*
    backend, not on the whole `Settings` object, which is not guaranteed hashable.
    """
    if supabase_url and service_key:
        return SupabaseAuth(base_url=supabase_url, service_key=service_key)
    # Dev-only fallback, and unreachable in production since Phase 14: the prod config
    # validator (app/core/config.py) now requires SUPABASE_SERVICE_KEY when ENV=prod.
    return LocalAuth()


def build_auth(settings: Settings) -> AuthBackend:
    """The selection point: Supabase when configured, an in-memory double otherwise."""
    return _cached_auth(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
