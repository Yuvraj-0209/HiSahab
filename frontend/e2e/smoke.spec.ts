/* Every screen renders under the production CSP, in both palettes, with nothing in the console
 * (Phase 23 D10, CLAUDE.md §13.18).
 *
 * A CSP violation is collected inside the page (`securitypolicyviolation`), because the
 * console wording differs by browser and a missed match would make the check vacuous.
 */

import { expect, type Page, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { CUSTOMER_2, CUSTOMER_ID, PREVIOUS_DATE, RECONCILED_DATE, SALESMAN_ID, SHIFT_ID, worksheet } from "./fixtures";
import { adminResponses, cashResponses, creditResponses, Failure, signedIn, summaryResponses, todayResponses } from "./mock";

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
 * ever reads, and reports it as a failure.
 *
 * Two kinds of motion to wait for (Phase 24 M3): GSAP's, which runs on its own ticker and is
 * invisible to the Web Animations API, so it is caught by its effect (opacity not yet 1); and
 * CSS's -- the screen entrance, view transitions -- which `document.getAnimations()` reports. An
 * infinite animation (the login photograph's slow drift) never finishes and is not waited on. */
async function settle(page: Page) {
  const still = () =>
    page.waitForFunction(
      () =>
        document.querySelector("[aria-busy='true']") === null &&
        Array.from(document.querySelectorAll<HTMLElement>("main, main *, [data-arrive]")).every(
          (node) => getComputedStyle(node).opacity === "1",
        ) &&
        document
          .getAnimations()
          // Only time-based animations finish. A scroll-driven one (the large titles) is tied to
          // the scroll position and is "running" for as long as the page exists.
          .filter((animation) => animation.timeline === document.timeline)
          .every((animation) => animation.playState !== "running" || animation.effect?.getComputedTiming().iterations === Infinity),
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
  await expect(page.locator("form").getByRole("button", { name: "Sign in" })).toBeVisible();
  await expectAccessible(page);
  expect(problems).toEqual([]);
});

/* --- the front door (Phase 24 D7) ---------------------------------------------------------- */

async function frontDoor(page: Page) {
  await page.route("**/api/v1/auth-config", (route) =>
    route.fulfill({ json: { supabase_url: "https://e2e-test.supabase.co", supabase_anon_key: "e2e-anon" } }),
  );
  await page.goto("/");
  await expect(page.locator("form").getByRole("button", { name: "Sign in" })).toBeVisible();
}

test("front door: the sign-in button is inside the first screen of a phone (§14)", async ({ page }) => {
  await frontDoor(page);
  const box = await page.locator("form").getByRole("button", { name: "Sign in" }).boundingBox();
  const viewport = page.viewportSize();
  expect(box && viewport && box.y + box.height <= viewport.height).toBe(true);
});

test("front door: under reduced motion every section is simply there, without scrolling", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const problems = await watch(page);
  await frontDoor(page);
  await expect(page.locator("[data-section]")).toHaveCount(3);
  const hidden = await page.evaluate(() =>
    Array.from(document.querySelectorAll<HTMLElement>("[data-section] *")).filter((node) => getComputedStyle(node).opacity !== "1").length,
  );
  expect(hidden).toBe(0);
  for (const label of ["Confirm the meter.", "The gap has a name.", "Every udhaar has a receipt.", "Bring HiSahab to your pump."]) {
    await expect(page.getByRole("heading", { name: label })).toBeAttached();
  }
  // Every figure on the page is labelled for what it is.
  expect(await page.getByText("Sample figures").count()).toBeGreaterThanOrEqual(5);
  expect(problems).toEqual([]);
});

test("installable: the manifest is served, starts on Today, and every icon it names exists (Phase 24 D8)", async ({ page }) => {
  const response = await page.request.get("/manifest.webmanifest");
  expect(response.ok()).toBe(true);
  const manifest = (await response.json()) as { start_url: string; display: string; icons: { src: string; purpose: string }[] };
  expect(manifest.start_url).toBe("/#/today");
  expect(manifest.display).toBe("standalone");
  expect(manifest.icons.some((icon) => icon.purpose === "maskable")).toBe(true);
  for (const icon of manifest.icons) {
    const image = await page.request.get(icon.src);
    expect(image.ok(), icon.src).toBe(true);
    expect(image.headers()["content-type"]).toBe("image/png");
  }
});

/** The scale GSAP has given the photograph's layer, read from its computed transform matrix. */
async function photoScale(page: Page): Promise<number> {
  return page.evaluate(() => {
    const layer = document.querySelector<HTMLElement>(".login-layer:not(.login-near)")!;
    return new DOMMatrixReadOnly(getComputedStyle(layer).transform).a;
  });
}

test("front door: scrolling walks into the station (Phase 25 D1)", async ({ page }) => {
  const problems = await watch(page);
  await frontDoor(page);
  expect(await photoScale(page)).toBeCloseTo(1, 1);
  const end = await page.evaluate(() => {
    const travel = document.querySelector<HTMLElement>("[data-travel]")!;
    return travel.offsetTop + travel.offsetHeight - window.innerHeight;
  });
  await page.mouse.wheel(0, Math.round(end * 0.7));
  await expect.poll(() => photoScale(page)).toBeGreaterThan(1.5);
  expect(problems).toEqual([]);
});

test("front door: on a phone each step of the day carries its own screen, inline (Phase 28 D3)", async ({ page }) => {
  const problems = await watch(page);
  await frontDoor(page);
  // No pinned phone here: a phone inside a phone is too small to read.
  await expect(page.locator("[data-story-step] [data-screen]")).toHaveCount(6);
  await expect(page.locator(".phone-story")).toHaveCount(0);
  const last = page.locator('[data-story-step="bank"] [data-screen]');
  await last.scrollIntoViewIfNeeded();
  await expect(last.getByText("Sample figures")).toBeVisible();
  expect(problems).toEqual([]);
});

test("front door: the page never ends without a way in -- Sign in goes back to the form (Phase 28 D3)", async ({ page }) => {
  const problems = await watch(page);
  await frontDoor(page);
  const close = page.locator('[data-section="close"]');
  await close.scrollIntoViewIfNeeded();
  // Present whether or not the owner has set a contact for "Talk to us".
  await close.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByLabel("Email")).toBeFocused();
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeLessThan(10);
  expect(problems).toEqual([]);
});

