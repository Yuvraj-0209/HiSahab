# Phase 23 — The React frontend: Plan, Decisions, and Test Inventory

> Follows the `phase-5` … `phase-21-plan.md` pattern: written **before** the code, with the
> final section recording what actually shipped where it differs.
>
> No migration, no new table, no new endpoint, no new business rule, no new error code. Every
> screen rebuilt on React, TypeScript, Tailwind and GSAP. Built **before** Phase 22 and numbered
> after it, because 22 was already the spec's name for the profit bridge.

---

## Context

The owner asked for an interface that feels premium, on React, Tailwind and GSAP, and
authorised overriding CLAUDE.md wherever its frontend rules conflicted, amending the spec to
match.

Today's frontend is **27 screens behind 43 hash routes**: about 14,200 lines of hand-written ES
modules and 1,830 lines of CSS in `app/static/`, no build step, a hand-rolled spring. Phase 12's
notes (§6) already listed what having no framework cost:

- every mutation re-renders the whole screen and refetches;
- `readings.js` grew to 700 lines;
- nothing can test behaviour (§13.18).

A framework fixes all three, which is the engineering case for the owner's choice. It also
means this is **a rewrite of every screen, not a reskin**: there is no way to put React into the
look without rewriting the code that builds each screen.

### What is overridden, and what is not

The rules overridden are the ones about **how the frontend is built**:

- §2's "no framework, no build step, no npm";
- §11 Phase 12's "no build step";
- §12's "any frontend framework or build step";
- §14's npm ban;
- `phase-12-plan.md` M2.

The rules kept are the ones about **what the frontend may do with money and with the
session**. They never conflicted with a framework, so "if they conflict" does not reach them:

- money stays a string — no `parseFloat`, no `?? 0`, bar heights and slice shares arrive from
  the server;
- the Idempotency-Key belongs to a submission, not a fetch;
- nothing is pre-confirmed — the chained opening reading, `testing_quantity`;
- read-only screens carry no write verbs;
- the CSP stays `'self'`; the access token stays in memory;
- a hidden control is never a permission check;
- no markup from strings — `dangerouslySetInnerHTML` is banned like `innerHTML`.

Each exists because the failure it prevents is a plausible wrong rupee figure, not a crash.

### The design skill, and where it stops

The work uses the `design-taste-frontend` skill, which declares dashboards, forms and data
tables **out of its scope** — and HiSahab is all three. So only its transferable parts apply:
typography and colour discipline (one accent, locked), motion that has a reason, full loading,
empty and error states, contrast checks, reduced motion, no em-dashes in UI copy, no invented
numbers. Its landing-page rules — heroes, eyebrows, logo walls, placeholder photography, CDN
logos — do not.

### Motion stance

"Add all these animations" is read as **motion everywhere it communicates something**, not
motion on every element. The skill makes the same rule mandatory ("motion must be motivated"),
and the practical reason is the salesman typing a whole day in at 10pm: decoration slows him
down. D8 is the inventory.

## Phase 21 audit

- **The suite at the start of this phase**: see *What shipped* (recorded when the run finished).
- **A live bug, fixed before anything else** (commit `633a1bf`). Phase 20's bank review posts
  `/bank-transactions/confirm-repayments` with a key, but `app/static/js/api.js`'s
  `NEEDS_IDEMPOTENCY` did not list that path, so `request()` never sent the header and the
  server's `_require_key` refused every "Record" with 400 `IDEMPOTENCY_KEY_REQUIRED`. The new
  `tests/test_idempotency_client_coverage.py` reads `create_app().openapi()`, collects every
  POST declaring an `Idempotency-Key` header, and asserts the client covers each. It failed on
  exactly that route before the fix. No other route was missing; the multipart statement import
  already sends its key through `postMultipart`.
- **Phase 20 still has no `docs/phase-20-plan.md` or notes** (recorded by Phase 21's audit,
  still owed).
- **Dead code the rebuild will delete rather than fix**: `placeholder()` in `main.js`,
  `translateYSpring` in `spring.js`, an unused `openReversalSheet` import in `shortfalls.js`,
  and a second `varianceLabel` in `today.js` duplicating `money.js`.
