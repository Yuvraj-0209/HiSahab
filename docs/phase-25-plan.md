# Phase 25 — The owner's first look: Plan

> Written before the code and approved by the owner on 4 October; "What shipped" is filled in as
> commits land. No migration, no table, no endpoint, no business rule, no npm dependency.

## Context

The owner used Phase 24 and liked the front door. He asked for six refinements; each is traced
here to its actual cause, found by driving the build at 1440×900 in Playwright and reading the
code.

| Ask | Cause found |
|---|---|
| Scrolling the front door should feel like walking into the station, towards the nozzles | Today the photograph only drifts (Ken Burns) and parallaxes 6%. **Owner's choice (4 Oct): no video, a camera push into the current photo.** |
| Tabs crammed in the middle | `Shell.tsx` TabBar is `max-w-xl` (36rem) centred, on any screen width |
| Today disproportionate; use the full width; six boxes in two rows | `main` is capped at `max-w-[76rem]`; Phase 24's `hero` makes Metered sales span 2 columns, so the six cards fall into 2 + 1 + 3 rows; the full-width "Close shift" bar reads cheap on a wide screen |
| "Entry" written twice | **Confirmed bug.** A large title collapses into the chrome by a scroll-driven animation; on a page too short to scroll the scroll timeline is *inactive*, the animation never applies, and both copies show. Hits every short tab root (Entry, Admin) |
| Nozzle readings look cheap: tiny boxes | `readings.tsx` grid is `grid-cols-2 sm:grid-cols-3 lg:grid-cols-4` of ~180px tiles. His reference is the old Phase 12 screen: two wide columns of big cards |
| The Add / nozzle animation feels glitchy | Frames are a steady 60 fps, so it is not dropped frames. Three real causes: (1) on desktop the sheet travels ~550 px from the bottom edge in ~300 ms, a lurch on a monitor, and starts far from the button that opened it; (2) `body { overflow: hidden }` removes a classic (mouse-attached Mac) scrollbar, so the whole page jumps sideways on open and back on close; (3) the first input is focused at mount, so on a phone the keyboard pops up mid-animation and resizes the viewport under it |

## Decisions

### D1 — The front door's push-in (`Login.tsx`, `styles.css`, new `showroom/PushIn` logic)

- The backdrop is already `position: fixed`, so **nothing is pinned**: the hero content scrolls
  up while the fixed photograph is scrubbed by the scroll over the first ~1.6 screens. Works the
  same on a phone, with no fight with the collapsing address bar.
- **Two layers for depth:**
  - The photograph pushes in, scaling about 1 → 2.3 towards the central dispenser.
  - A cut-out of the canopy and pillars scales faster (about 1 → 3.6) and leaves past the screen
    edges, which reads as walking under the roof rather than zooming into a picture.
  - The cut-out is made once from the licensed photograph: Adobe `image_select_by_prompt`
    ("canopy roof and pillars") gives a mask, and Pillow composites it to a transparent webp.
  - **Fallback if the mask is poor:** a single layer plus an expanding vignette.
- A warm light gradient brightens as you pass under the canopy (gradient opacity, not a
  `filter`, §13.29).
- A new transparent "travel" section sits between the hero and the story. One line rises in it
  over the dispenser: "Every night, somebody reads this meter." Then "Confirm the meter." slides
  up over it on the ground colour.
- **Aiming at the dispenser:** the photo uses `object-fit: cover`, so where the dispenser lands
  on screen depends on the viewport's shape. A small pure function computes the
  `transform-origin` for each viewport, recomputed on resize (`invalidateOnRefresh`). It is
  geometry only and unit-tested.
- **Unchanged:** the sign-in card stays in the first viewport (asserted); transforms and opacity
  only; reduced motion shows the still photo exactly as now; the Ken Burns drift stops once
  scrolling starts. The push-in replaces the existing 6% band parallax, so each element keeps a
  single engine.

### D2 — Wide screens (`Shell.tsx`, `routes.tsx`, `tabs.ts`)

