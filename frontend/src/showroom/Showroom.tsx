/* The front door's story (Phase 24 D7): what HiSahab does, told to a pump owner deciding whether
 * to buy it, below the sign-in card that a salesman uses every night.
 *
 * ## The rules this page lives under (CLAUDE.md §14)
 *
 * - **Every claim is a shipped feature.** Each section names the CLAUDE.md section it shows. No
 *   invented metrics, no testimonials, no customer logos.
 * - **Every figure is a sample, and says so.** They are literal strings in ./samples.ts, checked
 *   for agreeing with each other by pytest in Decimal; nothing is fetched or computed here.
 * - **The sign-in form is never behind any of this.** This file is loaded lazily, after the form
 *   has painted, and staff restored from a session never download it at all.
 *
 * ## The page, below the sign-in hero and the walk-in (Phase 28 D3)
 *
 *   Story     "A day at the pump": six steps, one phone that changes screen as they pass
 *   Features  six shipped features in a grid
 *   Close     "Bring HiSahab to your pump", with Sign in always and "Talk to us" when set
 *
 * ## Motion
 *
 * A desktop with a fine pointer gets the full story: a sticky phone and timelines scrubbed by the
 * scroll. Scrolling itself is the browser's own; ScrollSmoother is gone (Phase 28 D2). A phone
 * gets the same beats as one-shot reveals, because pinning fights a collapsing address bar.
 * Reduced motion gets every section, still. Everything moves by transform and opacity; no text is
 * ever rewritten to animate it (§14), so a beat that "changes" a figure is two figures, one
 * replacing the other.
 */

import { useRef } from "react";
import { DURATION, EASE, gsap } from "../motion/gsap";
import { ScrollTrigger, useStory } from "../motion/story";
import { Button } from "../ui/primitives";
import { CONTACT, goToSignIn } from "./doors";
import { Features } from "./Features";
import { Body, Headline } from "./parts";
import { Story } from "./Story";

export { HeroPhone } from "./HeroPhone";

export default function Showroom({ outletName }: { outletName?: string | undefined }) {
  const root = useRef<HTMLDivElement>(null);

  useStory(root, (desktop) => {
    revealHeadlines();
    compare(desktop);
    // The page just grew by several screens; the walk-in on the sign-in page measured the old
    // height.
    ScrollTrigger.refresh();
  });

  return (
    <div ref={root} className="relative bg-ground">
      <Story />
      <Features />
      <Close outletName={outletName} />
    </div>
  );
}

/* --- choreography ------------------------------------------------------------------------ */

/** Every section headline's words rise into place once, as the headline arrives. */
function revealHeadlines() {
  gsap.utils.toArray<HTMLElement>("[data-headline]").forEach((headline) => {
    gsap.from(headline.querySelectorAll("[data-word]"), {
      yPercent: 110,
      duration: DURATION.large,
      ease: EASE.enter.gsap,
      stagger: 0.06,
      scrollTrigger: { trigger: headline, start: "top 85%", once: true },
    });
  });
  gsap.utils.toArray<HTMLElement>("[data-reveal]").forEach((element) => {
    gsap.from(element, {
      opacity: 0,
      y: 24,
      duration: DURATION.large,
      ease: EASE.enter.gsap,
      scrollTrigger: { trigger: element, start: "top 88%", once: true },
    });
  });
}

/** Two palettes chosen by the phone: dark wipes in over light until each holds half the screen. */
function compare(desktop: boolean) {
  const trigger = "[data-night]";
  const scrollTrigger = desktop ? { trigger, start: "top 60%", end: "center center", scrub: 0.6 } : { trigger, start: "top 60%", once: true };
  gsap
    .timeline({ scrollTrigger, defaults: { ease: desktop ? "none" : EASE.move.gsap, duration: desktop ? 1 : DURATION.large } })
    .from("[data-wipe]", { xPercent: 100 })
    .from("[data-wipe-image]", { xPercent: -50 }, 0);
}

/* --- the close ------------------------------------------------------------------------- */

/* §4.7: the number of shifts is data, so one pump and a 24-hour forecourt are the same software.
 * The page never ends without something to press (Phase 28): "Talk to us" when the owner has set a
 * contact, and Sign in always, back at the top with the cursor in the email field. */
function Close({ outletName }: { outletName?: string | undefined }) {
  return (
    <section data-section="close" className="mx-auto flex min-h-[70dvh] max-w-3xl flex-col items-center justify-center px-6 py-28 text-center">
      <Headline text="Bring HiSahab to your pump." />
      <Body className="mx-auto">
        One shift a day or three: the shifts are data, not a setting. Readings, cash, udhaar and the bank, in one place, on the phone your staff already carry.
      </Body>
      <div data-reveal className="mt-10 flex flex-col items-center gap-3 sm:flex-row">
        {CONTACT ? (
          <a
            href={CONTACT}
            className="pressable inline-flex h-12 items-center justify-center rounded-[var(--radius-control)] bg-accent px-6 text-[1rem] font-medium text-on-accent shadow-2 hover:bg-accent-pressed"
          >
            Talk to us
          </a>
        ) : null}
        <Button variant={CONTACT ? "secondary" : "primary"} className="h-12 px-6" onClick={goToSignIn}>
          Sign in
        </Button>
      </div>
      <footer className="mt-24 flex flex-col items-center gap-2 text-[0.8125rem] text-ink-faint">
        <p className="wordmark text-[1rem] text-ink-muted">HiSahab</p>
        <p>{outletName ? `${outletName}, on HiSahab` : "Daily stock and cash flow, for the pump"}</p>
      </footer>
    </section>
  );
}
