"""Aggregating router for API v1.

Every route in the application is mounted below this prefix (CLAUDE.md §3 rule 3),
and tests/test_routes.py asserts that mechanically.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    collections,
    expenses,
    fuel_margins,
    fuel_prices,
    fuel_types,
    health,
    me,
    nozzles,
    readings,
    shift_templates,
    shifts,
)

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

# Phase 4 -- the shift spine. Same caveat as above about intra-module ordering:
# "/shifts/current" must precede "/shifts/{shift_id}" inside shifts.py.
api_router.include_router(shift_templates.router)
api_router.include_router(shifts.router)

# Phase 5 -- readings hang off a shift, so this follows shifts. Its paths are all
# /shifts/{shift_id}/... which is one segment deeper than anything in shifts.py, so
# there is no ordering hazard between the two routers.
api_router.include_router(readings.router)

# Phase 6 -- collections hang off a shift too, same depth as readings and with no path
# overlap between them.
api_router.include_router(collections.router)

# Phase 7 -- expenses, same depth and shape as collections. Its one top-level route,
# /expenses/flagged, is a static path shared with nothing else, so there is no ordering
# hazard against /shifts/{shift_id}/... routes either.
api_router.include_router(expenses.router)
