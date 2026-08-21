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
