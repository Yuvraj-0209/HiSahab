"""app/services/supabase_auth.py: the AuthBackend protocol, both implementations.

`SupabaseAuth` is exercised against `httpx.MockTransport` -- no real network call is ever
made, matching `tests/test_storage.py` and the rest of this suite's fully-offline posture.
`LocalAuth` is exercised directly, since it is only a dictionary.

The branch that matters most here is the 4xx split. GoTrue answers *both* "that email is
taken" and "that password is too short" with 422, and this codebase has to turn one into a
409 the admin can act on (§13.26 -- the detail names `provision_user.py`) and the other into
a plain validation failure. The distinction lives in the response body, so a test that only
checked status codes would pass while every duplicate was misreported.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest

from app.core.errors import AppError
from app.services.supabase_auth import (
    LocalAuth,
    SupabaseAuth,
    build_auth,
)

USER_ID = UUID("11111111-2222-3333-4444-555555555555")


def _client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://project.supabase.co/auth/v1",
        transport=httpx.MockTransport(handler),
    )


def _auth(handler) -> SupabaseAuth:
    return SupabaseAuth(
        base_url="https://project.supabase.co",
        service_key="k",
        client=_client(handler),
    )


# --- SupabaseAuth.create_user --------------------------------------------------


def test_create_user_posts_to_the_admin_endpoint_and_confirms_the_email() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"id": str(USER_ID)})

    result = _auth(handler).create_user(email="r@example.com", password="hunter22")

    assert result == USER_ID
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/auth/v1/admin/users")
    # email_confirm is what lets the person sign in immediately, without SMTP being
    # configured in the Supabase project. Asserted because dropping it would produce an
    # account that exists and cannot log in -- a failure nobody would attribute to here.
    assert '"email_confirm": true' in seen["body"] or '"email_confirm":true' in seen["body"]


def test_create_user_carries_the_bearer_and_apikey_headers() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["apikey"] = request.headers["apikey"]
        return httpx.Response(200, json={"id": str(USER_ID)})

    _auth(handler).create_user(email="r@example.com", password="hunter22")

    assert seen["auth"] == "Bearer k"
    assert seen["apikey"] == "k"


def test_create_user_reads_the_id_from_a_wrapped_response() -> None:
    """Some GoTrue versions answer {"user": {...}} rather than the user at the top level."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"user": {"id": str(USER_ID)}})

    assert _auth(handler).create_user(email="r@example.com", password="hunter22") == USER_ID


def test_a_success_with_no_id_is_a_502_not_a_crash() -> None:
    """A 2xx carrying no id is the dependency misbehaving, not the caller's fault."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    with pytest.raises(AppError) as excinfo:
        _auth(handler).create_user(email="r@example.com", password="hunter22")

    assert excinfo.value.status_code == 502
    assert excinfo.value.code == "AUTH_PROVIDER_UNAVAILABLE"


@pytest.mark.parametrize(
    "message",
    [
        "A user with this email address has already been registered",
        "user already exists",
        "Email address already registered by another user",
    ],
)
def test_a_duplicate_email_is_409_and_names_the_repair(message: str) -> None:
    """§13.26: this is the one create with no local pre-check, so the provider's refusal is
    the only signal -- and an admin who hits it has a real account they cannot attach."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"msg": message})

    with pytest.raises(AppError) as excinfo:
        _auth(handler).create_user(email="r@example.com", password="hunter22")

    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "AUTH_USER_EXISTS"
    assert "provision_user" in excinfo.value.detail


def test_a_rejected_password_is_422_and_is_not_confused_with_a_duplicate() -> None:
    """The same upstream status as the test above. Only the body separates them."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"msg": "Password should be at least 6 characters"}
        )

    with pytest.raises(AppError) as excinfo:
        _auth(handler).create_user(email="r@example.com", password="short")

    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "AUTH_USER_REJECTED"


def test_the_duplicate_match_is_case_insensitive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"msg": "ALREADY BEEN REGISTERED"})

    with pytest.raises(AppError) as excinfo:
        _auth(handler).create_user(email="r@example.com", password="hunter22")

    assert excinfo.value.code == "AUTH_USER_EXISTS"


def test_a_provider_5xx_is_502_and_never_a_duplicate() -> None:
    """A 500 whose body happens to mention "already exists" must not become a 409 -- the
    5xx check runs first, deliberately, because a server error is not a business outcome."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"msg": "user already exists"})

    with pytest.raises(AppError) as excinfo:
        _auth(handler).create_user(email="r@example.com", password="hunter22")

    assert excinfo.value.status_code == 502
    assert excinfo.value.code == "AUTH_PROVIDER_UNAVAILABLE"
    assert "500" in excinfo.value.detail


