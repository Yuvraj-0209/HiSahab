"""Structured logging with a request_id carried on every line.

CLAUDE.md §9 requires that every request gets a request_id, that it is logged, and
that it comes back in error responses. The id lives in a ContextVar so that any
logger call anywhere in the call stack picks it up automatically -- no threading a
correlation id through every function signature.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

# Set by RequestIdMiddleware at the start of each request. The default marks lines
# emitted outside any request (startup, shutdown, management commands).
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

# Attributes present on every LogRecord. Anything outside this set was passed by the
# caller via logger.info(..., extra={...}) and belongs in the JSON output.
_STANDARD_RECORD_FIELDS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"asctime", "message", "taskName", "request_id"}


class RequestIdFilter(logging.Filter):
    """Stamps the current request_id onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log aggregators."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }

        # Anything the caller passed via extra={...}.
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_FIELDS:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


class HumanFormatter(logging.Formatter):
    """Readable single-line output for local development.

    JSON in a terminal is miserable to read, and the owner is learning backend
    development -- legible logs matter more than machine-parseable ones on a laptop.
    """

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )


def configure_logging(env: str) -> None:
    """Install our handler on the root logger. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(HumanFormatter() if env == "dev" else JsonFormatter())
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if env == "dev" else logging.INFO)

    # Uvicorn ships its own access log in its own format. Silence it so there is
    # exactly one access line per request, ours, carrying the request_id.
    logging.getLogger("uvicorn.access").handlers.clear()
    logging.getLogger("uvicorn.access").propagate = False
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
