"""Users -- who may sign in, and what they may do (CLAUDE.md §5.1, §8, §13.25-27).

§8's permission table has said "Manage users, nozzles, customers -- admin only" since Phase
2. Nozzles got a router in Phase 3 and customers in Phase 9; **users never did**, so adding
a manager meant the Supabase dashboard, a copied UUID, and `app/jobs/provision_user.py` from
a shell. That command stays -- it is the bootstrap, and its own docstring explains why the
very first admin cannot come from an admin-only endpoint -- but everybody after the first
belongs here.

## What makes this router different from every other admin CRUD module

*One person, two tables.* Identity is `user_profiles`; the role is `outlet_memberships`,
because §5.1 puts it there so somebody can be a manager at one pump and an attendant at
another. So `POST` writes two rows and `PATCH` may write either, both, or neither -- and
each writes its own audit row, because `audit_logs.record_id` points at a row *in a named
table* and an admin asking "who made Ramesh a manager" must find that answer under
`outlet_memberships`.

*One person, two systems.* Supabase Auth owns the credential. `POST` therefore calls the
identity provider **first** -- `user_profiles.id` must equal `auth.users.id` and only
Supabase can say what that is -- and the two database rows follow. The window between them
is not transactional; see `create_user` below and §13.25.

*No local duplicate pre-check, unlike every other create in this codebase.* The unique key
is the email, §5.1 refuses to mirror it, and so Supabase is its only authority. A duplicate
surfaces as 409 `AUTH_USER_EXISTS` from `app/services/supabase_auth.py`, whose detail names
the repair (§13.26).

*A refusal that no `--force` can override.* §13.27: the last active admin at an outlet
cannot be demoted or deactivated here, because there is no caller left who could undo it.

## What this router deliberately cannot do

Read, change or reset a password; read or change an email; hard-delete anybody (§3 rule 6,
and fifteen non-cascading foreign keys); or write `user_profiles.is_active`, which means
"gone from every outlet" -- a sentence V1 has no way to mean. Retirement is a membership
act. See §12's out-of-scope note on credential management.
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import Actor, get_auth, require_role
from app.core.audit import AuditAction
from app.core.errors import AppError
from app.core.roles import Role
from app.db.session import get_db
from app.models.user import OutletMembership, UserProfile
from app.services import audit
from app.services.supabase_auth import AuthBackend

logger = logging.getLogger(__name__)

router = APIRouter(tags=["identity"])

# The same considered exception to §9's cursor rule that `fuel_types`, `expense_categories`
# and `credit_customers` take: a pump's staff list is bounded reference data, not a history
# to page through. The cap makes the bound structural rather than assumed.
_MAX_ROWS = 500


# --- schemas -------------------------------------------------------------------


class UserListItem(BaseModel):
    """What a manager may see: enough to fill a picker and resolve a name, and no more.

    Written as a separate model rather than one model filtered at runtime, for the reason
    `credit_customers.py` gives: a filter is a line of code someone can delete without any
    test noticing; a type that simply has no `phone` field cannot leak one. It also means
    the OpenAPI schema tells the truth about what each role sees.
    """

    id: UUID
    full_name: str
    role: Role
    is_active: bool


class UserResponse(BaseModel):
    """The admin view. Still no email -- that is not a column here (§5.1, §13.26)."""

    id: UUID
    full_name: str
    phone: str | None
    role: Role
    # The MEMBERSHIP flag, which is the one this router writes and the one every permission
    # check resolves against (§5.1).
    is_active: bool
    # The PROFILE flag, read-only here. Exposed rather than hidden because an admin looking
    # at somebody who cannot sign in deserves to see *which* switch is off -- `deps.py`
    # gives the two cases different error codes and this is where that becomes visible.
    profile_is_active: bool
    created_at: datetime


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    # Write-only, and it exists on no response model, so there is nothing to leak back.
    # Never reaches `_profile_snapshot` / `_membership_snapshot` either, which cover real
    # columns only -- so it cannot land in `audit_logs` by accident (§14).
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=200)
    role: Role
    phone: str | None = Field(default=None, max_length=30)

    @field_validator("email")
    @classmethod
    def _looks_like_an_email(cls, value: str) -> str:
        """A deliberately shallow check, hand-rolled rather than `pydantic.EmailStr`.

        `EmailStr` needs the `email-validator` package and §14 says ask before adding a
        dependency; §16 already set this precedent by hand-rolling content sniffing instead
        of installing `python-magic`. Supabase is the real authority and rejects everything
        this misses -- the job here is to catch a typo before spending a network round trip
        on it, not to implement RFC 5322.
        """
        cleaned = value.strip()
        local, separator, domain = cleaned.partition("@")
        if (
            not separator
            or "@" in domain
            or not local
            or any(character.isspace() for character in cleaned)
            # A domain needs at least two non-empty labels. Checked by splitting rather
            # than by `"." in domain`, which the first draft used and which happily accepts
            # "a@.com" -- caught by the parametrised test, and the reason that test lists
            # the near-misses rather than one obviously broken string.
            or len(domain.split(".")) < 2
            or not all(domain.split("."))
        ):
            raise ValueError("must be an email address")
        return cleaned

    @field_validator("full_name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        # `min_length` alone passes "   ", which would reach a NOT NULL text column as
        # whitespace and read as a nameless person in every list.
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class UserUpdate(BaseModel):
    """What may change after creation -- and, by omission, what may not.

    `email`, `password` and `id` are absent on purpose, and `extra="forbid"` turns an
    attempt to send one into a 422 rather than a silent no-op. `expense_categories.py`
    states why the silent version is the dangerous one: an admin who "changed" an email and
    got a 200 back would reasonably believe it worked. Here they would then hand somebody a
    login that does not exist.

    `outlet_id` is absent for a different reason: it is never a payload field anywhere, and
    accepting one is how a person grants themselves a role at a pump they do not work at.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=30)
    role: Role | None = None
    is_active: bool | None = None

    @field_validator("full_name")
    @classmethod
    def _not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


