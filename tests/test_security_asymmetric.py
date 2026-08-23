"""ES256 access tokens, and the dispatch that chooses how to verify (CLAUDE.md §8).

Phase 12. Supabase moved new projects from a shared HS256 secret to asymmetric **JWT signing
keys** -- ES256 over P-256 -- so a project created today issues tokens the original
HS256-only verifier rejects outright. This file covers the branch added for that.

## What is actually being defended here

Not "ES256 works" -- that is PyJWT's job. What matters is the **dispatch**: the algorithm is
read from the token's own header, which is unauthenticated, so the wrong design turns a
verifier into a forger's tool. The two attacks are:

* `alg: none` -- strip the signature and declare the token unsigned.
* **algorithm confusion** -- take a server that expects ES256/RS256, sign a token with HS256
  using the *public* key as the HMAC secret, and have the server verify it against that same
  public key.

Both are tested below against real forged tokens, not asserted in a comment. The defence is
that each branch uses different key material and pins a single-element algorithm list, so a
token declaring HS256 is checked against a secret the attacker does not have, and one
declaring ES256 against a key they cannot sign with.

No network. The ES256 keypair is generated in-process and `signing_key_for` is patched, which
is the seam the JWKS fetch sits behind -- so this stays as offline as the rest of the suite.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import base64
import hashlib
import hmac
import json

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.errors import AppError
from app.core.security import EXPECTED_AUDIENCE, decode_access_token

TEST_SECRET = "a-test-secret-of-at-least-32-characters"


# --- helpers ------------------------------------------------------------------


@pytest.fixture
def ec_keypair() -> tuple[object, object]:
    """A P-256 keypair, the shape Supabase's signing keys use."""
    private = ec.generate_private_key(ec.SECP256R1())
    return private, private.public_key()


def _claims(**overrides) -> dict:
    now = datetime.now(tz=timezone.utc)
    return {
        "sub": str(uuid4()),
        "aud": EXPECTED_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(hours=1),
        **overrides,
    }


