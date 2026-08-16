"""Declarative base for all ORM models.

Deliberately empty of models in Phase 1. It exists because alembic/env.py needs a
target_metadata to compare against. Models arrive from Phase 2 onward and must be
imported here so that autogenerate can see them.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
