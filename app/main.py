"""Application entry point.

Run with:  uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestIdMiddleware

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory.

    A factory rather than a module-level app so tests can build an instance with
    different settings without reimporting the module.
    """
    settings = settings or get_settings()
    configure_logging(settings.ENV)

    app = FastAPI(
        title="HiSahab",
        description="Daily stock and cash-flow manager for a fuel station.",
        version="0.1.0",
        # Interactive docs are a development convenience, not a production surface.
        docs_url="/docs" if settings.ENV != "prod" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.ENV != "prod" else None,
    )

    # Middleware runs bottom-up, so RequestIdMiddleware is added last to run first:
    # a rejected CORS preflight should still carry a request_id.
    app.add_middleware(
        CORSMiddleware,
        # Explicit allowlist from config. The "*" case is rejected at config load
        # (CLAUDE.md §9) so it cannot reach this call.
        allow_origins=settings.CORS_ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestIdMiddleware)

    install_error_handlers(app)
    app.include_router(api_router)

    logger.info("application configured", extra={"env": settings.ENV})
    return app


app = create_app()
