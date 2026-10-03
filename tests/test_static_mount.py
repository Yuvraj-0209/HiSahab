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

## Phase 23: a fixture build, and the policy as a header

`app/static/` is now Vite's build output and is gitignored, so these tests mount a small
fixture directory shaped like a build (index.html plus hashed assets) rather than depending on
whether anybody ran `npm run build`. The structural checks on the frontend's own source moved to
`frontend/` paths. And the Content-Security-Policy is a response header now
(`app/core/security_headers.py`), asserted on the responses themselves.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security_headers import CONTENT_SECURITY_POLICY
from app.main import create_app

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_FRONTEND = _ROOT / "frontend"


# --- helpers ------------------------------------------------------------------


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """A directory shaped like `npm run build`'s output: index.html and hashed assets."""
    root = tmp_path_factory.mktemp("static")
    (root / "assets").mkdir()
    (root / "index.html").write_text(
        "<!doctype html><html><head><title>HiSahab</title>"
        '<script type="module" crossorigin src="/assets/index-abc123.js"></script>'
        '<link rel="stylesheet" crossorigin href="/assets/index-abc123.css">'
        '</head><body><div id="root"></div></body></html>'
    )
    (root / "assets" / "index-abc123.js").write_text('import "./chunk-def456.js";\n')
    (root / "assets" / "index-abc123.css").write_text(":root{--accent:#2149c9}\n")
    return root


async def _get(path: str, static_dir: pathlib.Path | None = None) -> tuple[int, str, dict[str, str]]:
    """A request against a freshly built app, bypassing the `client` fixture.

    Built here rather than reusing the fixture because these tests are about
    `create_app` itself -- the fixture's `dependency_overrides` are irrelevant, and a test
    of the wiring should exercise the real wiring.
    """
    app = create_app(static_dir=static_dir)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)
        return response.status_code, response.text, dict(response.headers)


# --- the shadowing guarantee --------------------------------------------------


async def test_the_api_still_answers_with_the_mount_installed(built: pathlib.Path) -> None:
    """The assertion this file exists for.

    If the mount were registered before `include_router`, this returns 404 from the static
    handler and every other test in the suite still passes -- they use the ASGI app through
    the same `create_app`, but a route that no longer resolves looks identical to a route
    that was never registered until something asks for it.
    """
    status, body, _ = await _get("/api/v1/health")

    assert status == 200
    assert '"status":"ok"' in body.replace(" ", "")


async def test_an_authenticated_api_route_is_reachable_not_swallowed(built: pathlib.Path) -> None:
    """A 401 is the *correct* answer here and proves the route resolved.

    404 would mean the static mount answered instead. Asserting on the code rather than the
    status alone: `HTTP_404` from the static handler and a router 404 are indistinguishable
    by status, and this is precisely the confusion the test is guarding against.
    """
    status, body, _ = await _get("/api/v1/me", built)

    assert status == 401
    assert "NOT_AUTHENTICATED" in body


async def test_an_unknown_api_path_is_a_json_404_not_the_html_shell(built: pathlib.Path) -> None:
    """A missing endpoint must not return the app shell with a 200.

    That failure mode is worse than a 404: a client `fetch`ing a mistyped path would get
    HTML, `response.json()` would throw a parse error, and the real cause -- a wrong URL --
    would be invisible behind it.
    """
    status, body, _ = await _get("/api/v1/no-such-endpoint", built)

    assert status == 404
    assert "<!doctype html>" not in body.lower()


async def test_openapi_and_docs_are_not_shadowed(built: pathlib.Path) -> None:
    """FastAPI registers these at construction, so they precede the mount too -- and they sit
    outside the CSP wrapper, which would otherwise break Swagger UI's CDN script in dev."""
    status, body, headers = await _get("/openapi.json", built)

    assert status == 200
    assert '"openapi"' in body
    assert "content-security-policy" not in headers


