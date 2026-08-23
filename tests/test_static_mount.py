"""The frontend is served, and it does not shadow the API (CLAUDE.md §2, §3 rule 3).

Phase 12, Step 3. The mount is one line in `app/main.py` and it is the single largest
structural risk in the phase, because the failure is total and silent in the wrong direction:
mounted at `/` *before* `include_router`, a catch-all `StaticFiles` swallows every `/api/v1`
request and returns 404 from the static handler. Nothing raises. The API simply stops
existing, and the first thing to notice is a browser.

So the assertion that matters here is not "the mount serves index.html" -- that would pass
with the ordering inverted, because `/` is not an API path. It is **an API call succeeding
with the mount installed**, which is the only shape that distinguishes the two orderings.

## Why this is not covered by tests/test_routes.py

That module walks `APIRoute` objects and `app.openapi()["paths"]`. A `Mount` is neither, so
it is invisible there -- which is exactly why the mount cannot break those tests, and equally
why they cannot catch this. Two different questions, deliberately in two files.
"""

from __future__ import annotations

import pathlib

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app

_STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "static"


# --- helpers ------------------------------------------------------------------


async def _get(path: str) -> tuple[int, str, dict[str, str]]:
    """A request against a freshly built app, bypassing the `client` fixture.

    Built here rather than reusing the fixture because these tests are about
    `create_app` itself -- the fixture's `dependency_overrides` are irrelevant, and a test
    of the wiring should exercise the real wiring.
    """
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)
        return response.status_code, response.text, dict(response.headers)


# --- the shadowing guarantee --------------------------------------------------


async def test_the_api_still_answers_with_the_mount_installed() -> None:
    """The assertion this file exists for.

    If the mount were registered before `include_router`, this returns 404 from the static
    handler and every other test in the suite still passes -- they use the ASGI app through
    the same `create_app`, but a route that no longer resolves looks identical to a route
    that was never registered until something asks for it.
    """
    status, body, _ = await _get("/api/v1/health")

    assert status == 200
    assert '"status":"ok"' in body.replace(" ", "")


async def test_an_authenticated_api_route_is_reachable_not_swallowed() -> None:
    """A 401 is the *correct* answer here and proves the route resolved.

    404 would mean the static mount answered instead. Asserting on the code rather than the
    status alone: `HTTP_404` from the static handler and a router 404 are indistinguishable
    by status, and this is precisely the confusion the test is guarding against.
    """
    status, body, _ = await _get("/api/v1/me")

    assert status == 401
    assert "NOT_AUTHENTICATED" in body


async def test_an_unknown_api_path_is_a_json_404_not_the_html_shell() -> None:
    """A missing endpoint must not return the app shell with a 200.

    That failure mode is worse than a 404: a client `fetch`ing a mistyped path would get
    HTML, `response.json()` would throw a parse error, and the real cause -- a wrong URL --
    would be invisible behind it.
    """
    status, body, _ = await _get("/api/v1/no-such-endpoint")

    assert status == 404
    assert "<!doctype html>" not in body.lower()


async def test_openapi_and_docs_are_not_shadowed() -> None:
    """FastAPI registers these at construction, so they precede the mount too."""
    status, body, _ = await _get("/openapi.json")

    assert status == 200
    assert '"openapi"' in body


# --- the mount actually serving -----------------------------------------------


async def test_the_root_serves_the_app_shell() -> None:
    """`html=True` maps `/` to index.html."""
    status, body, headers = await _get("/")

    assert status == 200
    assert "text/html" in headers["content-type"]
    assert "<title>HiSahab</title>" in body


async def test_the_stylesheet_and_entry_module_are_served() -> None:
    """index.html references exactly these two absolute paths; both must resolve.

    A 404 on either is a blank page in a browser and nothing at all in the test suite,
    which is the kind of gap that survives a phase.
    """
    css_status, css_body, css_headers = await _get("/app.css")
    js_status, js_body, js_headers = await _get("/js/main.js")

    assert css_status == 200
    assert "text/css" in css_headers["content-type"]
    assert ":root" in css_body

    assert js_status == 200
    assert "javascript" in js_headers["content-type"]
    # Asserted on the module's *shape* rather than on any particular symbol. An earlier
    # version looked for `export function el(`, which broke the moment that helper moved
    # into js/dom.js in Step 5 -- a test failing because correctly-organised code moved is a
    # test coupled to the wrong thing. What must remain true is that this file is an ES
    # module the browser can load without a bundler.
    assert "import" in js_body


async def test_an_unknown_static_path_is_a_404_not_the_shell() -> None:
    """`html=True` serves index.html for a directory, not for every missing file.

    Worth pinning: some static servers are configured to fall back to the shell for any
    unmatched path, and this application deliberately does not need that (hash routing means
    the browser never requests a second document), so a typo stays a visible 404.
    """
    status, _, _ = await _get("/js/does-not-exist.js")

    assert status == 404


