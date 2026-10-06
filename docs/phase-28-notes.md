# Phase 28 — The front door shows the product; the app gets one finish: Notes

> A learning reference for why each change took the shape it did. It covers the bugs the tests
> caught along the way, and every place the build departed from `phase-28-plan.md`. The plan
> says what was decided; this says what happened.

## What the phase was for

The owner asked for the Motion skill and the 21st.dev skill to be used: "find the best template,
animations, scroll effects GSAP, everything". The phase started from looking rather than
guessing, with two sets of captures:

- the live front door at 1440×900 and 390×844, in thirteen frames each;
- the smoke suite's full-page screenshot of every in-app screen.

The owner then chose three things: the front door first, the "product first" direction, and GSAP
only.

## The two skills, and what "using" them meant here

**Neither skill's library was installed, and that was the decision, not a shortcut.**

- **Motion.** The Motion package is a second animation engine, and §14 has forbidden two engines
  in one tree since Phase 23. The reason is concrete: two engines write the same `transform` on
  the same frame, and the judder cannot be attributed to either. So the skill was used for the
  three things it offers without its library:
  - **Its universal rules, as the rubric.** Transform and opacity only, no allocation per
    frame, `will-change` sparingly, no overshoot in a money UI.
  - **Its reference effects, rebuilt in GSAP.** The hero phone's drift is its "hero parallax
    layers". The pinned phone that changes screen is its "screenshot scroll reveal", crossed with
    21st's sticky-scroll sections.
  - **Its CSS-spring technique** (D8). This is the one with a test behind it.