test("front door: 'How it works' scrolls down the page without leaving it (Phase 28 D3)", async ({ page }) => {
  const problems = await watch(page);
  await frontDoor(page);
  const route = await page.evaluate(() => location.hash);
  // A button, not an "#how" anchor: the app is hash-routed, so a hash is a route (§14).
  await page.getByRole("button", { name: "How it works" }).click();
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(400);
  expect(await page.evaluate(() => location.hash)).toBe(route);
  expect(problems).toEqual([]);
});

test("front door: the photograph's slow drift stops once the walk in starts (Phase 28 B3)", async ({ page }) => {
  await frontDoor(page);
  const drift = () => page.evaluate(() => getComputedStyle(document.querySelector(".login-drift")!).animationPlayState);
  expect(await drift()).toBe("running");
  // Two transforms on one photograph -- the breathing drift and the scrubbed push-in -- stack
  // into a zoom nobody asked for. Phase 25 D1 promised the drift would stop; it never did.
  await page.mouse.wheel(0, 600);
  await expect.poll(drift).toBe("paused");
});

test("front door: under reduced motion the photograph does not move when scrolled", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await frontDoor(page);
  await page.mouse.wheel(0, 900);
  await page.waitForTimeout(600);
  expect(await photoScale(page)).toBeCloseTo(1, 2);
});