# --- the mount actually serving -----------------------------------------------


async def test_the_root_serves_the_app_shell(built: pathlib.Path) -> None:
    """`html=True` maps `/` to index.html."""
    status, body, headers = await _get("/", built)

    assert status == 200
    assert "text/html" in headers["content-type"]
    assert "<title>HiSahab</title>" in body


async def test_the_shell_carries_the_content_security_policy_as_a_header(built: pathlib.Path) -> None:
    """§13.19's control, now where a browser enforces all of it.

    Phase 12's policy was a <meta> tag, and browsers ignore `frame-ancestors` there, so the
    clickjacking half was written down and never enforced. A header carries every directive.
    """
    _, _, headers = await _get("/", built)

    policy = headers["content-security-policy"]
    assert policy == CONTENT_SECURITY_POLICY
    assert "frame-ancestors 'none'" in policy
    # 'unsafe-inline' or 'unsafe-eval' on script-src would defeat the policy: an injected
    # <script> would execute, and the token is readable (§13.19).
    script_src = next(part for part in policy.split(";") if part.strip().startswith("script-src"))
    assert "unsafe" not in script_src


async def test_the_built_assets_are_served_with_the_policy(built: pathlib.Path) -> None:
    """index.html references hashed assets by absolute path; both must resolve.

    A 404 on either is a blank page in a browser and nothing at all in the test suite.
    """
    js_status, js_body, js_headers = await _get("/assets/index-abc123.js", built)
    css_status, css_body, css_headers = await _get("/assets/index-abc123.css", built)

    assert js_status == 200
    assert "javascript" in js_headers["content-type"]
    assert "import" in js_body
    assert css_status == 200
    assert "text/css" in css_headers["content-type"]
    assert js_headers["content-security-policy"] == CONTENT_SECURITY_POLICY


async def test_an_unknown_static_path_is_a_404_not_the_shell(built: pathlib.Path) -> None:
    """`html=True` serves index.html for a directory, not for every missing file.

    Worth pinning: some static servers fall back to the shell for any unmatched path, and this
    application deliberately does not need that (hash routing means the browser never requests
    a second document), so a typo stays a visible 404.
    """
    status, _, _ = await _get("/assets/does-not-exist.js", built)

    assert status == 404


def test_the_policy_the_smoke_suite_runs_under_is_the_one_production_sends() -> None:
    """Two copies of the policy, held equal (app/core/security_headers.py explains why two).

    `frontend/vite.config.ts` sends its copy from `vite preview`, so the Playwright suite runs
    under it. If the two drifted, the browser tests would be passing under a policy production
    never sends.
    """
    source = (_FRONTEND / "vite.config.ts").read_text()
    block = source.split("export const CONTENT_SECURITY_POLICY = [", 1)[1].split("]", 1)[0]
    directives = re.findall(r'"([^"]+)"', block)

    assert "; ".join(directives) == CONTENT_SECURITY_POLICY


# --- the ordering constraint the mount introduces -----------------------------


async def test_a_route_added_after_create_app_is_shadowed_by_the_mount(built: pathlib.Path) -> None:
    """The sharp edge, pinned so it is discovered by a test rather than by a person.

    A catch-all at "/" matches anything the routes above it did not, so a route attached
    *after* `create_app` returns is unreachable. This is not a hypothesis: adding the mount
    broke all twenty-one tests in `test_permissions.py` and both app-building tests in
    `test_shift_permissions.py`, every one of which registers a `/_test/...` route this way.

    Asserting the shadowing rather than quietly working around it means the next person to
    write `app = create_app()` and hang a route off it finds this test explaining why their
    404 happens, instead of rediscovering it.
    """
    app = create_app(static_dir=built)

    @app.get("/api/v1/_test/added-late")
    def added_late() -> dict[str, bool]:  # pragma: no cover - never reached, that is the point
        return {"reached": True}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/_test/added-late")

    assert response.status_code == 404


