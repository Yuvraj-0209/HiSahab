/* Hash routing, in about sixty lines and with no framework (CLAUDE.md §14).
 *
 * ## Why hash rather than the History API
 *
 * A hash never reaches the server. So a deep link like `/#/shifts/{id}/readings` asks for
 * exactly one document -- index.html -- and needs no SPA fallback rewrite, which means the
 * `StaticFiles` mount in app/main.py stays a single line with nothing to get wrong. The
 * History API would require the server to answer every unknown path with the shell, and that
 * is precisely the behaviour tests/test_static_mount.py asserts it must NOT have, because a
 * mistyped API path returning HTML with a 200 is far worse than a 404.
 *
 * ## Why one document rather than a page per screen
 *
 * The access token lives in memory (§13.19), so a full page load on every navigation would
 * re-read sessionStorage and re-fetch /me each time. It would also make every transition a
 * white flash, which rules out any continuity between screens.
 */

import { closeAnySheet } from "./ui/sheet.js";

const routes = [];
let notFound = null;
let beforeEach = null;
let currentPath = null;

/**
 * Register a route.
 *
 * @param {string}   pattern  "/shifts/:id/readings" -- ":name" captures one segment
 * @param {Function} handler  (params, query) => void
 * @param {object}   [meta]   arbitrary, e.g. {tab: "entry", role: "manager"}
 */
export function route(pattern, handler, meta = {}) {
  const names = [];
  const regex = new RegExp(
    "^" +
      pattern.replace(/:([A-Za-z_]+)/g, (_, name) => {
        names.push(name);
        // Not a UUID pattern: business dates are path params too
        // (/daily-summaries/{business_date}), and a stricter regex here would silently fail
        // to match rather than 404 loudly.
        return "([^/]+)";
      }) +
      "$",
  );
  routes.push({ regex, names, handler, meta, pattern });
}

/** Handler for a path nothing matched. */
export function setNotFound(handler) {
  notFound = handler;
}

/** Runs before every navigation. Return false to cancel -- used for the auth gate. */
export function setBeforeEach(guard) {
  beforeEach = guard;
}

/** Navigate. Assigning the hash triggers `hashchange`, so there is one code path. */
export function navigate(hash) {
  const target = hash.startsWith("#") ? hash : `#${hash}`;
  if (location.hash === target) {
    resolve(); // same route, re-render (a form saved and wants a refresh)
    return;
  }
  location.hash = target;
}

/** Replace the current entry rather than pushing, so a redirect leaves no back-button trap. */
export function redirect(hash) {
  const target = hash.startsWith("#") ? hash : `#${hash}`;
  history.replaceState(null, "", target);
  resolve();
}

export function currentRoute() {
  return currentPath;
}

function parse() {
  const raw = location.hash.slice(1) || "/";
  const [path, search = ""] = raw.split("?");
  return { path, query: Object.fromEntries(new URLSearchParams(search)) };
}

function resolve() {
  const { path, query } = parse();
  currentPath = path;

  // A sheet belongs to the screen that opened it. Navigating away with one open would strand
  // it over an unrelated screen, still holding a half-filled form.
  closeAnySheet();

  for (const entry of routes) {
    const match = entry.regex.exec(path);
    if (!match) continue;

    const params = {};
    entry.names.forEach((name, index) => {
      params[name] = decodeURIComponent(match[index + 1]);
    });

    if (beforeEach && beforeEach(entry, params) === false) return;

    // Every navigation starts at the top. Without this, moving from a long list to a short
    // screen leaves the page scrolled into empty space -- and the chrome's scroll-edge
    // hairline would be showing over content that is not there.
    window.scrollTo(0, 0);
    entry.handler(params, query);
    return;
  }

  notFound?.(path);
}

/** Start routing. Call once, after the shell exists. */
export function startRouter() {
  window.addEventListener("hashchange", resolve);
  resolve();
}
