# Phase 25 — The owner's first look: Notes

> A learning reference for why each of the owner's six requests became the change it did. The plan
> and the running "What shipped" record are in `docs/phase-25-plan.md`.

---

## What was built

- **The front door walks into the station.** Scrolling past the sign-in card pushes the
  forecourt photograph in towards the dispenser, while a cut-out of the canopy and pillars, in
  front of it, grows faster and passes overhead. It ends on one line over the dispenser:
  "Every night, somebody reads this meter."
- **Sheets open calmly on a desktop.** They are a centred dialog that rises 16 px and fades in,
  instead of a panel thrown 550 px up from the screen edge. Opening one no longer shifts the page
  sideways, and on a phone the keyboard no longer rises mid-animation.
- **"Entry" is written once.**
- **Six tabs spread across the window,** with a fixed-width pill on the active one.
- **Today, Readings and Summary use a wide monitor.** Today's six cards sit three across and two
  down, under one shift band with its action on the right.
- **Nozzle readings are big cards,** two across. Each has a step strip, three figures, and the
  next action in words.
- No migration, table, endpoint, business rule or npm dependency.
- **Tests:** pytest 1,708 (unchanged), Vitest 78 (+3), Playwright 154 (+16).
- **Budget:** first paint 124.1 KB and front door 64.8 KB, against caps of 150 and 70.
- **On a Slow 4G phone at DPR 3:** LCP 2.29 s and CLS 0.

## Find the cause before changing anything

Every request was traced to a cause first, by driving the build at 1440×900 and reading the
code. Two requests turned out to be something other than what they sounded like.

- **"Entry is written twice" was a real bug, not a layout preference.** The large title folds
  into the chrome through a scroll-driven animation. The chrome's copy was visible by default,
  and the animation hid it until you scrolled. On a page too short to scroll, the scroll timeline
  is *inactive*, and an inactive timeline applies no animation at all — so the default won and
  both copies showed. The fix inverts the default: hidden unless the animation reveals it. A
  test was written first and failed at opacity 1. **When a feature is "progressive
  enhancement", its absent state is the one users see most.** Here, any short page.
- **"The animation is glitchy" was not dropped frames.** A video stepped frame by frame showed a
  steady 60 fps. Three other causes made it feel wrong:
  - A bottom sheet on a monitor travelled 550 px in 300 ms, from nowhere near the button that was
    pressed. It now opens as a centred dialog.
  - Locking the scroll removed a classic scrollbar, and the page jumped sideways. Playwright draws
    a classic scrollbar, so a test now asserts the width does not change.
  - The keyboard rose mid-animation on a phone. Focus now moves to the dialog itself.

  **"Smooth" is about where things move and what moves with them,** not only the frame rate.

## How a photograph becomes a walk

The owner chose a push into the existing photo over filming or generating footage. A single
zoom reads as zooming into a picture, so the depth came from parallax:
- **Two layers.** A cut-out of the canopy and pillars is laid exactly over the photograph and
  grows faster (3.4× against 2.3×), so the near roof passes overhead.
- **The patch that was dropped.** The obvious worry was the photograph's own canopy showing
  behind the lifted cut-out. The first fix painted a "ceiling" patch behind it, and offline
  previews showed a flat olive block, worse than the problem. Without any patch, the
  photograph's canopy reads as the *far* side of the roof and a far row of pillars, which is
  what walking under one looks like. It was judged from rendered frames, not by reasoning.
- **Nothing is pinned.** The backdrop was already fixed, so the hero scrolls away over a moving
  photograph. A phone's collapsing address bar, which fights every pinned section, has nothing
  to fight.
- **The aim depends on the window's shape.** With `object-fit: cover`, a portrait phone shows a
  narrow middle slice while a monitor shows nearly everything. `showroom/focus.ts` computes where
  the dispenser sits, and Vitest covers both shapes.
- **Magnification exposes the first-paint file.** The light 1280 px image that keeps LCP fast
  turned to mush at 2.3× on a phone. The 3000 px original now fades in over it once the page is
  idle (not on data saver or 3G). LCP is unchanged, because the largest paint is the headline.

## Deviations from the plan

- **No painted patch behind the canopy** (see above).
- **No hover-lift suppression under an open sheet.** With the app `inert`, the hover ends and the
  card eases back under the scrim on its normal transition. Seen in the recordings, there was
  nothing to fix.
- **The full-resolution images** were not in the plan; magnification forced them.

## What was not verified

- A real phone and a real mouse-attached Mac, the two machines the original complaints came from.
- Safari: View Transitions and scroll timelines are enhancements, but nobody has looked.
- A Railway deploy, still owed from Phase 23.

## Still owed by the owner

- His look at all six changes on his own screens.
- The Phase 24 items: copy approval, `VITE_SALES_CONTACT`, icon approval, `PUBLIC_ORIGIN`, his
  own photographs, and a deploy.
