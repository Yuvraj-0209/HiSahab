# Phase 24 — The front door and the finish: Plan, Decisions, and Test Inventory

> Follows the `phase-5` … `phase-23-plan.md` pattern: written **before** the code, approved by
> the owner on 4 October, with the final section recording what actually shipped where it
> differs.
>
> No migration, no new table, no new endpoint, no new business rule, no new error code, and
> **no new npm dependency**: GSAP 3.15 already ships SplitText, ScrollSmoother, Flip and
> DrawSVG free, and they are in `node_modules/gsap` today.

---

## Context

The owner liked Phase 23 and wants HiSahab to feel like something that sells for $10k: GSAP,
scroll effects, "anything and everything". Two audiences have to feel it, and they want opposite
things:

- **The buyer**, another pump owner, opening a link on their phone. They have no account, so
  they see the front door, and today that is a photograph, a sign-in card and two statements.
  For them, scroll storytelling *is* the product demo.
- **The daily user**, a salesman typing a whole day in at 10pm on a cheap Android, and the owner
  reviewing by day. For them, "premium" means nothing jumps, every tap answers at once, motion
  has one consistent physics, and a few moments have weight (a shift closing, a day finalised).
  Decoration would slow them down, and §14 forbids it on data screens.

**Owner's answers (4 Oct):** build both the front door and the inside; inside, motion is "rich
but purposeful" (every animation says something, nothing loops or floats); photographs are
AI-generated now and replaced with real photos of the pump later.

### Design read (design-taste-frontend §0.B)

- **Front door:** a product story page for petrol-pump owners deciding whether to buy, read on a
  phone first. A calm, precise premium language (cobalt on paper and graphite, Instrument Serif as
  the brand voice), leaning toward GSAP ScrollTrigger scrollytelling built from **live product
  components with sample figures**. Dials `VARIANCE 7 / MOTION 7 / DENSITY 3`.
- **Inside the app:** unchanged from Phase 23 D9 except `MOTION_INTENSITY` 5 → 6. The skill
  declares dashboards and forms out of its scope, so only its transferable rules apply inside:
  motivated motion, transform/opacity only, shaped loading states, reduced motion.

### The three engines, three territories

| Engine | Owns | Never touches |
|---|---|---|
| Browser View Transitions (new) | Route changes, shared elements between screens | Anything inside a screen |
| GSAP | In-screen choreography, the front door's scroll story | Sheets, toasts, the chart rail |
| The Phase 12 spring | Gestures: sheets, toasts, the chart's paging rail | Route changes, choreography |

§14's "one element, one engine" rule extends to the third engine.

## Phase 23 audit

1. **Initial JS is over budget, and nothing enforces the budget.** Measured on the current build
   at gzip level 9 (what the server would send): entry `index-*.js` 141.6 KiB plus the
   modulepreloaded `IconBase` 8.8 KiB, so **150.4 KiB against 150** (154 kB in decimal units).
   The notes recorded about 147. No test checks it. GSAP core (28 KB gz) is in
   the entry only because `Shell.tsx` uses it for the tab indicator and the screen entrance. The
   notes already named the first lever: take GSAP out of the first paint.
1b. **Found while fixing item 1: the frontend was never delivered the way it was measured.**
   `StaticFiles` sends files as they sit on disk and nothing wrapped it in compression, so a
   phone downloaded about 460 KB of uncompressed JavaScript to reach the sign-in card. And
   Phase 23's M7 (*"index.html served uncached, assets cached immutably"*) never shipped: no
   `Cache-Control` header was sent at all, so every open revalidated every chunk.
2. **Three D8 promises shipped differently, and none of the changes was recorded.**
   - The tab indicator was meant to use `Flip` and uses an `xPercent` tween instead.
   - Reading tiles were meant to morph with `Flip` and use a scale pop instead.
   - The login statements were meant to be pinned with ScrollTrigger and are one-shot reveals.

   Separately, charts animate **on mount**, so a chart below the fold grows where nobody can see
   it. This phase delivers what D8 meant.
3. **Still owed from Phase 23:** a Railway deploy, parity on a real trading day, a hand-check on a
   real phone, and LCP on Slow 4G. This phase re-runs the LCP and phone checks anyway, because
   the login page changes.
4. **Branch state:** `phase-23-react-frontend` is 25 commits ahead of `main` and unmerged.
   Phase 24 branches from it (`phase-24-front-door`).
