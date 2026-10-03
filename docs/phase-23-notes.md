# Phase 23 — The React frontend: Notes

> A learning reference, not a changelog: *why* the frontend is built the way it is now, and what
> the rewrite turned up in the one it replaced. The plan, with a running "What shipped" record,
> is `docs/phase-23-plan.md`; the rules live in `CLAUDE.md` §2, §11 (phase 23), §12, §13.18–19,
> §13.28 and §14.

---

## What was built

- All 27 screens behind the 43 hash routes, rebuilt in React 19 + TypeScript, styled with
  Tailwind v4, animated with GSAP, built by Vite from `frontend/` into `app/static/`. Every hash
  path is unchanged, so old bookmarks and deep links still land.
- Two palettes, light and dark, chosen by the phone's `prefers-color-scheme` and nothing else.
  One accent (cobalt); red, green and amber are reserved for short, surplus and warning.
- The Content-Security-Policy moved from a `<meta>` tag to a response header.
- No migration, no new table, no API change. One business-rule question is raised (below).
- Tests: pytest 1,697 (structural checks retargeted, typecheck and Vitest run from pytest);
  Vitest 63; Playwright 126 (63 scenarios × light and dark, each with an axe WCAG 2 AA pass).

## Why a rewrite and not a reskin

The Phase 12 frontend built every screen by hand from DOM calls, re-rendered the whole screen
after every write, and had no behavioural tests at all (§13.18). A new look could not be added
without rewriting the code that builds each screen, so the look and the rewrite were one job.
The rewrite is what bought the tests: React components can be driven by Vitest and Playwright,
and hand-built DOM could not.

## What did not change: the rules about money and the session

The owner authorised overriding CLAUDE.md where its frontend rules conflicted. Only the rules
about *how the frontend is built* conflicted (no framework, no npm, no build step). The rules
about *what the frontend may do with money and the session* did not, so they carried over
unchanged, and most are now enforced by the type system or a test rather than by care:

- **Money stays a string.** The API types are generated from a committed OpenAPI snapshot, so
  every money field is typed `string`; `parseFloat` is banned by a structural test; the
  `Amount` component renders the server's string and nothing else.
- **`null` is a word, never zero.** `Amount` takes an `absent` prop ("no limit", "not entered",
  "not knowable"), so a null cannot reach the screen as ₹0.00 without someone writing that.
- **Chart geometry comes from the server.** Bar heights are `bar_height_pct`, shares are
  `share_pct`, assigned verbatim; the donut draws each arc as a circle with `pathLength="100"`,
  so the server's percentage *is* the dash length and nothing is divided, even for geometry.
- **The Idempotency-Key belongs to the submission.** `useSubmission` mints it once per form;
  `useRepeatableSubmission` (the bank review, which stays open) reuses it only for an
  identical body. Both are tested through a mocked fetch.
- **Nothing is pre-confirmed, and read-only screens carry no write verbs** — asserted in the
  Playwright suite where the old checks were by hand.

## Motion that communicates, and one engine per element