test.describe("front door on a desktop", () => {
  test.use({ viewport: { width: 1280, height: 800 }, isMobile: false, hasTouch: false, deviceScaleFactor: 1 });

  test("the whole story scrolls through under the production CSP, with nothing in the console", async ({ page }) => {
    const problems = await watch(page);
    await frontDoor(page);
    await expect(page.locator("[data-section]")).toHaveCount(3);
    for (let step = 0; step < 60; step += 1) {
      await page.mouse.wheel(0, 400);
      await page.waitForTimeout(40);
    }
    await page.waitForTimeout(1500);
    await expect(page.getByRole("heading", { name: "Bring HiSahab to your pump." })).toBeInViewport();
    expect(problems).toEqual([]);
    await expectAccessible(page);
  });

  test("the hero shows the product beside the form, and the nav's Sign in puts the cursor in it (Phase 28 D3)", async ({ page }) => {
    const problems = await watch(page);
    await frontDoor(page);
    // The phone arrives with the story, once the page is idle; it is a picture, named by its caption.
    await expect(page.getByRole("figure", { name: /^The Today screen on/ })).toBeInViewport();
    await page.getByRole("navigation", { name: "Front door" }).getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByLabel("Email")).toBeFocused();
    expect(problems).toEqual([]);
  });

  test("one phone stays put and changes screen as each step of the day reaches the middle (Phase 28 D5)", async ({ page }) => {
    const problems = await watch(page);
    await frontDoor(page);
    const step = page.locator('[data-story-step="cash"]');
    await expect(step).toBeAttached();
    await step.evaluate((element) => {
      const box = element.getBoundingClientRect();
      window.scrollBy(0, box.top + box.height / 2 - window.innerHeight / 2);
    });
    // Fade through, not cross-fade: the step's screen is shown and the one before it is gone.
    await expect(page.locator('[data-screen="cash"]')).toHaveCSS("opacity", "1");
    await expect(page.locator('[data-screen="udhaar"]')).toHaveCSS("opacity", "0");
    await expect(step).toHaveAttribute("data-active", "true");
    await expect(page.locator('[data-story-step="meter"]')).toHaveAttribute("data-active", "false");
    // The phone is held in the window, not left behind with the step above it.
    await expect(page.locator(".phone-story")).toBeInViewport({ ratio: 1 });
    await expect(page.getByRole("figure", { name: /collections: cash counted/ })).toBeAttached();
    expect(problems).toEqual([]);
  });

  test("under reduced motion every step's screen is simply there, even on a wide screen (Phase 28 D3)", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await frontDoor(page);
    // A screen that swaps by itself is motion by another name: each step shows its own instead.
    await expect(page.locator("[data-story-step] [data-screen]")).toHaveCount(6);
    await expect(page.locator("[data-story-step][data-active='false']")).toHaveCount(0);
  });

  test("a headline below the fold waits lowered, then rises as it arrives (Phase 28 B1)", async ({ page }) => {
    await frontDoor(page);
    const last = page.getByRole("heading", { name: "Bring HiSahab to your pump." });
    await expect(last).toBeAttached();
    // Each word sits inside a clipping box; until its headline arrives it is lowered out of view.
    // The story queried an attribute nothing set, so every headline was simply there, static.
    const lowered = () => last.locator("[data-word]").first().evaluate((word) => new DOMMatrixReadOnly(getComputedStyle(word).transform).f);
    await expect.poll(lowered).toBeGreaterThan(0);
    await last.scrollIntoViewIfNeeded();
    await expect.poll(lowered).toBe(0);
  });

  test("every sample figure says so on a wide screen too (Phase 28 B2, §14)", async ({ page }) => {
    await frontDoor(page);
    await expect(page.getByRole("heading", { name: "Bring HiSahab to your pump." })).toBeAttached();
    const labels = page.getByText("Sample figures");
    const count = await labels.count();
    expect(count).toBeGreaterThanOrEqual(3);
    // Visible, not merely present: a label hidden at this width labels nothing.
    for (let index = 0; index < count; index += 1) await expect(labels.nth(index)).toBeVisible();
  });

  test("nothing but the page's own scroll moves the sign-in form (Phase 28 B4, §14)", async ({ page }) => {
    await frontDoor(page);
    await expect(page.getByRole("heading", { name: "Bring HiSahab to your pump." })).toBeAttached();
    await page.mouse.wheel(0, 300);
    await page.waitForTimeout(400);
    const moved = await page.locator("form").evaluate((form) => {
      const transformed: string[] = [];
      for (let node = form.parentElement; node; node = node.parentElement) {
        if (getComputedStyle(node).transform !== "none") transformed.push(node.id || node.className);
      }
      return transformed;
    });
    expect(moved).toEqual([]);
  });
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

test("today: a card whose figures failed to load says so, never '…' and never 'not declared' (Phase 28 B5)", async ({ page }) => {
  const failed = new Failure(500, "INTERNAL_ERROR");
  await signedIn(page, {
    role: "admin",
    responses: {
      ...todayResponses("open"),
      [`GET /shifts/${SHIFT_ID}/collections`]: failed,
      [`GET /shifts/${SHIFT_ID}/expenses`]: failed,
      [`GET /shifts/${SHIFT_ID}/credit-sales`]: failed,
      [`GET /shifts/${SHIFT_ID}/bank-deposits`]: failed,
    },
  });
  await page.goto("/#/today");
  await expect(page.getByText("₹3,02,827.45").first()).toBeVisible();
  // A failed read is not an answer. "not declared" would claim nobody counted the cash; "…"
  // would claim it is still coming, forever (§6.8: zero as an answer, never as an omission).
  for (const title of ["Collections", "Expenses", "Credit", "Bank deposits"]) {
    const card = page.getByRole("heading", { name: title, exact: true }).locator("xpath=ancestor::section[1]");
    await expect(card.getByText("not loaded"), title).toBeVisible({ timeout: 10_000 });
    await expect(card.getByText("…"), title).toHaveCount(0);
  }
  await expect(page.getByText("not declared")).toHaveCount(0);
});

test("today: a card opens its details from anywhere on it, not from a button at its foot (Phase 28 D6)", async ({ page }) => {
  const problems = await watch(page);
  await signedIn(page, { role: "admin", responses: todayResponses("open") });
  await page.goto("/#/today");
  const card = page.getByRole("heading", { name: "Collections", exact: true }).locator("xpath=ancestor::section[1]");
  await expect(card.getByRole("button", { name: "Details" })).toHaveCount(0);
  // Pressed where a row's label is drawn, well away from the title that carries the button. A
  // click at a position, as a thumb makes one: the stretched button lies over the whole card, so
  // the row underneath is not itself a target and Playwright would refuse to click it directly.
  await card.evaluate((element) => element.scrollIntoView({ block: "center" }));
  await page.waitForTimeout(300);
  const box = (await card.getByText("Card", { exact: true }).boundingBox())!;
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect(page.getByRole("dialog", { name: "Collections" })).toBeVisible();
  expect(problems).toEqual([]);
});

