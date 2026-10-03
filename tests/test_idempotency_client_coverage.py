"""The client sends an Idempotency-Key to every endpoint that asks for one (§6.10).

The client attaches the header only to paths on its `NEEDS_IDEMPOTENCY` list, and silently
omits it everywhere else (Phase 12's `api.js`; since Phase 23, `frontend/src/api/client.ts`,
which refuses loudly instead of omitting -- but only for paths on the list). That made the list a second, hand-kept copy of a fact the
server already publishes: every route that requires a key declares an `Idempotency-Key` header
parameter, so it appears in the OpenAPI document.

The two copies drifted. Phase 20 added `POST /bank-transactions/confirm-repayments`, its screen
passed a key, and `request()` dropped it because the path was not on the list -- so the server
refused every "Record" on the bank review screen with 400 IDEMPOTENCY_KEY_REQUIRED. No Python
test could see it: the endpoint was correct, and so was the screen's call. Only the list
between them was wrong.

So this test reads the server's document, not a list of its own, and asserts the client
covers every route in it. A new money endpoint with a key now fails here on the day it lands.
"""

from __future__ import annotations

import pathlib
import re

from app.main import create_app

_SRC = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src"
_CLIENT = _SRC / "api" / "client.ts"

# A path parameter is a UUID everywhere in this API.
_SAMPLE_ID = "00000000-0000-0000-0000-000000000001"


def _routes_requiring_a_key() -> dict[str, str]:
    """`{route_without_prefix: request_content_type}` for every POST declaring the header."""
    spec = create_app(serve_ui=False).openapi()
    routes: dict[str, str] = {}
    for path, operations in spec["paths"].items():
        post = operations.get("post")
        if post is None:
            continue
        declares_key = any(
            parameter.get("in") == "header" and parameter.get("name", "").lower() == "idempotency-key"
            for parameter in post.get("parameters", [])
        )
        if not declares_key:
            continue
        content_types = list(post.get("requestBody", {}).get("content", {}))
        routes[path.removeprefix("/api/v1")] = content_types[0] if content_types else ""
    return routes


def _client_patterns() -> list[re.Pattern[str]]:
    """The regex literals inside `NEEDS_IDEMPOTENCY`, translated to Python.

    JS regex literals escape `/` as `\\/`; Python needs a bare `/`. Nothing else in these
    patterns differs between the two dialects.
    """
    source = _CLIENT.read_text()
    block = source.split("export const NEEDS_IDEMPOTENCY: readonly RegExp[] = [", 1)[1].split("];", 1)[0]
    patterns = []
    for line in block.splitlines():
        match = re.fullmatch(r"\s*/(.+)/,\s*", line)
        if match:
            patterns.append(re.compile(match.group(1).replace(r"\/", "/")))
    assert patterns, "no patterns parsed out of NEEDS_IDEMPOTENCY -- did client.ts change shape?"
    return patterns


def _concrete(route: str) -> str:
    return re.sub(r"\{[^}]+\}", _SAMPLE_ID, route)


def test_the_server_declares_keyed_routes() -> None:
    # Guards against this file passing vacuously if the header stops being declared.
    assert len(_routes_requiring_a_key()) >= 10


def test_every_json_route_requiring_a_key_is_on_the_clients_list() -> None:
    patterns = _client_patterns()
    missing = [
        route
        for route, content_type in sorted(_routes_requiring_a_key().items())
        if content_type == "application/json"
        and not any(pattern.search(_concrete(route)) for pattern in patterns)
    ]
    assert missing == [], (
        "These endpoints require an Idempotency-Key, but client.ts NEEDS_IDEMPOTENCY does not "
        f"list them, so request() silently drops the header: {missing}"
    )


def test_every_multipart_route_requiring_a_key_is_sent_one() -> None:
    """Multipart bypasses `request()` (FormData sets its own boundary), so the key travels in
    `postMultipart`'s `extraHeaders`. The screen that posts it must say so."""
    sources = {path: path.read_text() for path in _SRC.rglob("*.ts*") if ".test." not in path.name}
    unsent = []
    for route, content_type in sorted(_routes_requiring_a_key().items()):
        if content_type != "multipart/form-data":
            continue
        posts_it = [
            text for text in sources.values() if f'"{route}"' in text and '"Idempotency-Key"' in text
        ]
        if not posts_it:
            unsent.append(route)
    assert unsent == [], f"No screen posts these multipart routes with an Idempotency-Key: {unsent}"
