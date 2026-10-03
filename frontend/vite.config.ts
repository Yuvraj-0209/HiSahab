import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Phase 23 (CLAUDE.md §2). Until the cutover commit, the build lands in frontend/dist/ and
// production keeps serving the Phase 12 frontend from app/static/. The cutover changes
// `outDir` to "../app/static" and nothing else here.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // The dev server has no CSP and no API of its own: /api goes to uvicorn on :8000,
    // so the browser sees one origin exactly as it does in production.
    proxy: { "/api": "http://localhost:8000" },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
