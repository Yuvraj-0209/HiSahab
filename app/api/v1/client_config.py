"""The two things a browser needs before it can render anything (CLAUDE.md §2, §16).

Phase 12. Both routes are reads, both return configuration this server already holds, and
between them they exist to enforce one rule: **no config value is copied into JavaScript.**

## Why these are endpoints rather than constants in a `.js` file

§6.7 and §6.11 both say, of the expense thresholds, that *"changing it must not require a
deploy"*. A client that hardcodes ₹5,000 in order to warn a user before the server refuses
their expense has quietly made that false -- raise the threshold and the warning keeps firing
at the old figure, so the UI and the rule disagree and the UI is the one the user believes.
§3 rule 4 has the same shape for the timezone: display conversion happens in the frontend, but
the *zone* is `TZ_DISPLAY`, server configuration, and a second copy in a JS constant is a
second source of truth waiting to drift.

## Why there are two routes and not one

The split is the authentication boundary, and it is the whole design:

* **`/auth-config` is unauthenticated**, because a login screen cannot authenticate. It is the
  chicken-and-egg case -- you cannot require a token to learn how to get a token.
* **`/client-config` sits at the attendant floor**, because thresholds and the outlet name are
  operational detail with no reason to be readable by the open internet.

Folding them into one unauthenticated route would leak the second set; folding them into one
authenticated route would make logging in impossible. So: two.

## What is deliberately absent

`SUPABASE_SERVICE_KEY`, `SUPABASE_JWT_SECRET` and `DATABASE_URL`. The first is the dangerous
one, because it sits two lines from `SUPABASE_ANON_KEY` in `Settings` and is the same shape --
a long opaque string from the same dashboard -- while granting full database access. The anon
key is *designed* to ship in a browser and the service key must never leave the server.

`tests/test_client_config.py` asserts their absence **by name** rather than by checking the
response's key count, because the failure mode is somebody adding a field to a response model
years from now, and a count assertion would simply be updated to match.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import Actor, require_role
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.roles import Role

logger = logging.getLogger(__name__)

router = APIRouter(tags=["configuration"])


class AuthConfigResponse(BaseModel):
    """Everything a browser needs to reach Supabase Auth, and nothing else.

    Both fields are public by design. The anon key is meant to be embedded in client
    applications -- that is what distinguishes it from the service key -- and Supabase's own
    row-level security, not the secrecy of this string, is what protects the database.
    """

    supabase_url: str
    supabase_anon_key: str


class ClientConfigResponse(BaseModel):
    """Server-held configuration the UI must not duplicate.

    Money is `Decimal`, serialised as a string like every other money field in this API
    (§3 rule 1). A client that parses these to a float to compare against an amount has
    reintroduced the bug the rule exists to prevent, one language further out (§14).
    """

    # §3 rule 4: the frontend does the display conversion, but the zone comes from here.
    tz_display: str
    # §6.7 -- "a manager should look at this".
    expense_review_threshold: Decimal
    # §6.11 -- "this needs paper proof". A DIFFERENT dial, deliberately (§16).
    expense_receipt_threshold: Decimal
    # §13.23 -- above this, a day's cash variance is flagged. Here for the same reason the two
    # above are: the reports screen marks a row "over threshold" and must mark exactly the days
    # the server marks. A copy hardcoded in JavaScript would keep flagging at the old figure
    # the moment this one moved, and the screen is the version the manager believes.
    variance_alert_threshold: Decimal
    # §7.2's ceiling, so the client can refuse an oversized file before spending a minute of
    # rural bandwidth uploading it and receiving a 413. The server still enforces it -- this
    # is a courtesy, not a control (§8: hiding a button is UX, not a control).
    max_upload_bytes: int
    outlet_name: str


@router.get("/auth-config", response_model=AuthConfigResponse)
def read_auth_config(
    settings: Settings = Depends(get_settings),
) -> AuthConfigResponse:
    """How to reach Supabase Auth. **Unauthenticated, by necessity.**

    This is the second route in the application with no role dependency, after `/health`, and
    `tests/test_routes.py` keeps a list of those precisely so adding one is a deliberate act
    rather than an oversight. The justification is in the module docstring: requiring a token
    to discover how to obtain a token cannot work.

    Returns 503 rather than an empty string when the values are absent. A frontend handed
    `{"supabase_url": ""}` would fail somewhere deep inside a fetch to a malformed URL; being
    told plainly that the server is not configured for authentication is a better answer, and
    it matches how `/health` reports a database it cannot reach. In production this state is
    unreachable -- `Settings` refuses to boot without both (§16).
    """
    if not (settings.SUPABASE_URL or "").strip() or not (
        settings.SUPABASE_ANON_KEY or ""
    ).strip():
        logger.warning("auth-config requested but Supabase auth is not configured")
        raise AppError(
            status_code=503,
            code="AUTH_NOT_CONFIGURED",
            detail=(
                "This server is not configured for Supabase authentication. Set "
                "SUPABASE_URL and SUPABASE_ANON_KEY."
            ),
        )

    return AuthConfigResponse(
        supabase_url=settings.SUPABASE_URL.rstrip("/"),
        supabase_anon_key=settings.SUPABASE_ANON_KEY,
    )


@router.get("/client-config", response_model=ClientConfigResponse)
def read_client_config(
    actor: Actor = Depends(require_role(Role.attendant)),
    settings: Settings = Depends(get_settings),
) -> ClientConfigResponse:
    """Configuration the UI reads instead of hardcoding.

    Attendant floor: every role needs the timezone to render a single timestamp, and an
    attendant filing an expense needs the receipt threshold to be warned before the server
    refuses them. Nothing here is sensitive, but nothing here is public either.

    `actor` is unused beyond the role check, which is the point -- this returns the same
    answer to everybody at the outlet. It is a parameter rather than a bare dependency so the
    role floor is visible in the signature.
    """
    return ClientConfigResponse(
        tz_display=settings.TZ_DISPLAY,
        expense_review_threshold=settings.EXPENSE_REVIEW_THRESHOLD,
        expense_receipt_threshold=settings.EXPENSE_RECEIPT_THRESHOLD,
        variance_alert_threshold=settings.VARIANCE_ALERT_THRESHOLD,
        max_upload_bytes=settings.MAX_UPLOAD_BYTES,
        outlet_name=settings.DEFAULT_OUTLET_NAME,
    )
