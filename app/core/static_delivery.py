"""How the built frontend travels to a phone: compressed, and cached by what can change.

Phase 24 audit. Phase 23 measured its bundle budget in gzipped bytes, and nothing served gzipped
bytes: `StaticFiles` sends a file exactly as it sits on disk, so a phone on rural 4G (§6.10's
real network) downloaded about 460 KB of uncompressed JavaScript to reach the sign-in card.
Phase 23's M7 also promised *"index.html served uncached, assets cached immutably"*, and no
`Cache-Control` header was ever sent, so every open of the app revalidated every chunk, one round
trip each.

## Three kinds of file, three cache rules

* **`/assets/*`** -- Vite writes a content hash into every name here, so a changed file is a new
  URL. The old URL can be cached for a year without ever going stale: `immutable`.
* **`index.html`** -- it *names* the hashed assets. Cached, it would hold a phone on an old build
  after a deploy, pointing at chunks that no longer exist. `no-cache` means "revalidate every
  time", which costs one small conditional request (StaticFiles answers 304 from the ETag).
* **Everything else** (`/fonts`, `/img`) keeps its name across deploys, so it cannot be
  immutable. A day spares the round trip and still lets a replaced photograph show up.

## Why the wrappers sit around the UI mount only

The same reason `SecurityHeaders` does: these are the frontend's rules. API responses are JSON
that must never be cached by a shared proxy, and compressing them is a separate decision with its
own trade-offs (a response carrying a secret next to attacker-influenced text is the BREACH
shape), which nothing here needs to take.
"""

from __future__ import annotations

from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.security_headers import SecurityHeaders

IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"
BRIEF = "public, max-age=86400"


def cache_rule(path: str) -> str:
    """The Cache-Control value for a path under the UI mount."""
    if path.startswith("/assets/"):
        return IMMUTABLE
    if path == "/" or path.endswith(".html"):
        return REVALIDATE
    return BRIEF


class CacheHeaders:
    """Set `Cache-Control` on every response the wrapped app sends, by `cache_rule`.

    A plain ASGI wrapper for the reason `SecurityHeaders` is one: it rewrites the response-start
    message and never buffers a body. Error responses are left alone -- a 404 for a mistyped
    asset must not be cached for a year.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rule = cache_rule(scope["path"])

        async def send_with_cache(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] < 400:
                headers = [
                    (name, value) for name, value in message.get("headers", []) if name.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", rule.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cache)


def deliver(app: ASGIApp) -> ASGIApp:
    """Wrap the static app: policy innermost, cache rules, then compression outermost.

    GZip goes outside so it compresses the final body and adds `Vary: Accept-Encoding` to
    headers the inner wrappers have already settled; it skips webp, woff2 and the other formats
    that are compressed already.
    """
    return GZipMiddleware(CacheHeaders(SecurityHeaders(app)), minimum_size=1024)