5. **The owner's open question on `confirm-repayments`** is a business rule. This phase leaves it
   alone.

## Decisions

### D1 — Take GSAP out of the first paint (audit item 1)

- The tab indicator becomes a CSS `transform` transition (`translateX(calc(100% * var(--i)))`).
  It is a pure transform, so nothing re-lays out.
- The route entrance becomes View Transitions (D3), with a CSS keyframe fallback.
- GSAP then loads with the screen chunks, which are already lazy.
- `scripts/budget.mjs` builds the app and gzips the entry plus every modulepreloaded file. It
  fails above 150 KB, and above 70 KB for the front-door chunk. It runs from `npm run budget` and
  from the existing pytest frontend wrapper.

### D2 — One motion module

- **`frontend/src/motion/gsap.ts`** is the only file that imports `gsap`. It registers `useGSAP`
  and exports the motion tokens and `useMotion(scope, deps, build)`. `useMotion` wraps
  `useGSAP` + `gsap.matchMedia("(prefers-reduced-motion: no-preference)")`, so reduced motion
  is guaranteed in one place rather than in every file.
- **Plugins sit in sibling modules** (`motion/scroll.ts`, `motion/flip.ts`, `motion/draw.ts`), so
  a chunk pays only for what it uses. The front door registers SplitText and ScrollSmoother
  itself.
- **Tokens:** `DURATION { tap .14, small .24, medium .42, large .7 }` and
  `EASE { enter "expo.out", exit "power2.in", move "power3.inOut", settle "back.out(1.8)" }`.
  They are mirrored as `--dur-*` / `--ease-*` in `styles.css` for CSS transitions and view
  transitions. A Vitest test holds the two copies equal, the way `test_static_mount.py` holds
  the CSP copies equal.
- **The six existing GSAP users migrate:** `ui/motion.ts`, `ui/Amount.tsx`, `ui/chart.tsx`,
  `screens/days.tsx`, `screens/readings.tsx` and `screens/Login.tsx`.

### D3 — Route changes are View Transitions

- **`useGo()`** wraps `useNavigate()` and calls `navigate(to, { viewTransition: true })` (React
  Router 8 supports it). Before navigating, it sets `<html data-nav="forward|back|lateral">`.
  - Lateral is a tab-to-tab move, worked out from tab indices: a 24 px slide plus a crossfade, in
    the direction of the tab.
  - Forward is a drill-down, worked out from route depth: it pushes in from the right. Back pops.
  - All of it is CSS on `::view-transition-*(screen)` and runs on the compositor.
- **The chrome and tab bar get their own `view-transition-name`** with `animation: none`. Only the
  screen region moves, and the blurred chrome is never animated (§13.29).
- **Shared elements:** the tapped day row's date flies into the day screen's title, and a
  customer row's name flies into the ledger header. The name is set on the tapped row only, at
  click time, because names must be unique at capture.
- **Fallback:** browsers without the API get a CSS keyframe entrance on the keyed screen `div`.
  Browser back/forward gets that plain entrance too (§13.43).
- **Reduced motion:** `::view-transition-group(*) { animation: none }`.
- A structural test bans bare `useNavigate` in `screens/`, so new screens get transitions
  automatically.

### D4 — Scroll effects inside the app, each with a reason

| Effect | Mechanism | What it communicates |
|---|---|---|
| Large title on the six tab-root screens collapses into the compact chrome title | CSS `animation-timeline: scroll()` under `@supports`; zero JS | Where you are |
| Sticky group headers (statement customers, audit days, bank lines by date, days worklist) gain a hairline when stuck | `position: sticky` + scroll timeline | Which group you are reading |
| Charts grow when scrolled into view, not on mount | ScrollTrigger `once` (scale only, never opacity) | The figure at the moment you read it |

- **Without scroll-timeline support:** the compact title is always shown and the large title is
  hidden, which is exactly today's look.
- **Reduced motion:** scroll-driven animations are `animation: none`, explicitly. The existing
  `1ms` override does not reliably neutralise a scroll timeline.
- **The large title is the `<h1>`**; the compact copy is `aria-hidden`.

### D5 — State transitions with weight

