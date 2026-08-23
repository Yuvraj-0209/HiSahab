"""Application entry point.

Run with:  uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestIdMiddleware

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, serve_ui: bool = True) -> FastAPI:
    """Application factory.

    A factory rather than a module-level app so tests can build an instance with
    different settings without reimporting the module.

    `serve_ui=False` omits the Phase 12 static mount, and the reason is worth stating
    because it is the one sharp edge that mount introduces.

    The mount is a **catch-all at `/`**, registered last so the API wins (see below). Route
    matching is by registration order, so *anything added to the app after this function
    returns lands behind the catch-all and can never be reached.* That is not hypothetical:
    `tests/test_permissions.py` and `tests/test_shift_permissions.py` both build an app and
    then attach a `/_test/...` route to exercise a role dependency in isolation, and all
    twenty-one of those tests began returning 404 the moment the mount was added.

    So the flag exists for callers that need to register their own routes. It is deliberately
    an explicit parameter rather than something clever with `app.router.default`: the
    ordering constraint is real and a reader should be able to see it in the signature
    (CLAUDE.md §2 -- prefer boring and explicit over clever).
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

    # Phase 12. The frontend, mounted LAST -- and that ordering is load-bearing.
    #
    # Starlette matches routes in registration order, so every /api/v1 route above -- plus
    # /docs and /openapi.json, which FastAPI registers at construction -- is tried before
    # this catch-all. Mounted any earlier it swallows the entire API and returns 404 from
    # the static handler, silently: nothing raises, and the first thing to notice would be a
    # browser. tests/test_static_mount.py therefore asserts an API call still succeeds *with
    # the mount installed*, which is the only assertion that tells the two orderings apart.
    #
    # This does not breach §2's "the backend never renders HTML". StaticFiles hands over a
    # file it did not generate; nothing here builds markup and no endpoint knows what a page
    # is. It also cannot disturb tests/test_routes.py -- a Mount is not an APIRoute, so it
    # appears in neither app.openapi()["paths"] nor the dependency walk that finds
    # unauthenticated routes. These assets are public and carry no secret: the Supabase anon
    # key arrives from GET /api/v1/auth-config at runtime, never baked into a file.
    #
    # html=True serves index.html for "/". Combined with hash-based routing, a deep link like
    # /#/shifts/{id}/readings never asks the server for a second document, so there is no SPA
    # fallback rewrite to get wrong.
    #
    # Resolved from __file__ rather than the working directory, so `uvicorn app.main:app`
    # serves the same files whatever directory it was started from.
    static_dir = Path(__file__).parent / "static"
    if not serve_ui:
        logger.debug("UI mount skipped at the caller's request")
    elif static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")
    else:
        # Not fatal. The API is useful without the UI, and a wheel installed without package
        # data should still serve data rather than refuse to boot -- but it says so loudly,
        # because a silently missing frontend looks exactly like a broken deploy.
        logger.warning(
            "static directory not found; UI not mounted",
            extra={"static_dir": str(static_dir)},
        )

    logger.info("application configured", extra={"env": settings.ENV})
    return app


app = create_app()
