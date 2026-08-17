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

import jwt
from pydantic import BaseModel, ValidationError

from app.core.errors import AppError

# Supabase stamps this on access tokens issued to signed-in users. Verifying it stops a
# token minted for a different purpose (a password-reset link, say) from being replayed
# here as though it were a login session.
EXPECTED_AUDIENCE = "authenticated"

# Tolerance for clock skew between Supabase's servers and ours, in seconds. Without any
# leeway, a few seconds of drift rejects tokens that are legitimately still valid.
CLOCK_SKEW_LEEWAY_SECONDS = 10

_ALGORITHMS = ["HS256"]


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


def decode_access_token(
    token: str, secret: str, issuer: str | None = None
) -> TokenClaims:
    """Verify a Supabase access token and return its claims.

    Raises AppError(401) on any failure. There is no partial trust: if this raises, no
    caller should read anything out of the token.

    `issuer` is optional because SUPABASE_URL is not configured in local development. When
    it is supplied the `iss` claim is checked, which keeps a token minted by a *different*
    Supabase project (dev vs. prod) from being accepted here. The config validator makes it
    mandatory in production.
    """
    try:
        payload = jwt.decode(
            token,
            secret,
            # An explicit allowlist, never None and never read from the token's own header.
            # This is what defeats the classic "alg: none" forgery, where an attacker
            # strips the signature and declares the token unsigned.
            algorithms=_ALGORITHMS,
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