| Moment | Motion | Communicates |
|---|---|---|
| Opening-reading confirm, and every `CheckboxField` | The tick draws itself (DrawSVG on a two-point stroke) | You confirmed it. It never animates on first render, so nothing ever looks pre-ticked (§4.7, §14) |
| A nozzle tile changes state | `Flip` morph of pill and figures in place (D8's original intent) | The reading landed |
| Shift close / lock | The status pill morphs; the lock icon swaps closed with a turn | Irreversibility |
| Day lifecycle advances | The connector draws (`scaleX`), then the dot settles | A step completed |
| A row leaves a queue (finalised day, reviewed flagged expense, cleared reading flag) | `Flip` captured before the mutation and played after the refetch; the remaining rows close ranks | Done, gone, the rest moved up |
| The one figure that matters: Today's metered sales (`total_sale_value`, today a card among five) and the cash position's gap (today 1.5 rem) | Displayed at display size, rolled by `Amount` on change; same server strings, only the layout changes | Hierarchy |
| Commit actions (close, lock, finalise, record repayment) | `navigator.vibrate(8)` where supported | Touch feedback on Android |

- **The money rule is unchanged:** `Amount` rolls server strings. It never counts and never
  derives a glyph.
- **A structural test bans** `textContent`, `innerText` and `ScrambleText` in any file that
  animates. The "never tween a money value" rule becomes mechanical.

### D6 — Loading and resting finish

- **Skeletons shaped like their screens** (Today, days, ledger, statement, summary), with a
  shimmer that is a `translateX` on a pseudo-element. The swap to content causes no layout shift.
- **Hover lift on clickable cards and rows**, on `(hover: hover)` devices only: `translateY(-1px)`
  plus `shadow-2`. Focus rings ease in.
- **A type ramp** as tokens (`display / title / headline / body / caption`), so large titles and
  hero figures share one scale.

### D7 — The front door

`Login.tsx` splits into `SignIn` and a lazily loaded `src/showroom/`, fetched after the sign-in
card has painted. A salesman on Slow 4G gets the form first, and staff restored from a refresh
token never download the showroom.

**Constraint (asserted by Playwright):** the Sign in button sits inside the first viewport at
390×844 in both palettes. Nothing pins, smooth-scrolls or covers the form.

Eight sections, seven layout families, at most two eyebrows, and no em-dashes in any UI string.
Copy is a draft for the owner to approve.

| # | Section (draft headline) | Layout family | Motion (desktop ≥1024 / phone) |
|---|---|---|---|
| 1 | Hero: "The day's cash, checked against the meters." + 18-word subtext + sign-in card | Photo hero | Headline reveals line by line on load; photo parallax via ScrollSmoother `data-speed` / none |
| 2 | "Confirm the meter." | Pinned split, live nozzle tile | Scrubbed: opening carried forward, tick draws, pill flips to Confirmed; second beat a mismatch going to Needs review / one-shot beats as each enters |
| 3 | "The gap has a name." | Full-width typographic equation | The §6.4 terms slide in one by one; the gap rolls between two **literal** sample strings / same, one-shot |
| 4 | "Every udhaar has a receipt." | Sticky stack (skill 5.A) | Receipt photo, then ledger with running balance, then printed bill / stacked cards, one-shot |
| 5 | "The bank statement checks the books." | Horizontal pan (skill 5.B) | Statement lines tick: Paytm T+1, BY CASH, a named repayment / scroll-snap row |
| 6 | "The whole month on one screen." | Bento, 4 cells, live chart components | Bars grow and the donut draws on enter |
| 7 | "Built for the night shift." | Split compare: real light and dark screenshots | A divider wipes dark over light (translate, not clip-path); line: "A dropped connection never records ₹5,000 twice" (§6.10, a real feature) |
| 8 | "Bring HiSahab to your pump." | Closing CTA + footer | One "Talk to us" link (contact from the owner) |

**Rules that make it honest:**

- **Every claim names a shipped feature.** Each section carries a comment citing its CLAUDE.md
  section. No invented metrics, testimonials or customer logos.
- **Every figure is a literal string** in `showroom/samples.ts`, labelled "Sample figures" on
  screen. Nothing is fetched, nothing is computed, and the names are invented, never a real
  customer's.
- **Live components** (`Amount`, `Pill`, `SalesBars`, the donut, `LifecycleStrip`, a tile shaped
  like `NozzleTile`) are rendered with those samples. The skill prefers a real component preview
  to a picture of one.
- **Screenshots for section 7** come from `npm run shots`, a Playwright script over the existing
  e2e mocks, so they regenerate when the UI changes.

**Engines and type:**

- ScrollSmoother runs on desktop with a fine pointer only (`smoothTouch: false`). It is not
  created under reduced motion. Pins and scrubs are desktop-only through `matchMedia`
  breakpoints, because pinning on a phone fights the collapsing address bar. Phones get one-shot
  reveals.
- Instrument Serif dresses the wordmark and the front door's headlines, and nothing inside the
  app (§13.28 amended).

**Photographs:**

- Three images: a forecourt at dusk, a salesman with a phone, a handwritten udhaar slip.
- Generated with the image tool and recorded in `public/img/CREDITS.txt` with the prompt,
  flagged for replacement with the owner's own photos.
- Served as webp at two widths; the hero keeps `fetchPriority="high"` and the LQIP.

**Share preview:** `og:title` and `og:description` in `index.html`. `og:image` is written at
build time only when `PUBLIC_ORIGIN` is set, because Open Graph needs an absolute URL.

### D8 — Installable on a phone

- `manifest.webmanifest`: `display: standalone`, `start_url: "/#/today"`, the theme colours,
  192/512 and maskable icons, an `apple-touch-icon`.
- Opened from the home screen, it runs full-screen with no browser bar; safe-area insets are
  already handled.
- **No service worker.** Offline queuing of money writes is a different system, and §6.10's
  idempotency already makes retries safe.
- The icon (an "H" in Instrument Serif on cobalt) needs the owner's approval (skill §11.F: the
  brand mark never changes silently).

