"""Supabase access-token verification (CLAUDE.md §8).

Deliberately free of FastAPI and database concerns: this module takes a string and a
secret and returns claims, so the whole verification path -- including every failure mode
-- can be unit-tested without an HTTP client or a Postgres instance. The FastAPI wiring
lives in app/api/deps.py.

It does import AppError from app.core.errors so that a bad token produces the project's
standard error envelope (§3 rule 10) rather than a second, parallel error vocabulary that
deps.py would then have to translate. That import is the one concession; there are no
FastAPI *concepts* here -- no Request, no Depends, no HTTPException.

## How this works, briefly

A JWT is `header.payload.signature`, base64url-encoded and dot-joined. The payload is
**not** encrypted -- anyone holding the token can read it. What the signature proves is
that the payload has not been altered since Supabase issued it, because only a holder of
the shared secret can produce a matching signature.

That is why verification needs no network call to Supabase: it is pure local computation
over the token and the secret. It is also why SUPABASE_JWT_SECRET is server-side only --
whoever holds it can mint a valid token for any user.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import logging

import jwt
from pydantic import BaseModel, ValidationError

from app.core.errors import AppError
from app.core.jwks import ASYMMETRIC_ALGORITHMS, signing_key_for

logger = logging.getLogger(__name__)

# Supabase stamps this on access tokens issued to signed-in users. Verifying it stops a
# token minted for a different purpose (a password-reset link, say) from being replayed
# here as though it were a login session.
EXPECTED_AUDIENCE = "authenticated"

# Tolerance for clock skew between Supabase's servers and ours, in seconds. Without any
# leeway, a few seconds of drift rejects tokens that are legitimately still valid.
CLOCK_SKEW_LEEWAY_SECONDS = 10

# The symmetric algorithm Supabase used to sign with, and still the one the test suite
# mints (tests/conftest.py::make_token), which is what keeps the suite offline.
_SYMMETRIC_ALGORITHM = "HS256"


class TokenClaims(BaseModel):
    """The verified claims this application actually uses.

    `sub` is typed as UUID rather than str on purpose: it is about to be used as a primary
    key lookup, and a `sub` that is not a UUID means the token is not one of ours. Better
    to reject it here as a 401 than to hand a malformed value to the database and get a
    500.
    """

    sub: UUID
    exp: datetime
    aud: str
    iss: str | None = None


def _resolve_key_and_algorithm(
    token: str, secret: str | None, jwks_uri: str | None
) -> tuple[object, list[str]]:
    """Choose the key material to verify `token` with, from the algorithm it declares.

    ## Reading the header is safe here; trusting it would not be

    The header is unauthenticated -- anyone can write anything in it -- so the classic
    forgery is `{"alg": "none"}`, and the classic *confusion* attack is an attacker taking a
    server that expects RS256, signing a token with HS256 using the RSA **public key** as the
    HMAC secret, and having the server verify it against that same public key.

    Neither works against the dispatch below, and the reason is that the two branches use
    **entirely different key material**:

      HS256          -> SUPABASE_JWT_SECRET, which an attacker does not have
      ES256 / RS256  -> a public key from the JWKS, only ever used to verify

    An attacker who declares HS256 is verified against a secret they do not know. One who
    declares ES256 is verified against a public key they cannot sign with. And an algorithm
    outside the allowlist is refused before any key is fetched at all -- `alg: none` never
    reaches PyJWT.

    Each call also passes a **single-element** algorithm list to `jwt.decode`, not the union,
    so the algorithm actually used is pinned to the branch that chose the key.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        raise AppError(
            status_code=401,
            code="INVALID_TOKEN",
            detail="The access token is not valid.",
        ) from None

    algorithm = header.get("alg")

    if algorithm == _SYMMETRIC_ALGORITHM:
        if not (secret or "").strip():
            # A deployment with no secret must refuse, never fall through to "allow".
            logger.error("an HS256 token arrived but SUPABASE_JWT_SECRET is not configured")
            raise AppError(
                status_code=500,
                code="AUTH_NOT_CONFIGURED",
                detail="Authentication is not configured on this server.",
            )
        return secret, [_SYMMETRIC_ALGORITHM]

    if algorithm in ASYMMETRIC_ALGORITHMS:
        if not (jwks_uri or "").strip():
            logger.error(
                "an asymmetric token arrived but SUPABASE_URL is not configured, "
                "so there is no JWKS to verify it against",
            )
            raise AppError(
                status_code=500,
                code="AUTH_NOT_CONFIGURED",
                detail="Authentication is not configured on this server.",
            )
        return signing_key_for(token, jwks_uri), [algorithm]

    # Anything else, including the absent-or-"none" case.
    raise AppError(
        status_code=401,
        code="INVALID_TOKEN",
        detail="The access token is not valid.",
    )


def decode_access_token(
    token: str,
    secret: str | None = None,
    issuer: str | None = None,
    jwks_uri: str | None = None,
) -> TokenClaims:
    """Verify a Supabase access token and return its claims.

    Handles both signing schemes Supabase has used. `secret` covers HS256; `jwks_uri` covers
    the ES256/RS256 keys a project created today issues. Which one applies is decided by
    `_resolve_key_and_algorithm` from the token's own header -- see there for why that is
    safe.

    Only the asymmetric path touches the network, and only on a cache miss, so an HS256
    token (every token the test suite mints) is still pure local computation.

    Raises AppError(401) on any failure. There is no partial trust: if this raises, no
    caller should read anything out of the token.

    `issuer` is optional because SUPABASE_URL is not configured in local development. When
    it is supplied the `iss` claim is checked, which keeps a token minted by a *different*
    Supabase project (dev vs. prod) from being accepted here. The config validator makes it
    mandatory in production.
    """
    key, algorithms = _resolve_key_and_algorithm(token, secret, jwks_uri)

    try:
        payload = jwt.decode(
            token,
            key,
            # A single-element allowlist, chosen by the branch that selected the key above --
            # never None, and never the union of what we support. This is what defeats both
            # the "alg: none" forgery and the algorithm-confusion attack; see
            # _resolve_key_and_algorithm.
            algorithms=algorithms,
            audience=EXPECTED_AUDIENCE,
            # PyJWT skips the issuer check when this is None.
            issuer=issuer,
            leeway=CLOCK_SKEW_LEEWAY_SECONDS,
            # Fail closed on a *missing* claim. Without this, a token that simply omits
            # `exp` would sail through as one that never expires.
            options={"require": ["exp", "sub", "aud"]},
        )
    except jwt.ExpiredSignatureError:
        # Checked before InvalidTokenError because it is a subclass of it. Reported with a
        # distinct code so the frontend can refresh silently rather than bouncing the user
        # to a login screen.
        raise AppError(
            status_code=401,
            code="TOKEN_EXPIRED",
            detail="The access token has expired.",
        ) from None
    except jwt.InvalidTokenError:
        # Everything else -- bad signature, wrong audience, wrong issuer, malformed,
        # unsupported algorithm, missing required claim. Reported vaguely on purpose:
        # telling a caller precisely why their forged token failed only helps them
        # calibrate the next attempt.
        raise AppError(
            status_code=401,
            code="INVALID_TOKEN",
            detail="The access token is not valid.",
        ) from None

    try:
        return TokenClaims.model_validate(payload)
    except ValidationError:
        # Signature was good, so this really was issued by whoever holds the secret -- but
        # the payload is not shaped like a Supabase user token (e.g. `sub` is not a UUID).
        raise AppError(
            status_code=401,
            code="INVALID_TOKEN",
            detail="The access token is not valid.",
        ) from None