# `phone` is the only nullable column this router writes, so it is the only field where an
# explicit null in a PATCH means *clear it* rather than *I did not touch this*. The
# reasoning is `credit_customers.py`'s: skipping every null is right for a table whose
# editable columns are all NOT NULL, and wrong the moment one is not.
_NULLABLE_FIELDS = {"phone"}


# --- helpers -------------------------------------------------------------------


def _profile_snapshot(row: UserProfile) -> dict[str, object]:
    """Columns only, deliberately. There is no email or password here to include, and
    keeping this function a plain column list is what guarantees the create payload cannot
    reach `audit_logs` (§14)."""
    return {
        "full_name": row.full_name,
        "phone": row.phone,
        "is_active": row.is_active,
    }


def _membership_snapshot(row: OutletMembership) -> dict[str, object]:
    return {"role": row.role, "is_active": row.is_active}


def _to_response(profile: UserProfile, membership: OutletMembership) -> UserResponse:
    return UserResponse(
        id=profile.id,
        full_name=profile.full_name,
        phone=profile.phone,
        role=Role(membership.role),
        is_active=membership.is_active,
        profile_is_active=profile.is_active,
        created_at=profile.created_at,
    )


def _load_member(
    db: Session, *, user_id: UUID, outlet_id: UUID
) -> tuple[UserProfile, OutletMembership]:
    """Fetch a person and their role at this outlet, or 404.

    **No `resolve_outlet_from_*` dependency, unlike every other router with an id in the
    path.** Those work because the row carries its own `outlet_id`; a membership's outlet is
    not derivable from a `user_id` alone -- that is precisely the point of §5.1 putting the
    role on a per-outlet join table. So the outlet comes from the caller and the lookup is
    scoped to it.

    The side effect is *better* than the house pattern rather than a compromise: a user who
    exists at another outlet is a **404 here, never a 403**, so this router cannot be used
    to probe whether a person exists elsewhere. `credit_customers.py` answers 403 in the
    equivalent case, which leaks existence across a tenancy boundary.
    """
    row = db.execute(
        select(UserProfile, OutletMembership)
        .join(OutletMembership, OutletMembership.user_id == UserProfile.id)
        .where(UserProfile.id == user_id, OutletMembership.outlet_id == outlet_id)
    ).one_or_none()

    if row is None:
        raise AppError(
            status_code=404,
            code="USER_NOT_FOUND",
            detail="No user with that id at this outlet.",
        )
    return row[0], row[1]