### D9 — CSP risk is proven before anything is built on it

`style-src 'self'` permits CSSOM writes (`el.style.x`) and blocks style **attributes** written
through markup. SplitText, ScrollSmoother and ScrollTrigger's `pin` are each proven in Playwright
under the real policy (step 6a) before a section depends on them. If SplitText violates it, the
headlines use a JSX word splitter instead: words as spans in React, which goes through the CSSOM,
with `aria-label` on the parent.

### D10 — Deliberately not done

- A money figure counting up
- Parallax, ambient loops or scroll reveals on data screens
- ScrollSmoother inside the app (it fights native scroll, inputs, the sticky chrome and sheets)
- Scaling the page behind an open sheet (a transformed `#root` breaks the fixed chrome)
- A custom cursor, sound, a marquee, a service worker

### Mechanical decisions

| # | Decision |
|---|---|
| M1 | Branch `phase-24-front-door` from `phase-23-react-frontend` |
| M2 | `src/motion/` holds every GSAP import; `src/showroom/` holds every scroll-story component |
| M3 | e2e `settle()` waits for `document.getAnimations()` to empty instead of polling opacity (covers CSS, WAAPI and view transitions) |
| M4 | New assets live in `frontend/public/`; the `static/**/*` glob ships them (§13.28) |
| M5 | Every scroll-driven CSS rule sits inside `@supports (animation-timeline: scroll())` and has an explicit reduced-motion `animation: none` |

## Build order

| # | Commit | What lands |
|---|---|---|
| 0 | `Phase 24 audit: serve the UI compressed and cached; enforce the bundle budget` | D1; audit 1b; budget script + pytest hook; re-measure |
| 1 | `CLAUDE.md: Phase 24 amends the motion and asset rules` | §11 (24); §14 (below); §13.28; §12 (manifest yes, service worker no); §13 (view-transition fallback); this plan → `docs/phase-24-plan.md` |
| 2 | `Phase 24: motion foundation` | D2: `src/motion/*`, tokens and the CSS mirror, `useMotion`, migrate the six users, structural tests, M3 |
| 3 | `Phase 24: navigation` | D3: `useGo`, directional transitions, shared elements, fallback |
| 4 | `Phase 24: chrome and scroll` | D4 + D6: large titles, sticky headers, charts on view, shaped skeletons, hover, type ramp |
| 5 | `Phase 24: state transitions` | D5: check draw, tile Flip, pill/lock, lifecycle connector, queue Flip, hero figures, haptics |
| 6 | `Phase 24: the front door` | 6a: CSP proofs (D9); 6b: SignIn/showroom split, the eight sections, samples, photographs, `npm run shots`, OG |
| 7 | `Phase 24: installable` | D8 |
| 8 | `Phase 24 notes` | `docs/phase-24-notes.md`, "What shipped" below |

