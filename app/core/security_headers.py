"""The frontend's Content-Security-Policy, sent as a response header (CLAUDE.md §13.19).

Phase 12 carried the policy in a `<meta http-equiv>` tag inside the hand-written index.html.
Phase 23 builds index.html with Vite, and moves the policy here for two reasons:

* **A meta tag cannot carry `frame-ancestors`.** Browsers ignore that directive outside a real
  header, so Phase 12's clickjacking protection was written down and never enforced.
* **The policy should live with the server that serves the page**, not inside an artefact a
  build tool regenerates.

## Why it wraps the UI mount rather than the whole app

The policy is the frontend's: `default-src 'self'`, no inline script, no third-party host. The
API returns JSON, which a browser never executes as a document. `/docs` in development is
Swagger UI, which loads its script from a CDN; a global policy would break it. So the header is
attached to exactly the responses the UI mount produces: index.html and its assets.

## The same policy is in two places, on purpose, and a test holds them equal

`frontend/vite.config.ts` exports `CONTENT_SECURITY_POLICY` so `vite preview` (and so the smoke
suite) runs under the real policy. `tests/test_static_mount.py` parses that constant and
asserts it equals this one, so the policy the browser tests run under cannot drift from the one
production sends.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        # No 'unsafe-inline' and no 'unsafe-eval'. §13.19: the session token is readable by
        # script, so an injected <script> that ran would be a stolen session.
        "script-src 'self'",
        # React's `style` prop and GSAP write through the CSSOM, which `style-src 'self'`
        # permits; Tailwind emits one stylesheet. Nothing needs inline <style>.
        "style-src 'self'",
        # Signed receipt URLs come from Supabase Storage (§7.3).
        "img-src 'self' data: https://*.supabase.co",
        "font-src 'self'",
        # Supabase Auth for sign-in and token refresh.
        "connect-src 'self' https://*.supabase.co",
        # Every write is a fetch; a native form post is never wanted (and under hash routing
        # it would reach the static mount, which answers 405).
        "form-action 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    ]
)


class SecurityHeaders:
    """Wrap an ASGI app so every HTTP response it sends carries the CSP.

    A plain ASGI wrapper rather than `BaseHTTPMiddleware`: it only rewrites the response-start
    message, never buffers a body, and so cannot change how a static file streams.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"content-security-policy"
                ]
                headers.append((b"content-security-policy", CONTENT_SECURITY_POLICY.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
