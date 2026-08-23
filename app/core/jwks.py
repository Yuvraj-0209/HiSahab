"""Public-key material for verifying asymmetrically-signed access tokens (CLAUDE.md §8).

## Why this module exists at all

`app/core/security.py` was written when Supabase signed access tokens with **HS256**, a
symmetric algorithm: one shared secret both mints and verifies. `pyproject.toml` still
carried the note "Supabase signs access tokens with HS256, which is symmetric -- so no
cryptography extra is needed."

Supabase has since moved new projects to **asymmetric JWT signing keys** -- ES256 over
NIST P-256 by default. The private key never leaves Supabase; verification uses a public key
published at a well-known URL. A project created today issues `{"alg": "ES256", "kid": ...}`
tokens, and HS256 verification rejects every one of them.

## Why this is an improvement rather than a workaround

Under HS256 this server had to hold a secret that could **mint** a token for any user. Under
ES256 it holds only a public key: it can check a signature and cannot forge one. If this
application's environment leaks, an attacker gains no ability to impersonate a user. That is
a strictly better position, and it is the direction Supabase is deprecating towards.

## Both algorithms stay supported, deliberately

HS256 is not removed. The test suite mints its own HS256 tokens with a known secret
(`tests/conftest.py::make_token`), which is what lets every failure mode in `security.py` be
reached from an HTTP test without a network call or a live Supabase project. Dropping HS256
would trade a fully offline suite for nothing.

`security.py` therefore dispatches on the token's declared algorithm, and each branch uses
**different key material**: HS256 verifies against `SUPABASE_JWT_SECRET`, ES256/RS256 against
the JWKS. That separation is what makes the dispatch safe -- see the note there about the
algorithm-confusion attack.

## Caching

`PyJWKClient` caches fetched keys, so the network call happens once per key rather than once
per request. The client itself is cached per URL by `lru_cache`, because constructing a new
one each time would defeat that. Keys rotate rarely; `lifespan` bounds how long a rotated-out
key stays trusted.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import jwt

from app.core.errors import AppError

logger = logging.getLogger(__name__)

# How long a fetched key set stays cached, in seconds. An hour is short enough that a
# rotation propagates on its own within a working day, and long enough that a burst of
# requests does not hammer Supabase.
JWKS_CACHE_SECONDS = 3600

# Asymmetric algorithms accepted from a JWKS. ES256 is what Supabase issues today; RS256 is
# included because it is the other algorithm Supabase's signing-key UI offers, and a project
# configured that way should work without a code change.
ASYMMETRIC_ALGORITHMS = ("ES256", "RS256")


@lru_cache(maxsize=4)
def get_jwks_client(jwks_uri: str) -> jwt.PyJWKClient:
    """A cached JWKS client for one URL.

    `lru_cache` rather than a module-level singleton so tests can build one against a
    different URL without monkeypatching, and so the cache is keyed by the thing that
    actually distinguishes two clients. maxsize is small on purpose: a deployment talks to
    one Supabase project, and an unbounded cache keyed on a URL is a slow leak if that URL is
    ever attacker-influenced.
    """
    return jwt.PyJWKClient(jwks_uri, cache_keys=True, lifespan=JWKS_CACHE_SECONDS)


def signing_key_for(token: str, jwks_uri: str) -> object:
    """The public key that signed `token`, looked up by its `kid`.

    Raises AppError(401) rather than letting a network or lookup failure surface as a 500.
    A token naming a `kid` we cannot find is indistinguishable, from here, from a forged one
    -- so it is refused with the same vague message as any other bad token, for the reason
    `security.py` gives: telling a caller precisely why their token failed only helps them
    calibrate the next attempt.

    The one case worth logging loudly is a JWKS endpoint that cannot be reached at all. That
    is an operational problem on our side, not a bad client, and it would otherwise present
    as every user being unable to sign in with no explanation anywhere.
    """
    try:
        return get_jwks_client(jwks_uri).get_signing_key_from_jwt(token).key
    except jwt.PyJWKClientConnectionError:
        logger.exception("could not reach the JWKS endpoint", extra={"jwks_uri": jwks_uri})
        raise AppError(
            status_code=503,
            code="AUTH_KEYS_UNAVAILABLE",
            detail="Could not reach the identity provider to verify your session.",
        ) from None
    except jwt.PyJWKClientError:
        # No matching kid, or a malformed key set. Both mean "this token is not one we can
        # verify", which is a 401.
        raise AppError(
            status_code=401,
            code="INVALID_TOKEN",
            detail="The access token is not valid.",
        ) from None
