/* The smoke suite (Phase 23 D10): every screen renders, in both palettes, with no console
 * error, no CSP violation and no accessibility failure.
 *
 * It runs against the PRODUCTION build served by `vite preview` with the production
 * Content-Security-Policy header (vite.config.ts), because the dev server runs without one --
 * and a library that injects a <style> element fails only under the real policy (§14). The API
 * is mocked in the browser (e2e/mock.ts), so no server or database is needed.
 */

import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  fullyParallel: true,
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:4173",
    ...devices["Pixel 7"],
    viewport: { width: 390, height: 844 },
  },
  projects: [
    { name: "light", use: { colorScheme: "light" } },
    { name: "dark", use: { colorScheme: "dark" } },
  ],
  webServer: {
    command: "npm run build && npx vite preview --port 4173 --strictPort",
    url: "http://localhost:4173",
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
