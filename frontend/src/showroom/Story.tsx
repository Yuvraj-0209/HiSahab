/* "A day at the pump" (Phase 28 D3, D5): the trading day in the order it happens, told beside the
 * screen each step happens on.
 *
 * ## Two layouts, chosen by the device and by the reader's motion setting
 *
 * - **Pinned** (a desktop with a fine pointer, motion welcome). The steps scroll on the left. One
 *   phone stays put on the right (CSS `position: sticky`, which is why ScrollSmoother had to go,
 *   D2) and changes screen as each step reaches the middle of the window. The Motion skill's
 *   "screenshot scroll reveal" and 21st's sticky-scroll sections are the references, rebuilt
 *   here in GSAP over the app's own primitives.
 * - **Inline** (a phone, or reduced motion at any width). Each step's copy is followed by the
 *   part of its screen the step is about. A phone inside a phone is too small to read, and
 *   under reduced motion a screen that swaps by itself would be motion by another name, so
 *   every screen is simply there.
 *
 * ## One element, one engine (§14)
 *
 * GSAP moves the phone's screens (opacity, a small y), the beats inside them, and the rail's
 * fill (scaleY). CSS changes the step copy's colour on `data-active`. React decides which step is
 * active, from the ScrollTriggers. No element is driven by two of them.
 *
 * Every claim names its CLAUDE.md section; every figure is a literal in ./samples.ts (§14).
 */

import { type ComponentType, type ReactNode, useRef, useState, useSyncExternalStore } from "react";
import type { TabId } from "../app/tabs";
import { DURATION, EASE, gsap } from "../motion/gsap";
import "../motion/draw";
import { NO_PREFERENCE } from "../motion/preference";
import { DESKTOP, ScrollTrigger, useStory } from "../motion/story";
import { Body, Eyebrow, Headline, SampleNote } from "./parts";
import { MiniApp } from "./phone/MiniApp";
import { PhoneFrame } from "./phone/PhoneFrame";
import { BankFocus, CashFocus, GapFocus, MeterFocus, OpenFocus, UdhaarFocus } from "./phone/screens";
import { DAY } from "./samples";
import { Words } from "./Words";

interface Step {
  id: "open" | "meter" | "udhaar" | "cash" | "gap" | "bank";
  when: string;
  title: string;
  body: ReactNode;
  screen: { title: string; subtitle: string; tab: TabId };
  /** What the phone shows, for a screen reader: the screen itself is a picture. */
  describe: string;
  Focus: ComponentType<{ settled: boolean }>;
}

const STEPS: Step[] = [
  {
    // §4.7: the number of shifts is data, so one pump and a 24-hour forecourt are the same software.
    id: "open",
    when: "06:00",
    title: "The shift opens.",
    body: "One shift a day, or three at a forecourt that never shuts: the shifts are data, not a setting. Everything the day records hangs off this one, and nothing on it counts until somebody enters it.",
    screen: { title: "Today", subtitle: `${DAY.date} · shift 1`, tab: "today" },
    describe: "The Today screen with the shift just opened and nothing entered yet.",
    Focus: OpenFocus,
  },
  {
    // §4.7, §5.2 nozzle_readings: the chained opening is confirmed, never assumed.
    id: "meter",
    when: "06:00",
    title: "Confirm the meter.",
    body: "Every opening reading is carried forward from the last closing and shown, never assumed. Somebody has to say it matches. A reading that disagrees becomes a question, before it becomes anyone's debt.",
    screen: { title: "Readings", subtitle: `${DAY.date} · shift 1`, tab: "entry" },
    describe: "Two nozzle readings: one confirmed against the meter, one that disagrees and is raised for review.",
    Focus: MeterFocus,
  },
  {
    // §6.6: credit_sales.attachment_id is NOT NULL, in the database as well as the form.
    id: "udhaar",
    when: "All day",
    title: "Every udhaar has a receipt.",
    body: "A credit sale cannot be saved without a photograph of the slip. Each customer's ledger starts from what they already owed, and the fortnight's bill prints on the 16th and the 1st.",
    screen: { title: "Credit sales", subtitle: `${DAY.date} · shift 1`, tab: "entry" },
    describe: "A photographed udhaar slip, and the day's two credit sales.",
    Focus: UdhaarFocus,
  },
  {
    // §5.2 collections: the cash row is a declaration, checked against the meters, never an input.
    id: "cash",
    when: "22:00",
    title: "Declare the cash.",
    body: "Card and UPI are entered as the machines report them. The cash is counted into the locker and declared as a figure of its own, so it can be checked against the meters instead of being worked backwards from them.",
    screen: { title: "Collections", subtitle: `${DAY.date} · shift 1`, tab: "entry" },
    describe: "The day's collections: cash counted into the locker, card and UPI.",
    Focus: CashFocus,
  },
  {
    // §6.4 accountable_cash, §5.2 salesman_shortfalls: the gap is recorded, and booked by a person.
    id: "gap",
    when: "22:00",
    title: "The gap has a name.",
    body: "Expected cash comes from the meters. Counted cash is declared separately. When the two differ, the difference is recorded rather than quietly corrected away, and it becomes a debt only when a manager books it, with a reason.",
    screen: { title: "Cash position", subtitle: `${DAY.date} · shift 1`, tab: "cash" },
    describe: "The cash position: what the salesman should be holding, what he counted, and the gap between them.",
    Focus: ({ settled }) => <GapFocus closed={settled} />,
  },
  {
    // §5.3a, §11 phase 20: the statement verifies the books; nothing is written until a person ticks.
    id: "bank",
    when: "Next morning",
    title: "The bank checks the books.",
    body: "Upload the bank's statement. Paytm's next-day settlement, cash deposits and udhaar payments are matched against what was entered. Nothing is recorded until a person ticks it.",
    screen: { title: "Review statement", subtitle: "Bank of the outlet", tab: "credit" },
    describe: "Bank statement lines, each matched to what the books say.",
    Focus: BankFocus,
  },
];

