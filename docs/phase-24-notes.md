# Phase 24 — The front door and the finish: Notes

> A learning reference, not a changelog: *why* the frontend moves the way it does now, and what
> building it turned up. The plan, with a running "What shipped" record, is
> `docs/phase-24-plan.md`; the rules live in `CLAUDE.md` §11 (24), §12, §13.28, §13.43, §14, §15
> and §16.

---

## What was built

- **A front door.** The sign-in page carries a scroll-driven story for a pump owner deciding
  whether to buy HiSahab: seven sections after the hero, built from live components showing
  labelled sample figures. On desktop it uses smooth scrolling, pinned and scrubbed beats, a
  sideways pan and a stacked set of cards; a phone gets the same beats once each.
- **Inside the app, motion that says something.**
  - Route changes are View Transitions with a direction; a day's date and a customer's name fly
    into the next screen's header.
  - Tab roots have large titles that fold into the chrome as you scroll.
  - Charts grow when you reach them; lists move rows to their new places when they change.
  - Every tick draws itself; status pills land; the lifecycle strip draws each step in order.
  - The figure each screen is read for is set large; commits tick under the thumb.
- **Delivery fixed.** The UI is gzipped and cached by file kind. GSAP left the first paint, and a
  budget check fails the suite when JavaScript grows past its caps.
- **Installable.** A web app manifest and icons, and a link preview for when the address is
  shared.
- No migration, no table, no endpoint, no business rule, no new npm dependency.
- **Tests:** pytest 1,708 (+11), Vitest 75 (+12), Playwright 138 (+12). First paint 124.0 KB
  gzipped (was 150.4); the front door adds 64.1 KB more. LCP 2.23 s and CLS 0 on a Slow 4G
  profile with 4× CPU throttling, served by uvicorn the way production serves it.

## Why "anything and everything" was split in two

The owner asked for every animation and scroll effect GSAP can do. Applied inside the app, that
would have slowed the person the app exists for: a salesman typing a whole day in at 10pm on a
cheap Android, for whom a list that fades in row by row is half a second of waiting, every
screen, every night. §14 already forbade decoration on data screens for that reason.

The front door has no such user. It has no data on it, nobody types a day into it, and the
person reading it is deciding whether the product is worth buying. **The rule became a place**
rather than a restriction: pins, scrubs, parallax and smooth scrolling live in
`src/showroom/` and nowhere else (§14).

Inside, the test for every animation stayed the one Phase 23 set: name what it communicates.
- A view transition says where you went.
- A large title folding says where you are.
- A drawn tick says you did that.
- A list closing ranks says your entry is in, and here.

## Three engines, each with its own ground

| Engine | Owns | Why it is the right tool there |
|---|---|---|
| View Transitions | Route changes | It animates snapshots of the page, never the elements, so it cannot fight anything else for a transform |
| GSAP | In-screen choreography and the front door | It sequences, scrubs, pins and flips |
| The Phase 12 spring | Gestures | It carries velocity through an interruption, which GSAP cannot |

Plus plain CSS for the motion that is one transform and nothing else (the screen entrance, the
tab indicator, a pill landing, the shimmer). That is not a fourth engine, just the absence of
one. It matters in the first paint, where every kilobyte is paid by every user on every open.

## What the work turned up

1. **The frontend was never delivered the way it was measured.**
   - Phase 23's budget was gzipped bytes, but `StaticFiles` sent files uncompressed, so a phone
     downloaded 385 KB for the entry chunk alone. Compressed, it is 118.5 KB on the wire.
   - Phase 23's promised cache headers were never sent either, so every open revalidated every
     chunk.
   - Both were found by reading `app/main.py` while fixing the budget. Neither had a test,
     because nothing ever asked the server *how* it sent a file.
2. **The budget was over, and nobody knew.** 150.4 KiB against 150. The notes had said about
   147, because the count missed a modulepreloaded chunk. `scripts/budget.mjs` now builds into a
   temporary directory and pytest runs it. **It caught a regression on its first day:** importing
   one tiny helper through `motion/gsap.ts` dragged GSAP's whole core back into the first paint.
   That helper now lives in a GSAP-free `motion/preference.ts`.
