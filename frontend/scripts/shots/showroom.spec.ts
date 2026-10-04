/* The front door's "night shift" phone: the Entry hub, as a salesman sees it, in each palette.
 * Entry rather than Today because Today's e2e fixture carries a real trading day's takings, and
 * the front door shows sample figures only (CLAUDE.md §14). */

import { test } from "@playwright/test";
import { signedIn, todayResponses } from "../../e2e/mock";

for (const scheme of ["light", "dark"] as const) {
  test(`entry in ${scheme}`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await signedIn(page, { role: "attendant", responses: todayResponses("open") });
    await page.goto("/#/entry");
    await page.getByRole("button", { name: /Nozzle readings/ }).waitFor();
    await page.waitForTimeout(400);
    await page.screenshot({ path: `public/img/showroom/entry-${scheme}.jpg`, type: "jpeg", quality: 82 });
  });
}