/** Whether a media query matches, kept current as the window changes. */
function useMedia(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const list = window.matchMedia(query);
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    },
    () => window.matchMedia(query).matches,
    () => false,
  );
}

/** Each step's moment, played once, the first time its screen is shown. Transform, opacity and
 * a drawn stroke only; nothing here writes text, and no figure is ever counted (§14). */
function beat(screen: Element, id: Step["id"]): gsap.core.Timeline {
  const q = gsap.utils.selector(screen);
  const tl = gsap.timeline({ defaults: { ease: EASE.enter.gsap, duration: DURATION.medium } });
  switch (id) {
    case "open":
      tl.from(q("[data-beat]"), { opacity: 0, y: 18, stagger: 0.12 });
      break;
    case "meter":
      tl.from(q("[data-tile='agree']"), { opacity: 0, y: 24 })
        .fromTo(q("[data-tile='agree'] [data-check]"), { drawSVG: "0%" }, { drawSVG: "100%" })
        .from(q("[data-tile='agree'] [data-verdict]"), { opacity: 0, scale: 0.8, ease: EASE.settle.gsap }, "<0.1")
        .from(q("[data-tile='disagree']"), { opacity: 0, y: 24 }, "+=0.15")
        .from(q("[data-tile='disagree'] [data-verdict]"), { opacity: 0, scale: 0.8, ease: EASE.settle.gsap })
        .from(q("[data-moved]"), { opacity: 0, x: -10 }, "<0.1");
      break;
    case "udhaar":
    case "cash":
      tl.from(q("[data-beat]"), { opacity: 0, y: 14, stagger: 0.12 });
      break;
    case "gap":
      tl.from(q("[data-beat]"), { opacity: 0, x: -16, stagger: 0.1 }).from(q("[data-gap]"), { opacity: 0, y: 12, ease: EASE.settle.gsap }, "+=0.05");
      break;
    case "bank":
      tl.from(q("[data-beat]"), { opacity: 0, y: 12, stagger: 0.14 }).fromTo(q("[data-check]"), { drawSVG: "0%" }, { drawSVG: "100%", stagger: 0.14 }, 0.15);
      break;
  }
  return tl;
}

