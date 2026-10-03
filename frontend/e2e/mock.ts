/* A signed-in session against a mocked API, for the smoke suite (Phase 23 D10).
 *
 * Nothing here reaches a real server or a real Supabase project. The browser is given a refresh
 * token before the page loads, so the app's own boot sequence (auth-config, token refresh, /me,
 * /client-config) runs exactly as it does in production -- against routes that answer with the
 * typed fixtures. Any GET without a fixture answers 404 in the API's own envelope, so a screen
 * that calls an endpoint the test forgot to describe shows its real error state rather than
 * hanging.
 */

import type { Page, Route } from "@playwright/test";
import * as fixture from "./fixtures";

const SUPABASE = "https://e2e-test.supabase.co";

/** A body, a Failure, or -- where one path answers differently by query string -- a function
 * of the request URL. */
export type Responses = Record<string, unknown>;

/** A response that is the API's error envelope rather than a 200, e.g. 404 NO_OPEN_SHIFT. */
export class Failure {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly detail = code,
  ) {}
}

export interface MockOptions {
  role?: "attendant" | "manager" | "admin";
  /** `"GET /path"` (path below /api/v1, no query string) -> JSON body. */
  responses?: Responses;
}

export interface Recorded {
  method: string;
  path: string;
  body: unknown;
  headers: Record<string, string>;
}

function json(route: Route, status: number, body: unknown) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

export async function signedIn(page: Page, { role = "admin", responses = {} }: MockOptions = {}): Promise<Recorded[]> {
  const writes: Recorded[] = [];

  await page.addInitScript(() => sessionStorage.setItem("hisahab.refresh", "e2e-refresh-token"));

  await page.route(`${SUPABASE}/auth/v1/token**`, (route) =>
    json(route, 200, { access_token: "e2e-access-token", refresh_token: "e2e-refresh-token" }),
  );

  await page.route("**/api/v1/**", (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api\/v1/, "");
    const key = `${request.method()} ${path}`;

    if (key === "GET /auth-config") return json(route, 200, { supabase_url: SUPABASE, supabase_anon_key: "e2e-anon" });
    if (key === "GET /me") return json(route, 200, fixture.me(role));
    if (key === "GET /client-config") return json(route, 200, fixture.clientConfig);

    if (request.method() !== "GET") {
      writes.push({ method: request.method(), path, body: request.postDataJSON(), headers: request.headers() });
    }
    if (key in responses) {
      let answer = responses[key];
      if (typeof answer === "function") answer = (answer as (url: URL) => unknown)(url);
      if (answer instanceof Failure) {
        return json(route, answer.status, { detail: answer.detail, code: answer.code, request_id: "e2e" });
      }
      return json(route, 200, answer);
    }
    return json(route, 404, { detail: `No fixture for ${key}`, code: "E2E_NO_FIXTURE", request_id: "e2e" });
  });

  return writes;
}

/** Everything the Cash tab reads, over the three-day scenario in fixtures.ts. */
export function cashResponses(): Responses {
  return {
    ...todayResponses("open"),
    "GET /shifts": fixture.shiftPage,
    "GET /daily-summaries": fixture.summaryPage,
    "GET /reports/range": fixture.rangeReport,
    "GET /reports/variance-alerts": fixture.alerts,
    "GET /salesman-shortfalls/outstanding": fixture.shortfallOutstanding,
    [`GET /salesman-shortfalls/${fixture.SALESMAN_ID}/ledger`]: fixture.shortfallLedger,
    "GET /expenses/flagged": fixture.flaggedPage,
    [`GET /reports/daily/${fixture.PREVIOUS_DATE}`]: { ...fixture.dailyReport, business_date: fixture.PREVIOUS_DATE, shifts: [] },
  };
}

/** Everything a manager's Today screen reads for an open shift. */
export function todayResponses(status: "open" | "closed" | "locked" = "open"): Responses {
  const id = fixture.SHIFT_ID;
  return {
    ...(status === "open" ? { "GET /shifts/current": fixture.shift("open") } : {}),
    [`GET /shifts/${id}`]: fixture.shift(status),
    [`GET /shifts/${id}/sales`]: fixture.sales,
    [`GET /shifts/${id}/collections`]: fixture.collections,
    [`GET /shifts/${id}/expenses`]: fixture.expenses,
    [`GET /shifts/${id}/credit-sales`]: fixture.creditSales,
    [`GET /shifts/${id}/credit-repayments`]: fixture.repayments,
    [`GET /shifts/${id}/bank-deposits`]: fixture.deposits,
    "GET /credit-customers": fixture.customers,
    [`GET /reports/daily/${fixture.BUSINESS_DATE}`]: fixture.dailyReport,
    [`GET /shifts/${id}/readings`]: fixture.worksheet,
    [`GET /shifts/${id}/non-fuel-sales`]: fixture.nonFuelSales,
    [`GET /shifts/${id}/cash-position`]: fixture.cashPosition,
    "GET /expense-categories": fixture.categories,
    "GET /fuel-types": fixture.fuelTypes,
  };
}

/** Everything the Credit tab and its Bank screens read. */
export function creditResponses(): Responses {
  return {
    "GET /credit-opening-balances": fixture.openingBalances,
    "GET /credit-customers": [...fixture.customers, ...fixture.moreCustomers],
    [`GET /credit-customers/${fixture.CUSTOMER_ID}`]: fixture.customerDetail,
    [`GET /credit-customers/${fixture.CUSTOMER_ID}/ledger`]: fixture.customerLedger,
    "GET /credit-repayments": fixture.datedRepayments,
    "GET /credit-customers/statement": fixture.statement,
    "GET /bank-accounts": fixture.bankAccounts,
    "GET /bank-statements/imports": fixture.imports,
    "GET /bank-transactions": (url: URL) =>
      url.searchParams.get("direction") === "debit" ? fixture.debitLines : fixture.creditLines,
    "GET /bank-statements/reconciliation": fixture.reconciliation,
  };
}
