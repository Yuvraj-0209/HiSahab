"""Aggregating router for API v1.

Every route in the application is mounted below this prefix (CLAUDE.md §3 rule 3),
and tests/test_routes.py asserts that mechanically.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    attachments,
    audit_logs,
    bank_deposits,
    cash_position,
    collections,
    credit_customers,
    credit_repayments,
    credit_sales,
    daily_summaries,
    expense_categories,
    expenses,
    fuel_margins,
    fuel_prices,
    fuel_types,
    health,
    me,
    non_fuel_sales,
    nozzles,
    readings,
    shift_templates,
    shifts,
    shortfalls,
    uploads,
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

# Phase 8 -- expense categories, reference data like fuel_types but outlet-scoped. Grouped
# with the Phase 3 reference-data block conceptually, but included here because its paths
# (/expense-categories, /expense-categories/{id}) overlap with nothing at all -- in
# particular they do not collide with expenses.py's /expenses/flagged.
api_router.include_router(expense_categories.router)

# Phase 8 continued -- attachments. uploads.py's one route (POST /uploads/receipt) and
# attachments.py's one route (GET /attachments/{id}/url) share nothing path-wise with any
# router above, including expenses.py's /expenses/flagged.
api_router.include_router(uploads.router)
api_router.include_router(attachments.router)

# Phase 9 -- credit customers, outlet-scoped reference data like expense_categories.
#
# **Intra-module ordering matters here**, and credit_customers.py carries the note at the
# point it matters: /credit-customers/outstanding is declared before
# /credit-customers/{customer_id}, or FastAPI parses "outstanding" as a customer id and the
# report 422s on the UUID. Same hazard fuel_prices.py's "/current" has.
api_router.include_router(credit_customers.router)

# Phase 9 continued -- credit sales hang off a shift, same depth and shape as expenses.
# All their paths are /shifts/{shift_id}/credit-sales..., one segment deeper than anything
# in shifts.py, so there is no ordering hazard against it or against credit_customers.py.
api_router.include_router(credit_sales.router)
api_router.include_router(credit_repayments.router)

# Phase 10 -- the cash engine. Non-fuel sales hang off a shift, same depth and shape as
# collections and expenses, so there is no ordering hazard against shifts.py or against
# each other. Its paths are /shifts/{shift_id}/non-fuel-sales..., which overlap with
# nothing above.
api_router.include_router(non_fuel_sales.router)
api_router.include_router(bank_deposits.router)
# Reads §6.4's per-shift figures and writes nothing. Its single path,
# /shifts/{shift_id}/cash-position, is one segment deeper than anything in shifts.py.
api_router.include_router(cash_position.router)
# Shortfalls: shift-scoped write routes plus two top-level reports. Intra-module
# ordering matters -- /salesman-shortfalls/outstanding is declared before
# /salesman-shortfalls/{salesman_id}/ledger, the same hazard credit_customers.py has.
api_router.include_router(shortfalls.router)
# The daily summary. /daily-summaries/{business_date} takes a DATE, not a UUID, so the
# static-before-parameterised hazard does not apply -- 'outstanding' would never parse
# as a date either way. Its two action routes are one segment deeper than the resource.
api_router.include_router(daily_summaries.router)

# Phase 11 -- reading the audit trail. A single static path, /audit-logs, which collides with
# nothing above and has no parameterised sibling, so the static-before-parameterised hazard
# that fuel_prices.py's "/current" and credit_customers.py's "/outstanding" carry does not
# arise here. Deliberately last: it is the only router that reads *about* the others.
api_router.include_router(audit_logs.router)