def _es256(private_key, **overrides) -> str:
    return jwt.encode(_claims(**overrides), private_key, algorithm="ES256")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _hs256_by_hand(claims: dict, key: bytes) -> str:
    """Build an HS256 token without PyJWT, so any key material can be used as the secret.

    Needed because PyJWT refuses to sign with a PEM key -- see the confusion test. Claims
    are JSON-serialised with `default=str` so the datetime objects in `_claims` survive.
    """
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps(claims, default=lambda value: int(value.timestamp())).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = _b64(hmac.new(key, signing_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


# --- the happy path -----------------------------------------------------------


def test_an_es256_token_is_accepted(monkeypatch: pytest.MonkeyPatch, ec_keypair) -> None:
    """The case that made this change necessary: a token from a modern Supabase project."""
    private, public = ec_keypair
    monkeypatch.setattr("app.core.security.signing_key_for", lambda token, uri: public)

    token = _es256(private)
    claims = decode_access_token(token, secret=None, jwks_uri="https://example/jwks.json")

    assert claims.aud == EXPECTED_AUDIENCE


def test_hs256_still_works_alongside_it() -> None:
    """Both schemes are supported, and this is why the whole suite stayed green.

    tests/conftest.py mints HS256 tokens, which is what keeps every other test offline.
    Dropping HS256 would have traded that for nothing.
    """
    token = jwt.encode(_claims(), TEST_SECRET, algorithm="HS256")

    claims = decode_access_token(token, secret=TEST_SECRET)

    assert claims.aud == EXPECTED_AUDIENCE


def test_an_expired_es256_token_reports_token_expired(
    monkeypatch: pytest.MonkeyPatch, ec_keypair
) -> None:
    """The distinct code matters as much here as on the HS256 path.

    app/core/security.py raises TOKEN_EXPIRED separately so the frontend can refresh
    silently instead of bouncing the user to a login screen; that must not be lost for the
    algorithm every real user is now on.
    """
    private, public = ec_keypair
    monkeypatch.setattr("app.core.security.signing_key_for", lambda token, uri: public)

    token = _es256(private, exp=datetime.now(tz=timezone.utc) - timedelta(hours=1))

    with pytest.raises(AppError) as caught:
        decode_access_token(token, secret=None, jwks_uri="https://example/jwks.json")

    assert caught.value.status_code == 401
    assert caught.value.code == "TOKEN_EXPIRED"


# --- the forgeries ------------------------------------------------------------


def test_alg_none_is_refused(ec_keypair) -> None:
    """The classic forgery: strip the signature, declare the token unsigned.

    Refused before any key is fetched, because "none" is in neither branch of the dispatch.
    """
    token = jwt.encode(_claims(), key="", algorithm="none")

    with pytest.raises(AppError) as caught:
        decode_access_token(token, secret=TEST_SECRET, jwks_uri="https://example/jwks.json")

    assert caught.value.status_code == 401
    assert caught.value.code == "INVALID_TOKEN"


def test_algorithm_confusion_is_refused(
    monkeypatch: pytest.MonkeyPatch, ec_keypair
) -> None:
    """The attack the dispatch exists to survive.

    An attacker takes the ES256 **public** key -- which is public, so they have it -- and
    signs a token with HS256 using it as the HMAC secret, hoping the server verifies against
    the same bytes it would use for ES256.

    It fails because the HS256 branch reaches for SUPABASE_JWT_SECRET, never the JWKS. The
    attacker's signature is checked against a secret they do not have.
    """
    private, public = ec_keypair
    monkeypatch.setattr("app.core.security.signing_key_for", lambda token, uri: public)

    public_pem = public.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    # Forged by hand, and that is the point. `jwt.encode` REFUSES to build this -- PyJWT
    # rejects a PEM key as an HMAC secret with InvalidKeyError, which is a defence on the
    # *signing* side. An attacker is not using PyJWT, so testing through it would have
    # proven only that PyJWT declines to help, not that our verifier holds.
    forged = _hs256_by_hand(_claims(), public_pem)

    with pytest.raises(AppError) as caught:
        decode_access_token(forged, secret=TEST_SECRET, jwks_uri="https://example/jwks.json")

    assert caught.value.status_code == 401
    assert caught.value.code == "INVALID_TOKEN"


def test_an_es256_token_signed_by_the_wrong_key_is_refused(
    monkeypatch: pytest.MonkeyPatch, ec_keypair
) -> None:
    """A well-formed token from a different project must not be accepted."""
    _, public = ec_keypair
    attacker_key = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setattr("app.core.security.signing_key_for", lambda token, uri: public)

    token = _es256(attacker_key)

    with pytest.raises(AppError) as caught:
        decode_access_token(token, secret=None, jwks_uri="https://example/jwks.json")

    assert caught.value.code == "INVALID_TOKEN"


@pytest.mark.parametrize("algorithm", ["HS384", "HS512", "PS256"])
def test_an_algorithm_outside_the_allowlist_is_refused(algorithm: str) -> None:
    """Supporting two algorithms must not mean supporting everything PyJWT can parse."""
    # Built by hand: jwt.encode would refuse to produce some of these without a matching
    # key, and the point is that the dispatch refuses them before a key is even chosen.
    header = _b64(json.dumps({"alg": algorithm, "typ": "JWT"}).encode())
    payload = _b64(json.dumps({"sub": str(uuid4())}).encode())
    token = f"{header}.{payload}.signature"

    with pytest.raises(AppError) as caught:
        decode_access_token(token, secret=TEST_SECRET, jwks_uri="https://example/jwks.json")

    assert caught.value.status_code == 401
    assert caught.value.code == "INVALID_TOKEN"


# --- misconfiguration ---------------------------------------------------------


def test_an_hs256_token_with_no_secret_configured_refuses_rather_than_allows() -> None:
    """An unconfigured deployment must refuse traffic, never authenticate everyone.

    500, not 401: this is our problem, not the caller's, and a client retrying with a
    different token would not help.
    """
    token = jwt.encode(_claims(), TEST_SECRET, algorithm="HS256")

    with pytest.raises(AppError) as caught:
        decode_access_token(token, secret=None, jwks_uri="https://example/jwks.json")

    assert caught.value.status_code == 500
    assert caught.value.code == "AUTH_NOT_CONFIGURED"


def test_an_es256_token_with_no_jwks_configured_refuses_rather_than_allows(
    ec_keypair,
) -> None:
    """The mirror image: a JWKS-less deployment cannot verify an asymmetric token."""
    private, _ = ec_keypair
    token = _es256(private)

    with pytest.raises(AppError) as caught:
        decode_access_token(token, secret=TEST_SECRET, jwks_uri=None)

    assert caught.value.status_code == 500
    assert caught.value.code == "AUTH_NOT_CONFIGURED"


def test_an_unreachable_jwks_endpoint_is_a_503_not_a_500(
    monkeypatch: pytest.MonkeyPatch, ec_keypair
) -> None:
    """A dependency being down is not a bug in this application.

    503 tells an operator "the identity provider is unreachable" rather than burying it in a
    generic INTERNAL_ERROR, and it is the one auth failure worth logging loudly -- otherwise
    it presents as every user being unable to sign in, with no explanation anywhere.
    """
    from app.core import jwks as jwks_module

    private, _ = ec_keypair
    token = _es256(private)

    class Unreachable:
        def get_signing_key_from_jwt(self, _token):
            raise jwt.PyJWKClientConnectionError("boom")

    monkeypatch.setattr(jwks_module, "get_jwks_client", lambda uri: Unreachable())

    with pytest.raises(AppError) as caught:
        decode_access_token(token, secret=None, jwks_uri="https://example/jwks.json")

    assert caught.value.status_code == 503
    assert caught.value.code == "AUTH_KEYS_UNAVAILABLE"


def test_an_unknown_kid_is_a_401(monkeypatch: pytest.MonkeyPatch, ec_keypair) -> None:
    """A token naming a key we cannot find is indistinguishable from a forged one."""
    from app.core import jwks as jwks_module

    private, _ = ec_keypair
    token = _es256(private)

    class NoSuchKey:
        def get_signing_key_from_jwt(self, _token):
            raise jwt.PyJWKClientError("no matching kid")

    monkeypatch.setattr(jwks_module, "get_jwks_client", lambda uri: NoSuchKey())

    with pytest.raises(AppError) as caught:
        decode_access_token(token, secret=None, jwks_uri="https://example/jwks.json")

    assert caught.value.status_code == 401
    assert caught.value.code == "INVALID_TOKEN"


def test_the_jwks_client_is_built_once_per_url() -> None:
    """The cache is the point, not an optimisation.

    `PyJWKClient` fetches over the network on a miss. A fresh client per request would mean
    a round trip to Supabase on **every authenticated call**, which on the rural connectivity
    §6.10 is written for would be far more expensive than the verification itself.

    Asserted by identity: the same URL must give back the same object.
    """
    from app.core.jwks import get_jwks_client

    first = get_jwks_client("https://example.supabase.co/auth/v1/.well-known/jwks.json")
    second = get_jwks_client("https://example.supabase.co/auth/v1/.well-known/jwks.json")
    other = get_jwks_client("https://other.supabase.co/auth/v1/.well-known/jwks.json")

    assert first is second
    assert first is not other


def test_settings_derives_the_jwks_uri_from_the_supabase_url() -> None:
    """Derived rather than configured separately, so the two cannot disagree.

    A second environment variable would be one more thing to get wrong in a .env, pointing
    at a different project than the issuer check uses.
    """
    from app.core.config import Settings

    required = {"DATABASE_URL": "postgresql+psycopg://u:p@localhost:5433/db"}

    configured = Settings(
        _env_file=None, SUPABASE_URL="https://project.supabase.co/", **required
    )
    assert (
        configured.supabase_jwks_uri
        == "https://project.supabase.co/auth/v1/.well-known/jwks.json"
    )

    # No URL, no JWKS -- which is what tells decode_access_token it cannot verify an
    # asymmetric token, rather than letting it try and fail obscurely.
    absent = Settings(_env_file=None, SUPABASE_URL=None, **required)
    assert absent.supabase_jwks_uri is None
