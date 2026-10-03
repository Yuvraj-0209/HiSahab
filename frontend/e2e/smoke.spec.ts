/* Every screen renders under the production CSP, in both palettes, with nothing in the console
 * (Phase 23 D10, CLAUDE.md §13.18).
 *
 * A CSP violation is collected inside the page (`securitypolicyviolation`), because the
 * console wording differs by browser and a missed match would make the check vacuous.
 */

import { expect, type Page, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { CUSTOMER_2, CUSTOMER_ID, PREVIOUS_DATE, RECONCILED_DATE, SALESMAN_ID, SHIFT_ID, worksheet } from "./fixtures";
import { cashResponses, creditResponses, Failure, signedIn, todayResponses } from "./mock";

async function watch(page: Page) {
  const problems: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") problems.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (event) => {
      console.error(`CSP violation: ${event.violatedDirective} ${event.blockedURI}`);
    });
  });
  return problems;
}

/** Wait for every entrance to finish. Measuring contrast mid-fade measures a blend that no one
 * ever reads, and reports it as a failure. */
async function settle(page: Page) {
  const still = () =>
    page.waitForFunction(
      () =>
        document.querySelector("[aria-busy='true']") === null &&
        Array.from(document.querySelectorAll<HTMLElement>("main, main *, [data-arrive]")).every(
          (node) => getComputedStyle(node).opacity === "1",
        ),
    );
  // Twice, half a second apart: the first pass can land while a loading skeleton is on screen,
  // before the data arrives and the real entrance begins.
  await still();
  await page.waitForTimeout(500);
  await still();
}

async function expectAccessible(page: Page) {
  await settle(page);
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  // Each failing element named with axe's own explanation (it carries the measured contrast
  // ratio), so a failure says what to fix rather than only that something is wrong.
  const summary = results.violations.flatMap((v) =>
    v.nodes.map((node) => `${v.id} at ${node.target.join(" ")}: ${node.failureSummary?.replace(/\s+/g, " ")}`),
  );
  expect(summary).toEqual([]);
}

test("login renders with no console errors", async ({ page }) => {
  const problems = await watch(page);
  await page.route("**/api/v1/auth-config", (route) =>
    route.fulfill({ json: { supabase_url: "https://e2e-test.supabase.co", supabase_anon_key: "e2e-anon" } }),
  );
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
  await expectAccessible(page);
  expect(problems).toEqual([]);
});

test("today: an open shift, as a manager", async ({ page }, info) => {
  const problems = await watch(page);
  await signedIn(page, { role: "admin", responses: todayResponses("open") });
  await page.goto("/#/today");
  await expect(page.getByRole("button", { name: "Close shift" })).toBeVisible();
  await expect(page.getByText("₹3,02,827.45").first()).toBeVisible();
  await settle(page);
  await page.screenshot({ path: info.outputPath("today.png"), fullPage: true });
  await expectAccessible(page);
  expect(problems).toEqual([]);
});

test("today: no open shift", async ({ page }) => {
  const problems = await watch(page);
  await signedIn(page, {
    role: "manager",
    responses: {
      "GET /shifts/current": new Failure(404, "NO_OPEN_SHIFT"),
      "GET /shifts": { items: [], next_cursor: null },
    },
  });
  await page.goto("/#/today");
  await expect(page.getByRole("button", { name: "Open a shift" })).toBeVisible();
  await expectAccessible(page);
  // The browser logs the 404 for /shifts/current as a failed resource; that 404 is the API
  // saying "no open shift", which is the state under test.
  expect(problems.filter((p) => !p.includes("404"))).toEqual([]);
});

test("entry: the hub lists the register's lines", async ({ page }) => {
  const problems = await watch(page);
  await signedIn(page, { role: "attendant", responses: todayResponses("open") });
  await page.goto("/#/entry");
  await expect(page.getByRole("button", { name: /Nozzle readings/ })).toBeVisible();
  // Bank deposits sit at the manager floor (§8); an attendant is not offered them.
  await expect(page.getByRole("button", { name: /Bank deposits/ })).toHaveCount(0);
  await expectAccessible(page);
  expect(problems).toEqual([]);
});

