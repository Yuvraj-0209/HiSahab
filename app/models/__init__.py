"""ORM models.

Importing this package registers every model on `Base.metadata`, which is what
`alembic revision --autogenerate` compares the live database against. alembic/env.py
imports this package for exactly that reason, so a new model file must be added here or
autogenerate will not see it.
"""

from __future__ import annotations

from app.models.outlet import Outlet
from app.models.user import OutletMembership, UserProfile

__all__ = ["Outlet", "OutletMembership", "UserProfile"]