- **Tab bar** spans the full width (with gutters), six equal columns like the Phase 12 bar. The
  active pill is a fixed width (about 7.5rem) centred in its column, so it doesn't become a
  300 px slab. It stays opaque (Phase 24's contrast fix) and slides with the same CSS transform.
- **Content width:** a route `handle.wide` lets the shell widen `main` to
  `max-w-[100rem]` with `lg:px-10`. It is set on Today, Readings and Summary only: list-and-form
  screens stay readable at 76rem instead of stretching rows across 1600 px.

### D3 — Today (`today.tsx`)

- **The shift band:** one full-width card showing status pill, date, "06:00 onwards ·
  shift 1", and the lifecycle action (Close / Lock / Reopen) as a right-aligned button. On a
  phone it stacks and the button goes full width. This replaces the stretched full-width
  button bar.
- **Six equal cards, three across and two down** (`sm:grid-cols-2 xl:grid-cols-3`), all the same
  height. Phase 24's `hero` span is removed, because it broke the rows.
- **Each card:**
  - an icon chip in an accent tint;
  - title and caption;
  - the figure at one size (`text-title`), still rolled by `Amount`;
  - three supporting rows;
  - a quiet "Details" row.
- **Motion:** arrival stagger (exists), the figure roll (exists), hover lift on desktop.
- The money rules are untouched: same server strings, same absent words.

### D4 — The duplicated title (`styles.css`)

Inside the existing `@supports (animation-timeline)` + no-preference block:
- the collapsible compact title gets a **base `opacity: 0`**;
- its keyframes run *to* visible.

On a page that cannot scroll, the inactive timeline leaves it hidden and only the large title
shows. Once the page scrolls, it behaves as it does now. Without support or under reduced
motion: compact title only, unchanged. Covers Entry, Admin and any future short tab root.

### D5 — Nozzle readings (`readings.tsx`)

- **Big cards:** one column on a phone, two from `md`, about 13rem tall, in the wide container.
  Grouped by fuel under the existing sticky-able labels.
- **Each card:**
  - **Header:** the nozzle label at headline size, "dispenser · fuel · unit", and the status
    pill.
  - **Progress strip:** Opening → Closing → Done. It reuses the lifecycle-strip idea and draws
    as a step completes.
  - **Three figures:** Opening, Closing, Sold (with unit, tabular).
  - **Footer:** the next action in words ("Confirm the opening", "Enter the closing", "Review
    the flag", "View"), so the card says what a tap does.
- **Never shown as confirmed before it is (§4.7).** A not-started nozzle shows the chained value
  labelled "Carried forward, check it against the meter", and its strip's first step is empty.
- `useFlipList` stays on the grid, so a saved reading reflows the cards smoothly.
- The old `NozzleTile` is replaced, not kept beside the new card.

### D6 — Sheets that open smoothly (`ui/Sheet.tsx`, `styles.css`)

- **Desktop / fine pointer (`min-width: 640px and pointer: fine`): a centred dialog.** It rises
  16 px, scales 0.97 → 1 and fades in on a critically damped spring (`PRESETS.ui`, no
  overshoot). The scrim fades with it. There is no drag on desktop. The same spring drives it,
  so open and close stay interruptible.
- **Phone: the bottom sheet, unchanged in feel**, including drag, throw and rubber-band.
- **`html { scrollbar-gutter: stable }`:** locking the scroll no longer changes the page width,
  so nothing jumps sideways.
- **Focus moves to the dialog itself on open** (`tabIndex=-1`), not to its first input:
  - a phone keyboard no longer pops up mid-animation;
  - on desktop, the first input is focused once the spring settles.
- **Hover lift is suppressed while a sheet is open**, so the tapped card doesn't snap back
  under the scrim.

### Mechanical

- **Branch:** `phase-25-first-look`, stacked on `phase-24-front-door`.
- **Docs:** `docs/phase-25-plan.md` and `docs/phase-25-notes.md`, plus a short CLAUDE.md §11 (25)
  entry.
- **Credits:** the cut-out layer goes in `CREDITS.txt` (derived from Adobe Stock 969116629).

## Build order

| # | Commit | What lands |
|---|---|---|
| 1 | `CLAUDE.md: Phase 25` + plan | §11 (25); this plan → `docs/phase-25-plan.md` |
| 2 | `Phase 25: sheets open smoothly` | D6 |
| 3 | `Phase 25: the duplicated title` | D4, with a failing test first (§10) |
| 4 | `Phase 25: wide screens and the tab bar` | D2 |
| 5 | `Phase 25: Today` | D3 |
| 6 | `Phase 25: nozzle readings` | D5 |
| 7 | `Phase 25: walking into the station` | D1 + the canopy cut-out asset |
| 8 | `Phase 25 notes` | notes, "What shipped" |

## Verification

1. **Failing test first for the title bug:** at 1440×900 on `#/entry`, the chrome's compact
   title has computed opacity 0 and exactly one "Entry" is visible. It fails before D4 and passes
   after.
2. **New Playwright checks**, in both palettes, with axe and zero CSP violations each:
   - the tab buttons span ≥ 90% of a 1440 viewport;
   - Today's six cards sit in exactly three columns and two rows at 1440;
   - Readings cards are ≥ 500 px wide at 1440 and full width at 390;
   - a desktop sheet's box is centred and `scrollbar-gutter` is `stable`;
   - after scrolling 70% of the travel section, the front door's photo is transformed
     (scale > 1.5);
   - under reduced motion, the photo does not move;
   - the sign-in button is still in the first viewport at 390×844.
3. **Vitest:** the cover-crop origin function (landscape, portrait, square viewports).
4. **Frames, by eye**, the Phase 24 habit:
   - a recorded video of a nozzle sheet and an Add sheet opening on desktop and at 390;
   - the push-in at five scroll positions on desktop and phone;
   - Today, Entry and Readings at 1440 and 390.
5. `pytest` (full), `npm test`, `npm run budget` (initial ≤ 150 KB; front door ≤ 70 KB JS), and a
   Slow-4G LCP re-measure (must stay < 2.5 s: the cut-out loads after idle and is not the LCP).

## What shipped

*Recorded as each commit lands.*

## Still owed by the owner

- The Phase 24 items: copy approval, `VITE_SALES_CONTACT`, icon approval, `PUBLIC_ORIGIN`, his
  own photographs, a phone hand-check, a Railway deploy.
- Optional later: real night footage from his pump would turn the push-in into a true walk-in;
  that would be its own small phase (a frame sequence on a canvas needs a §13.29 decision).