test("readings: an opening is confirmed by a deliberate act, and testing is an answer", async ({ page }, info) => {
  const problems = await watch(page);
  const writes = await signedIn(page, {
    role: "attendant",
    responses: { ...todayResponses("open"), [`POST /shifts/${SHIFT_ID}/readings`]: worksheet.lines[0]?.reading },
  });
  await page.goto(`/#/shifts/${SHIFT_ID}/readings`);
  await settle(page);
  await page.screenshot({ path: info.outputPath("readings.png"), fullPage: true });

  await page.getByRole("button", { name: /CBG-1/ }).click();
  const sheet = page.getByRole("dialog", { name: "CBG-1" });
  // Nothing is pre-confirmed: the chained opening is shown as text, and the only way forward is
  // one of the explicit choices. No form, no Save button, until a person picks one (§4.7, §14).
  await expect(sheet.getByText("61822.10")).toBeVisible();
  await expect(sheet.getByRole("button", { name: "Save reading" })).toHaveCount(0);

  await sheet.getByRole("button", { name: "The meter reads exactly this" }).click();
  await expect(sheet.getByLabel(/Testing quantity \(kg\)/)).toHaveValue("");

  // Blank testing is refused client-side: the server would default it to 0 (§4.2).
  await sheet.getByRole("button", { name: "Save reading" }).click();
  await expect(sheet.getByText("Zero is a valid answer; blank is not.")).toBeVisible();
  expect(writes).toHaveLength(0);

  await sheet.getByLabel(/Testing quantity/).fill("0");
  await sheet.getByLabel("Closing reading").fill("62034.50");
  await sheet.getByRole("button", { name: "Save reading" }).click();
  await expect(sheet).toHaveCount(0);

  expect(writes).toHaveLength(1);
  expect(writes[0]?.body).toEqual({
    nozzle_id: worksheet.lines[1]?.nozzle_id,
    opening_confirmed: true,
    testing_quantity: "0",
    closing_reading: "62034.50",
    rollover_occurred: false,
    meter_reset_occurred: false,
  });
  // A reading is idempotent by its unique key; it carries no Idempotency-Key (§6.10).
  expect(writes[0]?.headers["idempotency-key"]).toBeUndefined();
  expect(problems).toEqual([]);
});

/* Every Entry-tab screen renders cleanly for a manager, in both palettes. */
for (const [name, path] of [
  ["collections", "collections"],
  ["expenses", "expenses"],
  ["non-fuel sales", "non-fuel-sales"],
  ["credit sales", "credit-sales"],
  ["credit repayments", "credit-repayments"],
  ["bank deposits", "bank-deposits"],
  ["cash position", "cash-position"],
] as const) {
  test(`renders: ${name}`, async ({ page }, info) => {
    const problems = await watch(page);
    await signedIn(page, { role: "admin", responses: todayResponses("open") });
    await page.goto(`/#/shifts/${SHIFT_ID}/${path}`);
    // The screen's own title, in the chrome: present on every screen, including an empty one.
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await settle(page);
    await page.screenshot({ path: info.outputPath(`${path}.png`), fullPage: true });
    await expectAccessible(page);
    expect(problems).toEqual([]);
  });
}

test("collections: cash is an answer, and a declaration carries an Idempotency-Key", async ({ page }) => {
  const problems = await watch(page);
  const writes = await signedIn(page, {
    role: "attendant",
    responses: {
      ...todayResponses("open"),
      [`GET /shifts/${SHIFT_ID}/collections`]: { items: [], declared_cash: null, totals_by_mode: {}, cash_basis: "declared", truncated: false },
      [`POST /shifts/${SHIFT_ID}/collections`]: { id: "new" },
    },
  });
  await page.goto(`/#/shifts/${SHIFT_ID}/collections`);
  // null is shown as words, never ₹0.00 (§6.8).
  await expect(page.getByText("not declared").first()).toBeVisible();

  await page.getByRole("button", { name: "Declare cash" }).click();
  const sheet = page.getByRole("dialog", { name: "Declare cash" });
  await sheet.getByRole("button", { name: "Declare" }).click();
  await expect(sheet.getByText("Enter an amount. Zero is valid.")).toBeVisible();
  expect(writes).toHaveLength(0);

  await sheet.getByLabel("Cash amount").fill("0");
  await sheet.getByRole("button", { name: "Declare" }).click();
  await expect(sheet).toHaveCount(0);
  expect(writes[0]?.body).toEqual({ mode: "cash", amount: "0" });
  expect(writes[0]?.headers["idempotency-key"]).toMatch(/^[0-9a-f-]{36}$/);
  expect(problems).toEqual([]);
});

