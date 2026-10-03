/* Entry point (Phase 23). Everything the app needs exactly once: the stylesheet, the query
 * cache, and the root. */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "./api/client";
import { App } from "./app/App";
import "./styles.css";

/* Reads only. Writes never retry automatically: a money POST retried by a library would need
 * its own idempotency discipline, and a person pressing "Try again" with the same Submission is
 * the one retry that is known to be safe (§6.10). A 4xx is an answer, not a blip, so it is never
 * retried either. */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      retry: (failures, error) => failures < 1 && !(error instanceof ApiError && error.status < 500),
    },
    mutations: { retry: false },
  },
});

const root = document.getElementById("root");
if (!root) throw new Error("index.html is missing #root");

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