Step 1's §14 additions:

1. A data screen never scroll-tells; pins, scrubs, parallax and smooth scroll live in
   `src/showroom/` only.
2. Showroom figures are labelled literal sample strings, never fetched, never computed, never a
   real customer.
3. Every showroom claim names a shipped feature.
4. Never put the sign-in form below the fold or behind a pin.
5. Three engines, three territories (the table above).
6. Import GSAP only through `src/motion/`.

## Error codes introduced

None.

## Verification

1. `pytest`, including the retargeted structural tests and the new ones:
   - gsap imported only from `src/motion/`;
   - no text tweens;
   - no bare `useNavigate` in screens;
   - the token mirror;
   - the budget.
2. `npm test`, `npm run build` with no warnings, `npm run budget` (≤150 KB initial, ≤70 KB
   showroom), `npm run e2e`.
3. **New Playwright scenarios**, in both palettes, each with an axe pass and zero CSP
   violations:
   - the front door at 390×844, with the Sign in button inside the first viewport;
   - the front door scrolled section by section on desktop;
   - the front door under reduced motion, with every section's text visible without
     scrolling animation;
   - a day row opening its day screen via a view transition, with no console error;
   - finalising a day, after which the row is gone and the rest of the list is intact.
4. **Money unchanged:** every money string on the cash position, day screen, statement and
   summary is byte-identical before and after this phase on the e2e fixtures. The phase changes
   how figures arrive, never what they are.
5. **Performance, by hand:**
   - Lighthouse Slow 4G on the front door: LCP < 2.5 s, CLS < 0.1, INP < 200 ms.
   - Chrome DevTools at 4× CPU throttle, recording a tab change, a drill-down and a showroom
     scroll: no frame task over 50 ms.
6. **On a real phone:**
   - every tab in light and dark, with reduced motion on and off;
   - install to the home screen;
   - throw a sheet during a route transition;
   - the statement's print preview.

## What shipped

*Recorded as each commit lands.*

- **Commit 0 — `d76d075`.** Each defect had a test that failed first.
  - `app/core/static_delivery.py` wraps the UI mount: `GZipMiddleware` (Starlette's, level 9,
    skipping webp and woff2) around a `CacheHeaders` wrapper around the existing
    `SecurityHeaders`. `/assets/*` is `public, max-age=31536000, immutable`; `index.html` is
    `no-cache`; unhashed fonts and photographs are `public, max-age=86400`; error responses get
    no cache header. The API stays outside, as it is for the CSP.
  - The shell's screen entrance is a CSS keyframe (`.screen-enter`, fill mode `backwards` so no
    transform lingers on the screen after it ends) and the tab indicator a CSS transition.
    `useScreenEntrance` was deleted, now unused.
  - **Initial JS: 150.4 → 123.1 KiB.** Front door chunk 18.8 KiB. `scripts/budget.mjs` builds
    into a temporary directory and `tests/test_frontend_suite.py` runs it.
  - pytest frontend + mount files 33 passed; Vitest 63; Playwright 126.
- **Commit 1.** CLAUDE.md: §11 (24), §12 (manifest yes, service worker no), §13.28 (serif scope,
  generated photographs), §13.43 (view-transition fallback), §14 (six guardrails), §15
  (`npm run budget`).

- **Commit 2 — motion foundation.** `src/motion/gsap.ts` (core, tokens, `useMotion`,
  `motionAllowed`), `scroll.ts`, `flip.ts`, `draw.ts`. The six GSAP users migrated, and with them
  the six hand-written `matchMedia` gates: `useMotion` passes its callback a `play` function, and
  only what goes through `play` is gated, so bookkeeping (the previous figure, which dots are new)
  still runs under reduced motion.
  - **Deviation:** the token mirror is checked from pytest, not Vitest. Vitest hands a test an
    empty string for any CSS import, `?raw` included, and reading the file with `node:fs` would
    need `@types/node`, a new dependency. It sits beside the CSP-equality test, which is the same
    shape of check.
  - Two new structural tests: `gsap` imported only from `src/motion/`; no animating module
    touches `textContent` / `innerText`, and no text-rewriting GSAP plugin is used anywhere.
  - e2e `settle()` now also waits on `document.getAnimations()` (CSS and view transitions), with
    infinite animations excluded.
  - Vitest 65, Playwright 126.

