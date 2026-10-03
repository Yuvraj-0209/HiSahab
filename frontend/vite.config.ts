/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Phase 23 (CLAUDE.md §2). The build lands in app/static/, inside the Python package, which
// FastAPI mounts after every API route. That directory is build output and gitignored;
// emptyOutDir is explicit because it lies outside this project root, where Vite otherwise
// refuses to clear it.

/** The production Content-Security-Policy (§13.19). FastAPI sends it as a header
 * (app/core/security_headers.py); `vite preview` sends the same one so the smoke suite runs under
 * the real policy, and tests/test_static_mount.py holds the two equal. The dev server
 * deliberately runs without it (HMR injects inline styles). */
export const CONTENT_SECURITY_POLICY = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self' data: https://*.supabase.co",
  "font-src 'self'",
  "connect-src 'self' https://*.supabase.co",
  "form-action 'none'",
  "base-uri 'none'",
  "frame-ancestors 'none'",
].join("; ");

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // The dev server has no CSP and no API of its own: /api goes to uvicorn on :8000, so the
    // browser sees one origin exactly as it does in production.
    proxy: { "/api": "http://localhost:8000" },
  },
  preview: {
    headers: { "Content-Security-Policy": CONTENT_SECURITY_POLICY },
  },
  build: {
    outDir: "../app/static",
    emptyOutDir: true,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
