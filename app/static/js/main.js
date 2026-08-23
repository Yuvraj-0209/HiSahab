/* Application entry point (CLAUDE.md §2).
 *
 * Step 3 of Phase 12: this file exists to prove the serving pipeline end to end -- the mount
 * resolves, ES modules load natively with no bundler, the stylesheet applies, and the API is
 * reachable from the browser at the same origin. It is the frontend's equivalent of Phase 1's
 * "one migration proving the pipeline works", and it grows into the real shell in Step 6.
 *
 * Two rules this file already obeys, because they are easier to keep than to retrofit:
 *
 *   1. Nothing is built with innerHTML. Every node is createElement + textContent (§14).
 *      §13.19 makes the session token readable by script, so injected markup is a session
 *      theft rather than a cosmetic bug.
 *   2. No import from anywhere but this origin. The CSP in index.html refuses it, and §14
 *      forbids the dependency a CDN would smuggle in.
 */

const APP = document.getElementById("app");

/** Build an element. The only DOM constructor this codebase uses.
 *
 * `text` goes through textContent, never innerHTML -- that is the entire point, and it is
 * why every screen builds its DOM through here instead of assembling strings.
 */
export function el(tag, { className, text, attrs } = {}, children = []) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value !== undefined && value !== null) node.setAttribute(key, String(value));
    }
  }
  for (const child of children) {
    if (child) node.appendChild(child);
  }
  return node;
}

/** Replace a container's contents. */
export function render(container, ...nodes) {
  container.replaceChildren(...nodes);
}

async function boot() {
  // Same-origin, so a relative path is all that is needed -- and it is why
  // CORS_ALLOWED_ORIGINS lists the uvicorn port itself.
  let health = null;
  let error = null;
  try {
    const response = await fetch("/api/v1/health");
    health = await response.json();
  } catch (cause) {
    error = cause;
  }

  const reachable = health?.status === "ok";

  render(
    APP,
    el("main", { className: "screen stack" }, [
      el("h1", { className: "t-display", text: "HiSahab" }),
      el("p", {
        className: "t-caption",
        text: "Daily stock and cash-flow manager",
      }),
      el("div", { className: "card stack" }, [
        el("p", { className: "t-micro", text: "Serving pipeline" }),
        el("div", { className: "row-between" }, [
          el("span", { className: "t-body", text: "Static assets" }),
          el("span", { className: "pill pill-open", text: "served" }),
        ]),
        el("div", { className: "row-between" }, [
          el("span", { className: "t-body", text: "API" }),
          el("span", {
            className: reachable ? "pill pill-open" : "pill pill-review",
            text: reachable ? "reachable" : "unreachable",
          }),
        ]),
        el("div", { className: "row-between" }, [
          el("span", { className: "t-body", text: "Database" }),
          el("span", {
            className: health?.database === "ok" ? "pill pill-open" : "pill pill-review",
            text: health?.database === "ok" ? "ok" : "unknown",
          }),
        ]),
      ]),
      error
        ? el("p", {
            className: "t-caption text-short",
            text: `Could not reach the API: ${error.message}`,
          })
        : null,
      el("p", {
        className: "t-caption",
        text: "Sign-in and the shift screens land in the following steps.",
      }),
    ]),
  );
}

boot();
