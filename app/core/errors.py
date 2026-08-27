"""Consistent error envelope (CLAUDE.md §3 rule 10).

    {"detail": "human message", "code": "MACHINE_READABLE_CODE"}

plus the request_id, which §9 requires be returned in error responses so a user can
quote it and we can find the matching log line.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# A database constraint is the last line of defence, not the first. Every rule below is
# *also* enforced in application code -- §6.6 calls this belt and braces -- so reaching
# one of these means either a bug or a client that found a path the application missed.
# Either way the caller deserves the §3 rule 10 envelope rather than a bare 500.
#
# This is an allowlist on purpose. Blanket-converting IntegrityError to 409 would turn an
# unforeseen constraint failure -- the kind that means the code is wrong -- into a tidy
# business error the client is invited to retry, and it would disappear from the logs as a
# handled response. An unrecognised constraint stays a 500 and stays loud.
_CONSTRAINT_ERRORS: dict[str, tuple[int, str, str]] = {
    "ck_nozzle_readings_flags_exclusive": (
        422,
        "METER_FLAGS_MUTUALLY_EXCLUSIVE",
        "A rollover and a meter reset cannot both have happened to one nozzle in one "
        "shift. §6.2 has a formula for a rollover and a manual, admin-entered path for a "
        "reset; together they have no defined meaning. Record whichever actually "
        "occurred.",
    ),
    # Reached only when two managers reverse the same row at the same instant: the
    # service-level ALREADY_REVERSED check passes for both, and the unique index refuses
    # the second INSERT. Without this entry that manager got an opaque 500 and no way to
    # tell whether their reversal had landed.
    #
    # There must be one entry here for EVERY `uq_<table>_reverses_id` in the schema. §6.9's
    # correction shape lands on one more table per phase, and the constraint is unreachable
    # except under a race -- so nothing fails in testing when a copy is forgotten, and the
    # omission only surfaces in production with two people clicking at once. Phase 7 copied
    # the constraint onto `expenses` without copying this entry; Phase 8 Step 0 found it.
    # `tests/test_errors.py::test_every_reversal_unique_constraint_is_mapped_to_a_business_error`
    # reads pg_constraint directly so the next table to grow a reversal is covered on the
    # day its migration lands, rather than the day someone remembers.
    "uq_collections_reverses_id": (
        409,
        "ALREADY_REVERSED",
        "This collection has already been reversed.",
    ),
    "uq_expenses_reverses_id": (
        409,
        "ALREADY_REVERSED",
        "This expense has already been reversed.",
    ),
    "uq_credit_sales_reverses_id": (
        409,
        "ALREADY_REVERSED",
        "This credit sale has already been reversed.",
    ),
    "uq_credit_repayments_reverses_id": (
        409,
        "ALREADY_REVERSED",
        "This repayment has already been reversed.",
    ),
    # Phase 16. Mapped in the same commit as migration 0015, which is the whole point of the
    # structural test below -- §6.9 records that this was forgotten twice before it existed.
    "uq_credit_opening_balances_reverses_id": (
        409,
        "ALREADY_REVERSED",
        "This opening balance has already been reversed.",
    ),
    # Phase 10. Four more tables carrying §6.9's shape, mapped on the day 0013 landed rather
    # than a phase later -- which is what the structural test below exists to force.
    "uq_non_fuel_sales_reverses_id": (
        409,
        "ALREADY_REVERSED",
        "This non-fuel sale has already been reversed.",
    ),
    "uq_bank_deposits_reverses_id": (
        409,
        "ALREADY_REVERSED",
        "This deposit has already been reversed.",
    ),
    "uq_salesman_shortfalls_reverses_id": (
        409,
        "ALREADY_REVERSED",
        "This shortfall has already been reversed.",
    ),
    "uq_salesman_shortfall_settlements_reverses_id": (
        409,
        "ALREADY_REVERSED",
        "This settlement has already been reversed.",
    ),
    # §6.11. The API evaluates and refuses this before ever reaching the database, but a
    # client that bypasses the API's own check (or a future caller that forgets to) still
    # reaches this CHECK, and it is the only genuinely-reachable one 0011 adds -- the three
    # on `attachments` itself are unreachable because upload validation refuses every case
    # first (see that migration's docstring).
    "ck_expenses_receipt_required_has_attachment": (
        422,
        "EXPENSE_REQUIRES_RECEIPT",
        "This expense requires a receipt attachment before it can be recorded.",
    ),
    # Attachment paths are {outlet_id}/{YYYY}/{MM}/{DD}/{uuid4}.{ext} -- a collision needs a
    # uuid4 repeat, which is probabilistically unreachable rather than structurally so. A
    # retry beats an opaque 500 on the one-in-a-very-large-number day it happens.
    "uq_attachments_storage_path": (
        409,
        "ATTACHMENT_PATH_COLLISION",
        "That storage path is already in use. Please retry the upload.",
    ),
    # --- check-then-insert races (Phase 9 Step 0) ----------------------------
    #
    # A reversal race is one instance of a shape this codebase uses everywhere: SELECT to
    # see whether a row exists, then INSERT. Two callers pass the SELECT together, the
    # unique index refuses the second INSERT, and before these entries the loser got an
    # opaque 500 -- with no way to tell whether their write had landed, which is the
    # dangerous half.
    #
    # Phase 8 fixed the reversal instance and generalised its test to `uq_%_reverses_id`
    # only, one size too small: `uq_expense_categories_outlet_code` was added by Phase 8
    # itself and shipped unmapped in the very phase paying attention to this bug. Phase 9
    # Step 0 widened `tests/test_errors.py::test_every_unique_constraint_the_api_pre_checks_
    # is_mapped_to_a_business_error` to every `uq_*` in the schema, with two justified
    # exclusions named there.
    #
    # Each entry reuses the code and the meaning its endpoint's own pre-check already
    # raises, so a client cannot tell -- and does not need to care -- whether it lost the
    # SELECT or lost the race. The detail text is necessarily more general than the
    # endpoint's, which can name the conflicting row; here we only have a constraint name.
    "uq_expense_categories_outlet_code": (
        409,
        "CATEGORY_CODE_EXISTS",
        "An expense category with that code already exists at this outlet.",
    ),
    "uq_fuel_types_code": (
        409,
        "FUEL_TYPE_CODE_EXISTS",
        "A fuel type with that code already exists.",
    ),
    "uq_nozzles_outlet_label": (
        409,
        "NOZZLE_LABEL_EXISTS",
        "A nozzle with that label already exists at this outlet.",
    ),
    "uq_fuel_prices_outlet_fuel_effective": (
        409,
        "PRICE_ALREADY_EFFECTIVE_AT",
        "A price for that fuel is already effective from that moment.",
    ),
    "uq_fuel_margins_outlet_fuel_effective": (
        409,
        "MARGIN_ALREADY_EFFECTIVE_AT",
        "A margin for that fuel is already effective from that moment.",
    ),
    "uq_nozzle_readings_shift_nozzle": (
        409,
        "READING_ALREADY_EXISTS",
        "This nozzle already has a reading on this shift. Update it instead.",
    ),
    # (outlet_id, business_date, sequence). `sequence` is server-assigned from
    # `next_sequence`, so two simultaneous opens compute the same number and collide. From
    # the loser's point of view the outcome is the one §5.2 already names: somebody else's
    # shift is now the open one.
    "uq_shifts_outlet_date_sequence": (
        409,
        "SHIFT_ALREADY_OPEN",
        "Another shift was opened at the same moment. Reload before opening one.",
    ),
    "uq_outlet_shift_templates_outlet_sequence": (
        409,
        "SHIFT_TEMPLATE_SEQUENCE_EXISTS",
        "A shift template with that sequence already exists at this outlet.",
    ),
    # Phase 9. The first instance to be mapped on the day its migration landed rather than a
    # phase later -- which is the whole point of the widened structural test above.
    "uq_credit_customers_outlet_phone": (
        409,
        "CREDIT_CUSTOMER_PHONE_EXISTS",
        "A credit customer with that phone number already exists at this outlet.",
    ),
    # Phase 10. `daily_cash_summaries` is one row per outlet per business date, and the API
    # checks for an existing row before inserting -- the same check-then-insert window as
    # every entry above. Two managers reconciling the same day at the same moment is not a
    # far-fetched race here: §4.7 says the whole day is typed in after the fact, so both of
    # them are looking at yesterday.
    "uq_daily_cash_summaries_outlet_date": (
        409,
        "SUMMARY_ALREADY_EXISTS",
        "A cash summary already exists for that business date at this outlet.",
    ),
    # Phase 14, and it arrives by *deletion* rather than addition: this constraint sat in
    # `tests/test_errors.py`'s exclusion list until now, on the grounds that it was "only
    # reachable from app/jobs/provision_user.py, a CLI command run by one operator" and that
    # mapping it would be an entry no request could produce. `app/api/v1/users.py` makes
    # every word of that false. The exclusion is gone and this is what replaces it.
    "uq_outlet_memberships_user_outlet": (
        409,
        "MEMBERSHIP_EXISTS",
        "That user already has a role at this outlet.",
    ),
}


class AppError(Exception):
    """Base class for every business-rule failure in this application.

    Later phases raise subclasses of this for the codes named in CLAUDE.md --
    TOTALIZER_DECREASED, PRIOR_DAY_NOT_RECONCILED, CREDIT_LIMIT_EXCEEDED and so on.
    """

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


def _envelope(request: Request, status_code: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail, "code": code, "request_id": _request_id(request)},
    )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return _envelope(request, exc.status_code, exc.code, exc.detail)


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Give bare HTTP errors the same shape as business errors.

    Without this a 404 from the router looks different from a 409 we raised
    ourselves, and a client ends up parsing two response shapes.
    """
    return _envelope(request, exc.status_code, f"HTTP_{exc.status_code}", str(exc.detail))


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 keeps FastAPI's field-level shape, which §3 rule 10 explicitly permits.

    `exc.errors()` echoes the offending input back inside each error, so for a money field
    that value is a `Decimal` -- which `json.dumps` refuses. Without jsonable_encoder the
    handler raises *inside* the exception handler and the caller gets an opaque 500 instead
    of being told which field was wrong. That is worst precisely where it matters most, on
    the `condecimal` money fields of §3 rule 1.

    Not caught until Phase 3 because no endpoint accepted a Decimal before `fuel_prices`.
    """
    return JSONResponse(
        status_code=422,
        content={
            "detail": jsonable_encoder(exc.errors()),
            "code": "VALIDATION_ERROR",
            "request_id": _request_id(request),
        },
    )


async def integrity_error_handler(
    request: Request, exc: IntegrityError
) -> JSONResponse:
    """Translate a *known* database constraint failure into the standard envelope.

    Anything not in `_CONSTRAINT_ERRORS` falls through to the same 500 the generic handler
    would have produced, with the traceback logged. See that dict for why this allowlists
    rather than converting every IntegrityError.
    """
    message = str(getattr(exc, "orig", exc))
    for constraint, (status_code, code, detail) in _CONSTRAINT_ERRORS.items():
        if constraint in message:
            logger.warning(
                "database constraint refused a write",
                extra={"constraint": constraint, "code": code},
            )
            return _envelope(request, status_code, code, detail)

    logger.exception("unmapped integrity error")
    return _envelope(request, 500, "INTERNAL_ERROR", "An unexpected error occurred.")


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Never leak a traceback to a client; the request_id ties it to the logs."""
    logger.exception("unhandled exception")
    return _envelope(
        request, 500, "INTERNAL_ERROR", "An unexpected error occurred."
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(IntegrityError, integrity_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
