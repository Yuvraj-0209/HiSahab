/* Application entry point: boot, auth gate, and the shell (CLAUDE.md §2, §8).
 *
 * ## The boot sequence, and why it is in this order
 *
 *   1. `/api/v1/auth-config` -- unauthenticated. Without it there is no way to reach Supabase
 *      and nothing else can happen, so a failure here is fatal and says so plainly.
 *   2. Restore a session from the refresh token, if there is one. A page refresh mid-shift
 *      must not mean signing in again.
 *   3. `/api/v1/me` -- the app-boot call. It answers two different questions at once: is this
 *      token valid, and is this person *provisioned at this outlet*. A valid Supabase identity
 *      with no `user_profiles` row 403s here with PROFILE_NOT_PROVISIONED, which is why the
 *      login screen treats that case separately (see screens/login.js).
 *   4. `/api/v1/client-config` -- the timezone and thresholds the UI must not hardcode.
 *      Requires a token, so it comes after /me.
 *
 * ## Role gates rendering; the server gates everything
 *
 * §8: "Hiding a button is UX, not a control." The tab bar is filtered by the role /me
 * returns, and the router refuses a route above the caller's role -- but both are courtesies.
 * Every endpoint enforces its own floor, and every screen handles a real 403, because a
 * client-side check is a suggestion and this one is deliberately not treated as more.
 */

import { el, render } from "./dom.js";
import { api, explain } from "./api.js";
import {
  isSignedIn,
  loadAuthConfig,
  restoreSession,
  setSignedOutHandler,
  signOut,
} from "./auth.js";
import { navigate, redirect, route, setBeforeEach, setNotFound, startRouter } from "./router.js";
import { buildShell, satisfies } from "./ui/nav.js";
import { notify } from "./ui/toast.js";
import { setZone } from "./time.js";
import { renderLogin } from "./screens/login.js";
import { renderToday } from "./screens/today.js";

const APP = document.getElementById("app");

/** Everything the shell needs to know about the session. Read by screens via `session`. */
export const session = {
  me: null, // {id, full_name, phone, outlet_id, role}
  config: null, // {tz_display, expense_review_threshold, ...}
  shell: null,
};

/* --- fatal boot failure ------------------------------------------------------
 *
 * Distinct from a toast: if the app cannot start there is no shell for a toast to sit on,
 * and a blank screen with a floating message reads as broken rather than as informative.
 */
function renderFatal(title, detail) {
  render(
    APP,
    el("main", { className: "screen stack" }, [
      el("h1", { className: "t-title", text: title }),
      el("p", { className: "t-body", text: detail }),
      el("button", {
        className: "btn",
        text: "Try again",
        attrs: { type: "button" },
        on: { click: () => location.reload() },
      }),
    ]),
  );
}

function showLogin(outletName) {
  session.me = null;
  session.shell = null;
  renderLogin(APP, { outletName, onSignedIn: startSession });
}

/* --- the signed-in shell ----------------------------------------------------- */

async function startSession() {
  let me;
  try {
    me = await api.get("/me");
  } catch (error) {
    // A correct password with no profile at this outlet is not a failed sign-in, and saying
    // so is the difference between "ask an admin" and "retype your password".
    if (error.code === "PROFILE_NOT_PROVISIONED" || error.code === "NOT_A_MEMBER") {
      signOut();
      notify.error(explain(error));
      return;
    }
    if (error.code === "PROFILE_INACTIVE" || error.code === "MEMBERSHIP_INACTIVE") {
      signOut();
      notify.error(explain(error));
      return;
    }
    notify.error(explain(error), { requestId: error.requestId });
    signOut();
    return;
  }

  session.me = me;

  try {
    session.config = await api.get("/client-config");
    // §3 rule 4: display conversion happens in the frontend, but the zone is server config.
    setZone(session.config.tz_display);
  } catch (error) {
    // Not fatal. The app is usable with the default zone and no threshold warnings; the
    // server still enforces every rule. Say so rather than failing to start.
    notify.warning(
      "Could not load outlet settings; using defaults. Server rules are unaffected.",
    );
  }

  const shell = buildShell({
    role: me.role,
    onNavigate: (target) => navigate(target),
  });
  session.shell = shell;

  render(APP, ...shell.nodes);

  registerRoutes();
  startRouter();
}

/* --- routes ------------------------------------------------------------------
 *
 * Screens land one step at a time. Until each arrives, its route renders an honest
 * placeholder naming what will be there -- §16's wayfinding rule: a screen must answer
 * "what's here?", and a blank region answers nothing.
 */

let routesRegistered = false;

function placeholder(title, description, tab) {
  return () => {
    const { shell } = session;
    shell.setTab(tab);
    shell.setActions();
    shell.setTitle(title);
    render(
      shell.screen,
      el("div", { className: "card stack" }, [
        el("p", { className: "t-body", text: description }),
        el("p", { className: "t-caption", text: "This screen arrives in a later step of Phase 12." }),
      ]),
    );
  };
}

function registerRoutes() {
  if (routesRegistered) return;
  routesRegistered = true;

  // The role gate. A courtesy, not a control -- see the module header.
  setBeforeEach((entry) => {
    const required = entry.meta.role;
    if (required && !satisfies(session.me.role, required)) {
      notify.warning("That section is not available for your role.");
      redirect("#/today");
      return false;
    }
    return true;
  });

  route("/", () => redirect("#/today"));

  route(
    "/today",
    () => renderToday(session.shell.screen, { session, navigate }),
    { tab: "today", role: "attendant" },
  );

  route("/entry", placeholder(
    "Entry",
    "Readings, collections, expenses, credit sales and repayments for the open shift.",
    "entry",
  ), { tab: "entry", role: "attendant" });

  route("/cash", placeholder(
    "Cash",
    "The cash position, shortfalls, deposits and the daily summary.",
    "cash",
  ), { tab: "cash", role: "manager" });

  route("/admin", placeholder(
    "Admin",
    "Fuel types, nozzles, prices, margins, categories, customers and the audit log.",
    "admin",
  ), { tab: "admin", role: "admin" });

  setNotFound((path) => {
    const { shell } = session;
    shell.setTitle("Not found");
    shell.setActions();
    render(
      shell.screen,
      el("div", { className: "card stack" }, [
        el("p", { className: "t-body", text: `There is no screen at ${path}.` }),
        el("button", {
          className: "btn btn-primary",
          text: "Go to Today",
          attrs: { type: "button" },
          on: { click: () => navigate("#/today") },
        }),
      ]),
    );
  });
}

/* --- boot -------------------------------------------------------------------- */

async function boot() {
  setSignedOutHandler(() => showLogin(session.config?.outlet_name));

  try {
    await loadAuthConfig();
  } catch (error) {
    renderFatal(
      "Sign-in is not available",
      error.message ??
        "This server is not configured for Supabase authentication. Set SUPABASE_URL and SUPABASE_ANON_KEY.",
    );
    return;
  }

  // A refresh token in sessionStorage means this tab was already signed in.
  const restored = await restoreSession();
  if (restored && isSignedIn()) {
    await startSession();
    return;
  }

  showLogin();
}

boot();