def _refuse_stranding_the_outlet(
    db: Session,
    *,
    outlet_id: UUID,
    membership: OutletMembership,
    new_role: Role | None,
    new_is_active: bool | None,
) -> None:
    """§13.27. The last active admin cannot be demoted or deactivated.

    `provision_user.py` guards the same thing for the CLI and explains the stake -- "a
    careless re-run with the wrong --role would otherwise silently demote the only admin and
    lock everyone out of §8's admin-only actions". There it is recoverable, because `--force`
    exists and a shell operator outranks any API role. **Here there is no `--force`, because
    there is no caller left who could send it**, so this refuses outright and names the
    command that can still get in.
    """
    if Role(membership.role) is not Role.admin or not membership.is_active:
        return

    losing_admin = new_role is not None and new_role is not Role.admin
    being_retired = new_is_active is False
    if not (losing_admin or being_retired):
        return

    # Joins `user_profiles.is_active` as well as the membership flag. An admin who cannot
    # sign in is not a second admin, and counting them would strand the outlet on the
    # strength of an entirely correct query.
    others = db.execute(
        select(func.count())
        .select_from(OutletMembership)
        .join(UserProfile, UserProfile.id == OutletMembership.user_id)
        .where(
            OutletMembership.outlet_id == outlet_id,
            OutletMembership.role == Role.admin.value,
            OutletMembership.is_active.is_(True),
            UserProfile.is_active.is_(True),
            OutletMembership.user_id != membership.user_id,
        )
    ).scalar_one()

    if others == 0:
        raise AppError(
            status_code=409,
            code="LAST_ADMIN_AT_OUTLET",
            detail=(
                "This is the only admin who can still sign in at this outlet. Removing "
                "their access would leave nobody able to restore it through the app. "
                "Make somebody else an admin first, or use: python -m "
                "app.jobs.provision_user --user-id <uuid> --full-name '<name>' "
                "--role admin --force"
            ),
        )


# --- routes --------------------------------------------------------------------


@router.get("/users", response_model=list[UserListItem])
def list_users(
    include_inactive: bool = Query(default=False),
    actor: Actor = Depends(require_role(Role.manager)),
    db: Session = Depends(get_db),
) -> list[UserListItem]:
    """The outlet roster. Manager floor, and §8 explains why not attendant: an attendant has
    nobody to pick and no foreign name to resolve, because they cannot read another
    attendant's shift and `POST /shifts` refuses them a foreign `attendant_id` anyway.

    Ordered by role then name. The Postgres `membership_role` enum's label order is
    `admin, manager, attendant` (migration 0002), so sorting on the column puts the most
    privileged first -- which happens to be how an admin wants to read a staff list. That
    looks like an accident and is not, so: reordering those labels would reorder this
    screen, which is one more reason migration 0002 pins them.
    """
    statement = (
        select(UserProfile, OutletMembership)
        .join(OutletMembership, OutletMembership.user_id == UserProfile.id)
        .where(OutletMembership.outlet_id == actor.outlet_id)
        .order_by(OutletMembership.role, UserProfile.full_name)
    )
    if not include_inactive:
        statement = statement.where(
            OutletMembership.is_active.is_(True), UserProfile.is_active.is_(True)
        )

    rows = db.execute(statement.limit(_MAX_ROWS)).all()
    return [
        UserListItem(
            id=profile.id,
            full_name=profile.full_name,
            role=Role(membership.role),
            is_active=membership.is_active,
        )
        for profile, membership in rows
    ]