- **21st.dev.** Its components are built on framer-motion (Motion's old name), shadcn, Radix,
  Lucide or Lenis. Each of those is a dependency this codebase does not have and §13.19 says to
  ask about. So 21st was always going to be a *template source*: read the component, port the
  layout onto our tokens and primitives.

**The 21st CLI was never signed in.** The owner chose to sign in, and every check through the
session found it signed out. So the CLI steps (`21st init`, `search`, `get`, `review`) did not
run. 21st's public pages were browsed instead; the references are under "What shipped". That
is a weaker use of the skill than planned, and it is listed as owed rather than glossed.

**MotionScore**, the skill's performance audit, needs a paid Motion+ account. Motion was
verified the way Phase 24 established: by capturing frames mid-animation and reading them.

## The front door

### The hero reads as a product, and the form stays where it was

The two-column hero applies only at 1024px and up. Below that, nothing about the sign-in moved,
and the Phase 24 Playwright assertion (Sign in inside the first viewport at 390×844) still holds.
The phone in the hero is a **named lazy export from the same module as the story**:

```ts
const HeroPhone = lazy(() => import("../showroom/Showroom").then((m) => ({ default: m.HeroPhone })));
```

Same module, same chunk, one request after idle. Its column reserves its full height before it
arrives, so nothing shifts when it does.

**The wash turned sideways.** On a phone, the photograph fades to the ground colour at the bottom,
where the form sits. On a wide screen the words are on the left, so the wash becomes a
left-to-right dark scrim (still gradients, §13.29). The old hazy-headline problem goes with it.

### Why the phone is drawn with CSS `zoom`, not `scale()`

The replica screens are laid out at 390px, a real phone's width, from the app's own primitives
at their own sizes, and then drawn smaller. Two reasons for `zoom` over `transform: scale()`:

1. **`zoom` re-lays text out at the smaller size.** `scale()` shrinks a rendering of it, so text
   is crisp with `zoom` and soft with `scale()`.
2. **`zoom` leaves `transform` free.** The story animates the screens, and an element can have
   only one `transform`.

The zoom steps down on short windows (`max-height` media queries), so the pinned phone always fits
between the top and bottom of the screen.

### Why the story needed ScrollSmoother gone (D2)

Two reasons, one of them a bug:

- **The bug (B4).** ScrollSmoother wraps the page in a fixed element and moves the content by
  transform, and the sign-in form was inside that content. §14 forbids putting the form "behind a
  pin or a smooth-scroll wrapper". This had shipped in Phase 24 and was never recorded.
- **The design.** ScrollSmoother breaks `position: sticky`, because the content no longer
  scrolls; it is moved. The pinned phone is a sticky element. With native scrolling it needs no
  ScrollTrigger `pin`, and so no pin spacer to fight.

Removing it took the front door from 65.6 to 60.7 KB, which is the budget the phone and the story
then spent.

### Fade through, never cross-fade

The phone changes screen as each step reaches the middle of the window. The first draft
cross-faded two whole phones. The second cross-faded two screens inside one phone. Both would put
two screens of figures on top of each other at half opacity each, which is the double exposure
the owner rejected in Phase 24 (memory: "a double exposure of two money screens at 60 ms").

What shipped is the route transition's own shape:

- the old screen leaves in `DURATION.tap`;
- the new one arrives after 80 ms;
- one bezel, never fading.

The frame captures confirm it: the frame after the scroll shows the outgoing Readings screen
gone and Credit sales arriving, with nothing of both.

### The step copy recedes by colour, not opacity

Inactive steps use `ink-faint` rather than `opacity: 0.32`. Axe reads every step whether it is on
screen or not, and a step dimmed by opacity fails contrast. A step dimmed by a palette colour that
already passes AA does not.

### On a phone, and under reduced motion, there is no pinned phone

A phone inside a phone is too small to read, so each step shows the part of its screen it is
about, inline under its copy.

Under reduced motion the inline layout is used **at every width**. A screen that swaps by itself
as you scroll is motion by another name. The rule that "every section is simply there" is easier
to keep honest when nothing is stacked out of sight.

### The order of the steps changed from the plan

The plan listed open, meter, cash, udhaar, gap, bank. What shipped is the order the day happens:

| When | Step |
|---|---|
| 06:00 | Open |
| 06:00 | Meter |
| All day | Udhaar |
| 22:00 | Cash |
| 22:00 | Gap |
| Next morning | Bank |

A story told in time reads as one, and the time labels make the order checkable.

### The payment mix left the front door

`PAYMENT_MIX` ("How it was paid") was retired from the samples and from their pytest. Phase 26
removed "How the money arrived" from the Summary at the owner's request. A front door showing it
would advertise a screen that no longer exists in that form, which breaks §14's "every claim is a
shipped feature".

## Bugs the tests caught

1. **`data-step` meant two things.** The story's ScrollTriggers selected `[data-step]`, and the
   lifecycle strip inside the gap's screen already used `data-step` for its dots. On the phone
   layout this threw an error. On desktop it would have quietly switched the phone to "step 8",
   making every step inactive.
   - Caught by the capture script's console listener, not by a test.
   - Now `data-story-step`, with a comment at the selector.
2. **A doubled tint.** The old gap section put a `short` pill (short-tint background) on a
   short-tint box: contrast 4.21:1 against the 4.5 required.
   - Axe had never flagged it, because the section had always been moved by ScrollSmoother.
     Removing the smoother is what made axe able to read it.
3. **Two "Sign in" buttons.** The nav's Sign in made the e2e helper's
   `getByRole("button", { name: "Sign in" })` ambiguous on desktop. The tests now scope to the form.
4. **A failed read claimed a fact (B5).** The audit found three Today cards stuck on "…" when
   their read failed. Writing the test found a worse case: a failed collections read showed
   **"not declared"**, a claim that nobody counted the cash when the page had simply failed to
   load it. One helper now separates three facts:
   - "…" means still loading;
   - "not loaded" means the read failed;
   - the card's own wording appears only when the server answered with nothing.
5. **The build broke on a test file.** The spring test first read `styles.css` with `node:fs`.
   - Vitest ran it happily, but `tsc` (part of `npm run build`) has no Node types, so the
     Playwright web server could not start.
   - It was committed before anyone noticed, because the command chained with `;` instead of
     `&&`. The commit was amended before anything was pushed.
   - The fix reads the stylesheet through Vite's `?raw`, with Vitest told to pass that one CSS
     file through instead of stubbing it empty, rather than adding `@types/node` (a new
     dependency, which needs asking).

One thing that looked like a bug and was not: **the hero phone's rise-in did not appear in the
frame captures**. A probe sampled its computed style every frame. The rise-in does run: opacity
0 → 1 and 56px → 0 over about 650 ms, most of it in the first 170 ms. Each screenshot takes about
100 ms, so the captures were simply too slow to catch it.

## Inside the app

### The type scale, and why it has twelve steps rather than six

The plan said six. The codemod found three recurring compound sizes that were real design
decisions, not noise:

- the **hero figure** (2–2.25rem semibold, tight; 7 copies);
- the **card title** (1.125rem semibold; 11);
- the **secondary figure** (1.375–1.5rem semibold; 9).

Each became a step (`figure`, `subhead`, `amount`), beside `lead` (1.0625) and the existing
`headline`, `title` and `display`.

**The five reading sizes carry no line-height of their own**, so swapping
`text-[0.8125rem]` for `text-footnote` is a pure rename: the line-height is still inherited from
around it. Tailwind v4 writes `line-height: var(--tw-leading, var(--text-x--line-height))`, and
with neither defined the declaration is invalid at computed time, so it inherits. That is exactly
what the one-off size did. The Today and Summary captures before and after are identical.

The structural test exempts lines containing `wordmark`. The wordmark is a single-weight serif,
and a step that carries a `font-weight` would render it as a faux bold.

### One shape per pattern

`LinkTile`, `RowLink`, `ListCard`, `HeroFigure` and `Notice` replaced hand-built copies. The plan
also named a `CardTitle` component; it was not built, because the type scale had already made
every card title `text-subhead`, and a component around one class is ceremony.

Today's six "Details" buttons went, and **the whole card opens its sheet**. The title is the
button, and an `::after` stretches its hit area over the card:

- the `h2` stays a heading for a screen reader, which a `<button>` wrapping the whole card would
  not be (a heading inside a button is invalid);
- the focus ring is drawn around the card with `:has(:focus-visible)`.

One consequence for testing: Playwright refuses to click a row inside the card, because the
stretched button lies over it. That is the design, so the test clicks at the row's *position*,
as a thumb does.

### What was dropped from the plan, and why

- **"Metered sales leads, spanning two columns."** Phase 25 removed exactly this at the owner's
  request. The first card spanning two columns broke the six into rows of 2, 1 and 3, and a
  Playwright assertion pins the 3×2 grid at 1440. Re-litigating an owner decision through a
  layout tweak is the failure the house workflow warns against.
- **Sticky labels on statement customers, audit days and the days worklist.** Those screens have
  no group labels for anything to stick to. Adding grouping would be new UI, not consistency.
- **The pill landing on the CSS spring.** It keeps its `ease-settle` overshoot, a deliberate
  Phase 24 D5 choice for a status changing. The springs went to the tab indicator and
  `pressable`, where the old curve was an approximation of the gesture physics.

### The CSS springs (D8)

`motion/cssSpring.ts` steps the **same** `Spring` class the sheets use, at 60 fps, from 0 to 1
until it settles. It then resamples that trace into a 25-point `linear()`. The durations include
the settle tail: 633 ms for `ui` and 433 ms for `snap`. Perceptually `snap` is about 150 ms (63%
at 72 ms, 94% by about 160 ms), which is why the press feels as quick as the old 140 ms
cubic-bezier.

The Vitest test has two jobs:
- checking the physics' own shape (monotone, no overshoot for a critically damped preset);
- failing if the CSS copy drifts from it.

It is the pytest ease-mirror's idea, one engine over.

## What was measured

| | Before | After |
|---|---|---|
| Backend tests | 1,740 | 1,742 (the anchor ban, the type-scale ban) |
| Vitest | 86 | 90 (the spring) |
| Playwright | 182 | 200 |
| Initial JS (gzip -9) | 124.2 KB | 125.1 KB (the caret moved into the shared primitives) |
| Front door JS | 65.6 KB | 66.5 KB (−4.9 KB from ScrollSmoother, then the phone, story and grid) |

## What was not verified

- **Safari and Firefox.** The phase leans on three browser features. All degrade quietly where
  unsupported, and none has been looked at in those browsers:
  - CSS `zoom` (Firefox 126+);
  - `linear()` (Safari 17.2+; elsewhere the transition simply does not run);
  - `:has()` (the focus ring around a Today card).
- **A real phone and a Mac with a mouse.** The hover lift and the caret nudge exist only where
  there is a mouse.
- **`21st review`.** It never ran, because the CLI was never signed in.

## Still owed by the owner

- **21st:** sign in (`! npx @21st-dev/cli login`), then `21st review` on the changed paths. It
  belongs in a follow-up if it finds anything.
- **Copy:** approval of the new step copy and the feature tiles' wording.
- **Carried from Phase 24:** `VITE_SALES_CONTACT`, `PUBLIC_ORIGIN`, icon approval, his own
  photographs. Without the first there is still no "Talk to us", but the close now always offers
  Sign in.
- **A look on his own phone and Mac,** in both palettes.
