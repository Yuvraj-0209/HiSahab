/* "Everything else it does" (Phase 28 D3): six shipped features in one grid, after the day has
 * been told.
 *
 * A bento, from the 21st catalogue's feature grids, rebuilt here on the app's own cards. Every
 * tile names a shipped feature and its CLAUDE.md section, and every figure is a literal in
 * ./samples.ts (§14). No invented metrics, testimonials or logos.
 *
 * Motion: each tile rises once as it arrives (Showroom's `[data-reveal]`); the night-shift tile's
 * dark half wipes in over the light one, scrubbed on a desktop (Showroom's `compare`); a tile
 * lifts on hover where there is a mouse (`.liftable`, CSS). Nothing loops.
 */

import type { ReactNode } from "react";
import { Donut, SalesBars, Swatch } from "../ui/chart";
import { Body, Eyebrow, Headline, SampleNote } from "./parts";
import { AUDIT, DAYS, FUEL_MIX, LEDGER, MONTH, TWICE } from "./samples";

/** One tile. Two elements on purpose (§14, one element one engine): the wrapper is the grid item
 * and rises into place by GSAP; the card inside lifts on hover by CSS. */
function Tile({ title, body, className = "", children }: { title: string; body: ReactNode; className?: string; children?: ReactNode }) {
  return (
    <div data-reveal className={className}>
      <article className="liftable flex h-full flex-col gap-5 rounded-[var(--radius-card)] border border-hairline bg-surface p-5 shadow-1 sm:p-6">
        <div>
          <h3 className="text-headline text-ink">{title}</h3>
          <p className="mt-2 max-w-[38ch] text-body leading-relaxed text-ink-muted">{body}</p>
        </div>
        {children}
      </article>
    </div>
  );
}