3. **The budget then measured the wrong thing for the front door.** A file-name pattern missed
   the `Showroom` chunk (capital S) and every shared chunk the story pulls in. It now walks Vite's
   build manifest from the two entry points. **A budget must measure what is downloaded, not
   what is named.**
4. **The first route transition was a double exposure.** It passed every test, and frames
   captured mid-flight showed two screens of rupee figures on top of each other at 60 ms. The fix
   was "fade through": the old screen is gone in 140 ms, before the new one arrives. **Motion has
   to be looked at, frame by frame.** A passing test only proves the transition finished.
5. **Two contrast failures were real, not test noise.** The longer pages put a cobalt button under
   the translucent tab bar, and labels read against it: 3.8:1 for the active tab, 4.4:1 for the
   others. The tab indicator became opaque and the chrome 88% / 86% opaque. A translucent surface
   is only as legible as the worst thing that can scroll beneath it.
6. **ScrollSmoother silently never attached.** It was handed `"#smooth-wrapper"`, and inside
   `useGSAP` a selector string is resolved within the component's own subtree, where the wrapper
   is not. GSAP logged one warning, and only a test that asserted the wrapper's computed style
   noticed.
7. **SplitText was the wrong tool, for a React reason.** It replaces a heading's text with its own
   spans and restores it with `innerHTML`, inside DOM that React owns. `Words.tsx` renders the
   spans in JSX instead.
8. **A permanent `opacity: 0` looks like an animation that never ends.** The unticked checkbox's
   tick was hidden with opacity, and the e2e settle check waited forever for it. `visibility:
   hidden` is both what the test needed and the more honest markup.

## The honesty rules for a sales page

A sales page is where plausible-but-wrong figures are most tempting and least checked, so the
front door got three rules (§14), each enforced where it could be:

- **Sample figures only, labelled.** They are literal strings in `samples.ts`, never fetched or
  computed. A pytest test proves in `Decimal` that they agree with each other: the gap with its
  terms, the ledger with its balances, Paytm's credit with yesterday's card and UPI, each share
  with its rupees. The test was mutation-checked by changing one share and one gap.
- **No real data.** The night-shift phone shows the Entry screen, not Today, because Today's e2e
  fixture carries the outlet's real 30 July takings.
- **Every claim is a shipped feature,** commented with its CLAUDE.md section. There are no
  invented metrics, testimonials or logos.

## Deviations from the plan

- **Licensed stock instead of generated photographs.** The image generator's account had no
  credits; the owner chose Adobe Stock on the free tier. The hero is now a forecourt at night
  (58 KB against the skyline's 147 KB), and the udhaar section shows hands holding paper bills.
  Both are placeholders for the owner's own photographs.
- **"A row leaves a queue" became "a list changed".** Finalising a day and reviewing an expense
  happen on screens where the queue is not visible, so the row was never seen leaving. The
  general version — rows moving when any list changes — covers the moments that are actually
  seen.
- **No lock-icon morph.** The lock button disappears once a shift is locked; the pill carries the
  change.
- **Browser back/forward gets the plain fade, not nothing** (§13.43 amended to match).
- **The token mirror is checked from pytest, not Vitest,** which reads every CSS import as an
  empty string.

## What was not verified

- **A real phone.** Every tab in both palettes, with reduced motion on and off, the home-screen
  install, a sheet thrown during a route transition, and the statement's print preview. The
  Playwright suite covers 390 px in both palettes and axe covers contrast; neither can say whether
  motion *feels* right on a cheap Android (§13.18's remaining half).
- **Safari.** View Transitions and scroll-driven animations are progressive enhancements, so
  Safari without them gets Phase 23's look, but nobody has looked.
- **A Railway deploy.** Still owed from Phase 23.

## Still owed by the owner

- Approve the front-door copy, and choose the "Talk to us" contact (`VITE_SALES_CONTACT`).
- Approve the app icon.
- Photographs of the pump, to replace the two stock placeholders.
- The production domain (`PUBLIC_ORIGIN`), for the link preview's image.
- Phase 23's open items: a Railway deploy, real-day parity, and the `confirm-repayments`
  business-rule question.