- **The Phase 21 statement's print preview has never been driven in a browser**
  (`phase-21-notes.md`). It becomes part of this phase's verification, since the print
  stylesheet is rebuilt.

## Decisions

### D1 — Stack

Vite, React 19, TypeScript, Tailwind v4 (`@tailwindcss/vite`), GSAP 3.13+ with `ScrollTrigger`
and `Flip` and the `@gsap/react` `useGSAP` hook (all free since 3.13), React Router
(`createHashRouter`), TanStack Query, `@phosphor-icons/react`. Fonts self-hosted through
`@fontsource` packages, bundled under `'self'`.

**No Motion library** — the skill forbids mixing it with GSAP in one tree. **No headless UI kit**
— Radix's scroll lock injects a `<style>` element, which `style-src 'self'` refuses, and native
`<select>` and `<input type=date>` are the better controls on a phone anyway.

### D2 — The spring stays

`motion/spring.js` and `gesture.js` become `frontend/src/motion/*.ts` with behaviour unchanged;
`tests/motion_assertions.mjs` becomes a Vitest suite. They drive sheet drag, throw,
rubber-banding and toasts. GSAP has no physics spring that carries velocity through an
interruption, and these are tuned and tested. **One element is animated by the spring or by
GSAP, never both.**

### D3 — Layout and build output

Source in `frontend/` at the repo root, with its own `package.json` and lockfile (the root
`package.json` is Railway tooling and stays separate). During development the Vite dev server
proxies `/api` to uvicorn on :8000 and production keeps serving the old `app/static/`. At
cutover, Vite's `build.outDir` becomes `../app/static` — gitignored build output, still inside
the package, so the wheel is still one artefact — and `pyproject.toml` package-data becomes
`static/**/*`. Fonts and the skyline images move to `frontend/public/`.

### D4 — The deploy needs Node at build time

The only real unknown, so it is proven **first** (commit 2), on a non-production Railway
environment. Preferred: Railpack with Node added, the build running `npm ci && npm run build` in
`frontend/` before the Python install. Fallback: a two-stage Dockerfile. Nothing else depends on
which wins; `preDeploy` migrations are untouched.

### D5 — The CSP becomes a response header

Set by FastAPI on HTML responses; the policy is unchanged. This also fixes a latent defect:
browsers ignore `frame-ancestors` delivered by `<meta>`. React's `style` prop and GSAP write
through the CSSOM, which `style-src 'self'` permits; Tailwind emits one file. The Vite dev
server runs without a CSP, so production is verified with zero CSP violations.

### D6 — The API layer

- TypeScript types generated from FastAPI's `/openapi.json` with `openapi-typescript`, so every
  money field is typed `string`. A pytest snapshot fails when the API changes and the types were
  not regenerated.
- GETs through TanStack Query: cached, refetched on focus, invalidated after a mutation.
- Writes through `useSubmission(method, path)`: the key minted once, in a `useRef`, when the
  sheet opens; every retry reuses it; discarded only on success. Mutations never auto-retry.
- The idempotent-path list is checked against the OpenAPI document (`test_idempotency_client_
  coverage.py`, retargeted at cutover).
- Tokens unchanged: access token in memory, refresh token in `sessionStorage`, one
  refresh-and-retry on `TOKEN_EXPIRED`.

### D7 — Routing

Every hash path identical, so bookmarks and deep links keep working, the five redirects
included. The role guard redirects to `#/today` exactly as `main.js` does. A 403 is still a real
outcome on every screen. Each of the six tabs is a lazily loaded chunk.

### D8 — Motion inventory

Every animation is GSAP inside `useGSAP` (automatic cleanup) and inside `gsap.matchMedia()`, so
reduced motion collapses each to an instant change or a fade.

