"""Give a Supabase Auth user a profile and a role at an outlet.

    python -m app.jobs.provision_user \
        --user-id 3f2b1c8e-... --full-name "Yuvraj Dhamija" --role admin

## Why this is a command and not an admin endpoint

§8 makes user management admin-only, which creates a chicken-and-egg: the very first admin
has no admin to create them. A command breaks that cycle, because anyone with shell access
to the server is already more privileged than any API role. Admin user-management endpoints
are not part of §11's Phase 2 scope and are deliberately not built here.

## Where --user-id comes from

Supabase Auth owns identity. Create the user there first (dashboard: Authentication ->
Users, or the signup API), then copy their UUID here. This command cannot invent it: the
value must equal `auth.users.id`, because that is the `sub` claim their tokens will carry,
and it is what get_current_user() looks the profile up by. A mismatch produces a user who
authenticates successfully and is then refused with PROFILE_NOT_PROVISIONED forever.

The command is idempotent, so it is safe to re-run. It will not silently change an existing
role -- that needs --force.
"""

from __future__ import annotations

import argparse
import sys
from uuid import UUID

from sqlalchemy import select

from app.core.config import get_settings
from app.core.roles import Role
from app.db.session import SessionLocal
from app.models.outlet import Outlet
from app.models.user import OutletMembership, UserProfile


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.jobs.provision_user",
        description="Create a user_profiles row and an outlet_memberships row.",
    )
    parser.add_argument(
        "--user-id",
        required=True,
        type=UUID,
        help="The user's UUID from Supabase Auth (auth.users.id). Must match exactly.",
    )
    parser.add_argument("--full-name", required=True, help="Display name.")
    parser.add_argument(
        "--role",
        required=True,
        choices=[role.value for role in Role],
        help="Role held at this outlet.",
    )
    parser.add_argument("--phone", default=None, help="Optional contact number.")
    parser.add_argument(
        "--outlet-id",
        type=UUID,
        default=None,
        help="Defaults to DEFAULT_OUTLET_ID, the single V1 outlet.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow changing the role of an existing membership.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Return an exit code: 0 on success, 1 on any refusal."""
    args = _parse_args(argv)
    outlet_id = args.outlet_id or get_settings().DEFAULT_OUTLET_ID
    role = Role(args.role)

    with SessionLocal() as session:
        # Checked first so a typo'd outlet id fails loudly here rather than creating a
        # profile and then blowing up on the membership's foreign key.
        outlet = session.get(Outlet, outlet_id)
        if outlet is None:
            print(f"error: no outlet with id {outlet_id}", file=sys.stderr)
            return 1

        profile = session.get(UserProfile, args.user_id)
        if profile is None:
            # created_by stays NULL: this is a system action, and for the first admin
            # there is no user to credit. See migration 0002.
            session.add(
                UserProfile(
                    id=args.user_id,
                    full_name=args.full_name,
                    phone=args.phone,
                )
            )
            # Flushed before the membership is added below. These models are linked by a
            # column-level ForeignKey but no ORM relationship(), so SQLAlchemy's unit of
            # work has no dependency edge between them and will happily order the two
            # INSERTs the wrong way round -- which Postgres then rejects on the foreign
            # key. Still inside the transaction; the commit is at the end.
            session.flush()
            print(f"created profile   {args.user_id}  {args.full_name}")
        else:
            print(
                f"profile exists    {args.user_id}  {profile.full_name} "
                "(left unchanged)"
            )

        membership = session.execute(
            select(OutletMembership).where(
                OutletMembership.user_id == args.user_id,
                OutletMembership.outlet_id == outlet_id,
            )
        ).scalar_one_or_none()

        if membership is None:
            session.add(
                OutletMembership(
                    user_id=args.user_id, outlet_id=outlet_id, role=role.value
                )
            )
            print(f"created role      {role.value} at {outlet.name}")
        elif membership.role == role.value:
            print(f"role unchanged    already {role.value} at {outlet.name}")
        elif args.force:
            previous = membership.role
            membership.role = role.value
            print(f"role changed      {previous} -> {role.value} at {outlet.name}")
        else:
            # Refusing by default matters: a careless re-run with the wrong --role would
            # otherwise silently demote the only admin and lock everyone out of §8's
            # admin-only actions.
            print(
                f"error: {args.user_id} is already {membership.role} at {outlet.name}; "
                f"pass --force to change it to {role.value}",
                file=sys.stderr,
            )
            return 1

        session.commit()

    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