test("credit sales: nothing can be saved without a receipt", async ({ page }) => {
  await signedIn(page, { role: "attendant", responses: todayResponses("open") });
  await page.goto(`/#/shifts/${SHIFT_ID}/credit-sales`);
  await page.getByRole("button", { name: "Add" }).click();
  const sheet = page.getByRole("dialog", { name: "Record udhaar" });
  await expect(sheet.getByRole("button", { name: "Record credit sale" })).toBeDisabled();
  await expect(sheet.getByText("A receipt is required before this can be saved.")).toBeVisible();
  // The admin-only override field is not offered to an attendant (§6.6).
  await expect(sheet.getByLabel(/override reason/)).toHaveCount(0);
});

test("expenses: crossing the receipt threshold is warned about before the server refuses", async ({ page }) => {
  await signedIn(page, { role: "attendant", responses: todayResponses("open") });
  await page.goto(`/#/shifts/${SHIFT_ID}/expenses`);
  await page.getByRole("button", { name: "Add" }).click();
  const sheet = page.getByRole("dialog", { name: "Record an expense" });
  await sheet.getByLabel("Category").selectOption({ label: "Tea" });
  await sheet.getByLabel("Amount").fill("5000.00");
  await expect(sheet.getByText("No receipt is required for this expense")).toBeVisible();
  // One paisa over the configured threshold (§6.11's strict >), compared as strings.
  await sheet.getByLabel("Amount").fill("5000.01");
  await expect(sheet.getByText(/A receipt is required for this expense/)).toBeVisible();
});

test("cash position: the gap carries its word, and booking never names the salesman", async ({ page }) => {
  const problems = await watch(page);
  const writes = await signedIn(page, {
    role: "manager",
    responses: { ...todayResponses("open"), [`POST /shifts/${SHIFT_ID}/shortfalls`]: { id: "sf-1" } },
  });
  await page.goto(`/#/shifts/${SHIFT_ID}/cash-position`);
  await expect(page.getByText("₹500.00 · short")).toBeVisible();

  await page.getByRole("button", { name: "Book a shortfall" }).click();
  const sheet = page.getByRole("dialog", { name: "Book a shortfall" });
  // Pre-filled with the computed gap, and still editable (§5.2).
  await expect(sheet.getByLabel("Amount to book")).toHaveValue("500.00");
  await sheet.getByLabel("Why is this being booked?").fill("Counted twice, still ₹500 short");
  await sheet.getByRole("button", { name: "Book shortfall" }).click();
  await expect(sheet).toHaveCount(0);
  expect(writes[0]?.body).toEqual({ amount: "500.00", reason: "Counted twice, still ₹500 short" });
  expect(problems).toEqual([]);
});

/* --- the Cash tab ----------------------------------------------------------------------- */

for (const [name, path] of [
  ["cash hub", "/#/cash"],
  ["days", "/#/days"],
  ["one day", `/#/days/${PREVIOUS_DATE}`],
  ["reports", "/#/reports"],
  ["alerts", "/#/reports/alerts"],
  ["salesman ledger", `/#/salesmen/${SALESMAN_ID}/ledger`],
  ["flagged expenses", "/#/expenses/flagged"],
] as const) {
  test(`renders: ${name}`, async ({ page }, info) => {
    const problems = await watch(page);
    await signedIn(page, { role: "admin", responses: cashResponses() });
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await settle(page);
    await page.screenshot({ path: info.outputPath(`${name.replace(/ /g, "-")}.png`), fullPage: true });
    await expectAccessible(page);
    expect(problems).toEqual([]);
  });
}

