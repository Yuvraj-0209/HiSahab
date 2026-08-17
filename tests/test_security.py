"""JWT verification (CLAUDE.md §8, §9).

No database and no HTTP: this exercises app/core/security.py directly. Tokens are minted
here with a test secret rather than obtained from Supabase, which is not a shortcut -- it
is the only way to produce the failure cases at all. Supabase will not hand you an expired
token, or one signed with the wrong key, on request.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
import pytest

from app.core.errors import AppError
from app.core.security import EXPECTED_AUDIENCE, decode_access_token

# At least 32 bytes: RFC 7518 §3.2 wants an HMAC key no shorter than the hash it feeds,
# and PyJWT warns below that. A real Supabase JWT secret is comfortably longer.
SECRET = "test-secret-not-a-real-key-0123456789"
ISSUER = "http://localhost:54321/auth/v1"


def _token(
    *,
    sub: object | None = None,
    secret: str = SECRET,
    audience: str | None = EXPECTED_AUDIENCE,
    issuer: str | None = ISSUER,
    expires_in: int = 3600,
    algorithm: str = "HS256",
    omit: set[str] | None = None,
) -> str:
    """Mint a token, with every field overridable so each failure mode is reachable."""
    now = datetime.now(tz=timezone.utc)
    payload: dict[str, object] = {
        "sub": str(uuid4()) if sub is None else sub,
        "aud": audience,
        "iss": issuer,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
    }
    for field in omit or set():
        payload.pop(field, None)

    if algorithm == "none":
        # PyJWT refuses to sign with "none" unless the key is empty.
        return jwt.encode(payload, key="", algorithm="none")
    return jwt.encode(payload, secret, algorithm=algorithm)


def test_valid_token_returns_its_subject() -> None:
    subject = uuid4()

    claims = decode_access_token(_token(sub=str(subject)), SECRET, ISSUER)

    assert claims.sub == subject
    assert claims.aud == EXPECTED_AUDIENCE


def test_expired_token_is_rejected_with_its_own_code() -> None:
    """A distinct code so the frontend can refresh instead of forcing a re-login."""
    token = _token(expires_in=-3600)

    with pytest.raises(AppError) as excinfo:
        decode_access_token(token, SECRET, ISSUER)

    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "TOKEN_EXPIRED"


def test_token_expired_within_leeway_is_still_accepted() -> None:
    """Documents the clock-skew tolerance deliberately, so shrinking it fails a test."""
    token = _token(expires_in=-5)

    claims = decode_access_token(token, SECRET, ISSUER)

    assert claims.sub is not None


def test_token_signed_with_the_wrong_secret_is_rejected() -> None:
    """The whole point of the signature: this payload was not issued by us."""
    token = _token(secret="a-different-secret-also-at-least-32-bytes")

    with pytest.raises(AppError) as excinfo:
        decode_access_token(token, SECRET, ISSUER)

    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "INVALID_TOKEN"


def test_unsigned_alg_none_token_is_rejected() -> None:
    """The classic JWT forgery: strip the signature, declare the token unsigned.

    Defeated by passing an explicit `algorithms` allowlist to jwt.decode() rather than
    trusting the algorithm named in the token's own header.
    """
    token = _token(algorithm="none")

    with pytest.raises(AppError) as excinfo:
        decode_access_token(token, SECRET, ISSUER)

    assert excinfo.value.code == "INVALID_TOKEN"


def test_wrong_audience_is_rejected() -> None:
    """Stops a token issued for another purpose being replayed as a login session."""
    token = _token(audience="some-other-audience")

    with pytest.raises(AppError) as excinfo:
        decode_access_token(token, SECRET, ISSUER)

    assert excinfo.value.code == "INVALID_TOKEN"


def test_wrong_issuer_is_rejected_when_an_issuer_is_configured() -> None:
    """Keeps a dev-project token from being accepted by production."""
    token = _token(issuer="https://someone-elses-project.supabase.co/auth/v1")

    with pytest.raises(AppError) as excinfo:
        decode_access_token(token, SECRET, ISSUER)

    assert excinfo.value.code == "INVALID_TOKEN"


def test_issuer_is_not_checked_when_none_is_configured() -> None:
    """SUPABASE_URL is blank in local development, so the check has to be skippable."""
    token = _token(issuer="https://anything-at-all.example/auth/v1")

    claims = decode_access_token(token, SECRET, issuer=None)

    assert claims.sub is not None


def test_garbage_string_is_rejected() -> None:
    with pytest.raises(AppError) as excinfo:
        decode_access_token("this-is-not-a-jwt", SECRET, ISSUER)

    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "INVALID_TOKEN"


def test_token_missing_exp_is_rejected() -> None:
    """Without options={"require": [...]}, a token with no exp would never expire."""
    token = _token(omit={"exp"})

    with pytest.raises(AppError) as excinfo:
        decode_access_token(token, SECRET, ISSUER)

    assert excinfo.value.code == "INVALID_TOKEN"


def test_token_missing_sub_is_rejected() -> None:
    token = _token(omit={"sub"})

    with pytest.raises(AppError) as excinfo:
        decode_access_token(token, SECRET, ISSUER)

    assert excinfo.value.code == "INVALID_TOKEN"


def test_subject_that_is_not_a_uuid_is_rejected() -> None:
    """Signature is valid, but this is not a Supabase user token.

    Rejected here as a 401 rather than passed on to a primary-key lookup that would
    surface as a 500.
    """
    token = _token(sub="not-a-uuid")

    with pytest.raises(AppError) as excinfo:
        decode_access_token(token, SECRET, ISSUER)

    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "INVALID_TOKEN"
