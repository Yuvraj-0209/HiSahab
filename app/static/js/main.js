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
import { renderToday, renderShiftById } from "./screens/today.js";
import { renderReadings } from "./screens/readings.js";
import { renderEntry } from "./screens/entry.js";
import { renderCollections } from "./screens/collections.js";
import { renderExpenses } from "./screens/expenses.js";
import { renderNonFuelSales } from "./screens/non_fuel_sales.js";
import { renderCreditSales } from "./screens/credit_sales.js";
import {
  renderCreditRepayments,
  renderLedgerRepayments,
} from "./screens/credit_repayments.js";
import { renderCreditHub, renderCustomerLedger as renderLedger } from "./screens/credit.js";
import { renderOpeningBalances } from "./screens/credit_opening_balances.js";
import { renderBankDeposits } from "./screens/bank_deposits.js";
import { renderCash } from "./screens/cash.js";
import { renderCashPosition } from "./screens/cash_position.js";
import { renderDay, renderDays } from "./screens/days.js";
import { renderAlerts, renderReports } from "./screens/reports.js";
import {
  renderAdmin,
  renderCategories,
  renderFuelTypes,
  renderNozzles,
  renderShiftTemplates,
} from "./screens/admin.js";
import { renderPricing } from "./screens/admin_pricing.js";
import { renderCustomers } from "./screens/admin_customers.js";
import { renderAuditLogs } from "./screens/audit_logs.js";
import { renderUsers } from "./screens/admin_users.js";
import { renderShortfallLedger } from "./screens/shortfalls.js";
import { renderFlaggedExpenses } from "./screens/flagged_expenses.js";

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

/* The login screen owns a canvas outside #app (js/backdrop/skyline.js) and a subscription to
 * the shared frame loop. Neither is torn down by rendering over #app, so the handle it hands
 * back is held here and called the moment a session starts. A missed call is not a visual
 * bug -- it is a requestAnimationFrame running behind the shell for the life of the session.
 */
let detachLogin = null;