- **Commit 3 — navigation.** `src/app/navigation.ts`: `useGo()` (all 41 `navigate` calls in 12
  screens and the shell migrated mechanically; same signature), `directionOf`, `sharedSource`.
  `TABS` moved to `src/app/tabs.ts` so navigation can read it without importing the shell.
  Shared elements: day rows, worklist cards, report and alert rows fly their date into the day
  screen's subtitle; credit rows fly the customer's name into the ledger's.
  - **Fade-through, found by looking.** Frames captured mid-transition showed the first timing
    (outgoing over 240 ms on a slow-start curve) as a double exposure: two screens of figures on
    top of each other at 60 ms. The outgoing screen now clears within 140 ms on a fast-start
    curve and the incoming one starts 60 ms in.
  - **The budget caught its first regression.** `navigation.ts` asked `motionAllowed` of
    `motion/gsap.ts`, which brought GSAP's core back into the first paint (150.3 KiB). The answer
    moved to a GSAP-free `motion/preference.ts`: 123.8 KiB.
  - The scroll reset in the shell became a layout effect, so it lands inside the transition's
    update rather than after the new screen was captured.
  - **Deviation:** back/forward gets the plain CSS entrance rather than nothing (§13.43 amended
    to match). `data-nav` marks a router-started transition and suppresses the plain entrance
    only while one plays.
  - Tests: `directionOf` (Vitest, 7); `useNavigate` banned outside navigation.ts (pytest);
    Playwright records every `startViewTransition` and asserts each reaches `ready` and
    `finished` (a duplicate `view-transition-name` aborts one silently), plus none under reduced
    motion.

- **Commit 4 — chrome and scroll.**
  - Large titles on the six tab roots (`ScreenTitle large`), collapsing into the chrome by CSS
    scroll-driven animations; no JavaScript. Without scroll timelines or under reduced motion the
    large copy is hidden and the chrome is exactly as before. The chrome's copy stays the `<h1>`.
  - Charts grow when they come into view (ScrollTrigger, `once`, transforms only).
  - Sticky group labels (`SectionLabel sticky`) on the Cash hub and the bank screens, with a
    hairline only while stuck (a scroll-state container query).
  - Shaped skeletons (`rows`, `list`, `cards`, `figure`) with a transform shimmer; hover lift on
    standalone clickable cards for fine pointers only; the type ramp's top three steps.
  - **Two contrast defects found by axe, both real.** The longer pages put a cobalt button
    under the translucent tab bar, and labels read against it: the active tab fell to 3.8:1
    (dark) and the muted labels to 4.4:1 (light). The tab indicator is now an opaque mix of
    accent and ground, and the chrome is 88% / 86% opaque instead of 78% / 74%.
  - `settle()` now ignores scroll-driven animations, which are "running" for as long as the page
    exists.
  - Vitest 72, Playwright 130.

- **Commit 5 — state transitions.**
  - `ui/Tick.tsx`: every `CheckboxField` is a native checkbox with a drawn tick (DrawSVG). It
    never draws on first render, so a box that opens ticked does not look as if it was just
    ticked; Vitest asserts both that and a single draw per fresh tick.
  - `useFlipList` (`motion/flip.ts`) on the four `ShiftRowsFrame` lists, expenses, collections
    and the nozzle grid: when a list's contents change, rows move to their new places and new
    rows rise in. It replaced the nozzle tile's scale pop (one engine per element).
  - The lifecycle strip draws each new connector, then lands the dot, in order.
  - `Pill` lands with a CSS settle when its status changes after mount (CSS, because
    primitives.tsx is in the first paint).
  - Hero figures: Today's metered sales (a two-column card at `text-display`) and the cash
    position's gap (`text-title`, on its own line so a long figure wraps rather than overflows).
  - `commitTick()` (`motion/haptic.ts`): 8 ms on close, lock, reconcile, finalise and a new
    repayment; nothing under reduced motion or on iPhone.
  - **Deviations.** The "row leaves a queue" moment became "a list changed": finalising and
    reviewing happen on screens where the queue is not visible, so the row was never seen
    leaving. And no lock-icon morph: the lock button disappears once a shift is locked, so the
    pill landing carries that change.
  - The unticked tick's SVG is hidden with `visibility`, not `opacity: 0`: the e2e settle check
    read a permanent zero opacity as an entrance that never finished, which is also the more
    honest markup.
  - Vitest 75, Playwright 130.

