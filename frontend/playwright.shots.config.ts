/* `npm run shots` (Phase 24 D7): the front door's light and dark screenshots, taken from the real
 * app against the e2e mocks, so they regenerate whenever a screen changes rather than going stale
 * as a hand-made mock-up would. Same production build and CSP as the smoke suite. */

import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "scripts/shots",
  reporter: [["list"]],
  use: { baseURL: "http://localhost:4173", ...devices["Pixel 7"], viewport: { width: 390, height: 844 } },
  webServer: {
    command: "npm run build && npx vite preview --port 4173 --strictPort",
    url: "http://localhost:4173",
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