export function Story() {
  const root = useRef<HTMLElement>(null);
  const desktop = useMedia(DESKTOP);
  const motion = useMedia(NO_PREFERENCE);
  const pinned = desktop && motion;
  const [active, setActive] = useState(0);
  // The gap step's lifecycle strip moves on from Entered to Closed when that step is reached. With
  // no motion it is simply shown closed.
  const [gapReached, setGapReached] = useState(!motion);

  useStory(
    root,
    (wide) => {
      // `data-story-step`, not `data-step`: the lifecycle strip inside the gap's screen already uses
      // that name for its dots, and a trigger made for a dot would switch the phone to step 8.
      const steps = gsap.utils.toArray<HTMLElement>("[data-story-step]");
      if (wide && pinned) {
        const screens = gsap.utils.toArray<HTMLElement>("[data-screen]");
        const played = new Set<number>();
        let shown = 0;
        gsap.set(screens, { autoAlpha: 0 });
        gsap.set(screens[0] ?? [], { autoAlpha: 1 });
        const show = (index: number) => {
          const next = screens[index];
          const previous = screens[shown];
          setActive(index);
          if (STEPS[index]?.id === "gap") setGapReached(true);
          if (!next || index === shown) return;
          // Fade through, never cross-fade: the old screen leaves quickly and the new one arrives
          // just after, the shape route changes take in the app. Two screens of figures at half
          // opacity each is a double exposure, and this project has shipped one of those before.
          if (previous) gsap.to(previous, { autoAlpha: 0, duration: DURATION.tap, ease: EASE.exit.gsap, overwrite: "auto" });
          gsap.fromTo(next, { autoAlpha: 0 }, { autoAlpha: 1, duration: DURATION.medium, delay: 0.08, ease: EASE.enter.gsap, overwrite: "auto" });
          shown = index;
          if (!played.has(index)) {
            played.add(index);
            beat(next, STEPS[index]!.id);
          }
        };
        played.add(0);
        beat(screens[0]!, STEPS[0]!.id);
        steps.forEach((step, index) =>
          ScrollTrigger.create({ trigger: step, start: "top center", end: "bottom center", onToggle: (self) => self.isActive && show(index) }),
        );
        // The rail fills as the day goes by: one scaleY, scrubbed by the whole list.
        gsap.fromTo(
          "[data-rail-fill]",
          { scaleY: 0 },
          { scaleY: 1, ease: "none", scrollTrigger: { trigger: "[data-steps]", start: "top center", end: "bottom center", scrub: 0.4 } },
        );
      } else {
        // Inline: each step's screen plays its moment once, as it comes into view.
        steps.forEach((step, index) => {
          const screen = step.querySelector("[data-screen]");
          if (!screen) return;
          ScrollTrigger.create({
            trigger: screen,
            start: "top 80%",
            once: true,
            onEnter: () => {
              if (STEPS[index]?.id === "gap") setGapReached(true);
              beat(screen, STEPS[index]!.id);
            },
          });
        });
      }
    },
    [pinned],
  );

  return (
    <section ref={root} data-section="how" className="relative bg-ground px-6 py-24 lg:py-32">
      <div className="mx-auto max-w-6xl">
        <div className="max-w-2xl">
          <Eyebrow>How it works</Eyebrow>
          <Headline text="A day at the pump." />
          <Body>From the morning reading to the bank statement, in the order the day happens: one sample day, six steps, on the phone the salesman already carries.</Body>
        </div>

        <div className={`mt-16 grid gap-12 ${pinned ? "grid-cols-[minmax(0,1fr)_auto] gap-x-20" : "grid-cols-1"}`}>
          {/* Pinned, the list runs on past its last step, so the phone is still held in the middle
           * of the window when the last step reaches it rather than leaving with the column. */}
          <ol data-steps className={`relative ${pinned ? "pb-[18vh]" : ""}`}>
            {pinned ? (
              <span className="absolute top-0 bottom-0 left-[0.6875rem] w-px bg-hairline-strong" aria-hidden="true">
                <span data-rail-fill className="absolute inset-0 origin-top bg-accent" />
              </span>
            ) : null}
            {STEPS.map((step, index) => (
              <li
                key={step.id}
                data-story-step={step.id}
                data-active={pinned ? String(active === index) : "true"}
                className={`story-step relative pl-12 ${pinned ? "flex min-h-[72vh] flex-col justify-center" : "pb-16 last:pb-0"}`}
              >
                <span className="story-dot absolute top-1 left-0 grid size-6 place-items-center rounded-full border bg-ground" aria-hidden="true">
                  <span className="size-2 rounded-full" />
                </span>
                <p className="tabular text-[0.8125rem] font-semibold tracking-[0.04em] text-accent">{step.when}</p>
                <Words as="h3" text={step.title} className="display mt-2 block text-[clamp(2rem,4vw,3rem)] text-balance" />
                <p className="story-body mt-4 max-w-[30rem] text-[1.0625rem] leading-relaxed">{step.body}</p>
                {pinned ? null : (
                  <div data-screen className="mt-8 max-w-[26rem]">
                    <p className="mb-3 text-[0.8125rem] text-ink-muted">
                      On the <span className="font-medium text-ink">{step.screen.title}</span> screen
                    </p>
                    <figure className="m-0">
                      <div className="flex flex-col gap-3 rounded-[1.75rem] border border-hairline bg-surface-sunken p-3" inert aria-hidden="true">
                        <step.Focus settled={gapReached} />
                      </div>
                      <figcaption className="sr-only-text">{step.describe}</figcaption>
                    </figure>
                    <SampleNote />
                  </div>
                )}
              </li>
            ))}
          </ol>

          {pinned ? (
            <div>
              <div className="sticky top-0 flex h-[100dvh] flex-col items-center justify-center">
                {/* One phone, six screens stacked inside it: the bezel never fades, only the glass. */}
                <PhoneFrame label={STEPS[active]?.describe ?? ""}>
                  <div className="relative h-full">
                    {STEPS.map((step) => (
                      <div key={step.id} data-screen={step.id} className="absolute inset-0">
                        <MiniApp title={step.screen.title} subtitle={step.screen.subtitle} tab={step.screen.tab}>
                          <step.Focus settled={gapReached} />
                        </MiniApp>
                      </div>
                    ))}
                  </div>
                </PhoneFrame>
                <SampleNote className="text-center" />
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}