def test_a_transport_failure_on_create_is_502() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(AppError) as excinfo:
        _auth(handler).create_user(email="r@example.com", password="hunter22")

    assert excinfo.value.status_code == 502
    assert excinfo.value.code == "AUTH_PROVIDER_UNAVAILABLE"


# --- SupabaseAuth.delete_user --------------------------------------------------


def test_delete_user_calls_delete_on_the_admin_endpoint() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200)

    _auth(handler).delete_user(user_id=USER_ID)

    assert seen["method"] == "DELETE"
    assert seen["url"].endswith(f"/auth/v1/admin/users/{USER_ID}")
    assert seen["auth"] == "Bearer k"


def test_deleting_an_account_that_is_already_gone_is_success() -> None:
    """§13.25's compensation only ever runs to undo a create seconds old. An account that
    has already vanished is the outcome wanted, so a 404 must not raise -- the same rule
    `storage.delete` follows for an object a prior sweep already removed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    _auth(handler).delete_user(user_id=USER_ID)


def test_a_failed_delete_is_502() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(AppError) as excinfo:
        _auth(handler).delete_user(user_id=USER_ID)

    assert excinfo.value.status_code == 502


def test_a_transport_failure_on_delete_is_502() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(AppError) as excinfo:
        _auth(handler).delete_user(user_id=USER_ID)

    assert excinfo.value.status_code == 502


# --- LocalAuth -----------------------------------------------------------------


def test_local_auth_mints_a_distinct_id_per_email() -> None:
    auth = LocalAuth()

    first = auth.create_user(email="a@example.com", password="hunter22")
    second = auth.create_user(email="b@example.com", password="hunter22")

    assert first != second


def test_local_auth_refuses_a_duplicate_with_the_same_error_as_supabase() -> None:
    """The double has to reproduce the *behaviour* the endpoint branches on, not merely
    return an id -- otherwise every offline test of the duplicate path proves nothing."""
    auth = LocalAuth()
    auth.create_user(email="a@example.com", password="hunter22")

    with pytest.raises(AppError) as excinfo:
        auth.create_user(email="a@example.com", password="hunter22")

    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "AUTH_USER_EXISTS"


def test_local_auth_treats_an_email_case_and_space_insensitively() -> None:
    auth = LocalAuth()
    auth.create_user(email="a@example.com", password="hunter22")

    with pytest.raises(AppError):
        auth.create_user(email="  A@Example.COM  ", password="hunter22")


def test_local_auth_delete_frees_the_email_again() -> None:
    """Which is what makes §13.25's compensation observable in an offline test: after the
    rollback the admin can retry the same address, exactly as they could in production."""
    auth = LocalAuth()
    user_id = auth.create_user(email="a@example.com", password="hunter22")

    auth.delete_user(user_id=user_id)

    assert auth.create_user(email="a@example.com", password="hunter22") != user_id


def test_local_auth_delete_of_an_unknown_id_is_silent() -> None:
    LocalAuth().delete_user(user_id=uuid4())


# --- build_auth ----------------------------------------------------------------


def test_build_auth_picks_supabase_when_both_settings_are_present() -> None:
    from types import SimpleNamespace

    settings = SimpleNamespace(
        SUPABASE_URL="https://project.supabase.co", SUPABASE_SERVICE_KEY="k"
    )
    assert isinstance(build_auth(settings), SupabaseAuth)


def test_build_auth_falls_back_to_local_when_unconfigured() -> None:
    from types import SimpleNamespace

    settings = SimpleNamespace(SUPABASE_URL=None, SUPABASE_SERVICE_KEY=None)
    assert isinstance(build_auth(settings), LocalAuth)


def test_build_auth_is_cached_across_calls_with_the_same_settings() -> None:
    from types import SimpleNamespace

    settings = SimpleNamespace(SUPABASE_URL=None, SUPABASE_SERVICE_KEY=None)
    assert build_auth(settings) is build_auth(settings)
