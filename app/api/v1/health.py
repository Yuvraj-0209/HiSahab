"""Health check.

Lives under /api/v1 like everything else. CLAUDE.md §3 rule 3 calls out health
checks by name: "No exceptions, not even health checks that obviously won't change."
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    """Report liveness and database reachability.

    The database round-trip is included because an API that answers 200 while
    unable to reach Postgres tells an operator nothing useful.
    """
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.exception("health check could not reach the database")
        raise AppError(
            status_code=503,
            code="DATABASE_UNAVAILABLE",
            detail="The database is not reachable.",
        ) from None

    return {"status": "ok", "database": "ok"}