"Add all these animations" was read as motion wherever it *says something*: a screen changed,
a list arrived, a figure moved, a sheet is being thrown. GSAP does the choreography (screen
entrance, list arrival, the tab indicator, bars and arcs growing once, lifecycle dots, a
per-digit roll of a changed figure). The Phase 12 spring was ported unchanged for gestures
(sheets, toasts, the chart's paging rail), because GSAP has no spring that carries velocity
through an interruption.

Three rules came out of it and are in §14. An element is animated by the spring *or* GSAP,
never both. A money figure is never tweened from zero, because the in-between values are rupee
amounts that never existed; the digit roll only swaps one server string for another. Everything
runs inside `gsap.matchMedia("(prefers-reduced-motion: no-preference)")`, so reduced motion
gets the content without the movement.

## Defects the rewrite found in the old frontend

Porting a screen means reading every line of it, and that turned up more than the look:

1. **The bank review's "Record" could never succeed** (fixed first, commit 0). The client only
   sent an Idempotency-Key for paths on a hand-kept list, and `confirm-repayments` was not on
   it. A test now reads the server's OpenAPI document and fails if the list misses a route.
2. **The bank review offered to record money already on a ledger.** It listed every incoming
   line with no repayment *linked*, including lines that match a repayment somebody typed in by
   hand (§13.37's live match). Recording one created a second repayment for the same money.
   The new review asks the reconciliation endpoint what each line is and offers only lines that
   match nothing. **The server still accepts the duplicate** — see the open question below.
3. **A remembered sender never pre-selected anyone**, although the hint said it would. It does
   now, for a single high-confidence match; the Confirm tick still starts empty.
4. **The Summary's "Last month" preset asked for a future date**: on 3 October, 1 September to
   30 *October*. The presets moved to `lib/calendar.ts` with tests.
5. **The opening-balance correction form sent an `undefined` field** and rendered an unlabelled
   box, from a string passed where a field spec was expected. Fixed in the port.
6. **The shift-open sheet had a template picker whose value was never sent.** Dropped: the
   server materialises the template itself (§5.1).
7. **Two contrast failures in the new design**, both caught by axe before commit: the faint ink
   colour was 4.2:1 (darkened to pass AA in both palettes), and a pill on a tinted table row was
   4.26:1 in dark mode (rows lost the tint; the state became a "flagged" pill).

The bank review's old behaviour also reused one key across changed ticks, which the server
refuses as `IDEMPOTENCY_KEY_REUSED`; that is what `useRepeatableSubmission` is for.

## The CSP is a header now, and only on the UI

Browsers ignore `frame-ancestors` in a `<meta>` tag, so Phase 12's clickjacking protection was
written down and never enforced. The header carries every directive. It wraps the static mount,
not the app: the API returns JSON, and `/docs` in development loads Swagger UI from a CDN that a
global policy would break. Vite's preview server sends the same policy, so the Playwright suite
runs under it, and a pytest test holds the two copies equal.

## Deploying a frontend that has a build step

`app/static/` is build output now: gitignored, packaged by one `static/**/*` glob, written by
`npm run build`. Railpack detects Python first, so a root `railpack.json` adds Node 24 and runs
the frontend build after `uv sync`. Both the plan's step-2 spike and the cutover were proven with
Railpack itself, run locally against an export of the git index for `linux/amd64`: inside the
image, `uvicorn app.main:app` imports `app` from the source tree where Vite wrote the build, and
serves the shell with the CSP header. `create_app` mounts the UI only when `index.html` exists
and warns otherwise, so a deploy whose build failed still serves the API.

Initial JavaScript is about **147 KB gzipped against a 150 KB budget**. React DOM, React Router
and GSAP are three quarters of it. The entry chunk appeared to jump from 118 KB in commit 8; a
side-by-side sourcemap build showed GSAP had only moved from a modulepreloaded chunk into the
entry. If the budget bites, taking GSAP out of the first paint is the first lever.

## What was not verified

- **A deploy to a Railway environment.** Proven with the same builder locally; not run on
  Railway. Verification item 6, and the owner's call.
- **Byte-identical money strings on a real trading day** (verification item 3). The server
  computes every figure, so a difference would be a rendering bug, but nobody has entered a day
  through both UIs and compared.
- **A hand-check on a real phone**, with reduced motion and transparency on and off, a sheet
  thrown and interrupted mid-flight, and the statement printed on paper. The Playwright suite
  covers 390 px in both palettes and axe covers contrast; it cannot say whether a sheet *feels*
  right (§13.18's remaining half).
- **LCP on a slow connection.** The bundle budget is met; Lighthouse's Slow 4G run was not done.

## Open question for the owner

`POST /bank-transactions/confirm-repayments` refuses a line already *linked* to a repayment but
not one that *matches* a repayment typed in by hand. The new screen no longer offers those lines,
but the server would still accept one. Two fixes are possible, and they are different business
rules: refuse it (409), or link the statement line to the existing repayment instead of creating
one. Either needs a decision, not a guess (§14: ask before changing a business rule).
