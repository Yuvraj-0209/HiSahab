/* The screens the front door's phone shows (Phase 28 D4, CLAUDE.md §13.46).
 *
 * Each is a replica of a real screen, composed from the same primitives the app uses, holding the
 * literal strings in ../samples.ts and nothing else. Nothing is fetched and nothing is computed
 * (§14). Every figure is one the pytest checks against the others, so the phone tells one sample
 * day that adds up.
 *
 * Each screen exports its *focus*: the card or cards the step is about. The pinned phone on a
 * desktop draws the focus inside the app's chrome (MiniApp); a phone, and reduced motion, show the
 * focus alone, inline under the step's copy, because a phone inside a phone is too small to read.
 *
 * Elements the story animates carry `data-beat` attributes. The story finds them inside the
 * active screen; nothing here moves anything itself, except the lifecycle strip, which animates
 * its own newly reached step as it does in the app.
 */

import { GasPumpIcon, type IconProps, NotebookIcon, VaultIcon } from "@phosphor-icons/react";
import type { ComponentType, ReactNode } from "react";
import { LifecycleStrip } from "../../ui/lifecycle";
import { Card, ListRow, Pill } from "../../ui/primitives";
import { BANK, DAY, GAP, METER, SLIPS } from "../samples";

/* --- small parts ------------------------------------------------------------------------- */

/** A drawn tick. Its stroke is what the story draws (DrawSVG), so a confirmation is seen to happen. */
export function Check({ tone = "accent" }: { tone?: "accent" | "surplus" }) {
  return (
    <span className={`grid size-6 shrink-0 place-items-center rounded-full ${tone === "accent" ? "bg-accent text-on-accent" : "bg-surplus-tint text-surplus"}`} aria-hidden="true">
      <svg viewBox="0 0 20 20" className="size-4">
        <polyline data-check points="5.5 10.5 8.75 13.75 14.75 6.75" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </span>
  );
}

/** The app's domain card header: an icon chip, a title and a caption (today.tsx DomainCard). */
function CardHead({ Icon, title, caption, badge }: { Icon: ComponentType<IconProps>; title: string; caption: string; badge?: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="flex min-w-0 items-center gap-3">
        <span className="grid size-10 shrink-0 place-items-center rounded-[12px] bg-accent-tint text-accent">
          <Icon size={20} />
        </span>
        <div className="min-w-0">
          <p className="text-body font-semibold text-ink">{title}</p>
          <p className="truncate text-footnote text-ink-muted">{caption}</p>
        </div>
      </div>
      {badge}
    </div>
  );
}

/** A button's look without a button: the phone is a picture, and a picture has no controls. */
function FakeButton({ children }: { children: ReactNode }) {
  return <span className="flex h-11 w-full items-center justify-center rounded-[var(--radius-control)] bg-accent font-medium text-on-accent shadow-1">{children}</span>;
}

function ShiftBand({ status, hours }: { status: "open" | "closed"; hours: string }) {
  return (
    <Card>
      <div className="flex items-center gap-2">
        <Pill kind={status === "open" ? "open" : "closed"}>{status}</Pill>
        <span className="text-footnote text-ink-muted">{DAY.shift}</span>
      </div>
      <p className="mt-1.5 text-headline text-ink">{DAY.date}</p>
      <p className="mt-0.5 text-callout text-ink-muted">{hours}</p>
    </Card>
  );
}

/* --- Today, at the end of the day (the hero's phone) ------------------------------------- */

export function TodayFocus() {
  return (
    <>
      <ShiftBand status="open" hours={DAY.opened} />
      <Card>
        <CardHead Icon={GasPumpIcon} title="Metered sales" caption="Valued from the nozzle readings" />
        <p className="tabular mt-4 text-title text-ink">{DAY.metered}</p>
        <div className="mt-2">
          <ListRow label="Card" value={DAY.cardTaken} />
          <ListRow label="UPI" value={DAY.upiTaken} />
          <ListRow label="Udhaar given" value={DAY.udhaarIssued} />
        </div>
      </Card>
      <Card>
        <CardHead Icon={VaultIcon} title="Cash" caption="Counted into the locker" />
        <p className="tabular mt-4 text-title text-ink">{DAY.cashCounted}</p>
      </Card>
    </>
  );
}

/* --- 06:00: the shift opens, nothing entered yet ----------------------------------------- */

export function OpenFocus() {
  return (
    <>
      <div data-beat>
        <ShiftBand status="open" hours={DAY.opened} />
      </div>
      <div data-beat>
        <Card>
          <CardHead Icon={GasPumpIcon} title="Metered sales" caption="Valued from the nozzle readings" />
          <p className="mt-4 text-title">
            <span className="t-absent">not entered</span>
          </p>
        </Card>
      </div>
      <div data-beat className="flex flex-col gap-2">
        <FakeButton>Enter this shift</FakeButton>
      </div>
    </>
  );
}

/* --- §4.7: the carried opening is confirmed, then a second meter disagrees ---------------- */

