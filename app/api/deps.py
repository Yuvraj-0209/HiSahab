"""Authentication and authorisation dependencies (CLAUDE.md §8).

## The shape of a permission check

§8: the question is always "does this user hold role R **at the outlet that owns this
row**", never "is this user an admin". So `require_role` takes an outlet, always -- even
though V1 has exactly one outlet and every check currently resolves to DEFAULT_OUTLET_ID.
Retrofitting the outlet argument into every endpoint later would be far worse than
carrying it from the start.

Where the outlet comes from depends on the endpoint's shape:

* **Creating something new** -- nothing exists yet to read an outlet off, so it comes from
  config via `get_default_outlet_id`. That function is the only place DEFAULT_OUTLET_ID is
  read in the whole application.
* **Acting on an existing row** -- the outlet must come from *the row*, via a resolver
  passed to `require_role`. In V1 that will always equal DEFAULT_OUTLET_ID, but wiring the
  check to ask the row means that the day a second outlet exists, this fails loudly rather
  than authorising against the wrong one. The first such resolver is
  `app/api/v1/nozzles.py::resolve_outlet_from_nozzle` (Phase 3 -- a phase earlier than this
  docstring originally predicted, because PATCH /nozzles/{id} needed one). The second is
  `resolve_outlet_from_shift`, below.

## Ownership is a separate axis from role (§8)

`require_role` answers "does this user clear role R here". It cannot answer "is this the
attendant's *own* shift", because that is not a property of the role -- an attendant may
write to a shift they hold and not to one they do not, at the same role. §8 states this
outright, and Phase 4 is where it lands: `require_shift_access` composes the role floor
with the ownership check, and applies ownership **only when the actor's role is
`attendant`**. Managers and admins may act on any shift at their outlet.

## Status codes (§9)

401 means "we do not know who you are" -- a token problem. 403 means "we know who you are
and the answer is no" -- Supabase authenticated the caller, but this application refuses.
An unprovisioned or deactivated user is therefore a 403, not a 401: retrying with a fresh
token would not help.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.roles import Role, satisfies
from app.core.security import decode_access_token
from app.core.shifts import ShiftStatus
from app.db.session import get_db
from app.models.shift import Shift
from app.models.user import OutletMembership, UserProfile

logger = logging.getLogger(__name__)

# auto_error=False so that a missing or malformed Authorization header returns None here
# and we raise our own AppError, keeping the §3 rule 10 envelope. With auto_error=True
# FastAPI raises its own HTTPException with a different body shape and a 403 status.
#
# Verified against FastAPI 0.141.1: with auto_error=False this returns None both when the
# header is absent and when its scheme is not "bearer", so one None check covers both.
bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Actor:
    """Who is acting, at which outlet, in what role -- a passed permission check.

    Returned by `require_role` so an endpoint never has to re-query the membership to find
    out the caller's role (which matters for the admin-only override paths in §6.2 and
    §6.6). Frozen because nothing downstream has any business rewriting who the caller is.
    """

    user: UserProfile
    outlet_id: UUID
    role: Role


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UserProfile:
    """Verify the bearer token and load the caller's profile.

    Identity only -- this deliberately does not look at roles or outlets. Keeping the two
    concerns separate means the role check can be composed per-endpoint while token
    verification stays identical everywhere.
    """
    if credentials is None:
        raise AppError(
            status_code=401,
            code="NOT_AUTHENTICATED",
            detail="An Authorization: Bearer <token> header is required.",
        )

    if not (settings.SUPABASE_JWT_SECRET or "").strip():
        # Misconfiguration, not a client error. Must never fall through to "allow":
        # an unconfigured deployment has to refuse traffic, not authenticate everyone.
        # The prod config validator makes this unreachable in production.
        logger.error("SUPABASE_JWT_SECRET is not configured; refusing to verify tokens")
        raise AppError(
            status_code=500,
            code="AUTH_NOT_CONFIGURED",
            detail="Authentication is not configured on this server.",
        )

    # Raises AppError(401) on any verification failure. No partial trust: nothing below
    # runs unless the signature, expiry, audience and (when configured) issuer all check
    # out. Note this needs no network call to Supabase -- it is local computation over the
    # token and the shared secret.
    claims = decode_access_token(
        credentials.credentials,
        secret=settings.SUPABASE_JWT_SECRET,
        issuer=settings.supabase_issuer,
    )

    user = db.execute(
        select(UserProfile).where(UserProfile.id == claims.sub)
    ).scalar_one_or_none()

    if user is None:
        # Supabase vouches for this identity, but nobody has provisioned it here. Use
        # app/jobs/provision_user.py.
        logger.warning("authenticated user has no profile", extra={"user_id": str(claims.sub)})
        raise AppError(
            status_code=403,
            code="PROFILE_NOT_PROVISIONED",
            detail="This account has not been set up for this application.",
        )

    if not user.is_active:
        raise AppError(
            status_code=403,
            code="PROFILE_INACTIVE",
            detail="This account has been deactivated.",
        )

    return user


def get_default_outlet_id(settings: Settings = Depends(get_settings)) -> UUID:
    """The V1 single-outlet resolver, for endpoints that create something new.

    This is the **only** place DEFAULT_OUTLET_ID is read outside config and the migrations.
    When V2 resolves the outlet from a header, a subdomain or a path parameter, this one
    function changes and no endpoint does.
    """
    return settings.DEFAULT_OUTLET_ID


def require_role(
    minimum: Role,
    outlet: Callable[..., UUID] = get_default_outlet_id,
) -> Callable[..., Actor]:
    """Build a dependency asserting `minimum` role at the resolved outlet.

    Usage:

        # create-shaped: outlet comes from config
        @router.post("/shifts")
        def open_shift(actor: Actor = Depends(require_role(Role.manager))): ...

        # resource-shaped (Phase 4): outlet comes from the row being acted on
        @router.patch("/shifts/{shift_id}/close")
        def close_shift(
            actor: Actor = Depends(require_role(Role.manager, resolve_outlet_from_shift))
        ): ...

    A note on the mechanics, because it is genuinely confusing the first time: this
    function's own parameters (`minimum`, `outlet`) are plain Python, evaluated once when
    the route is defined. Only the inner `dependency`'s parameters are introspected by
    FastAPI on each request. That is why `outlet` arrives as a bare callable and gets
    wrapped in Depends() *inside* -- a dependency cannot take arbitrary runtime arguments,
    so anything variable has to enter through the dependency system itself.
    """

    def dependency(
        outlet_id: UUID = Depends(outlet),
        user: UserProfile = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> Actor:
        membership = db.execute(
            select(OutletMembership).where(
                OutletMembership.user_id == user.id,
                OutletMembership.outlet_id == outlet_id,
            )
        ).scalar_one_or_none()

        if membership is None:
            # A real, active user -- but not at this outlet. In V1 with one outlet this
            # means simply "no role assigned yet".
            raise AppError(
                status_code=403,
                code="NOT_A_MEMBER",
                detail="You do not have access to this outlet.",
            )

        if not membership.is_active:
            raise AppError(
                status_code=403,
                code="MEMBERSHIP_INACTIVE",
                detail="Your access to this outlet has been revoked.",
            )

        # Converted explicitly rather than trusting the driver's string: an unexpected
        # value fails here, loudly, instead of silently comparing unequal to every Role.
        held = Role(membership.role)

        if not satisfies(held, minimum):
            logger.info(
                "insufficient role",
                extra={
                    "user_id": str(user.id),
                    "outlet_id": str(outlet_id),
                    "held": held.value,
                    "required": minimum.value,
                },
            )
            raise AppError(
                status_code=403,
                code="INSUFFICIENT_ROLE",
                detail=f"This action requires the {minimum.value} role.",
            )

        return Actor(user=user, outlet_id=outlet_id, role=held)

    return dependency


def resolve_outlet_from_shift(shift_id: UUID, db: Session = Depends(get_db)) -> UUID:
    """The outlet that owns this shift, for `require_role` to authorise against.

    Mirrors `app/api/v1/nozzles.py::resolve_outlet_from_nozzle`, including the 404: it lives
    in the dependency rather than the handler because this runs first, and without it a
    request against a nonexistent shift would report a permission failure instead of a
    missing row.
    """
    outlet_id = db.execute(
        select(Shift.outlet_id).where(Shift.id == shift_id)
    ).scalar_one_or_none()
    if outlet_id is None:
        raise AppError(
            status_code=404,
            code="SHIFT_NOT_FOUND",
            detail="No shift with that id.",
        )
    return outlet_id


@dataclass(frozen=True)
class ShiftAccess:
    """A passed permission check against one specific shift.

    Carries the loaded `Shift` so the endpoint does not re-query it, in the same spirit as
    `Actor` carrying the role. Frozen for the same reason: nothing downstream has any
    business rewriting which shift was authorised.
    """

    actor: Actor
    shift: Shift


def require_shift_access(
    minimum: Role, *, writable: bool = False
) -> Callable[..., ShiftAccess]:
    """Build a dependency asserting role, ownership and (optionally) writability.

    This is the §8 ownership axis, and Phases 5-10 are expected to depend on it rather than
    re-implementing any part of it:

        @router.post("/shifts/{shift_id}/collections")
        def add_collection(access: ShiftAccess = Depends(require_shift_access(
            Role.attendant, writable=True
        ))): ...

    `writable=True` means "this call is about to change financial state", and enforces §6.9:
    no writes to a shift that is closed or locked. Reads leave it False.

    `SHIFT_NOT_OPEN` and `SHIFT_LOCKED` are separate codes on purpose. One is recoverable by
    an admin (§6.8's reopen) and the other never is, and a client should be able to tell the
    user which without parsing prose.
    """

    role_dependency = require_role(minimum, resolve_outlet_from_shift)

    def dependency(
        shift_id: UUID,
        actor: Actor = Depends(role_dependency),
        db: Session = Depends(get_db),
    ) -> ShiftAccess:
        # Not None: resolve_outlet_from_shift already 404'd inside role_dependency.
        shift = db.get(Shift, shift_id)

        # §8: ownership applies to attendants only. A manager or admin may act on any shift
        # at their outlet, so this is a role-conditional check rather than an unconditional
        # one -- writing it unconditionally would lock managers out of the shifts they are
        # specifically there to close.
        if actor.role is Role.attendant and shift.attendant_id != actor.user.id:
            logger.info(
                "attendant reached for another attendant's shift",
                extra={
                    "user_id": str(actor.user.id),
                    "shift_id": str(shift_id),
                    "shift_attendant_id": str(shift.attendant_id),
                },
            )
            raise AppError(
                status_code=403,
                code="NOT_YOUR_SHIFT",
                detail="This shift belongs to another attendant.",
            )

        if writable:
            status = ShiftStatus(shift.status)
            if status is ShiftStatus.locked:
                raise AppError(
                    status_code=409,
                    code="SHIFT_LOCKED",
                    detail=(
                        "This shift has been locked and can no longer be changed. "
                        "Corrections must be recorded as reversal entries."
                    ),
                )
            if status is ShiftStatus.closed:
                raise AppError(
                    status_code=409,
                    code="SHIFT_NOT_OPEN",
                    detail=(
                        "This shift is closed. An admin must reopen it before it can be "
                        "changed."
                    ),
                )

        return ShiftAccess(actor=actor, shift=shift)

    return dependency