@router.get("/users/{user_id}", response_model=UserResponse)
def get_user(
    user_id: UUID,
    actor: Actor = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
) -> UserResponse:
    """One person's detail, including their phone -- which is why this is admin and the
    list above is manager (§8)."""
    profile, membership = _load_member(db, user_id=user_id, outlet_id=actor.outlet_id)
    return _to_response(profile, membership)


@router.post("/users", response_model=UserResponse, status_code=201)
def create_user(
    payload: UserCreate,
    actor: Actor = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
    auth: AuthBackend = Depends(get_auth),
) -> UserResponse:
    """Create an account, a profile and a role, in that order (§13.25).

    **Supabase first, and it has to be**: `user_profiles.id` must equal `auth.users.id`
    because that value is the JWT's `sub` claim, and only the provider can say what it is.
    Everything after is one database transaction, and if it fails the account is deleted
    again on a best-effort basis.

    **No `Idempotency-Key`** (§6.10, D9). That rule covers POSTs creating a *money* record;
    a user is reference data, exactly like a credit customer, and neither carries one. The
    retry hazard here belongs to the provider rather than to this system: a timed-out retry
    is refused with `AUTH_USER_EXISTS`, which is a clear error rather than a duplicate row.
    """
    auth_user_id = auth.create_user(email=payload.email, password=payload.password)

    try:
        profile = UserProfile(
            id=auth_user_id,
            full_name=payload.full_name,
            phone=payload.phone,
            # Unlike `provision_user.py`, which leaves this NULL because a system action has
            # nobody to credit and the first admin has no predecessor. Here there is always
            # an acting admin, by name (§5.1).
            created_by=actor.user.id,
        )
        db.add(profile)
        # Flushed before the membership is added. These models are linked by a column-level
        # ForeignKey but no ORM relationship(), so SQLAlchemy's unit of work has no
        # dependency edge between them and will happily order the two INSERTs the wrong way
        # round -- which Postgres then rejects on the foreign key. The same comment, and the
        # same reason, as app/jobs/provision_user.py.
        db.flush()

        membership = OutletMembership(
            user_id=auth_user_id,
            outlet_id=actor.outlet_id,
            role=payload.role.value,
            created_by=actor.user.id,
        )
        db.add(membership)
        db.flush()

        # Two audit rows, one per table. `record_id` points at a row in a named table, so
        # "who made Ramesh a manager" is only answerable under `outlet_memberships`.
        audit.record(
            db,
            outlet_id=actor.outlet_id,
            table_name="user_profiles",
            record_id=profile.id,
            action=AuditAction.insert,
            changed_by=actor.user.id,
            new_values=_profile_snapshot(profile),
        )
        audit.record(
            db,
            outlet_id=actor.outlet_id,
            table_name="outlet_memberships",
            record_id=membership.id,
            action=AuditAction.insert,
            changed_by=actor.user.id,
            new_values=_membership_snapshot(membership),
        )
        db.commit()
    except Exception:
        db.rollback()
        # §13.25's compensation. Best effort, and deliberately swallowing its own failure:
        # the caller is already getting an error, and replacing it with a *different* error
        # about the cleanup would hide what actually went wrong. What must not happen is
        # silence, so the orphan is logged with its id -- it fails closed (the account can
        # authenticate and is then refused everywhere with PROFILE_NOT_PROVISIONED) and
        # `provision_user.py` is exactly the tool that repairs it.
        try:
            auth.delete_user(user_id=auth_user_id)
        except Exception:
            logger.error(
                "orphaned auth user: created in the identity provider, no profile here, "
                "and the compensating delete also failed. Repair with "
                "app/jobs/provision_user.py or remove it in the Supabase dashboard.",
                extra={"auth_user_id": str(auth_user_id)},
            )
        raise

    db.refresh(profile)
    db.refresh(membership)

    logger.info(
        "user created",
        extra={
            "user_id": str(profile.id),
            "outlet_id": str(actor.outlet_id),
            "role": membership.role,
            "created_by": str(actor.user.id),
        },
    )
    return _to_response(profile, membership)