- **Commit 6 — the front door.**
  - `src/showroom/`: seven sections after the hero (meter, gap, udhaar, bank, month, night shift,
    close), each a different layout, each claim commented with the CLAUDE.md section it shows.
    `samples.ts` holds every figure as a literal string; a pytest check proves in `Decimal` that
    they agree (the gap with its terms, the ledger with its balance, Paytm with card + UPI, every
    share with its rupees). It was mutation-checked: changing one share or the gap fails it.
  - `motion/story.ts`: `useStory` splits desktop (pins, scrubs, ScrollSmoother) from phone (the
    same beats, one-shot) and reduced motion (nothing moves; every section is there).
  - The hero gained a headline that rises word by word and a one-line value proposition; the
    sign-in card stays in the first viewport (asserted at 390x844 in both palettes).
  - **6a, the CSP proof.** ScrollTrigger pins and ScrollSmoother write styles through `cssText`,
    which `style-src 'self'` permits; the desktop scroll-through runs under the production CSP
    with zero violations. **SplitText was not used**, for a React reason rather than a CSP one:
    it replaces a heading's text nodes with its own spans and reverts by assigning `innerHTML`
    inside DOM React owns. `showroom/Words.tsx` renders the word spans in JSX instead.
  - **Deviation: licensed stock, not generated photographs.** Higgsfield had 0 credits; the owner
    chose Adobe Stock on the free tier: 969116629 (a forecourt at night, the new hero, 58 KB at
    1280 against the skyline's 147 KB) and 288456618 (hands holding paper bills). The skyline
    files were deleted. Only two photographs were needed, not three.
  - **The night-shift phone shows the Entry screen, not Today.** Today's e2e fixture carries a
    real trading day's takings (CLAUDE.md §6.4's 30 July), and the front door shows sample
    figures only. `npm run shots` re-takes both palettes from the real app.
  - Bugs found on the way: ScrollSmoother was handed selector strings, which `useGSAP` resolves
    inside the component, so it silently never attached; the phone's sideways bank row was a
    scrollable region with no keyboard access (axe); headlines stranded a last word ("shift.");
    the budget's front-door pattern missed the `Showroom` chunk and every shared chunk, so it now
    walks Vite's manifest: **64.1 KB beyond the first paint, against 70**.
  - "Talk to us" reads `VITE_SALES_CONTACT` at build time and is absent without it.
  - Playwright 136.

- **Commit 7 — installable.** `public/manifest.webmanifest` (standalone, starts on `#/today`,
  cobalt splash), icons at 32/180/192/512 plus a maskable 512 with the glyph inside the safe zone,
  a meta description, and Open Graph title and description. `og:image` (a 1200x630 card made from
  the hero photograph) is written by a small Vite hook only when `PUBLIC_ORIGIN` is set at build,
  because a relative one is silently ignored by every link preview. No service worker. Playwright
  checks the manifest and that every icon it names is served as a PNG. Playwright 138.

- **Commit 8 — verification and notes.**
  - pytest **1,708 passed** (Phase 23 ended at 1,697; the eleven new tests are the five delivery
    tests, the budget, and five structural checks). Vitest **75**, Playwright **138**, `npm run
    budget` within both caps.
  - **Slow 4G, measured** (Chrome DevTools Protocol: 150 ms latency, 1.6 Mbps down, 4× CPU),
    against `uvicorn app.main:app` serving the build with the new delivery wrappers: Sign in
    usable at 1.94 s, **LCP 2.23 s**, **CLS 0**. The entry chunk crosses the wire at 118.5 KB
    gzipped, against 385 KB uncompressed before this phase.
  - Not verified, and said so in the notes: a real phone, Safari, a Railway deploy.

## Still owed by the owner

- Approval of the front-door copy, and the contact for "Talk to us" (WhatsApp number or email),
  set as `VITE_SALES_CONTACT` in the build environment.
- Approval of the app icon.
- Real photographs of the pump, to replace the two licensed stock placeholders (same file names
  and sizes; nothing else changes).
- The production domain, set as `PUBLIC_ORIGIN` in the build environment, for `og:image`.
- The Phase 23 items still open: a Railway deploy, real-day parity, a phone hand-check, and the
  `confirm-repayments` business-rule question.