# --- the ordering constraint the mount introduces -----------------------------


async def test_a_route_added_after_create_app_is_shadowed_by_the_mount() -> None:
    """The sharp edge, pinned so it is discovered by a test rather than by a person.

    A catch-all at "/" matches anything the routes above it did not, so a route attached
    *after* `create_app` returns is unreachable. This is not a hypothesis: adding the mount
    broke all twenty-one tests in `test_permissions.py` and both app-building tests in
    `test_shift_permissions.py`, every one of which registers a `/_test/...` route this way.

    Asserting the shadowing rather than quietly working around it means the next person to
    write `app = create_app()` and hang a route off it finds this test explaining why their
    404 happens, instead of rediscovering it.
    """
    app = create_app()

    @app.get("/api/v1/_test/added-late")
    def added_late() -> dict[str, bool]:  # pragma: no cover - never reached, that is the point
        return {"reached": True}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/_test/added-late")

    assert response.status_code == 404


async def test_serve_ui_false_is_the_escape_hatch() -> None:
    """...and the same route is reachable when the mount is omitted.

    The pair is the point: the previous test alone could pass because the path was wrong.
    """
    app = create_app(serve_ui=False)

    @app.get("/api/v1/_test/added-late")
    def added_late() -> dict[str, bool]:
        return {"reached": True}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        reachable = await client.get("/api/v1/_test/added-late")
        root = await client.get("/")

    assert reachable.status_code == 200
    assert reachable.json() == {"reached": True}
    # And with no mount, "/" is a plain 404 -- proving serve_ui actually skipped it rather
    # than the route happening to win on order.
    assert root.status_code == 404


async def test_the_api_is_unaffected_by_serve_ui() -> None:
    """Omitting the UI must not change a single thing about the API."""
    app = create_app(serve_ui=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200


def test_a_missing_static_directory_is_a_warning_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A wheel installed without package data must still serve the API.

    The frontend is a convenience; the money endpoints are not. Refusing to boot because a
    data file is absent would take the whole service down for a packaging mistake.
    """
    import app.main as main_module

    monkeypatch.setattr(
        main_module, "Path", lambda *_: tmp_path / "definitely-not-here"
    )

    # Must not raise.
    built = main_module.create_app()

    assert built is not None


# --- structural: the assets themselves ----------------------------------------


def test_no_asset_references_an_external_host() -> None:
    """§14: no framework, no bundler, no npm -- and no CDN, which is the same dependency
    with worse failure modes.

    §13.19 makes this a security control rather than a matter of taste: the session token is
    readable by script, so a third-party script is a session theft waiting for that host to
    be compromised. index.html's CSP refuses it at runtime; this refuses it at commit time,
    which is the half that shows up in review.

    The scan skips `https://*.supabase.co`, which is the one external origin this app talks
    to deliberately (Supabase Auth for login, signed URLs for receipts) and which the CSP's
    `connect-src` allows by name.
    """
    offenders: list[str] = []
    for path in sorted(_STATIC.rglob("*")):
        if path.suffix not in {".html", ".css", ".js"} or not path.is_file():
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            lowered = line.lower()
            if "supabase.co" in lowered:
                continue
            for marker in ('src="http', "src='http", 'href="http', "href='http", "@import url(http"):
                if marker in lowered:
                    offenders.append(f"{path.name}:{number}: {line.strip()}")

    assert offenders == [], f"external asset references: {offenders}"


def test_the_entry_point_is_a_module_and_the_csp_is_present() -> None:
    """Two properties of index.html that everything else assumes.

    `type="module"` is what makes "no bundler" work -- without it the relative imports in
    js/ are a syntax error in the browser and the app is blank. The CSP is §13.19's stated
    mitigation, so its absence would quietly downgrade a control this project has written
    down as load-bearing.
    """
    html = (_STATIC / "index.html").read_text()

    assert '<script type="module" src="/js/main.js">' in html
    assert "Content-Security-Policy" in html
    assert "script-src 'self'" in html
    # 'unsafe-inline' on script-src would defeat the entire policy: an injected <script>
    # would execute, and the token is readable (§13.19).
    assert "'unsafe-inline'" not in html.split("Content-Security-Policy")[1].split("/>")[0]


@pytest.mark.parametrize("required", ["prefers-reduced-motion", "prefers-reduced-transparency", "prefers-contrast"])
def test_the_accessibility_media_queries_are_honoured(required: str) -> None:
    """§12's scope note excludes per-user *theming*, not accessibility signals.

    The Phase 12 spec amendment is explicit that refusing these would be a bug rather than
    scope discipline, so each is pinned individually -- a loop asserting "at least one of
    these appears" is the shape that silently drops two of the three.
    """
    css = (_STATIC / "app.css").read_text()

    assert f"@media ({required}" in css
