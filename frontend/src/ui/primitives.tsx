/* The small vocabulary every screen is built from.
 *
 * Shape rule (styles.css header): cards 18px, controls 12px, pills fully round. Colour comes
 * from semantic tokens only. Every list has an empty state and every load has an error state
 * that quotes the request id (§9) -- a blank region answers nobody's question.
 */

import { type ButtonHTMLAttributes, type ComponentType, type ReactNode, useRef } from "react";
import { CaretRightIcon, type IconProps } from "@phosphor-icons/react";
import { explain, requestIdOf } from "../api/client";

/* --- buttons ----------------------------------------------------------------------------- */

type Variant = "primary" | "secondary" | "plain" | "danger";

const VARIANT: Record<Variant, string> = {
  primary: "bg-accent text-on-accent shadow-1 hover:bg-accent-pressed",
  secondary: "border border-hairline-strong bg-surface-raised text-ink shadow-1 hover:bg-surface",
  plain: "text-accent hover:bg-accent-tint",
  danger: "bg-short-tint text-short hover:brightness-95",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  block?: boolean;
  size?: "md" | "sm";
  /** Why a disabled button is disabled. `title` is invisible on a phone, so it is shown as a
   * line under the button instead (§16's wayfinding rule). */
  reason?: string | undefined;
  icon?: ReactNode;
}

export function Button({
  variant = "secondary",
  block = false,
  size = "md",
  reason,
  icon,
  className = "",
  children,
  type = "button",
  ...rest
}: ButtonProps) {
  const sizing = size === "sm" ? "h-9 px-3 text-callout" : "h-11 px-4 text-body";
  const button = (
    <button
      type={type}
      className={`pressable inline-flex items-center justify-center gap-2 rounded-[var(--radius-control)] font-medium whitespace-nowrap transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${sizing} ${VARIANT[variant]} ${block ? "w-full" : ""} ${className}`}
      {...rest}
    >
      {icon}
      {children}
    </button>
  );
  if (rest.disabled && reason) {
    return (
      <div className={`flex flex-col gap-1 ${block ? "w-full" : ""}`}>
        {button}
        <p className="text-footnote leading-snug text-ink-faint">{reason}</p>
      </div>
    );
  }
  return button;
}

/* --- surfaces ---------------------------------------------------------------------------- */

export function Card({
  children,
  className = "",
  as: Tag = "section",
}: {
  children: ReactNode;
  className?: string;
  as?: "section" | "div" | "article";
}) {
  return (
    <Tag className={`rounded-[var(--radius-card)] border border-hairline bg-surface p-4 shadow-1 sm:p-5 ${className}`}>
      {children}
    </Tag>
  );
}

