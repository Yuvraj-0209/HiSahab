"""Consistent error envelope (CLAUDE.md §3 rule 10).

    {"detail": "human message", "code": "MACHINE_READABLE_CODE"}

plus the request_id, which §9 requires be returned in error responses so a user can
quote it and we can find the matching log line.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


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
    """422 keeps FastAPI's field-level shape, which §3 rule 10 explicitly permits."""
    return JSONResponse(
        status_code=422,
        content={
            "detail": exc.errors(),
            "code": "VALIDATION_ERROR",
            "request_id": _request_id(request),
        },
    )


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
    app.add_exception_handler(Exception, unhandled_exception_handler)
