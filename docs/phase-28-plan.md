# Phase 28 — The front door shows the product; the app gets one finish: Plan

> Written before the code on 6 October, from the owner's brief. "What shipped" is filled in as
> commits land. No migration, no table, no endpoint, no business rule, no npm dependency.

## Context

The owner asked: *"Use both the motion ai skill and the 21st dev skill, find the best template,
animations, scroll effects GSAP, everything and make this ui of hisahab website better."*

Phases 23–25 rebuilt the interface on React, Tailwind v4 and GSAP 3.15, with View Transitions and
the Phase 12 spring, and gave the front door a scroll story. So this is a second pass, and it was
driven by looking rather than guessing. Two sources:

- The live site, captured at 1440×900 and 390×844 in thirteen frames each, top to bottom.
- The smoke suite's full-page screenshots of every in-app screen, in both palettes.

### What the capture showed

**On the front door:**
- **The hero reads as a login page.**
  - There is no navigation and nothing for a buyer to press.
  - The headline sits on the haziest part of the photograph.
  - On a 1440 screen everything lives in a 28rem column.
- **Pinned beats hold a headline alone on a blank screen.** This is most of their 120–140 % pin
  length.
- **The udhaar stack fades each card to half opacity while the next slides over it.** The result
  is a double exposure.
- **Weak middle and ending:**
  - The bank pan clips its cards at both edges of a mostly empty screen.
  - The phone mockup is small.
  - The close has no button at all, because `VITE_SALES_CONTACT` is unset.
- **No rhythm after the hero.** Grey paper and a serif headline continue all the way down.

**Inside the app:**
- About 390 one-off `text-[…rem]` sizes, 179 of them `0.8125rem`.
- Today is six equal cards, each ending in a full-width "Details" button.
- Link tiles, clickable rows, hero figures and warning notices are hand-built up to five times
  each, in five slightly different styles.

### Owner's answers (6 October)

| Question | Answer |
|---|---|
| Scope | The front door first, then the app |
| Front-door direction (three were offered: product first, cinematic night shift, one continuous night) | **Product first**: a phone in the hero, then a pinned phone that walks through a day, then a feature grid |
| Install the Motion library? | **No.** Stay on GSAP and use the skill as a source |
| 21st.dev | The owner signs in so its catalogue can be searched and its code pulled |

## Previous-phase audit

Phase 27 is merged and deployed (`f2d4c84`). It touched the shift lifecycle and one button, and
nothing on the front door or in the shared UI. The audit for this phase is therefore of the
front door and the screens themselves. It found six bugs:

| # | Bug | Where | How it hid |
|---|---|---|---|
| B1 | Section headlines never animate. `revealHeadlines` queries `[data-headline]`, and nothing sets it | `Showroom.tsx:78`, `Words.tsx` | Static headlines look finished |
| B2 | On desktop the bank section's rupee figures have no "Sample figures" label: it is `lg:hidden` | `Showroom.tsx:411` | The e2e check counts hidden nodes |
| B3 | The Ken Burns drift never pauses, so its scale stacks on the walk-in's (a Phase 25 D1 promise) | `styles.css:699` | Nothing measured it |
| B4 | The sign-in form sits inside `#smooth-content`, so on desktop ScrollSmoother moves it, against §14 | `Login.tsx:223` | Never recorded as a deviation |
| B5 | Today's Expenses, Credit and Deposits cards show "…" forever if their query fails | `today.tsx:447,456,469` | Only the Sales card handles an error |
| B6 | A past shift opened by id is titled "Today" | `today.tsx:110,134` | The title is a literal |

Baseline measured before any change:
- **1,740 backend tests pass in 73 s**;
- the initial JavaScript, **124.2 of 150 KB**;
- the front-door JavaScript, **65.6 of 70 KB**.

## Decisions

### D1 — The two skills are sources, not dependencies

**GSAP stays the only animation engine.**

**The Motion skill provides three things:**
- **Its universal rules, as the audit rubric:**
  - transform and opacity only;
  - no allocation in per-frame callbacks;
  - `will-change` used sparingly;
  - no overshoot in a money UI.
- **CSS `linear()` springs** (D8).
- **Its reference effects, rebuilt in GSAP:** "screenshot scroll reveal" (a screenshot tilts
  upright and scales in as you scroll), "hero parallax layers" and "smooth tabs".

The library itself is refused by §14 (one element, one engine), and MotionScore audits need a paid
Motion+ account. Motion is verified frame by frame instead, as Phase 24 established.