/** A card holding a list of rows, each with its own hairline: the card's padding is the rows'. */
export function ListCard({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <Card className={`py-1 sm:py-1 ${className}`}>{children}</Card>;
}

/* --- ways to somewhere else (Phase 28 D6) -------------------------------------------------- */

/**
 * A tile that opens another screen: an icon chip, a label, an optional hint, and a caret. The
 * Entry list, the Admin hub, the Cash tab's look-backs, the Credit hub and the Bank hub all used
 * to build this by hand, in five slightly different shapes; one tap target now looks like one.
 */
export function LinkTile({
  Icon,
  label,
  hint,
  onClick,
  arrive = false,
}: {
  Icon?: ComponentType<IconProps> | undefined;
  label: ReactNode;
  hint?: ReactNode;
  onClick: () => void;
  /** Join the screen's arrival choreography (`useArrival`'s `[data-arrive]`). */
  arrive?: boolean;
}) {
  return (
    <button
      type="button"
      data-arrive={arrive ? "" : undefined}
      onClick={onClick}
      className="pressable liftable link-row flex w-full items-center gap-3.5 rounded-[var(--radius-card)] border border-hairline bg-surface px-4 py-3.5 text-left shadow-1"
    >
      {Icon ? (
        <span className="grid size-10 shrink-0 place-items-center rounded-[var(--radius-control)] bg-accent-tint text-accent">
          <Icon size={20} aria-hidden />
        </span>
      ) : null}
      <span className="min-w-0 grow">
        <span className="block text-body font-medium text-ink">{label}</span>
        {hint ? <span className="block text-footnote text-ink-muted">{hint}</span> : null}
      </span>
      <CaretRightIcon size={16} className="link-caret shrink-0 text-ink-faint" aria-hidden />
    </button>
  );
}

/**
 * A row inside a list card that opens something: the whole row is the target and a caret ends it.
 * What the row says is the caller's; the shape, the hairline and the caret are this. Pass
 * `caret={false}` where the row ends in something else that already says "more".
 */
export function RowLink({
  children,
  caret = true,
  className = "",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { caret?: boolean }) {
  return (
    <button type="button" className={`pressable link-row flex w-full items-center gap-3 border-b border-hairline py-3 text-left last:border-b-0 ${className}`} {...rest}>
      {children}
      {caret ? <CaretRightIcon size={16} className="link-caret shrink-0 text-ink-faint" aria-hidden /> : null}
    </button>
  );
}

/* --- the one figure a card is for (Phase 28 D6) --------------------------------------------- */

/**
 * A label over the figure a card exists to show, at the scale's `figure` step. The value is
 * whatever the caller renders -- an `Amount` (so a changed figure rolls), a meter reading -- and is
 * never formatted here.
 */
export function HeroFigure({ label, children, className = "" }: { label: ReactNode; children: ReactNode; className?: string }) {
  return (
    <div>
      <p className="text-footnote font-medium text-ink-muted">{label}</p>
      <p className={`tabular mt-1 text-figure text-ink ${className}`}>{children}</p>
    </div>
  );
}

/** A small label naming a group of rows inside a screen. A section heading in an app, not a
 * landing-page eyebrow: it is how a reader finds their place in a long day.
 *
 * `sticky` (Phase 24 D4) pins it under the chrome while its own `<section>` scrolls past, so a
 * long list always says which group you are reading; it gains a hairline only once it is
 * actually stuck (a scroll-state container query, where the browser has one). Use it only where
 * the label's parent is the section it names -- sticky is bounded by the parent. */
export function SectionLabel({ children, className = "", sticky = false }: { children: ReactNode; className?: string; sticky?: boolean }) {
  const label = (
    <h3 className={`${sticky ? "section-sticky-label py-1.5" : "mb-2"} text-caption font-semibold tracking-[0.06em] text-ink-faint uppercase ${className}`}>
      {children}
    </h3>
  );
  return sticky ? <div className="section-sticky mb-1">{label}</div> : label;
}

/** A labelled value: the shape most reading surfaces take. Figures are tabular. */
export function ListRow({
  label,
  detail,
  value,
  valueClassName = "",
  strong = false,
}: {
  label: ReactNode;
  detail?: ReactNode;
  value?: ReactNode;
  valueClassName?: string;
  strong?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-hairline py-3 last:border-b-0">
      <div className="min-w-0">
        <div className={`text-body ${strong ? "font-semibold text-ink" : "text-ink"}`}>{label}</div>
        {detail ? <div className="mt-0.5 text-footnote text-ink-muted">{detail}</div> : null}
      </div>
      {value !== undefined ? (
        <div className={`tabular shrink-0 text-right text-body ${strong ? "font-semibold" : ""} ${valueClassName}`}>
          {value}
        </div>
      ) : null}
    </div>
  );
}

/* --- status ------------------------------------------------------------------------------ */

export type PillKind = "neutral" | "open" | "closed" | "locked" | "review" | "short" | "surplus";

const PILL: Record<PillKind, string> = {
  neutral: "bg-surface-sunken text-ink-muted",
  open: "bg-accent-tint text-accent",
  closed: "bg-surface-sunken text-ink",
  locked: "bg-ink text-surface",
  review: "bg-warning-tint text-warning",
  short: "bg-short-tint text-short",
  surplus: "bg-surplus-tint text-surplus",
};

/**
 * A status in a pill. When the status changes after the pill first appeared -- a shift going
 * open, closed, locked; a day being reconciled -- the new pill lands with a small settle
 * (Phase 24 D5): the state changed, and here is where. Never on first render, so a screen full
 * of pills does not pop on arrival.
 *
 * CSS rather than GSAP (`.pill[data-changed]` in styles.css), because this file is in the first
 * paint and GSAP is not. Re-keying the span on a change remounts it, which replays the keyframe.
 */
export function Pill({ kind = "neutral", children }: { kind?: PillKind; children: ReactNode }) {
  const first = useRef(kind);
  const changed = first.current !== kind;
  return (
    <span
      key={changed ? kind : "first"}
      data-changed={changed ? "" : undefined}
      className={`pill inline-flex h-6 items-center rounded-full px-2.5 text-caption font-medium whitespace-nowrap ${PILL[kind]}`}
    >
      {children}
    </span>
  );
}

/** Every list says what an empty one means, and how it gets filled. */
export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-[var(--radius-card)] border border-dashed border-hairline-strong px-5 py-8 text-center text-body text-ink-muted">
      {children}
    </div>
  );
}

