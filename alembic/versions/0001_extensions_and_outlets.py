"""extensions and outlets

Revision ID: 0001
Revises:
Create Date: Phase 1 -- foundation

The first migration. It proves the whole pipeline (Alembic can reach the database,
apply DDL, and reverse it) and establishes the multi-tenancy root described in
CLAUDE.md §5.0.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.core.config import get_settings

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL 13+ ships gen_random_uuid() in core, so on a modern server this is
    # redundant. It is declared explicitly anyway because CLAUDE.md §5 makes
    # gen_random_uuid() the default for every table's primary key, and that
    # guarantee should not depend on whichever server version we happen to get.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # The tenancy root. V1 serves exactly one outlet, but the id it hands out is
    # what every scoped table will reference from Phase 2 onward. Creating it now
    # avoids the one retrofit that has no correct backfill: a shift row carries no
    # other clue as to which pump it belonged to.
    #
    # Deliberate deviation from §5's "every table has created_by": the first outlet
    # is seeded by the system before any user exists, and user_profiles is not
    # created until Phase 2, so the foreign key would point at a missing table.
    op.create_table(
        "outlets",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Seed the single V1 outlet at a fixed, well-known id so that development,
    # test and production databases all agree and fixtures stay deterministic.
    settings = get_settings()
    op.execute(
        sa.text(
            "INSERT INTO outlets (id, name) VALUES (CAST(:id AS uuid), :name)"
        ).bindparams(
            id=str(settings.DEFAULT_OUTLET_ID), name=settings.DEFAULT_OUTLET_NAME
        )
    )


def downgrade() -> None:
    op.drop_table("outlets")
    # Dropped last: the table's default depended on it.
    op.execute("DROP EXTENSION IF EXISTS pgcrypto")