**21st.dev is a template source.** Its components depend on framer-motion, shadcn, Radix, Lucide
or Lenis, so each one is read with `21st get` and ported onto our tokens, primitives and GSAP.
Nothing is installed. `.21st/` (the CLI's design context) stays untracked, like `.claude/`.

Searches, each run with `--context auto`:
- sticky-scroll phone steps
- a hero with a phone and a form
- a minimal landing navbar
- a bento feature grid
- an animated tab indicator
- a stat-card hero figure
- a settings link tile

The references used are recorded under "What shipped".

### D2 — ScrollSmoother is removed

ScrollSmoother is the cause of B4. It also breaks native `position: sticky`, which the pinned phone
needs, and it costs front-door budget. Removing it:
- `motion/story.ts` keeps `useStory` and ScrollTrigger only;
- `#smooth-wrapper` and `#smooth-content` go;
- scrubs keep a numeric `scrub`, so motion that follows the scroll still eases.

A §14 guardrail stops it coming back.

### D3 — The front door, product first

| Section | Desktop (≥1024, fine pointer) | Phone (<1024) |
|---|---|---|
| Nav (in `Login.tsx`, absolute) | Wordmark; "How it works", "Sign in" (focuses email); "Talk to us" only if `VITE_SALES_CONTACT` is set | Wordmark, "How it works" |
| Hero | Two columns, `max-w-7xl`. Left: a larger headline, the subline, the form, and "Trouble signing in?" under the form. Right: a phone showing the Today screen | Unchanged order; Sign in stays in the first viewport |
| Travel | The Phase 25 walk-in, unchanged (the owner asked for it) | Unchanged |
| `how` — "A day at the pump" | Steps on the left with a progress rail. The phone is CSS-sticky on the right and its screen changes per step | Each step's copy, then its screen inline |
| `features` | A bento of six tiles, each naming a shipped feature | Stacked |
| `close` | "Bring HiSahab to your pump.", "Talk to us" if set, and **always** a Sign in button | Same |

The steps of "A day at the pump" (copy is a draft for the owner):

| Step | Claim (CLAUDE.md) | Phone screen | Beat |
|---|---|---|---|
| 06:00 Open the shift | §4.7, the shifts are data | Today | — |
| Confirm the meter | §4.7 | Two nozzle tiles | The tick draws, Confirmed lands, then the second tile disagrees and flags for review |
| Declare the cash | §5.2, the cash row is a declaration | Collections | The mode rows arrive |
| Every udhaar has a receipt | §6.6 | The slip and the ledger | The ledger rows arrive |
| The gap has a name | §6.4 | The equation | The terms arrive; the gap lands |
| The bank checks the books | §5.3a | Statement lines | Each tick draws |

**Navigation never uses an `href="#…"` anchor.** The app is hash-routed, so `#how` is a route, not
a scroll target. Buttons call `scrollIntoView` instead: smooth when `motionAllowed()`, instant
otherwise.

**The nav is absolute, not sticky.** A blurred bar over the moving photograph is §13.29's failure.

**Under reduced motion the inline layout is used at every width**, so every step and every screen
is simply there.

### D4 — The phone's screens are replicas built from primitives

They live in `showroom/phone/`:
- **Built from real primitives:** `Card`, `Pill`, `ListRow`, `Amount`, and the lifecycle strip,
  moved to `ui/lifecycle.tsx`.
- **Every figure is a literal in `samples.ts`.**
- **The whole page tells one sample day.** Today's metered sales equal the gap section's, and the
  collections equal its card and UPI terms. The pytest agreement test is extended to hold them
  together.
- **Decorative.** Each screen is `inert` and `aria-hidden` behind a `sr-only` description, renders
  no buttons, and has "Sample figures" beside it.

The hero phone is a named lazy export from the showroom chunk: one request, after idle, with a
rise-in that hides the wait. §13.46 records the cost: a redesigned real screen does not update its
replica.

### D5 — Choreography

- **One ScrollTrigger per step** (`top center` to `bottom center`) sets the active step. GSAP
  fades the outgoing screen out and the incoming one in, then plays that screen's beat once.
- **The step copy dims through CSS on `data-active`.** That is a different element from anything
  GSAP moves.
- **The progress rail is a single scrubbed `scaleY`.**
- **Bento hover beats** run on desktop only, using transform and opacity only.
- **No money is tweened.** `Amount` rolls only between literal strings.

### D6 — The app's finish

- **A type scale as tokens.**
  - Steps: `body` .9375rem, `callout` .875, `footnote` .8125, `caption` .75, `micro` .6875 and
    `figure` 2.25rem/650, beside the existing `headline`, `title` and `display`.
  - About 390 one-off sizes are codemodded to them.
  - A pytest refuses new ones in `screens/`, `ui/` and `app/`, with an exemption list that gives a
    reason for each entry.
  - The serif CSS class `.display` becomes `.serif`, ending its clash with the `text-display`
    token.
- **One primitive per shape:**
  - `LinkTile` replaces 5 copies;
  - `RowLink` replaces about 7;
  - `HeroFigure` replaces 6;
  - `Notice` replaces 4;
  - `CardTitle` gives one card heading.
- **Today.**
  - Each domain card is one pressable surface that opens its sheet; the six "Details" buttons go.
  - Metered sales leads, spanning two columns.
  - B5 and B6 are fixed.
- **Consistency:**
  - arrival choreography on the screens that lack it;
  - shared-element sources from Summary and the statement into the ledger and the day;
  - sticky labels where Phase 24 D4 named them;
  - shaped skeletons for the Cash hub and the Day screen.

### D7 — Deliberately not done

- The Motion package, or any 21st component or dependency.
- ScrollSmoother anywhere.
- Pins, parallax or story on a data screen.
- A sticky blurred nav over the photograph.
- A count-up, a marquee or a loop.
- A desktop sidebar: Phase 25 asked for full-width tabs.
- New photographs: the owner's own are still owed.

### D8 — CSS springs that share the gesture physics

- **The script.** A small script samples `spring.ts`'s own `Spring` for `PRESETS.ui` and
  `PRESETS.snap` and writes `linear()` easings, with their settle durations, as `--spring-ui` and
  `--spring-snap`.
- **Who uses them.** The tab indicator, `pressable`'s release and the pill landing use them, so a
  CSS transition and a dragged sheet move with one physics.
- **The test.** A Vitest test re-samples and asserts the CSS copy matches.
- **What stays put.** The four `--ease-*` tokens and their mirror test are untouched.

### Mechanical decisions

| # | Decision |
|---|---|
| M1 | Branch `phase-28-ui-polish` from `main` |
| M2 | `showroom/Showroom.tsx` splits into `Story.tsx`, `Features.tsx` and `phone/*`; every scroll-story component stays in `src/showroom/` (§14) |
| M3 | Front-door headlines carry `data-headline` from `Words` itself, so a new headline cannot forget it |
| M4 | "Sample figures" is asserted with `toBeVisible()` per label, never by counting attached nodes |
| M5 | Budget: over the limit means cutting, never raising the cap without the owner |

## Build order

| # | Commit | What lands |
|---|---|---|
| 0 | — | `21st init --design-context`, the D1 searches and `21st get` (once the owner has signed in) |
| 1 | `CLAUDE.md: Phase 28` | §11 entry 28, §13.46, four §14 guardrails, this plan |
| 2 | `Phase 28: the front door's four bugs` | B1–B4 test-first; ScrollSmoother removed (D2) |
| 3 | `Phase 28: nav and the two-column hero` | D3's nav and hero |
| 4 | `Phase 28: the phone and its sample day` | `showroom/phone/*`, `samples.ts`, the agreement test, `ui/lifecycle.tsx` |
| 5 | `Phase 28: a day at the pump` | The `how` section and its choreography |
| 6 | `Phase 28: the feature grid and the close` | `features` and `close`; the old six sections retire |
| 7 | `Phase 28: the type scale` | Tokens, codemod, structural test |
| 8 | `Phase 28: one shape for tiles, rows, figures and notices` | D6's primitives |
| 9 | `Phase 28: Today and consistency` | B5 and B6 test-first, then the rest of D6 |
| 10 | `Phase 28: CSS springs from the spring presets` | D8 |
| 11 | `Phase 28 notes` | `docs/phase-28-notes.md`; "What shipped" below |

Pushing to `main` and deploying happen only when the owner says so.

## Error codes introduced

None.

## Verification

- [ ] `npm run typecheck`, `npm test`, `npm run budget`: initial ≤ 150 KB, front door ≤ 70 KB
- [ ] `npm run e2e` in both palettes, under the production CSP, with axe:
  - [ ] Sign in sits inside the first viewport at 390×844
  - [ ] nav buttons scroll without changing `location.hash`
  - [ ] the close always offers Sign in
  - [ ] every front-door headline has `data-headline`
  - [ ] every "Sample figures" label is visible
  - [ ] under reduced motion every step and screen is visible
- [ ] Full `pytest`, including the samples agreement, the anchor ban and the type-scale ban
- [ ] The live-site capture is re-run against `vite preview` at 1440 and 390 in both palettes and
      compared with the 6 October contact sheets
- [ ] Frames captured mid-swap for every step and for the hero phone's entrance, read, and the
      throwaway spec deleted
- [ ] `21st review` on the changed paths

## What shipped

_Filled in as commits land._

## Still owed by the owner

- Approval of the new step copy, and of the bento tiles' wording.
- `VITE_SALES_CONTACT`, `PUBLIC_ORIGIN`, icon approval and his own photographs (from Phase 24).
- A look on his own phone and Mac.
