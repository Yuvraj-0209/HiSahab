// The bundle budget, enforced (Phase 24 D1).
//
// Phase 23 set "initial JS <= 150 KB gzipped" and nothing checked it: the notes recorded about
// 147 KB, and the build actually shipped 150.4 KiB at gzip -9 (the entry plus a modulepreloaded
// icon chunk the count had missed). A budget nobody measures is a wish.
//
// "Initial" means what a browser downloads before the first screen can paint: the entry script
// and every file index.html modulepreloads. Lazily loaded screens are not in it, which is the
// point of loading them lazily.
//
// The front door gets a separate, looser cap (Phase 24 D7): everything the sign-in page and its
// story download *beyond* the initial set -- their own chunks and every shared chunk they pull
// in (GSAP's core, ScrollTrigger, ScrollSmoother, the charts). It is walked from Vite's build
// manifest rather than matched by file name, because the plugins land in shared chunks whose
// names say nothing about who needs them. It downloads only on the sign-in page, and never for a
// session restored from a refresh token.
//
// Sizes are gzip at level 9, which is what the server sends (Starlette's GZipMiddleware default,
// wrapped around the UI mount by app/core/static_delivery.py).
//
// It builds into a temporary directory rather than app/static, so running it (or pytest, which
// runs it) never replaces somebody's working build.

import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { gzipSync } from "node:zlib";
import { build } from "vite";

const INITIAL_LIMIT = 150 * 1024;
const FRONT_DOOR_LIMIT = 70 * 1024;
const FRONT_DOOR_ENTRIES = ["src/screens/Login.tsx", "src/showroom/Showroom.tsx"];

const outDir = mkdtempSync(join(tmpdir(), "hisahab-budget-"));
try {
  await build({ logLevel: "error", build: { outDir, emptyOutDir: true, manifest: true } });

  const gz = (file) => gzipSync(readFileSync(join(outDir, file)), { level: 9 }).length;
  const kb = (bytes) => `${(bytes / 1024).toFixed(1)} KB`;

  const html = readFileSync(join(outDir, "index.html"), "utf8");
  const initial = [
    ...html.matchAll(/<script[^>]*type="module"[^>]*src="\/([^"]+\.js)"/g),
    ...html.matchAll(/<link[^>]*rel="modulepreload"[^>]*href="\/([^"]+\.js)"/g),
  ].map((match) => match[1]);
  if (initial.length === 0) throw new Error("no entry script found in index.html");

  const failures = [];
  let initialTotal = 0;
  console.log("Initial JavaScript (entry + modulepreloads):");
  for (const file of initial) {
    const size = gz(file);
    initialTotal += size;
    console.log(`  ${kb(size).padStart(9)}  ${file}`);
  }
  console.log(`  ${kb(initialTotal).padStart(9)}  total, limit ${kb(INITIAL_LIMIT)}`);
  if (initialTotal > INITIAL_LIMIT) failures.push(`initial JS ${kb(initialTotal)} > ${kb(INITIAL_LIMIT)}`);

  // Everything the front door downloads: each entry's chunk and its static imports, recursively,
  // less whatever the initial download already brought.
  const manifest = JSON.parse(readFileSync(join(outDir, ".vite", "manifest.json"), "utf8"));
  const doorFiles = new Set();
  const visit = (key) => {
    const chunk = manifest[key];
    if (!chunk || doorFiles.has(chunk.file)) return;
    doorFiles.add(chunk.file);
    (chunk.imports ?? []).forEach(visit);
  };
  for (const entry of FRONT_DOOR_ENTRIES) {
    if (!manifest[entry]) throw new Error(`${entry} is not in the build manifest -- was it renamed?`);
    visit(entry);
  }
  let doorTotal = 0;
  console.log("Front door, beyond the initial download:");
  for (const file of [...doorFiles].filter((file) => file.endsWith(".js") && !initial.includes(file)).sort()) {
    const size = gz(file);
    doorTotal += size;
    console.log(`  ${kb(size).padStart(9)}  ${file}`);
  }
  console.log(`  ${kb(doorTotal).padStart(9)}  total, limit ${kb(FRONT_DOOR_LIMIT)}`);
  if (doorTotal > FRONT_DOOR_LIMIT) failures.push(`front door ${kb(doorTotal)} > ${kb(FRONT_DOOR_LIMIT)}`);

  if (failures.length > 0) {
    console.error(`\nOver budget:\n  ${failures.join("\n  ")}`);
    process.exitCode = 1;
  } else {
    console.log("\nWithin budget.");
  }
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