| Where | What | What it communicates |
|---|---|---|
| Route change | Cross-fade + 8px rise, ≤240ms, killed by the next navigation | State transition |
| Lists on first load | Stagger, 30ms apart, first 8 rows; never on refetch | Hierarchy |
| Sheets, toasts | The existing spring: drag, throw, rubber-band | Feedback under a finger |
| Tab bar | Active indicator slides between tabs (`Flip`) | Spatial consistency |
| Day lifecycle strip | Dot fills, connector draws when a step completes | State transition |
| Money after a save | **Per-digit roll over the server's string** | Feedback |
| Charts on first view | Bars grow from the baseline (`scaleY`; height still the server's `bar_height_pct`); donut arcs draw in | Storytelling |
| Reading tiles | Confirm / mismatch states morph in place (`Flip`) | State transition |
| Login | `ScrollTrigger` pins the two statements; Ken Burns stays CSS | Storytelling |
| Press | `scale(.98)` on `:active`, CSS | Feedback |

**Deliberately not animated:** a money figure counting up from zero (it computes rupee values
that never existed — §3 rule 1 in JavaScript); infinite loops; parallax on data screens; cursor
effects; a `filter` or `backdrop-filter` on anything that moves (§13.29 stands).

### D9 — Visual direction: light and dark, following the phone

**Owner's choice.** Salesmen enter the day at night; the owner reviews and prints bills by day.

*Design read (skill §0.B):* an operational money app for a fuel station's salesmen and owner,
used on phones, in a calm and precise premium language, built on Tailwind v4 tokens, restrained
GSAP choreography and native controls. *Dials:* `DESIGN_VARIANCE 3` (an app's layout must be
predictable), `MOTION_INTENSITY 5`, `VISUAL_DENSITY 6`.

- **Two palettes, switched by `prefers-color-scheme` only.** No in-app toggle (§12 amended).
  Tokens are CSS variables in Tailwind's `@theme`, redefined under the dark media query.
- **Semantic tokens only** in components — `surface`, `surface-raised`, `ink`, `ink-muted`,
  `hairline`, `accent`, `short`, `surplus`, `warning` — so a screen is designed once and
  checked twice.
- **Light:** off-white paper (not the skill's banned beige family), ink-black text, hairlines.
  **Dark:** graphite surfaces with real depth — layered surfaces, tinted shadows, no glows.
- **One accent: cobalt.** Red, green and amber already *mean* short, surplus and warning. Final
  values tuned in commit 3 against WCAG AA in both palettes; the six chart category colours get
  a variant per palette.
- **Type:** one self-hosted UI sans with tabular figures and the ₹ glyph (U+20B9) — Geist with
  Geist Mono for figures if it covers ₹, otherwise IBM Plex Sans; checked, never assumed. The
  Instrument Serif wordmark stays (the brand, skill §11.F).
- **Login** stays on the skyline photograph in both palettes; only its gradient grade changes.
  **Print** reuses the light tokens.

### D10 — Tests

- **Every structural rule survives**, retargeted to `frontend/src/**/*.{ts,tsx}`: no external
  host; no `parseFloat` / `Number(` on money; no `innerHTML` / `dangerouslySetInnerHTML`; every
  router reachable from a screen; chart geometry only from `bar_height_pct` / `share_pct`; a
  guarded `crypto.randomUUID`; the meter-reset tick in the readings edit sheet; the three report
  paths. The accessibility media-query test gains `prefers-color-scheme: dark`.
- `tests/test_frontend_assets.py` and `tests/test_static_mount.py` rewritten for the new layout;
  the mount-ordering tests use a fixture static directory, so pytest needs no build.
- **§13.18 closed**: Vitest + Testing Library for the money labels (`money_assertions.mjs`
  ported), `useSubmission` key reuse across a retry, `null` as a word vs `"0.00"` as ₹0.00, the
  opening-confirm box starting unticked, no write verb on a read-only screen.
- A pytest wrapper runs `npm test` in `frontend/`, skipping without Node like the existing
  harnesses, so `pytest` stays the one command.
- A Playwright smoke suite (`npm run e2e`, dev-only): every tab, both palettes, no console
  error, no CSP violation, an axe pass.

### Mechanical decisions

| # | Decision |
|---|---|
| M1 | `frontend/` at the repo root, not inside `app/` — source is not shipped, only its build |
| M2 | TypeScript `strict`; no `any` in the API layer |
| M3 | One `src/screens/<name>.tsx` per old screen module, so the parity checklist maps one to one |
| M4 | Tailwind v4 with tokens in CSS (`@theme`), no `tailwind.config.js` |
| M5 | Icons from `@phosphor-icons/react` only, one weight app-wide; replaces the ◎ ✎ ₹ ◈ ◱ ⚙ glyphs |
| M6 | `app/core/security_headers.py` holds the CSP middleware; `app/main.py` adds it before the mount |
| M7 | Vite `base: "/"`, hashed asset names; `index.html` served uncached, assets cached immutably |
| M8 | Node version pinned in `frontend/.nvmrc` and `engines` |

## Build order

| # | Commit | What lands |
|---|---|---|
| 0 | `Fix: bank review could not record a repayment (idempotency key dropped)` | Done — `633a1bf` |
| 1 | `CLAUDE.md: Phase 23 amends the frontend rules` | §2, §11 (23), §12, §13.18, §13.19, §13.28, §14, §15; this plan |
| 2 | `Phase 23: prove the Node build on Railway` | D4 spike on a non-production environment |
| 3 | `Phase 23: foundation` | Scaffold, tokens and both palettes, fonts, generated types, API client, `useSubmission`, auth, router, shell (chrome, tab bar, sheet, toast, field, error / empty states), money / time ports + Vitest, login |
| 4 | `Phase 23: Today and Entry` | today, entry, readings, collections, expenses, non-fuel sales, credit sales, credit repayments, bank deposits, cash position — the attendant's path first |
| 5 | `Phase 23: Cash` | cash, days (+ daily summaries), reports, alerts, shortfalls, flagged expenses |
| 6 | `Phase 23: Credit` | hub + ledger, repayments, opening balances, statement (+ print), the three bank screens |
| 7 | `Phase 23: Summary` | donut, share bars, sales bars |
| 8 | `Phase 23: Admin` | hub, fuel types, nozzles, prices and margins, categories, bank accounts, customers, shift templates, users, audit |
| 9 | `Phase 23: cutover` | `outDir` → `app/static`; old JS/CSS deleted; CSP header; package-data and `.gitignore`; structural tests retargeted; Railway build config |
| 10 | `Phase 23 notes` | `docs/phase-23-notes.md` |

Each screen commit carries a **parity checklist** against the screen it replaces: same
endpoints, same Submission-guarded POSTs, same null-versus-zero wording, same role gates.

## Error codes introduced

None.

## Verification

1. `pytest` — the full suite, including the retargeted structural tests and the Vitest wrapper.
2. `npm test`, `npm run build` with no warnings, `npm run e2e`.
3. **Parity on real figures.** One complete trading day entered on the local DB through the new
   UI — a confirmed opening, an explicit ₹0 collection, an expense over the receipt threshold, an
   udhaar with a receipt, a reversal, then close, lock, reconcile, finalise. Every money string on
   the cash position, the day screen, the credit statement and the summary must be
   **byte-identical** to the old UI on the same data. The server computes every figure, so any
   difference is a rendering bug.
4. **By hand at 390px** — every tab in light and in dark, reduced motion off and on, reduced
   transparency and increased contrast; throw a sheet and interrupt it mid-flight; the
   statement's print preview.
5. **Budget** — initial JS ≤ 150 KB gzipped (other tabs lazy-load); LCP < 2.5s on Lighthouse's
   Slow 4G profile, because §6.10's rural connectivity is the real network.
6. **Railway** — the build proven in commit 2; after cutover, a non-production deploy, signed
   in, one tab end to end, before `main`.

## What shipped

*Recorded as each commit lands.*

- **Suite at the start of the phase: 1,741 passed**, 98% coverage. Every module under 100% is a
  Phase 16 / 20 gap Phase 21's audit already recorded (`bank_statements.py` 87%,
  `credit_opening_balances.py` 88%, `credit_repayments.py` 92%); nothing new.
- Commit 0 — `633a1bf`. The idempotency coverage test (3 tests) failed on
  `/bank-transactions/confirm-repayments` alone and passes after a one-line fix to `api.js`.
- Commit 1 — `c9b7eaf`, the CLAUDE.md amendment.
- **Commit 2 — the build was proven locally with Railpack itself, not on a Railway
  environment.** A deviation from D4, and a more conservative one: Railpack is open source, so
  `railpack build` on this machine runs the exact builder Railway runs, against an export of the
  git index (what Railway checks out), for `linux/amd64` (what Railway builds for), with no
  change to any Railway environment. Findings:
  - A root `railpack.json` with `"packages": {"node": "24"}` and the build step extended by
    `"..."` keeps Python as the provider and appends `cd frontend && npm ci && npm run build`
    after `uv sync`.
  - `npm ci` on Linux resolved the native binaries (Tailwind's engine, Vite's bundler) from a
    lockfile generated on macOS — the failure mode the spike existed to catch did not occur.
  - The image's `frontend/dist` assets carry the **same content hashes** as a local build, so
    the build is deterministic across machines.
  - Node stays in the runtime image (262 MB total). Trimming it is possible through Railpack's
    `deploy` section; not worth it yet.
  - `uv.lock` **is** tracked in git, so Railway installs with `uv sync --locked`. (A note in the
    working memory said otherwise; it was stale.)
  - A real Railway deploy is still verification item 6, after cutover.
- **Commit 6 (Credit) found a money defect in the old bank review, and the port does not repeat
  it.** `bank.js` offered every unlinked incoming udhaar line for recording. But "unlinked" includes
  a line that §13.37's live `(date, amount)` match ties to a repayment somebody already *typed in*,
  and recording that line created a second repayment for the same money: the customer's balance
  went down twice. The new review asks `GET /bank-statements/reconciliation` what each line is and
  offers recording only for a line that matches nothing; a verified line reads "already on the
  ledger", and an ambiguous one is explained and not offered either. **The server still accepts
  the duplicate** — `confirm-repayments` checks `credit_repayment_id` but not the live match. That
  is a business-rule decision (refuse it, or link the line to the existing repayment instead) and
  is raised with the owner rather than made here.
- Commit 6 also stopped the old review breaking its own promise. Its hint said a remembered sender
  "will pre-select them", and the picker never did. A single high-confidence proposal now
  pre-selects; name matches show as suggestions; the Confirm tick still starts empty (§5.3a).
- The opening-balance correction form in `credit_opening_balances.js` passed a string where
  `reversal.js` expected a field spec, rendering an unlabelled field that sent an `undefined` key.
  The port takes a `replacementHint` instead.
- `useRepeatableSubmission` (in `submission.ts`) exists for the one screen that stays open and
  submits repeatedly. The key follows the body: same body is a retry, a changed body or a
  post-success call is a new submission. The old review reused one key across changed ticks,
  which the server refuses as `IDEMPOTENCY_KEY_REUSED`.
- **Commit 7 (Summary) found the old "Last month" preset asking for a future date.** `summary.js`
  took the end date's month from *today* and its day from last month, so on 3 October it asked
  for 1 September to **30 October**. The presets now live in `lib/calendar.ts`, tested across a
  year end and a leap February. The range sheet also opens on the window being shown rather than
  on "this month", which the server's trading-anchored default (§13.30) often is not.
- Chart category colours were re-chosen for two palettes, and none is red, green or amber: those
  mean short, surplus and warning here, and a fuel painted red reads as a shortage. Each is at
  least 3:1 against its surface (WCAG 1.4.11).
- The donut draws each arc as a circle with `pathLength="100"`, so the server's percentage *is*
  the dash length and nothing is divided anywhere, not even for geometry.

## Still owed by the owner

- A look at the two palettes and the typeface after commit 3, before 27 screens are built on
  them.
- A hand-check of one real trading day on the new UI before it replaces the old one in
  production.
- Phase 21's branch (`phase-21-credit-statement`) is 14 commits ahead of `main` and unmerged;
  this phase branches from it.