export function Features() {
  return (
    <section data-section="features" className="bg-surface-sunken px-6 py-24 lg:py-32">
      <div className="mx-auto max-w-6xl">
        <div className="max-w-2xl">
          <Eyebrow>And the rest of the month</Eyebrow>
          <Headline text="Everything after the day." />
          <Body>Reports, bills, the audit trail and the phone in the salesman's pocket: each one shipped, each one shown with sample figures.</Body>
        </div>

        <div className="mt-14 grid grid-cols-1 gap-4 lg:grid-cols-3 lg:grid-rows-[auto_auto_auto]">
          {/* §11 phases 13 and 19, §13.7: the owner's window, with profit labelled for what it is. */}
          <Tile
            className="lg:col-span-2"
            title="The whole month on one screen."
            body="Sales by day from figures the server has already reconciled. Nothing on it is added up in the browser."
          >
            <div>
              <div className="grid grid-cols-1 gap-6 md:grid-cols-[minmax(0,1fr)_13rem] md:items-end">
                <div className="min-w-0">
                  <p className="text-footnote font-medium text-ink-muted">Sales, 1 to 10 September</p>
                  <p className="tabular mt-1 text-title text-ink">{MONTH.sales}</p>
                  <div className="mt-4">
                    <SalesBars days={DAYS} />
                  </div>
                </div>
                <div>
                  <p className="text-footnote font-medium text-ink-muted">Fuel mix</p>
                  <Donut slices={FUEL_MIX}>
                    <span className="text-caption text-ink-muted">3 fuels</span>
                  </Donut>
                  <ul className="mt-2 flex flex-col gap-1 text-footnote">
                    {FUEL_MIX.map((fuel) => (
                      <li key={fuel.key} className="flex items-center justify-between gap-3">
                        <span className="flex items-center gap-2 text-ink">
                          <Swatch colour={fuel.colour} />
                          {fuel.key}
                        </span>
                        <span className="tabular text-ink-muted">{fuel.share_pct}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
              <p className="mt-4 border-t border-hairline pt-4 text-callout text-ink-muted">
                Gross fuel margin <span className="tabular font-semibold text-ink">{MONTH.margin}</span>: the dealer's margin on litres and kilograms sold,
                labelled for what it is. Not business profit, which also moves with stock held when prices change.
              </p>
            </div>
          </Tile>

          {/* Phase 23's two palettes (§12): chosen by the phone, never by a setting. */}
          <Tile className="lg:row-span-2" title="Built for the night shift." body="Light by day, dark at night, chosen by the phone. The same screen, as the salesman sees it at 10pm.">
            <div data-night className="mx-auto mt-auto w-full max-w-[15rem]">
              <div className="relative overflow-hidden rounded-[2.2rem] border-[6px] border-device bg-surface shadow-2">
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
          </Tile>

          {/* §11 phase 21: the billing-period statement, with a print stylesheet. */}
          <Tile title="Bills on the 16th and the 1st." body="Every udhaar customer's fortnight, cut at the dates the outlet bills on, ready to print.">
            <div className="mt-auto rounded-[var(--radius-control)] border border-hairline bg-surface-raised p-4">
              <div className="flex items-baseline justify-between gap-3 border-b border-hairline-strong pb-2.5">
                <p className="wordmark text-[0.875rem] text-ink">HiSahab</p>
                <p className="text-caption text-ink-muted">{LEDGER.customer}</p>
              </div>
              <dl className="tabular mt-2.5 grid grid-cols-[1fr_auto] gap-y-1.5 text-callout">
                <dt className="text-ink-muted">Owed before</dt>
                <dd className="text-right text-ink">{LEDGER.bill.before}</dd>
                <dt className="text-ink-muted">Udhaar</dt>
                <dd className="text-right text-ink">{LEDGER.bill.udhaar}</dd>
                <dt className="text-ink-muted">Paid</dt>
                <dd className="text-right text-ink">{LEDGER.bill.repaid}</dd>
                <dt className="border-t border-hairline-strong pt-1.5 font-semibold text-ink">To pay</dt>
                <dd className="border-t border-hairline-strong pt-1.5 text-right font-semibold text-ink">{LEDGER.bill.billed}</dd>
              </dl>
            </div>
          </Tile>

          {/* §6.10: every money write carries an Idempotency-Key that belongs to the submission. */}
          <Tile title="Never recorded twice." body="On patchy 4G a save can time out and be sent again. Every save carries a key, so the second copy is recognised and nothing doubles.">
            <div className="tabular mt-auto flex flex-col gap-2 text-callout">
              <div className="flex items-center justify-between gap-3 rounded-[var(--radius-control)] border border-hairline bg-surface-raised px-3.5 py-2.5">
                <span className="text-ink">{TWICE.what}</span>
                <span className="font-semibold text-ink">{TWICE.sum}</span>
              </div>
              <div className="flex items-center justify-between gap-3 rounded-[var(--radius-control)] border border-dashed border-hairline-strong px-3.5 py-2.5 text-ink-muted">
                <span>Sent again, same key</span>
                <span className="text-caption font-medium tracking-[0.04em] uppercase">Not saved</span>
              </div>
            </div>
          </Tile>

          {/* §5.3, §11 phase 11: append-only, admin-readable, outlet-scoped. */}
          <Tile className="lg:col-span-2" title="Who changed what, and what it was before." body="Every price, margin, customer limit and shift reopened is written to an audit trail nobody can edit, with the old value beside the new.">
            <div className="tabular mt-auto grid grid-cols-1 gap-3 rounded-[var(--radius-control)] border border-hairline bg-surface-raised p-4 text-callout sm:grid-cols-[1fr_auto_auto] sm:items-center">
              <div>
                <p className="font-semibold text-ink">{AUDIT.what}</p>
                <p className="text-ink-muted">
                  {AUDIT.who}, {AUDIT.when}
                </p>
              </div>
              <p className="text-ink-muted line-through decoration-hairline-strong">{AUDIT.was}</p>
              <p className="font-semibold text-ink">{AUDIT.now}</p>
            </div>
          </Tile>

          {/* Phase 24 D8: a web app manifest, standalone, starting on Today. No service worker. */}
          <Tile title="Opens like an app." body="Add it to the home screen and it opens full-screen, without the browser around it. Nothing to install from a store.">
            <div className="mt-auto flex items-center gap-4">
              <img src="/icons/icon-192.png" alt="" width={56} height={56} loading="lazy" decoding="async" className="size-14 rounded-[14px] shadow-2" />
              <p className="text-callout text-ink-muted">HiSahab, on the home screen</p>
            </div>
          </Tile>
        </div>
        <SampleNote />
      </div>
    </section>
  );
}