test("a shift opened by its id is titled as that shift, not as Today (Phase 28 B6)", async ({ page }) => {
  await signedIn(page, { role: "admin", responses: todayResponses("closed") });
  await page.goto(`/#/shifts/${SHIFT_ID}`);
  await expect(page.getByRole("button", { name: "Lock shift" })).toBeVisible();
  // A closed shift from last week is not "Today", and the tab title is what a reader sees first.
  await expect(page).toHaveTitle(/^Shift 1 · HiSahab$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(/^Shift 1/);
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

/* --- voiding an empty shift (Phase 27, §6.8) --------------------------------------------- */

/** Today's or the Cash tab's reads, with the shift disappearing once the void lands -- so the
 * screen is checked against what the server says afterwards, not against the button's hopes. */
function voidableResponses(base: Record<string, unknown>) {
  let voided = false;
  const open = base["GET /shifts/current"];
  return {
    ...base,
    "GET /shifts": { items: [], next_cursor: null },
    "GET /shifts/current": () => (voided ? new Failure(404, "NO_OPEN_SHIFT") : open),
    [`PATCH /shifts/${SHIFT_ID}/void`]: () => {
      voided = true;
      return { id: SHIFT_ID, business_date: "2026-10-02", sequence: 1 };
    },
  };
}

const VOID_REASON = "Opened by mistake before the earlier day was entered";

test("today: an admin voids an open shift with a reason, and Today then has no shift", async ({ page }) => {
  const problems = await watch(page);
  const writes = await signedIn(page, { role: "admin", responses: voidableResponses(todayResponses("open")) });
  await page.goto("/#/today");

  await page.getByRole("button", { name: "Void shift" }).click();
  const sheet = page.getByRole("dialog", { name: "Void shift" });
  await expect(sheet.getByText(/only while nothing has been recorded/)).toBeVisible();
  await sheet.getByLabel("Why is this being voided?").fill(VOID_REASON);
  await sheet.getByRole("button", { name: "Void shift" }).click();

  await expect(sheet).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Open a shift" })).toBeVisible();
  expect(writes).toEqual([expect.objectContaining({ method: "PATCH", path: `/shifts/${SHIFT_ID}/void`, body: { reason: VOID_REASON } })]);
  // The 404 is /shifts/current answering NO_OPEN_SHIFT after the void: the state under test.
  expect(problems.filter((p) => !p.includes("404"))).toEqual([]);
});

test("today: void is offered to an admin on an open shift only, never to a manager (§8)", async ({ page }) => {
  await signedIn(page, { role: "manager", responses: todayResponses("open") });
  await page.goto("/#/today");
  await expect(page.getByRole("button", { name: "Close shift" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Void shift" })).toHaveCount(0);
});

test("today: a closed shift offers reopen and lock to an admin, not void", async ({ page }) => {
  await signedIn(page, { role: "admin", responses: todayResponses("closed") });
  await page.goto(`/#/shifts/${SHIFT_ID}`);
  await expect(page.getByRole("button", { name: "Reopen shift" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Void shift" })).toHaveCount(0);
});

test("cash: an admin voids the open shift from the Cash tab", async ({ page }) => {
  const problems = await watch(page);
  const writes = await signedIn(page, { role: "admin", responses: voidableResponses(cashResponses()) });
  await page.goto("/#/cash");

  await page.getByRole("button", { name: "Void shift" }).click();
  const sheet = page.getByRole("dialog", { name: "Void shift" });
  await sheet.getByLabel("Why is this being voided?").fill(VOID_REASON);
  await sheet.getByRole("button", { name: "Void shift" }).click();

  await expect(sheet).toHaveCount(0);
  await expect(page.getByText("No shift is currently open.")).toBeVisible();
  expect(writes).toEqual([expect.objectContaining({ method: "PATCH", path: `/shifts/${SHIFT_ID}/void`, body: { reason: VOID_REASON } })]);
  expect(problems.filter((p) => !p.includes("404"))).toEqual([]);
});

test("cash: a manager sees the open shift but is not offered void (§8)", async ({ page }) => {
  await signedIn(page, { role: "manager", responses: cashResponses() });
  await page.goto("/#/cash");
  await expect(page.getByRole("button", { name: "Cash position" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Void shift" })).toHaveCount(0);
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

test.describe("large titles on a page too short to scroll (Phase 25 D4)", () => {
  test.use({ viewport: { width: 1440, height: 900 }, isMobile: false, hasTouch: false, deviceScaleFactor: 1 });

  test("the six tabs spread across the window instead of huddling in the middle (Phase 25 D2)", async ({ page }) => {
    await signedIn(page, { role: "admin", responses: todayResponses("open") });
    await page.goto("/#/entry");
    const tabs = page.getByRole("navigation", { name: "Sections" }).getByRole("button");
    await expect(tabs).toHaveCount(6);
    const first = await tabs.first().boundingBox();
    const last = await tabs.last().boundingBox();
    const span = last!.x + last!.width - first!.x;
    expect(span / 1440).toBeGreaterThan(0.9);
  });

  test("Today's six cards sit three across and two down on a monitor (Phase 25 D3)", async ({ page }) => {
    const problems = await watch(page);
    await signedIn(page, { role: "admin", responses: todayResponses("open") });
    await page.goto("/#/today");
    await expect(page.getByText("₹3,02,827.45").first()).toBeVisible();
    await settle(page);
    const boxes = await Promise.all(
      ["Metered sales", "Collections", "Expenses", "Credit", "Bank deposits", "Day cash"].map((title) =>
        page.getByRole("heading", { level: 2, name: title, exact: true }).locator("xpath=ancestor::*[@data-arrive][1]").boundingBox(),
      ),
    );
    const columns = new Set(boxes.map((box) => Math.round(box!.x)));
    const rows = new Set(boxes.map((box) => Math.round(box!.y)));
    expect([columns.size, rows.size]).toEqual([3, 2]);
    await expectAccessible(page);
    expect(problems).toEqual([]);
  });

  test("nozzle readings are big cards, two across on a monitor (Phase 25 D5)", async ({ page }) => {
    const problems = await watch(page);
    await signedIn(page, { role: "attendant", responses: todayResponses("open") });
    await page.goto(`/#/shifts/${SHIFT_ID}/readings`);
    const card = page.getByRole("button", { name: /CBG-1/ });
    await expect(card).toBeVisible();
    await settle(page);
    expect((await card.boundingBox())!.width).toBeGreaterThan(500);
    // A nozzle nobody has confirmed shows its chained value as carried forward, never as an opening.
    await expect(card.getByText("Carried forward")).toBeVisible();
    await expect(card.getByText("Confirm the opening")).toBeVisible();
    await expectAccessible(page);
    expect(problems).toEqual([]);
  });

  test("Entry's name is shown once, not twice", async ({ page }) => {
    await signedIn(page, { role: "admin", responses: todayResponses("open") });
    await page.goto("/#/entry");
    await expect(page.getByRole("button", { name: /Nozzle readings/ })).toBeVisible();
    await settle(page);
    // The page fits the window, so nothing scrolls -- the case the owner saw both titles in.
    expect(await page.evaluate(() => document.documentElement.scrollHeight <= window.innerHeight)).toBe(true);
    const shown = await page.evaluate(() => ({
      compact: getComputedStyle(document.querySelector(".compact-title")!).opacity,
      large: getComputedStyle(document.querySelector(".large-title")!).display,
    }));
    expect(shown).toEqual({ compact: "0", large: "block" });
  });

  test("on a page that scrolls, the chrome's copy still arrives once the large title has gone", async ({ page }) => {
    await signedIn(page, { role: "admin", responses: cashResponses() });
    await page.goto("/#/cash");
    await settle(page);
    const compact = () => page.evaluate(() => getComputedStyle(document.querySelector(".compact-title")!).opacity);
    expect(await compact()).toBe("0");
    await page.mouse.wheel(0, 400);
    await expect.poll(compact).toBe("1");
  });
});

test.describe("sheets on a desktop (Phase 25 D6)", () => {
  test.use({ viewport: { width: 1440, height: 900 }, isMobile: false, hasTouch: false, deviceScaleFactor: 1 });

  test("a sheet opens as a centred dialog, and locking the scroll cannot shift the page", async ({ page }) => {
    const problems = await watch(page);
    await signedIn(page, { role: "attendant", responses: todayResponses("open") });
    await page.goto(`/#/shifts/${SHIFT_ID}/readings`);
    // This browser draws a classic scrollbar, which is exactly the case that used to jump: the
    // page's width must be the same with the scroll locked as without.
    const width = () => page.evaluate(() => document.documentElement.clientWidth);
    const before = await width();
    await page.getByRole("button", { name: /CBG-1/ }).click();
    const dialog = page.getByRole("dialog", { name: "CBG-1" });
    await expect(dialog).toBeVisible();
    await expect.poll(() => dialog.evaluate((node) => getComputedStyle(node).opacity)).toBe("1");
    expect(await width()).toBe(before);
    const box = await dialog.boundingBox();
    const visible = await page.evaluate(() => ({ width: window.innerWidth, height: window.innerHeight }));
    expect(box).not.toBeNull();
    // Centred, not anchored to the bottom edge. Horizontally to within half a scrollbar: with a
    // stable gutter, fixed elements centre on the same column as the page beneath them.
    expect(Math.abs(box!.x + box!.width / 2 - visible.width / 2)).toBeLessThanOrEqual(8);
    expect(Math.abs(box!.y + box!.height / 2 - visible.height / 2)).toBeLessThan(2);
    // Focus landed inside the dialog, never left behind on the page.
    expect(await page.evaluate(() => document.querySelector("[role=dialog]")!.contains(document.activeElement))).toBe(true);
    await expectAccessible(page);
    expect(problems).toEqual([]);
  });
});

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

/** Record what every view transition did. A transition the browser aborts -- a duplicate
 * `view-transition-name` is the classic cause -- is skipped silently: the page still changes, and
 * nothing but this record would notice that the motion never happened. */
async function recordTransitions(page: Page) {
  await page.addInitScript(() => {
    const log: string[] = [];
    (window as unknown as { __transitions: string[] }).__transitions = log;
    const start = document.startViewTransition?.bind(document);
    if (!start) return;
    document.startViewTransition = ((update: ViewTransitionUpdateCallback) => {
      const transition = start(update);
      log.push("started");
      transition.ready.then(
        () => log.push("ready"),
        (error: Error) => log.push(`aborted: ${error.name}`),
      );
      transition.finished.then(() => log.push("finished"));
      return transition;
    }) as typeof document.startViewTransition;
  });
  return () => page.evaluate(() => (window as unknown as { __transitions: string[] }).__transitions);
}

test("navigation: a day row flies into its day, a tab change slides, and every transition completes (Phase 24 D3)", async ({ page }) => {
  const problems = await watch(page);
  const transitions = await recordTransitions(page);
  await signedIn(page, { role: "admin", responses: { ...cashResponses(), ...summaryResponses() } });
  await page.goto("/#/days");
  await settle(page);

  // A drill-down: the row's date is the shared element, the day screen's subtitle its landing.
  const row = page.locator("button:has([data-shared-source])").first();
  const date = (await row.locator("[data-shared-source]").textContent())?.trim();
  await row.click();
  await expect(page).toHaveURL(/#\/days\/\d{4}-\d{2}-\d{2}$/);
  await expect(page.getByRole("heading", { level: 1, name: "Day" })).toBeVisible();
  await expect(page.locator("[data-shared-target]")).toHaveText(date ?? "");
  await expect.poll(transitions).toEqual(["started", "ready", "finished"]);

  // A tab change to the right.
  await page.getByRole("navigation", { name: "Sections" }).getByRole("button", { name: "Summary" }).click();
  await expect.poll(transitions).toEqual(["started", "ready", "finished", "started", "ready", "finished"]);
  expect(problems).toEqual([]);
});

test("navigation: under reduced motion no transition is started at all (Phase 24 D3)", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const transitions = await recordTransitions(page);
  await signedIn(page, { role: "admin", responses: cashResponses() });
  await page.goto("/#/days");
  await page.locator("button:has([data-shared-source])").first().click();
  await expect(page.getByRole("heading", { level: 1, name: "Day" })).toBeVisible();
  expect(await transitions()).toEqual([]);
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

/* --- the Summary tab -------------------------------------------------------------------- */

test("renders: summary", async ({ page }, info) => {
  const problems = await watch(page);
  await signedIn(page, { role: "manager", responses: summaryResponses() });
  await page.goto("/#/summary");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await settle(page);
  await page.screenshot({ path: info.outputPath("summary.png"), fullPage: true });
  await expectAccessible(page);
  expect(problems).toEqual([]);
});

test("summary: a fuel with no commission withholds the combined margin, in words (§13.21)", async ({ page }) => {
  await signedIn(page, { role: "manager", responses: summaryResponses() });
  await page.goto("/#/summary");
  await expect(page.getByText("not knowable").first()).toBeVisible();
  await expect(page.getByText(/no dealer commission has been entered for CBG/)).toBeVisible();
  await expect(page.getByText("margin not entered")).toBeVisible();
  // Litres and kilograms side by side, never summed (§4.5).
  await expect(page.getByText("28135.200 L")).toBeVisible();
  await expect(page.getByText("1550.250 kg").first()).toBeVisible();
});

test("summary: every share is the server's string, assigned and never computed (§14)", async ({ page }) => {
  await signedIn(page, { role: "manager", responses: summaryResponses() });
  await page.goto("/#/summary");
  // The largest category's bar spans the chart; the next is the server's 36.45%, not a division.
  await expect(page.locator("[data-catbar]").first()).toHaveAttribute("style", /width: 100(\.00)?%/);
  await expect(page.locator("[data-catbar]").nth(1)).toHaveAttribute("style", /width: 36\.45%/);
  await expect(page.locator("[data-slice]")).toHaveCount(3);
  // The bridge's offsets and widths are assigned as sent: "collected" starts where it lands.
  await expect(page.locator('[data-bridge="collected"]')).toHaveAttribute("style", /left: 75\.75%; width: 24\.25%/);
});

test("summary: the money-arrived card is gone, and quantity sits in the headline (Phase 26)", async ({ page }) => {
  await signedIn(page, { role: "manager", responses: summaryResponses() });
  await page.goto("/#/summary");
  await expect(page.getByText("Non-fuel sales")).toBeVisible();
  await expect(page.getByText("Litres sold")).toBeVisible();
  await expect(page.getByText("Kilograms sold")).toBeVisible();
  await expect(page.getByText("How the money arrived")).toHaveCount(0);
  await expect(page.getByText("Quantity sold")).toHaveCount(0);
});

test("summary: the largest category's rows are listed by date before anything is chosen (Phase 26)", async ({ page }) => {
  await signedIn(page, { role: "manager", responses: summaryResponses() });
  await page.goto("/#/summary");
  const bars = page.getByRole("radiogroup", { name: "Expense categories" });
  await expect(bars.getByRole("radio", { name: /Salaries/ })).toHaveAttribute("aria-checked", "true");
  await expect(page.getByText("3 entries · 67.86% of all expenses")).toBeVisible();
  await expect(page.getByText("Ramesh", { exact: false }).first()).toBeVisible();
});

test("summary: choosing a category lists where its money went, reversals tagged (Phase 26)", async ({ page }) => {
  await signedIn(page, { role: "manager", responses: summaryResponses() });
  await page.goto("/#/summary");
  await page.getByRole("radio", { name: /Maintenance/ }).click();
  await expect(page.getByRole("radio", { name: /Maintenance/ })).toHaveAttribute("aria-checked", "true");
  await expect(page.getByText("Nozzle seal replaced")).toBeVisible();
  await expect(page.getByText("Cancelled: Entered twice")).toBeVisible();
  await expect(page.getByText("Reversed", { exact: true })).toBeVisible();
  // Each day's subtotal is the server's string: ₹3,300.00 on the 26th, reversal pair netted.
  await expect(page.getByRole("button", { name: /26 Sept|26 Sep/ })).toContainText("₹3,300.00");
});

test("summary: the arrow keys move the chosen category (Phase 26)", async ({ page }) => {
  await signedIn(page, { role: "manager", responses: summaryResponses() });
  await page.goto("/#/summary");
  await page.getByRole("radio", { name: /Salaries/ }).focus();
  await page.keyboard.press("ArrowDown");
  await expect(page.getByRole("radio", { name: /Electricity/ })).toHaveAttribute("aria-checked", "true");
  await expect(page.getByRole("radio", { name: /Electricity/ })).toBeFocused();
  await expect(page.getByText("PSPCL bill, September")).toBeVisible();
});

test("summary: udhaar is a bridge with who owes most, linking to the statement for the same dates (Phase 26)", async ({ page }) => {
  await signedIn(page, { role: "manager", responses: { ...summaryResponses(), ...creditResponses() } });
  await page.goto("/#/summary");
  await expect(page.getByText("Owed at start")).toBeVisible();
  await expect(page.getByText("₹4,64,519.50")).toBeVisible();
  await expect(page.getByText("Ramesh Transport")).toBeVisible();
  await page.getByRole("button", { name: "Statement for these dates" }).click();
  await expect(page).toHaveURL(/#\/credit\/statement\?from=2026-09-22&to=2026-10-02$/);
});

test("summary: 'Last month' asks for last month's own dates (the Phase 19 preset bug)", async ({ page }) => {
  await page.clock.setFixedTime(new Date("2026-10-03T12:00:00+05:30"));
  await signedIn(page, { role: "manager", responses: summaryResponses() });
  await page.goto("/#/summary");
  await page.getByRole("button", { name: "Change range" }).click();
  const sheet = page.getByRole("dialog", { name: "Date range" });
  // The sheet opens on the window being shown, not on a guess.
  await expect(sheet.getByLabel("From")).toHaveValue("2026-09-22");
  await sheet.getByRole("button", { name: "Last month" }).click();
  await sheet.getByRole("button", { name: "Show summary" }).click();
  await expect(page).toHaveURL(/#\/summary\?from=2026-09-01&to=2026-09-30$/);
});

/* --- the Admin tab ---------------------------------------------------------------------- */

for (const [name, path] of [
  ["admin hub", "/#/admin"],
  ["fuel types", "/#/admin/fuel-types"],
  ["nozzles", "/#/admin/nozzles"],
  ["prices", "/#/admin/prices"],
  ["margins", "/#/admin/margins"],
  ["categories", "/#/admin/categories"],
  ["customers", "/#/admin/customers"],
  ["shift templates", "/#/admin/shift-templates"],
  ["bank accounts", "/#/admin/bank-accounts"],
  ["users", "/#/admin/users"],
  ["audit log", "/#/admin/audit"],
] as const) {
  test(`renders: ${name}`, async ({ page }, info) => {
    const problems = await watch(page);
    await signedIn(page, { role: "admin", responses: adminResponses() });
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await settle(page);
    await page.screenshot({ path: info.outputPath(`${name.replace(/ /g, "-")}.png`), fullPage: true });
    await expectAccessible(page);
    expect(problems).toEqual([]);
  });
}

test("admin: a cleared credit limit is sent as null, meaning no limit, never zero (§6.6)", async ({ page }) => {
  const writes = await signedIn(page, {
    role: "admin",
    responses: { ...adminResponses(), [`PATCH /credit-customers/${CUSTOMER_2}`]: {} },
  });
  await page.goto("/#/admin/customers");
  // Bhullar has no limit, and says so in words rather than as ₹0.00.
  await expect(page.getByText("no limit")).toBeVisible();
  await expect(page.getByText("· in credit")).toBeVisible();

  const sandhu = page.locator("[data-arrive]").filter({ hasText: "Sandhu Dairy" });
  await sandhu.getByRole("button", { name: "Edit" }).click();
  const sheet = page.getByRole("dialog", { name: "Sandhu Dairy" });
  await sheet.getByLabel("Credit limit (optional)").fill("");
  await sheet.getByRole("button", { name: "Save" }).click();
  await expect(sheet).toHaveCount(0);
  // Only the edited field, and null rather than "" or "0".
  expect(writes[0]?.body).toEqual({ credit_limit: null });
});

test("admin: an edit sends only what changed, and a code is never offered for editing (§5.1)", async ({ page }) => {
  const writes = await signedIn(page, {
    role: "admin",
    responses: { ...adminResponses(), "PATCH /fuel-types/ft-1": {} },
  });
  await page.goto("/#/admin/fuel-types");
  await page.locator("[data-arrive]").filter({ hasText: "PETROL" }).getByRole("button", { name: "Edit" }).click();
  const sheet = page.getByRole("dialog", { name: "PETROL" });
  await expect(sheet.getByLabel("Code")).toHaveCount(0);
  await expect(sheet.getByLabel("Measured in")).toHaveCount(0);
  await sheet.getByLabel("Display name").fill("Petrol (MS)");
  await sheet.getByRole("button", { name: "Save" }).click();
  await expect(sheet).toHaveCount(0);
  expect(writes[0]?.body).toEqual({ display_name: "Petrol (MS)" });
});

test("margins: a fuel with no margin is named, and a backdated entry is warned about", async ({ page }) => {
  const writes = await signedIn(page, { role: "admin", responses: { ...adminResponses(), "POST /fuel-margins": {} } });
  await page.goto("/#/admin/margins");
  await expect(page.getByText("No margin in force")).toBeVisible();
  await expect(page.locator("section").filter({ hasText: "No margin in force" }).getByText("CBG")).toBeVisible();

  await page.getByRole("button", { name: "Add" }).click();
  const sheet = page.getByRole("dialog", { name: "New margin" });
  await sheet.getByLabel("Fuel").selectOption({ label: "CBG" });
  await sheet.getByLabel("Dealer margin per unit").fill("2.28");
  await sheet.getByLabel("Effective from").fill("2026-06-29T06:00");
  await expect(sheet.getByText(/This is in the past/)).toBeVisible();
  await sheet.getByRole("button", { name: "Record margin" }).click();
  await expect(sheet).toHaveCount(0);
  const body = writes[0]?.body as { fuel_type_id: string; margin_per_unit: string; effective_from: string };
  expect(body.fuel_type_id).toBe("ft-2");
  expect(body.margin_per_unit).toBe("2.28");
  // An instant with an offset, never a naive local time (§3 rule 4).
  expect(body.effective_from).toMatch(/Z$|[+-]\d{2}:\d{2}$/);
});

test("users: editing loads the full record, so a phone is never cleared by accident", async ({ page }) => {
  await signedIn(page, { role: "admin", responses: adminResponses() });
  await page.goto("/#/admin/users");
  await page.locator("[data-arrive]").filter({ hasText: "Gurpreet Singh" }).getByRole("button", { name: "Edit" }).click();
  const sheet = page.getByRole("dialog", { name: "Gurpreet Singh" });
  await expect(sheet.getByLabel("Phone (optional)")).toHaveValue("9876500000");
  // No email field: there is nothing here to edit (§13.26).
  await expect(sheet.getByLabel("Email")).toHaveCount(0);
});

test("audit: a null credit limit is the word null, and money stays a string (§5.3)", async ({ page }) => {
  await signedIn(page, { role: "admin", responses: adminResponses() });
  await page.goto("/#/admin/audit");
  const change = page.locator("[data-arrive]").filter({ hasText: "credit_customers" });
  // Only the key that changed; the unchanged name is not echoed.
  await expect(change.getByText("credit_limit")).toBeVisible();
  await expect(change.getByText("name", { exact: true })).toHaveCount(0);
  await expect(change.getByText("null")).toBeVisible();
  await expect(change.getByText("50000.00")).toBeVisible();
});

test("a manager is turned away from the Admin tab (§8)", async ({ page }) => {
  await signedIn(page, { role: "manager", responses: adminResponses() });
  await page.goto("/#/admin/users");
  await expect(page).toHaveURL(/#\/today$/);
});