/** Several endpoints return `truncated: true` rather than a cursor (§9's considered
 * exceptions). Showing a partial list silently is how a reader trusts a wrong total. */
export function TruncationNotice({ count }: { count: number }) {
  return <Notice>Showing the first {count}. There are more rows than this view lists.</Notice>;
}

/** A warning in words, on the warning tint: something a reader should know before trusting the
 * figures around it. Amber means warning and nothing else (§14). */
export function Notice({ children, className = "", role }: { children: ReactNode; className?: string; role?: "status" | undefined }) {
  return (
    <p role={role} className={`rounded-[var(--radius-control)] bg-warning-tint px-3.5 py-2.5 text-footnote text-warning ${className}`}>
      {children}
    </p>
  );
}

export function ErrorCard({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const requestId = requestIdOf(error);
  return (
    <Card>
      <div className="flex flex-col gap-3">
        <p className="text-body text-ink">{explain(error)}</p>
        {requestId ? (
          <p className="text-micro tracking-wide text-ink-faint select-all">Reference {requestId}</p>
        ) : null}
        {onRetry ? (
          <div>
            <Button onClick={onRetry}>Try again</Button>
          </div>
        ) : null}
      </div>
    </Card>
  );
}

/* --- loading ----------------------------------------------------------------------------- */

/** One placeholder bar. The shimmer is a gradient sliding across it on a pseudo-element -- a
 * transform, so it runs on the compositor (styles.css `.skeleton`). */
function Bone({ className = "" }: { className?: string }) {
  return <div className={`skeleton rounded-[8px] ${className}`} />;
}

/**
 * A loading placeholder in the shape of what is coming, rather than a spinner (Phase 24 D6).
 *
 * The shape matters more than the shimmer: when the data lands, a placeholder the size of the
 * real layout means nothing below it jumps. Four shapes cover every screen:
 *
 *   rows    stacked cards, the default (Phase 23's shape)
 *   list    one card of label/value rows: days, a ledger, a statement
 *   cards   a grid of summary cards with a figure each: Today, the Cash hub
 *   figure  one large figure over a chart: the Summary
 */
export function Skeleton({ rows = 3, shape = "rows" }: { rows?: number; shape?: "rows" | "list" | "cards" | "figure" }) {
  const items = Array.from({ length: rows }, (_, index) => index);
  let body: ReactNode;
  if (shape === "list") {
    body = (
      <div className="rounded-[var(--radius-card)] border border-hairline bg-surface px-4 py-1 shadow-1 sm:px-5">
        {items.map((index) => (
          <div key={index} className="flex items-center justify-between gap-4 border-b border-hairline py-3.5 last:border-b-0">
            <div className="flex grow flex-col gap-2">
              <Bone className="h-3.5 w-28" />
              <Bone className="h-2.5 w-20" />
            </div>
            <Bone className="h-3.5 w-24" />
          </div>
        ))}
      </div>
    );
  } else if (shape === "cards") {
    body = (
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {items.map((index) => (
          <div key={index} className="flex flex-col gap-3 rounded-[var(--radius-card)] border border-hairline bg-surface p-4 shadow-1 sm:p-5">
            <Bone className="h-3 w-24" />
            <Bone className="h-7 w-40" />
            <Bone className="h-2.5 w-full" />
            <Bone className="h-2.5 w-3/4" />
          </div>
        ))}
      </div>
    );
  } else if (shape === "figure") {
    body = (
      <div className="flex flex-col gap-4">
        <div className="flex flex-col gap-3 rounded-[var(--radius-card)] border border-hairline bg-surface p-4 shadow-1 sm:p-5">
          <Bone className="h-3 w-28" />
          <Bone className="h-10 w-56" />
          <Bone className="mt-2 h-40 w-full" />
        </div>
        {items.slice(1).map((index) => (
          <div key={index} className="h-[4.5rem] rounded-[var(--radius-card)] border border-hairline bg-surface p-4 shadow-1">
            <Bone className="h-3 w-1/3" />
          </div>
        ))}
      </div>
    );
  } else {
    body = (
      <div className="flex flex-col gap-3">
        {items.map((index) => (
          <Bone key={index} className="h-[4.5rem] rounded-[var(--radius-card)]" />
        ))}
      </div>
    );
  }
  return (
    <div aria-busy="true" aria-label="Loading">
      {body}
    </div>
  );
}
