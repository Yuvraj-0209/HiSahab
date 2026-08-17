"""The authenticated caller's own identity and role.

Why this endpoint exists, given that CLAUDE.md §11 does not list it and forbids scaffolding
ahead: it is not scaffolding for a later phase, it is the minimum surface that makes *this*
phase verifiable. Without one protected route, `require_role` can only be called as a plain
function, which never exercises the wiring that actually matters -- bearer extraction, the
dependency chain resolving in order, and the 401/403 envelopes travelling through the
installed exception handlers.

It also earns its keep in Phase 12: the frontend needs the caller's role to decide what to
render, and §8 is explicit that hiding a button is UX, not a control -- so the role has to
come from the server that enforces it.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import Actor, require_role
from app.core.roles import Role

router = APIRouter(tags=["identity"])


class MeResponse(BaseModel):
    """Note what is absent: no email, and nothing about other users.

    Email lives in Supabase `auth.users`, which this application deliberately does not
    mirror (§5.1) -- one source of truth for a credential.
    """

    id: UUID
    full_name: str
    phone: str | None
    outlet_id: UUID
    role: Role


@router.get("/me", response_model=MeResponse)
def read_me(actor: Actor = Depends(require_role(Role.attendant))) -> MeResponse:
    """Return the caller's profile and their role at the current outlet.

    Guarded at the lowest role floor, so any active member of the outlet may call it. That
    also makes it proof that membership resolution works end to end, not just token
    verification: a caller with a valid token but no membership gets a 403 here.
    """
    return MeResponse(
        id=actor.user.id,
        full_name=actor.user.full_name,
        phone=actor.user.phone,
        outlet_id=actor.outlet_id,
        role=actor.role,
    )