function showLogin(outletName) {
  session.me = null;
  session.shell = null;
  if (detachLogin) detachLogin();
  detachLogin = renderLogin(APP, { outletName, onSignedIn: startSession }).detach;
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
    onLogout: () => signOut(),
  });
  session.shell = shell;

  // Before the shell replaces #app, not after: the backdrop lives outside #app and would
  // otherwise keep painting for the rest of the session.
  if (detachLogin) {
    detachLogin();
    detachLogin = null;
  }

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

  route(
    "/entry",
    () => renderEntry(session.shell.screen, { session, navigate }),
    { tab: "entry", role: "attendant" },
  );

  const shiftScreen = (fn) => (params) =>
    fn(session.shell.screen, { session, navigate, shiftId: params.shiftId });

  // A shift stops being "current" the moment it closes, so this is the only page that can
  // still reach it -- to lock it, reopen it, or just look. See today.js's renderShiftById.
  route("/shifts/:shiftId", shiftScreen(renderShiftById), {
    tab: "today",
    role: "manager",
  });

  route("/shifts/:shiftId/collections", shiftScreen(renderCollections), {
    tab: "entry",
    role: "attendant",
  });
  route("/shifts/:shiftId/expenses", shiftScreen(renderExpenses), {
    tab: "entry",
    role: "attendant",
  });
  route("/shifts/:shiftId/non-fuel-sales", shiftScreen(renderNonFuelSales), {
    tab: "entry",
    role: "attendant",
  });
  route("/shifts/:shiftId/credit-sales", shiftScreen(renderCreditSales), {
    tab: "entry",
    role: "attendant",
  });
  route("/shifts/:shiftId/credit-repayments", shiftScreen(renderCreditRepayments), {
    tab: "entry",
    role: "attendant",
  });
  route("/shifts/:shiftId/bank-deposits", shiftScreen(renderBankDeposits), {
    tab: "entry",
    role: "manager",
  });
  route("/shifts/:shiftId/cash-position", shiftScreen(renderCashPosition), {
    tab: "cash",
    role: "manager",
  });

  route(
    "/shifts/:shiftId/readings",
    (params) =>
      renderReadings(session.shell.screen, {
        session,
        navigate,
        shiftId: params.shiftId,
      }),
    { tab: "entry", role: "attendant" },
  );

  route(
    "/cash",
    () => renderCash(session.shell.screen, { session, navigate }),
    { tab: "cash", role: "manager" },
  );
  route(
    "/days",
    () => renderDays(session.shell.screen, { session, navigate }),
    { tab: "cash", role: "manager" },
  );
  route(
    "/days/:businessDate",
    (params) =>
      renderDay(session.shell.screen, {
        session,
        navigate,
        businessDate: params.businessDate,
      }),
    { tab: "cash", role: "manager" },
  );

  // Phase 15. One business date had two screens -- `/daily-summaries/{date}` for the stored
  // snapshot and `/reports/{date}` for the fuel and expense detail -- described from two
  // tables with no link between them. They merged into `/days/{date}`; these three keep every
  // link, bookmark and half-typed URL that predates the merge working, and `redirect` replaces
  // the history entry so the back button does not bounce off them.
  // Same shape as `adminScreen` below: params flow through untouched, so a route with a
  // `:customerId` gets it by name.
  const creditScreen = (fn) => (params) =>
    fn(session.shell.screen, { session, navigate, ...params });

  // Phase 16 -- the Credit tab. Manager floor to read; the opening-balances screen is
  // admin, gated by `setBeforeEach` above as well as by the server (§8: "hiding a button is
  // UX, not a control"). Registered before "/credit/customers/:customerId" is irrelevant
  // here -- none of these four patterns can swallow another, since "customers",
  // "repayments" and "opening-balances" sit at the same depth under distinct literals.
  route("/credit", creditScreen(renderCreditHub), { tab: "credit", role: "manager" });
  route("/credit/repayments", creditScreen(renderLedgerRepayments), {
    tab: "credit",
    role: "manager",
  });
  route("/credit/opening-balances", creditScreen(renderOpeningBalances), {
    tab: "credit",
    role: "admin",
  });
  route("/credit/customers/:customerId", creditScreen(renderLedger), {
    tab: "credit",
    role: "manager",
  });

  route("/daily-summaries", () => redirect("#/days"), { tab: "cash", role: "manager" });
  route("/daily-summaries/:businessDate", (params) => redirect(`#/days/${params.businessDate}`), {
    tab: "cash",
    role: "manager",
  });

  // Phase 13. **"/reports/alerts" is registered before "/reports/:businessDate"**, and the
  // order is load-bearing: router.js's resolve() walks `routes` in registration order and
  // takes the first regex that matches, so the parameterised pattern would otherwise swallow
  // "alerts" as a business date. The screen would then request
  // GET /reports/daily/alerts and show a 422 instead of the alerts list -- the same
  // static-before-parameterised hazard app/api/v1/router.py calls out on the server side.
  route(
    "/reports",
    (_params, query) => renderReports(session.shell.screen, { session, navigate }, query),
    { tab: "cash", role: "manager" },
  );
  route(
    "/reports/alerts",
    (_params, query) => renderAlerts(session.shell.screen, { session, navigate }, query),
    { tab: "cash", role: "manager" },
  );
  route("/reports/:businessDate", (params) => redirect(`#/days/${params.businessDate}`), {
    tab: "cash",
    role: "manager",
  });

  const adminScreen = (fn, extra = {}) => (params) =>
    fn(session.shell.screen, { session, navigate, ...extra, ...params });

  route("/admin", adminScreen(renderAdmin), { tab: "admin", role: "admin" });
  route("/admin/fuel-types", adminScreen(renderFuelTypes), { tab: "admin", role: "admin" });
  route("/admin/nozzles", adminScreen(renderNozzles), { tab: "admin", role: "admin" });
  route("/admin/prices", adminScreen(renderPricing, { kind: "price" }), {
    tab: "admin",
    role: "admin",
  });
  route("/admin/margins", adminScreen(renderPricing, { kind: "margin" }), {
    tab: "admin",
    role: "admin",
  });
  route("/admin/categories", adminScreen(renderCategories), { tab: "admin", role: "admin" });
  route("/admin/customers", adminScreen(renderCustomers), { tab: "admin", role: "admin" });
  // Phase 16. The ledger moved to the Credit tab, where a manager can reach it -- it lived
  // under Admin only because that was the one screen that had ever shown a balance. This
  // keeps every existing link and bookmark working, and `redirect` replaces the history
  // entry so the back button does not bounce off it.
  route(
    "/admin/customers/:customerId/ledger",
    (params) => redirect(`#/credit/customers/${params.customerId}`),
    { tab: "credit", role: "manager" },
  );
  route("/admin/shift-templates", adminScreen(renderShiftTemplates), {
    tab: "admin",
    role: "admin",
  });
  route("/admin/users", adminScreen(renderUsers), { tab: "admin", role: "admin" });
  route("/admin/audit", adminScreen(renderAuditLogs), { tab: "admin", role: "admin" });

  // The two links the Cash hub was already offering, which reached the not-found route
  // until now (noted in Step 11's message).
  route(
    "/salesmen/:salesmanId/ledger",
    (params) =>
      renderShortfallLedger(session.shell.screen, {
        session,
        navigate,
        salesmanId: params.salesmanId,
      }),
    { tab: "cash", role: "manager" },
  );
  route(
    "/expenses/flagged",
    () => renderFlaggedExpenses(session.shell.screen, { session, navigate }),
    { tab: "cash", role: "manager" },
  );

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