test("cash: only the oldest unreconciled day can be reconciled, and reconciling writes the day", async ({ page }) => {
  const problems = await watch(page);
  const writes = await signedIn(page, {
    role: "manager",
    responses: { ...cashResponses(), "POST /daily-summaries": { business_date: PREVIOUS_DATE } },
  });
  await page.goto("/#/cash");
  const reconcile = page.getByRole("button", { name: "Reconcile this day" });
  // One day waits on reconciliation (1 Oct); 30 Sep is reconciled already. Exactly one button.
  await expect(reconcile).toHaveCount(1);
  // §6.5's variance is counted − expected: −₹200 is a shortage, and says so in words.
  await expect(page.getByText("−₹200.00 · short").first()).toBeVisible();

  await reconcile.click();
  await expect(page).toHaveURL(new RegExp(`#/days/${PREVIOUS_DATE}$`));
  expect(writes).toEqual([
    expect.objectContaining({ method: "POST", path: "/daily-summaries", body: { business_date: PREVIOUS_DATE } }),
  ]);
  // Naturally idempotent by UNIQUE (outlet_id, business_date): no key (§6.10).
  expect(writes[0]?.headers["idempotency-key"]).toBeUndefined();
  expect(problems).toEqual([]);
});

test("day: a fuel with no commission is unknown, never zero (§13.21)", async ({ page }) => {
  await signedIn(page, { role: "manager", responses: cashResponses() });
  await page.goto(`/#/days/${PREVIOUS_DATE}`);
  await expect(page.getByText("no commission entered")).toBeVisible();
  await expect(page.getByText(/no dealer commission has been entered for CBG/)).toBeVisible();
  // The combined margin is withheld, as a word, rather than a partial total.
  const marginRow = page.getByText("Gross fuel margin", { exact: true }).locator("xpath=../..");
  await expect(marginRow.getByText("not known")).toBeVisible();
});

test("old per-day links still land on the merged day screen (Phase 15)", async ({ page }) => {
  await signedIn(page, { role: "manager", responses: cashResponses() });
  await page.goto(`/#/daily-summaries/${PREVIOUS_DATE}`);
  await expect(page).toHaveURL(new RegExp(`#/days/${PREVIOUS_DATE}$`));
  await page.goto(`/#/reports/${RECONCILED_DATE}`);
  await expect(page).toHaveURL(new RegExp(`#/days/${RECONCILED_DATE}$`));
});