async def test_serve_ui_false_is_the_escape_hatch(built: pathlib.Path) -> None:
    """...and the same route is reachable when the mount is omitted.

    The pair is the point: the previous test alone could pass because the path was wrong.
    """
    app = create_app(serve_ui=False, static_dir=built)

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


@pytest.mark.parametrize("state", ["no directory", "directory without index.html"])
async def test_a_missing_build_is_a_warning_not_a_crash(
    state: str, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A wheel installed without package data, or a deploy that never ran `npm run build`,
    must still serve the API.

    The frontend is a convenience; the money endpoints are not. Refusing to boot because a
    data file is absent would take the whole service down for a packaging mistake. The second
    case is the Phase 23 one: Vite's output directory can exist (a stray file, a failed build)
    without an index.html, and mounting it would serve 404s at "/" with no warning at all.
    """
    static_dir = tmp_path / "static"
    if state == "directory without index.html":
        static_dir.mkdir()
        (static_dir / "stray.txt").write_text("not a build")

    app = create_app(static_dir=static_dir)  # must not raise

    # The application's structured logger writes JSON to stdout rather than propagating to the
    # root logger, so the warning is read where it actually lands.
    assert "frontend build not found" in capsys.readouterr().out
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/v1/health")).status_code == 200


# --- structural: the frontend source --------------------------------------------


def _frontend_text_files() -> list[pathlib.Path]:
    files = [_FRONTEND / "index.html"]
    for path in sorted((_FRONTEND / "src").rglob("*")):
        if path.suffix in {".ts", ".tsx", ".css"} and path.is_file() and path.name != "schema.d.ts":
            files.append(path)
    assert len(files) > 20, "frontend source not found -- did frontend/ move?"
    return files


def test_no_asset_references_an_external_host() -> None:
    """No CDN: the same dependency as npm with worse failure modes (§13.28, §14).

    §13.19 makes this a security control rather than a matter of taste: the session token is
    readable by script, so a third-party script is a session theft waiting for that host to be
    compromised. The CSP header refuses it at runtime; this refuses it at commit time, which
    is the half that shows up in review.

    The scan skips `supabase.co`, the one external origin this app talks to deliberately
    (Supabase Auth for sign-in, signed URLs for receipts), which the CSP allows by name.
    """
    offenders: list[str] = []
    for path in _frontend_text_files():
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            lowered = line.lower()
            if "supabase.co" in lowered:
                continue
            for marker in (
                'src="http', "src='http", 'href="http', "href='http",
                "url(http", "url('http", 'url("http', 'from "http', "from 'http", 'import("http',
            ):
                if marker in lowered:
                    offenders.append(f"{path.relative_to(_ROOT)}:{number}: {line.strip()}")

    assert offenders == [], f"external asset references: {offenders}"


def test_the_entry_point_is_a_module_with_no_inline_script() -> None:
    """`script-src 'self'` refuses inline script, so index.html must carry none: one inline
    <script> would be a blank page under the production header and nowhere else."""
    html = (_FRONTEND / "index.html").read_text()

    assert '<script type="module" src="/src/main.tsx"></script>' in html
    assert len(re.findall(r"<script\b", html)) == 1
    assert "<style" not in html


@pytest.mark.parametrize(
    "required",
    [
        "prefers-reduced-motion",
        "prefers-reduced-transparency",
        "prefers-contrast: more",
        # Phase 23: the second palette (§12 amended -- two palettes, no toggle).
        "prefers-color-scheme: dark",
    ],
)
def test_the_accessibility_media_queries_are_honoured(required: str) -> None:
    """§12's scope note excludes per-user *theming*, not accessibility signals.

    Each is pinned individually -- a loop asserting "at least one of these appears" is the
    shape that silently drops the rest.
    """
    css = (_FRONTEND / "src" / "styles.css").read_text()

    assert f"({required}" in css
