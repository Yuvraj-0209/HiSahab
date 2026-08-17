"""user profiles and outlet memberships

Revision ID: 0002
Revises: 0001
Create Date: Phase 2 -- auth

Supabase Auth owns identity (auth.users): email, password hash, confirmation state.
These two tables hold what Supabase has no concept of -- application state and, crucially,
*authorisation*. Supabase proves who someone is; this schema decides what they may do.

The split into two tables is the whole point of CLAUDE.md §5.0/§5.1: a role is not an
attribute of a person, it is an attribute of a person *at an outlet*. Someone can be a
manager at one pump and an attendant at another, so `role` lives on the membership, never
on the profile.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


# create_type=False because op.create_table() would otherwise try to emit CREATE TYPE
# implicitly as a side effect of the column definition. We create and drop it explicitly
# below instead, because the implicit behaviour is asymmetric: Alembic creates the type on
# the way up but op.drop_table() does NOT drop it on the way down. A leaked type makes the
# downgrade succeed once and then fail forever after with "type already exists", which
# breaks the test suite's teardown (tests/conftest.py downgrades to base).
#
# Naming convention for the enums arriving in Phases 4-7: <table_singular>_<column>.
role_enum = postgresql.ENUM(
    "admin", "manager", "attendant", name="membership_role", create_type=False
)


def upgrade() -> None:
    role_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "user_profiles",
        # Deliberately NO gen_random_uuid() default, unlike every other table in §5.
        # This id must equal auth.users.id -- it is the JWT's `sub` claim -- so Supabase
        # supplies it and we copy it. Generating one here would create a profile that no
        # token can ever resolve to.
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # §5 mandates created_by on every table, which here is a self-reference. Nullable
        # by necessity: the first admin is created by the bootstrap command
        # (app/jobs/provision_user.py) before any user exists to credit. NULL therefore
        # means "provisioned by the system", exactly as `outlets` in 0001 carries no
        # created_by at all for the same reason. Documented deviation, not an oversight.
        sa.Column(
            "created_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
    )

    op.create_table(
        "outlet_memberships",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=False
        ),
        # §5.0: this table carries its own outlet_id because tenancy is not derivable
        # from anything else in the row -- a membership IS the user-to-outlet link.
        sa.Column("outlet_id", sa.UUID(), sa.ForeignKey("outlets.id"), nullable=False),
        sa.Column("role", role_enum, nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_by", sa.UUID(), sa.ForeignKey("user_profiles.id"), nullable=True
        ),
        # One role per user per outlet. Named explicitly because this project has no
        # MetaData naming_convention, so an unnamed constraint gets whatever Postgres
        # invents and becomes awkward to reference later.
        #
        # The backing index is also exactly the lookup require_role() performs on every
        # single request, so no additional index is needed.
        sa.UniqueConstraint(
            "user_id", "outlet_id", name="uq_outlet_memberships_user_outlet"
        ),
    )


def downgrade() -> None:
    op.drop_table("outlet_memberships")
    op.drop_table("user_profiles")
    # Dropped last, and explicitly: outlet_memberships.role depended on it, and
    # op.drop_table() leaves the type behind. See the comment on role_enum above.
    role_enum.drop(op.get_bind(), checkfirst=True)
