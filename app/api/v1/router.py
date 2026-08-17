"""Aggregating router for API v1.

Every route in the application is mounted below this prefix (CLAUDE.md §3 rule 3),
and tests/test_routes.py asserts that mechanically.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import health, me

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(me.router)
