"""Aggregating router for API v1.

Every route in the application is mounted below this prefix (CLAUDE.md §3 rule 3),
and tests/test_routes.py asserts that mechanically.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import fuel_margins, fuel_prices, fuel_types, health, me, nozzles

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(me.router)

# Phase 3 -- reference data. Order of inclusion does not matter here because no two of
# these declare overlapping paths; the ordering that *does* matter is within
# fuel_prices.py and fuel_margins.py, where "/current" must precede any "/{id}" route.
api_router.include_router(fuel_types.router)
api_router.include_router(nozzles.router)
api_router.include_router(fuel_prices.router)
api_router.include_router(fuel_margins.router)