@router.patch("/users/{user_id}", response_model=UserResponse)
def update_user(
    user_id: UUID,
    payload: UserUpdate,
    actor: Actor = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
) -> UserResponse:
    """Change a name, a phone, a role, or whether somebody still has access here.

    One endpoint writing up to two tables. Splitting it into `/users/{id}` and
    `/users/{id}/membership` was considered and rejected: it makes a schema detail (the role
    lives on a join table) into a URL, and an admin changing a name and a role in one sheet
    would issue two requests that can half-fail.

    Deactivation writes the **membership** flag, never `user_profiles.is_active` (§5.1,
    §13.26). And it is an ordinary `AuditAction.update`, never `status_change` -- §14 is
    explicit that the label means a *shift* lifecycle move.
    """
    profile, membership = _load_member(db, user_id=user_id, outlet_id=actor.outlet_id)

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise AppError(
            status_code=422,
            code="NO_FIELDS_TO_UPDATE",
            detail="Send at least one field to change.",
        )

    _refuse_stranding_the_outlet(
        db,
        outlet_id=actor.outlet_id,
        membership=membership,
        new_role=payload.role,
        new_is_active=payload.is_active,
    )

    # Before the mutation, or old_values records the new state and the change is unreadable.
    profile_before = _profile_snapshot(profile)
    membership_before = _membership_snapshot(membership)

    # Written out field by field rather than looped, and that is a deliberate choice of
    # verbose over clever (§1). Four fields split across two tables, with three different
    # rules about what `None` means, is exactly the shape a `setattr` loop gets subtly
    # wrong -- and the way it goes wrong here is that somebody's role silently does not
    # change while the response says it did.
    touched_profile = False
    touched_membership = False

    if payload.full_name is not None:
        profile.full_name = payload.full_name
        touched_profile = True

    # The one field where an explicit null means *clear it* rather than *I did not touch
    # this*, so it reads `exclude_unset`'s answer rather than the attribute (§_NULLABLE_FIELDS
    # above). Every other field here is NOT NULL, where a null is a client bug that would
    # reach the database as an IntegrityError and surface as a 500.
    if "phone" in changes:
        profile.phone = payload.phone
        touched_profile = True

    if payload.role is not None:
        # `.value`, matching app/jobs/provision_user.py. `Role` is a StrEnum so the member
        # would coerce anyway, but passing the plain string is what the column's Postgres
        # enum actually holds and keeps the two writers of this column identical.
        membership.role = payload.role.value
        touched_membership = True

    if payload.is_active is not None:
        membership.is_active = payload.is_active
        touched_membership = True

    # One audit row per table *actually changed*. A PATCH of only `full_name` must not
    # record an `outlet_memberships` update that says nothing changed -- an audit log full
    # of no-op rows is one nobody reads.
    if touched_profile:
        audit.record(
            db,
            outlet_id=actor.outlet_id,
            table_name="user_profiles",
            record_id=profile.id,
            action=AuditAction.update,
            changed_by=actor.user.id,
            old_values=profile_before,
            new_values=_profile_snapshot(profile),
        )
    if touched_membership:
        audit.record(
            db,
            outlet_id=actor.outlet_id,
            table_name="outlet_memberships",
            record_id=membership.id,
            action=AuditAction.update,
            changed_by=actor.user.id,
            old_values=membership_before,
            new_values=_membership_snapshot(membership),
        )

    db.commit()
    db.refresh(profile)
    db.refresh(membership)

    logger.info(
        "user updated",
        extra={
            "user_id": str(profile.id),
            "fields": sorted(changes),
            "changed_by": str(actor.user.id),
        },
    )
    return _to_response(profile, membership)
