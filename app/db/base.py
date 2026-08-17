"""Declarative base for all ORM models.

This module holds *only* Base. Models live in app/models/ and are registered on
Base.metadata by `import app.models` in alembic/env.py, which is what autogenerate
compares against.

Phase 1's version of this docstring said to import the models *here*. That would
deadlock: app/models/user.py imports Base from this module, so a model import at the
bottom of this file re-enters a half-initialised module the moment anything (e.g.
app/api/deps.py) imports a model directly, and raises ImportError. Registering in
alembic/env.py keeps the dependency one-directional.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
