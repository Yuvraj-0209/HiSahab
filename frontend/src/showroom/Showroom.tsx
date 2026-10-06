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
 * ## Motion
 *
 * Desktop (a fine pointer, 1024px and up) gets the full story: pinned sections and timelines
 * scrubbed by the scroll. Scrolling itself is the browser's own (Phase 28 D2). A phone gets the same beats as one-shot reveals, because
 * pinning fights a collapsing address bar. Reduced motion gets every section, still. Everything
 * moves by transform and opacity; no text is ever rewritten to animate it (§14), so a beat that
 * "changes" a figure is two figures, one replacing the other.
 */

import { useRef } from "react";
import { DURATION, EASE, gsap } from "../motion/gsap";
import { ScrollTrigger, useStory } from "../motion/story";
import { Donut, SalesBars, ShareBars } from "../ui/chart";
import { CONTACT } from "./doors";
import { Body, Headline, SampleNote } from "./parts";
import { DAYS, FUEL_MIX, MONTH, PAYMENT_MIX } from "./samples";
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
      <MonthSection />
      <NightSection />
      <CloseSection outletName={outletName} />
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
  const trigger = "[data-section='night']";
  const scrollTrigger = desktop ? { trigger, start: "top 60%", end: "center center", scrub: 0.6 } : { trigger, start: "top 60%", once: true };
  gsap
    .timeline({ scrollTrigger, defaults: { ease: desktop ? "none" : EASE.move.gsap, duration: desktop ? 1 : DURATION.large } })
    .from("[data-wipe]", { xPercent: 100 })
    .from("[data-wipe-image]", { xPercent: -50 }, 0);
}

/* --- sections ---------------------------------------------------------------------------- */

/* §11 phases 13 and 19, §13.7: the owner's window, with profit labelled for what it is. */
function MonthSection() {
  return (
    <section data-section="month" className="mx-auto max-w-6xl px-6 py-24">
      <div className="max-w-2xl">
        <Headline text="The whole month on one screen." />
        <Body>Sales by day, the fuel mix and how customers paid, from figures the server has already reconciled. Nothing on it is added up in the browser.</Body>
      </div>
      <div className="mt-12 grid grid-cols-1 gap-4 md:grid-cols-3">
        <div data-reveal className="rounded-[var(--radius-card)] border border-hairline bg-surface p-5 shadow-1 md:col-span-2">
          <p className="text-[0.8125rem] font-medium text-ink-muted">Sales, 1 to 10 September</p>
          <p className="tabular mt-1 text-title text-ink">{MONTH.sales}</p>
          <div className="mt-4">
            <SalesBars days={DAYS} />
          </div>
        </div>
        <div data-reveal className="rounded-[var(--radius-card)] border border-hairline bg-accent-tint p-5 shadow-1">
          <p className="text-[0.8125rem] font-medium text-ink-muted">Fuel mix</p>
          <Donut slices={FUEL_MIX}>
            <span className="text-[0.75rem] text-ink-muted">3 fuels</span>
          </Donut>
          <ul className="mt-2 flex flex-col gap-1 text-[0.8125rem]">
            {FUEL_MIX.map((fuel) => (
              <li key={fuel.key} className="flex justify-between gap-3">
                <span className="text-ink">{fuel.key}</span>
                <span className="tabular text-ink-muted">{fuel.share_pct}</span>
              </li>
            ))}
          </ul>
        </div>
        <div data-reveal className="rounded-[var(--radius-card)] border border-hairline bg-surface p-5 shadow-1">
          <p className="text-[0.8125rem] font-medium text-ink-muted">How it was paid</p>
          <div className="mt-3">
            <ShareBars rows={PAYMENT_MIX} />
          </div>
        </div>
        <div data-reveal className="flex flex-col justify-between gap-6 rounded-[var(--radius-card)] border border-hairline bg-surface-sunken p-5 shadow-1 md:col-span-2">
          <div>
            <p className="text-[0.8125rem] font-medium text-ink-muted">Gross fuel margin</p>
            <p className="tabular mt-1 text-title text-ink">{MONTH.margin}</p>
          </div>
          <p className="max-w-[44ch] text-[0.875rem] text-ink-muted">
            The dealer's margin on litres and kilograms sold. Labelled for what it is: not business profit, which also moves with stock held when prices change.
          </p>
        </div>
      </div>
      <SampleNote />
    </section>
  );
}

/* Phase 23's two palettes (§12), §6.10 idempotency. */
function NightSection() {
  return (
    <section data-section="night" className="mx-auto grid max-w-6xl items-center gap-12 px-6 py-24 lg:grid-cols-2">
      <div>
        <Headline text="Built for the night shift." />
        <Body>
          Light by day, dark at night, chosen by the phone. Every save carries a key, so on patchy 4G a dropped connection never records ₹5,000 twice.
        </Body>
      </div>
      <div className="mx-auto w-full max-w-[19rem]">
        <div className="relative overflow-hidden rounded-[2.4rem] border-[6px] border-hairline-strong bg-surface shadow-3">
          <img src="/img/showroom/entry-light.jpg" alt="The Entry screen in the light palette" loading="lazy" decoding="async" className="block aspect-[390/844] w-full" />
          <div data-wipe className="absolute inset-y-0 right-0 w-1/2 overflow-hidden border-l border-hairline-strong">
            <img
              data-wipe-image
              src="/img/showroom/entry-dark.jpg"
              alt="The same screen in the dark palette"
              loading="lazy"
              decoding="async"
              className="absolute top-0 right-0 block aspect-[390/844] w-[200%] max-w-none"
            />
          </div>
        </div>
      </div>
    </section>
  );
}

/* §4.7: the number of shifts is data, so one pump and a 24-hour forecourt are the same software. */
function CloseSection({ outletName }: { outletName?: string | undefined }) {
  return (
    <section data-section="close" className="mx-auto flex min-h-[70dvh] max-w-3xl flex-col justify-center px-6 py-24">
      <Headline text="Bring HiSahab to your pump." />
      <Body>One shift a day or three: the shifts are data, not a setting. Readings, cash, udhaar and the bank, in one place, on the phone your staff already carry.</Body>
      {CONTACT ? (
        <div data-reveal className="mt-8">
          <a
            href={CONTACT}
            className="pressable inline-flex h-12 items-center justify-center rounded-[var(--radius-control)] bg-accent px-6 text-[1rem] font-medium text-on-accent shadow-2 hover:bg-accent-pressed"
          >
            Talk to us
          </a>
        </div>
      ) : null}
      <footer className="mt-20 flex flex-col gap-1 text-[0.8125rem] text-ink-faint">
        <p>{outletName ? `${outletName}, on HiSahab` : "HiSahab, daily stock and cash flow"}</p>
        <p>Trouble signing in? Your outlet admin can check your account.</p>
      </footer>
    </section>
  );
}
