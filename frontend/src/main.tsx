import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

// Phase 23, commit 2: the smallest app that exercises the whole toolchain (TypeScript,
// React, Tailwind's native engine, Vite's bundler) so the deploy build can be proven before
// any screen is written. Commit 3 replaces this with the real shell.
const root = document.getElementById("root");
if (!root) throw new Error("index.html is missing #root");

createRoot(root).render(
  <StrictMode>
    <p className="p-6 font-sans">HiSahab</p>
  </StrictMode>,
);