function NozzleTile({ agrees }: { agrees: boolean }) {
  return (
    <div data-beat data-tile={agrees ? "agree" : "disagree"} className={`rounded-[var(--radius-card)] border bg-surface p-4 shadow-1 ${agrees ? "border-hairline" : "border-warning"}`}>
      <div className="flex items-center justify-between gap-3">
        <p className="text-body font-semibold text-ink">
          {METER.nozzle} <span className="font-normal text-ink-muted">· {METER.fuel}</span>
        </p>
        <span data-verdict>
          <Pill kind={agrees ? "open" : "review"}>{agrees ? "Confirmed" : "Needs review"}</Pill>
        </span>
      </div>
      <dl className="tabular mt-3 grid grid-cols-[1fr_auto] gap-y-1.5 text-body">
        <dt className="text-ink-muted">Carried forward</dt>
        <dd className="text-right text-ink">{METER.carried}</dd>
        <dt className="text-ink-muted">The meter now</dt>
        <dd className={`text-right ${agrees ? "text-ink" : "font-semibold text-warning"}`}>{agrees ? METER.meterAgrees : METER.meterDisagrees}</dd>
      </dl>
      <div className="mt-3 flex items-center gap-3 border-t border-hairline pt-3 text-callout">
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

export function MeterFocus() {
  return (
    <>
      <NozzleTile agrees />
      <NozzleTile agrees={false} />
    </>
  );
}

/* --- §5.2: the cash row is a declaration, checked against what the meters say -------------- */

export function CashFocus() {
  return (
    <Card>
      <CardHead Icon={VaultIcon} title="Collections" caption="How the money arrived" />
      <p className="tabular mt-4 text-title text-ink">{DAY.cashCounted}</p>
      <p className="text-footnote text-ink-muted">cash, counted into the locker</p>
      <div className="mt-2">
        <div data-beat>
          <ListRow label="Card" detail="One machine, one figure" value={DAY.cardTaken} />
        </div>
        <div data-beat>
          <ListRow label="UPI" detail="The QR at the island" value={DAY.upiTaken} />
        </div>
        <div data-beat>
          <ListRow label="Wallet" value={<span className="t-absent">not entered</span>} />
        </div>
      </div>
    </Card>
  );
}

/* --- §6.6: a credit sale cannot be saved without a photograph of its slip ------------------- */

export function UdhaarFocus() {
  return (
    <>
      <div data-beat className="overflow-hidden rounded-[var(--radius-card)] border border-hairline bg-surface shadow-1">
        <img
          src="/img/showroom/udhaar-slip-960.webp"
          alt=""
          loading="lazy"
          decoding="async"
          className="aspect-[16/9] w-full object-cover"
        />
        <div className="flex items-center justify-between gap-3 px-4 py-3">
          <p className="text-body font-semibold text-ink">The slip, photographed</p>
          <Pill kind="open">Receipt attached</Pill>
        </div>
      </div>
      <Card>
        <CardHead Icon={NotebookIcon} title="Udhaar" caption={`${SLIPS.length} slips today`} />
        <div className="mt-2">
          {SLIPS.map((slip) => (
            <div data-beat key={slip.customer}>
              <ListRow label={slip.customer} detail={`${slip.vehicle} · ${slip.fuel}`} value={slip.slip} />
            </div>
          ))}
          <ListRow label="Given today" value={DAY.udhaarIssued} strong />
        </div>
      </Card>
    </>
  );
}

/* --- §6.4: expected cash from the meters, counted cash declared, and the gap between --------- */

export function GapFocus({ closed }: { closed: boolean }) {
  return (
    <Card>
      <div className="tabular">
        {GAP.rows.map((row) => (
          <div data-beat key={row.label} className="flex items-baseline justify-between gap-4 border-b border-hairline py-2 text-body">
            <span className="text-ink-muted">
              <span className="inline-block w-4 text-ink-faint">{row.sign}</span>
              {row.label}
            </span>
            <span className="text-ink">{row.value}</span>
          </div>
        ))}
        <div data-beat className="flex items-baseline justify-between gap-4 border-b border-hairline-strong py-2 text-body font-semibold">
          <span className="text-ink">
            <span className="inline-block w-4" />
            He should be holding
          </span>
          <span className="text-ink">{GAP.accountable}</span>
        </div>
        <div data-beat className="flex items-baseline justify-between gap-4 py-2 text-body">
          <span className="text-ink-muted">
            <span className="inline-block w-4" />
            He counted
          </span>
          <span className="text-ink">{GAP.declared}</span>
        </div>
      </div>
      <div data-gap className="mt-3 flex items-center justify-between gap-3 rounded-[var(--radius-control)] border border-hairline-strong bg-surface-raised px-4 py-3">
        <span className="font-semibold text-short">Gap</span>
        <span className="flex items-center gap-2">
          <span className="tabular text-headline text-short">{GAP.gap}</span>
          <Pill kind="short">short</Pill>
        </span>
      </div>
      <div className="mt-4">
        <LifecycleStrip state={closed ? { reached: 2, label: "Closed, ready to reconcile" } : { reached: 1, label: "Entered" }} />
      </div>
    </Card>
  );
}

/* --- §5.3a: the bank statement is matched line by line, and a person ticks ------------------ */

export function BankFocus() {
  return (
    <Card className="py-1 sm:py-1">
      {BANK.map((line) => (
        <div data-beat key={line.narration} className="flex flex-col gap-2 border-b border-hairline py-3 last:border-b-0">
          <div className="flex items-baseline justify-between gap-3">
            <p className="truncate font-mono text-footnote text-ink">{line.narration}</p>
            <p className="tabular shrink-0 text-body font-semibold text-ink">{line.amount}</p>
          </div>
          <div className="flex items-center gap-2.5">
            <Check tone={line.direction === "credit" ? "surplus" : "accent"} />
            <span className="text-footnote text-ink-muted">{line.verdict}</span>
          </div>
        </div>
      ))}
    </Card>
  );
}