test("an attendant is turned away from the Cash tab, politely (§8)", async ({ page }) => {
  await signedIn(page, { role: "attendant", responses: cashResponses() });
  await page.goto("/#/cash");
  await expect(page).toHaveURL(/#\/today$/);
  // The toast, not the screen-reader live region that carries the same words on purpose.
  await expect(page.getByRole("status").filter({ hasText: "That section is not available for your role." })).toBeVisible();
});

/* --- the Credit tab --------------------------------------------------------------------- */

for (const [name, path] of [
  ["credit hub", "/#/credit"],
  ["customer ledger", `/#/credit/customers/${CUSTOMER_ID}`],
  ["bank repayments", "/#/credit/repayments"],
  ["opening balances", "/#/credit/opening-balances"],
  ["statement", "/#/credit/statement?from=2026-09-16&to=2026-09-30"],
  ["bank hub", "/#/credit/bank"],
  ["bank review", "/#/credit/bank/review"],
  ["reconciliation", "/#/credit/bank/reconciliation"],
] as const) {
  test(`renders: ${name}`, async ({ page }, info) => {
    const problems = await watch(page);
    await signedIn(page, { role: "admin", responses: creditResponses() });
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await settle(page);
    await page.screenshot({ path: info.outputPath(`${name.replace(/ /g, "-")}.png`), fullPage: true });
    await expectAccessible(page);
    expect(problems).toEqual([]);
  });
}

test("statement: an opening balance nobody entered reads as unknown, never as ₹0.00 (§6.8)", async ({ page }) => {
  await signedIn(page, { role: "manager", responses: creditResponses() });
  await page.goto("/#/credit/statement?from=2026-09-16&to=2026-09-30");
  const sandhu = page.locator(".statement-customer").filter({ hasText: "Sandhu Dairy" });
  await expect(sandhu.getByText("not entered")).toBeVisible();
  await expect(page.getByText(/1 customer has no opening balance entered/)).toBeVisible();
  // The totals arrive summed from the server, and are rendered as received.
  await expect(page.locator(".statement-totals").getByText("₹39,110.00").first()).toBeVisible();
});

test("bank review: money already on a ledger is never offered again, and nothing is pre-ticked", async ({ page }) => {
  const problems = await watch(page);
  const writes = await signedIn(page, {
    role: "manager",
    responses: {
      ...creditResponses(),
      "POST /bank-transactions/confirm-repayments": { created: ["dr-9"], failed: [] },
    },
  });
  await page.goto("/#/credit/bank/review");

  // A line matching a payment typed in by hand, and one matching two of them: neither can be
  // recorded, because recording would count the money twice.
  await expect(page.getByText("already on the ledger")).toBeVisible();
  await expect(page.getByText("matches more than one")).toBeVisible();
  const ticks = page.getByLabel("Record this repayment");
  await expect(ticks).toHaveCount(1);
  // The remembered sender pre-selects the customer; the tick still starts empty (§5.3a).
  await expect(page.getByLabel("Who sent it")).toHaveValue(CUSTOMER_ID);
  await expect(ticks).not.toBeChecked();

  await ticks.check();
  await page.getByRole("button", { name: "Record ticked repayments" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Recorded 1 repayment." })).toBeVisible();
  expect(writes[0]?.body).toEqual({ items: [{ transaction_id: "tx-proposed", credit_customer_id: CUSTOMER_ID, remember_sender: true }] });
  // The Phase 20 bug: this POST's key used to be dropped, and the server refused every record.
  expect(writes[0]?.headers["idempotency-key"]).toMatch(/^[0-9a-f-]{36}$/);
  expect(problems).toEqual([]);
});

test("bank review: whether a debit was a cost is the person's answer", async ({ page }) => {
  const writes = await signedIn(page, {
    role: "manager",
    responses: { ...creditResponses(), "PATCH /bank-transactions/tx-iocl": {} },
  });
  await page.goto("/#/credit/bank/review");
  const iocl = page.locator("[data-arrive]").filter({ hasText: "INDIAN OIL CORPORATION" });
  await expect(iocl.getByText("Money moved between your own accounts, not spent.")).toBeVisible();
  await iocl.getByRole("button", { name: "Not an expense" }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toEqual(expect.objectContaining({ method: "PATCH", path: "/bank-transactions/tx-iocl", body: { is_expense: "no" } }));
});

test("reconciliation: a day Paytm has not paid is pending, never a ₹0.00 discrepancy", async ({ page }) => {
  await signedIn(page, { role: "manager", responses: creditResponses() });
  await page.goto("/#/credit/bank/reconciliation");
  await expect(page.getByText(/Nothing has arrived yet for this day/)).toBeVisible();
  await expect(page.getByText("pending")).toBeVisible();
  await expect(page.getByText("−₹500.00").first()).toBeVisible();
  await expect(page.getByText("In the app, never reached the bank")).toBeVisible();
});

test("opening balances: zero is a deliberate answer, dated on its face, and sends 0.00", async ({ page }) => {
  const writes = await signedIn(page, {
    role: "admin",
    responses: { ...creditResponses(), "POST /credit-opening-balances": {} },
  });
  await page.goto("/#/credit/opening-balances");
  await page.getByRole("button", { name: "Enter what they owed" }).click();
  const sheet = page.getByRole("dialog", { name: "Sandhu Dairy" });
  // The date defaults to the ledger's start, not to today: the batch shares one date.
  await expect(sheet.getByLabel("As of")).toHaveValue("2026-07-01");
  await sheet.getByRole("button", { name: "They owed nothing on 1 Jul 2026" }).click();
  await expect(sheet).toHaveCount(0);
  expect(writes[0]?.body).toEqual({ credit_customer_id: CUSTOMER_2, amount: "0.00", as_of_date: "2026-07-01" });
});

test("a manager is turned away from opening balances, which only an admin may enter (§8)", async ({ page }) => {
  await signedIn(page, { role: "manager", responses: creditResponses() });
  await page.goto("/#/credit/opening-balances");
  await expect(page).toHaveURL(/#\/today$/);
});

test("old admin ledger links land on the Credit tab (Phase 16)", async ({ page }) => {
  await signedIn(page, { role: "manager", responses: creditResponses() });
  await page.goto(`/#/admin/customers/${CUSTOMER_ID}/ledger`);
  await expect(page).toHaveURL(new RegExp(`#/credit/customers/${CUSTOMER_ID}$`));
});
