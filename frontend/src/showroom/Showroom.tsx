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
 * Desktop (a fine pointer, 1024px and up) gets the full story: smooth scrolling, pinned sections,
 * timelines scrubbed by the scroll. A phone gets the same beats as one-shot reveals, because
 * pinning fights a collapsing address bar. Reduced motion gets every section, still. Everything
 * moves by transform and opacity; no text is ever rewritten to animate it (§14), so a beat that
 * "changes" a figure is two figures, one replacing the other.
 */

import { type ReactNode, useRef } from "react";
import { DURATION, EASE, gsap } from "../motion/gsap";
import "../motion/draw";
import { ScrollSmoother, ScrollTrigger, useStory } from "../motion/story";
import { Donut, SalesBars, ShareBars } from "../ui/chart";
import { Pill } from "../ui/primitives";
import { BANK, DAYS, FUEL_MIX, GAP, LEDGER, METER, MONTH, PAYMENT_MIX } from "./samples";
import { Words } from "./Words";

/** Where "Talk to us" goes: a build-time setting, because the owner has not chosen one yet
 * (WhatsApp, a phone number or an email). Without it the closing section has no button rather
 * than a placeholder one. */
const CONTACT: string | undefined = import.meta.env.VITE_SALES_CONTACT || undefined;

export default function Showroom({ outletName }: { outletName?: string | undefined }) {
  const root = useRef<HTMLDivElement>(null);

  useStory(root, (desktop) => {
    if (desktop) {
      // Created before any trigger below, so pins land inside the smoothed content. Elements, not
      // selector strings: inside useGSAP a selector is resolved within this component, and the
      // wrapper belongs to the sign-in page around it.
      ScrollSmoother.create({
        wrapper: document.getElementById("smooth-wrapper"),
        content: document.getElementById("smooth-content"),
        smooth: 1.1,
        smoothTouch: false,
      });
    }
    revealHeadlines();
    meter(desktop);
    gap(desktop);
    udhaar(desktop);
    bank(desktop);
    compare(desktop);
    // The page just grew by several screens; the hero's parallax measured the old height.
    ScrollTrigger.refresh();
  });

  return (
    <div ref={root} className="relative bg-ground">
      <MeterSection />
      <GapSection />
      <UdhaarSection />
      <BankSection />
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

/** Desktop scrubs a pinned section; a phone plays the same timeline once as it arrives. */
function beat(section: string, desktop: boolean, length: string) {
  const trigger = `[data-section="${section}"]`;
  return gsap.timeline({
    scrollTrigger: desktop
      ? { trigger, start: "top top", end: length, pin: true, scrub: 0.6 }
      : { trigger, start: "top 65%", once: true },
    defaults: { ease: EASE.enter.gsap, duration: DURATION.medium },
  });
}

/** §4.7: the carried opening is confirmed, then a second meter disagrees and becomes a question. */
function meter(desktop: boolean) {
  const tl = beat("meter", desktop, "+=140%");
  tl.from("[data-tile='agree']", { opacity: 0, y: 30 })
    .fromTo("[data-tile='agree'] [data-check]", { drawSVG: "0%" }, { drawSVG: "100%" })
    .from("[data-tile='agree'] [data-verdict]", { opacity: 0, scale: 0.8, ease: EASE.settle.gsap }, "<0.1")
    .from("[data-tile='disagree']", { opacity: 0, y: 30 }, "+=0.2")
    .from("[data-tile='disagree'] [data-verdict]", { opacity: 0, scale: 0.8, ease: EASE.settle.gsap })
    .from("[data-tile='disagree'] [data-moved]", { opacity: 0, x: -12 }, "<0.1");
}

/** §6.4: the terms arrive one at a time, then the gap lands with its word. */
function gap(desktop: boolean) {
  const tl = beat("gap", desktop, "+=120%");
  tl.from("[data-term]", { opacity: 0, x: -24, stagger: 0.18 })
    .from("[data-gap]", { opacity: 0, y: 16, ease: EASE.settle.gsap }, "+=0.1");
}

/** §6.6: receipt, ledger, bill. On desktop each card pins while the next one stacks onto it. */
function udhaar(desktop: boolean) {
  const cards = gsap.utils.toArray<HTMLElement>("[data-stack-card]");
  if (!desktop) {
    cards.forEach((card) =>
      gsap.from(card, { opacity: 0, y: 32, duration: DURATION.large, ease: EASE.enter.gsap, scrollTrigger: { trigger: card, start: "top 85%", once: true } }),
    );
    return;
  }
  const last = cards[cards.length - 1];
  cards.forEach((card, index) => {
    const next = cards[index + 1];
    if (!next || !last) return;
    ScrollTrigger.create({ trigger: card, start: "top 18%", endTrigger: last, end: "top 18%", pin: true, pinSpacing: false });
    gsap.to(card, {
      scale: 0.94,
      opacity: 0.5,
      ease: "none",
      scrollTrigger: { trigger: next, start: "top bottom", end: "top 18%", scrub: true },
    });
  });
}

/** §5.3a: statement lines tick off as they pass. Desktop pans sideways; a phone swipes. */
function bank(desktop: boolean) {
  const track = document.querySelector<HTMLElement>("[data-bank-track]");
  if (!track) return;
  const checks = track.querySelectorAll("[data-check]");
  if (!desktop) {
    gsap.fromTo(checks, { drawSVG: "0%" }, { drawSVG: "100%", duration: DURATION.medium, stagger: 0.2, scrollTrigger: { trigger: track, start: "top 75%", once: true } });
    return;
  }
  const distance = () => track.scrollWidth - window.innerWidth;
  const tl = gsap.timeline({
    scrollTrigger: { trigger: "[data-section='bank']", start: "top top", end: () => `+=${distance()}`, pin: true, scrub: 0.8, invalidateOnRefresh: true },
  });
  tl.to(track, { x: () => -distance(), ease: "none" }).fromTo(checks, { drawSVG: "0%" }, { drawSVG: "100%", stagger: 0.25, ease: "none" }, 0);
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

function Headline({ text }: { text: string }) {
  // Balanced, so a headline never strands its last word on a line of its own.
  return <Words as="h2" text={text} className="display block text-[clamp(2.5rem,7vw,4.5rem)] text-balance text-ink" />;
}

function Body({ children }: { children: ReactNode }) {
  return (
    <p data-reveal className="mt-5 max-w-[34rem] text-[1.0625rem] leading-relaxed text-ink-muted">
      {children}
    </p>
  );
}

/** Said under every section that shows a figure: these are not anybody's real books. */
function SampleNote() {
  return <p className="mt-3 text-[0.75rem] text-ink-faint">Sample figures</p>;
}

/** A tick drawn as a stroke, for the demo's decorative confirmations. */
function Check({ tone = "accent" }: { tone?: "accent" | "surplus" }) {
  return (
    <span className={`grid size-6 shrink-0 place-items-center rounded-full ${tone === "accent" ? "bg-accent text-on-accent" : "bg-surplus-tint text-surplus"}`} aria-hidden="true">
      <svg viewBox="0 0 20 20" className="size-4">
        <polyline data-check points="5.5 10.5 8.75 13.75 14.75 6.75" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </span>
  );
}

/* §4.7, §5.2 nozzle_readings: the chained opening is confirmed, never assumed. */
function MeterSection() {
  return (
    <section data-section="meter" className="mx-auto grid min-h-[100dvh] max-w-6xl items-center gap-12 px-6 py-24 lg:grid-cols-[1fr_1fr]">
      <div>
        <Headline text="Confirm the meter." />
        <Body>
          Every opening reading is carried forward from the last closing and shown, never assumed. Somebody has to say it matches. A reading that disagrees
          becomes a question, before it becomes anyone's debt.
        </Body>
      </div>
      <div className="flex flex-col gap-4">
        <MeterTile state="agree" />
        <MeterTile state="disagree" />
        <SampleNote />
      </div>
    </section>
  );
}

function MeterTile({ state }: { state: "agree" | "disagree" }) {
  const agrees = state === "agree";
  return (
    <div data-tile={state} className="rounded-[var(--radius-card)] border border-hairline bg-surface p-5 shadow-2">
      <div className="flex items-center justify-between gap-3">
        <p className="text-[0.9375rem] font-semibold text-ink">
          {METER.nozzle} <span className="font-normal text-ink-muted">· {METER.fuel}</span>
        </p>
        <span data-verdict>
          <Pill kind={agrees ? "open" : "review"}>{agrees ? "Confirmed" : "Needs review"}</Pill>
        </span>
      </div>
      <dl className="tabular mt-4 grid grid-cols-[1fr_auto] gap-y-2 text-[0.9375rem]">
        <dt className="text-ink-muted">Carried forward</dt>
        <dd className="text-right text-ink">{METER.carried}</dd>
        <dt className="text-ink-muted">The meter now</dt>
        <dd className={`text-right ${agrees ? "text-ink" : "font-semibold text-warning"}`}>{agrees ? METER.meterAgrees : METER.meterDisagrees}</dd>
      </dl>
      <div className="mt-4 flex items-center gap-3 border-t border-hairline pt-4 text-[0.875rem]">
        {agrees ? (
          <>
            <Check />
            <span className="text-ink">The salesman checked the meter and said so.</span>
          </>
        ) : (
          <span data-moved className="text-warning">
            {METER.moved}. Raised for review before anybody is blamed.
          </span>
        )}
      </div>
    </div>
  );
}

/* §6.4 accountable_cash, §5.2 salesman_shortfalls: the gap is recorded, and booked by a person. */
function GapSection() {
  return (
    <section data-section="gap" className="flex min-h-[100dvh] flex-col justify-center px-6 py-24">
      <div className="mx-auto w-full max-w-3xl">
        <Headline text="The gap has a name." />
        <Body>
          Expected cash comes from the meters. Counted cash is declared separately. When the two differ, the difference is recorded rather than quietly
          corrected away, and it becomes a debt only when a manager books it, with a reason.
        </Body>
        <div className="tabular mt-12 text-[clamp(1.0625rem,2.4vw,1.375rem)]">
          {GAP.rows.map((row) => (
            <div key={row.label} data-term className="flex items-baseline justify-between gap-4 border-b border-hairline py-3">
              <span className="text-ink-muted">
                <span className="inline-block w-5 text-ink-faint">{row.sign}</span>
                {row.label}
              </span>
              <span className="text-ink">{row.value}</span>
            </div>
          ))}
          <div data-term className="flex items-baseline justify-between gap-4 border-b border-hairline-strong py-3 font-semibold">
            <span className="text-ink">
              <span className="inline-block w-5" />
              He should be holding
            </span>
            <span className="text-ink">{GAP.accountable}</span>
          </div>
          <div data-term className="flex items-baseline justify-between gap-4 py-3">
            <span className="text-ink-muted">
              <span className="inline-block w-5" />
              He counted into the locker
            </span>
            <span className="text-ink">{GAP.declared}</span>
          </div>
          <div data-gap className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-[var(--radius-card)] bg-short-tint px-5 py-4">
            <span className="font-semibold text-short">Gap</span>
            <span className="flex items-center gap-3">
              <span className="text-title text-short">{GAP.gap}</span>
              <Pill kind="short">short</Pill>
            </span>
          </div>
        </div>
        <SampleNote />
      </div>
    </section>
  );
}

/* §6.6 (credit_sales.attachment_id NOT NULL), §5.2 credit_opening_balances, Phase 21 statement. */
function UdhaarSection() {
  return (
    <section data-section="udhaar" className="mx-auto max-w-6xl px-6 py-24">
      <div className="max-w-2xl">
        <Headline text="Every udhaar has a receipt." />
        <Body>
          A credit sale cannot be saved without a photograph of the slip. Each customer's ledger starts from what they already owed, and the fortnight's
          bill prints on the 16th and the 1st.
        </Body>
      </div>
      <div className="mx-auto mt-14 flex max-w-xl flex-col gap-6">
        <article data-stack-card className="overflow-hidden rounded-[var(--radius-card)] border border-hairline bg-surface shadow-3">
          <img
            src="/img/showroom/udhaar-slip-960.webp"
            srcSet="/img/showroom/udhaar-slip-960.webp 960w, /img/showroom/udhaar-slip-1920.webp 1920w"
            sizes="(min-width: 640px) 36rem, 100vw"
            alt="Hands holding a sheaf of paper bills"
            loading="lazy"
            decoding="async"
            className="aspect-[4/3] w-full object-cover"
          />
          <div className="flex items-center justify-between gap-3 px-5 py-4">
            <p className="text-[0.9375rem] font-semibold text-ink">The slip, photographed</p>
            <Pill kind="open">Receipt attached</Pill>
          </div>
        </article>

        <article data-stack-card className="rounded-[var(--radius-card)] border border-hairline bg-surface p-5 shadow-3">
          <p className="text-[0.9375rem] font-semibold text-ink">{LEDGER.customer}</p>
          <p className="text-[0.8125rem] text-ink-muted">Ledger, newest balance last</p>
          <div className="tabular mt-3">
            {LEDGER.rows.map((row) => (
              <div key={row.date + row.label} className="grid grid-cols-[3.5rem_1fr_auto] items-baseline gap-3 border-b border-hairline py-2.5 text-[0.875rem] last:border-b-0">
                <span className="text-ink-faint">{row.date}</span>
                <span className="text-ink">
                  {row.label}
                  <span className="block text-ink-muted">{row.amount}</span>
                </span>
                <span className="font-semibold text-ink">{row.balance}</span>
              </div>
            ))}
          </div>
        </article>

        <article data-stack-card className="rounded-[var(--radius-card)] border border-hairline bg-surface-raised p-6 shadow-3">
          <div className="flex items-baseline justify-between gap-3 border-b border-hairline-strong pb-3">
            <p className="wordmark text-[1rem] text-ink">HiSahab</p>
            <p className="text-[0.8125rem] text-ink-muted">Bill, {LEDGER.bill.period}</p>
          </div>
          <dl className="tabular mt-3 grid grid-cols-[1fr_auto] gap-y-2 text-[0.9375rem]">
            <dt className="text-ink-muted">Owed before</dt>
            <dd className="text-right text-ink">{LEDGER.bill.before}</dd>
            <dt className="text-ink-muted">Udhaar in the fortnight</dt>
            <dd className="text-right text-ink">{LEDGER.bill.udhaar}</dd>
            <dt className="text-ink-muted">Paid in the fortnight</dt>
            <dd className="text-right text-ink">{LEDGER.bill.repaid}</dd>
            <dt className="border-t border-hairline-strong pt-2 font-semibold text-ink">To pay</dt>
            <dd className="border-t border-hairline-strong pt-2 text-right font-semibold text-ink">{LEDGER.bill.billed}</dd>
          </dl>
        </article>
        <SampleNote />
      </div>
    </section>
  );
}

/* §5.3a, §11 phase 20: the statement verifies the books; nothing is written until a person ticks. */
function BankSection() {
  return (
    <section data-section="bank" className="overflow-hidden py-24 lg:flex lg:min-h-[100dvh] lg:items-center lg:py-0">
      {/* On a phone this row scrolls sideways, so it takes keyboard focus and has a name (axe:
       * a scrollable region nobody can reach by keyboard is a region they cannot read). */}
      <div
        data-bank-track
        tabIndex={0}
        role="region"
        aria-label="Statement lines, matched"
        className="flex snap-x snap-mandatory gap-5 overflow-x-auto px-6 pb-4 lg:w-max lg:snap-none lg:overflow-visible lg:pb-0"
      >
        <div className="w-[min(85vw,26rem)] shrink-0 snap-start self-center lg:w-[30rem]">
          <Headline text="The bank statement checks the books." />
          <Body>
            Upload the bank's statement. Paytm's next-day settlement, cash deposits and udhaar payments are matched against what was entered. Nothing is
            recorded until a person ticks it.
          </Body>
        </div>
        {BANK.map((line) => (
          <article key={line.narration} className="flex w-[min(80vw,20rem)] shrink-0 snap-start flex-col justify-between gap-6 rounded-[var(--radius-card)] border border-hairline bg-surface p-5 shadow-2 lg:w-[22rem]">
            <div>
              <p className="text-[0.75rem] font-medium tracking-[0.04em] text-ink-faint">{line.direction === "credit" ? "Money in" : "Money out"}</p>
              <p className="mt-1 font-mono text-[0.875rem] text-ink">{line.narration}</p>
              <p className="tabular mt-3 text-headline text-ink">{line.amount}</p>
            </div>
            <div className="flex items-center gap-3 border-t border-hairline pt-4">
              <Check tone={line.direction === "credit" ? "surplus" : "accent"} />
              <span className="text-[0.875rem] text-ink">{line.verdict}</span>
            </div>
          </article>
        ))}
        <div className="w-6 shrink-0 lg:w-[10vw]" aria-hidden="true" />
      </div>
      <div className="px-6 lg:hidden">
        <SampleNote />
      </div>
    </section>
  );
}

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
