/* The small vocabulary every screen is built from.
 *
 * Shape rule (styles.css header): cards 18px, controls 12px, pills fully round. Colour comes
 * from semantic tokens only. Every list has an empty state and every load has an error state
 * that quotes the request id (§9) -- a blank region answers nobody's question.
 */

import type { ButtonHTMLAttributes, ReactNode } from "react";
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
  const sizing = size === "sm" ? "h-9 px-3 text-[0.875rem]" : "h-11 px-4 text-[0.9375rem]";
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
        <p className="text-[0.8125rem] leading-snug text-ink-faint">{reason}</p>
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

/** A small label naming a group of rows inside a screen. A section heading in an app, not a
 * landing-page eyebrow: it is how a reader finds their place in a long day.
 *
 * `sticky` (Phase 24 D4) pins it under the chrome while its own `<section>` scrolls past, so a
 * long list always says which group you are reading; it gains a hairline only once it is
 * actually stuck (a scroll-state container query, where the browser has one). Use it only where
 * the label's parent is the section it names -- sticky is bounded by the parent. */
export function SectionLabel({ children, className = "", sticky = false }: { children: ReactNode; className?: string; sticky?: boolean }) {
  const label = (
    <h3 className={`${sticky ? "section-sticky-label py-1.5" : "mb-2"} text-[0.75rem] font-semibold tracking-[0.06em] text-ink-faint uppercase ${className}`}>
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
        <div className={`text-[0.9375rem] ${strong ? "font-semibold text-ink" : "text-ink"}`}>{label}</div>
        {detail ? <div className="mt-0.5 text-[0.8125rem] text-ink-muted">{detail}</div> : null}
      </div>
      {value !== undefined ? (
        <div className={`tabular shrink-0 text-right text-[0.9375rem] ${strong ? "font-semibold" : ""} ${valueClassName}`}>
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

export function Pill({ kind = "neutral", children }: { kind?: PillKind; children: ReactNode }) {
  return (
    <span className={`inline-flex h-6 items-center rounded-full px-2.5 text-[0.75rem] font-medium whitespace-nowrap ${PILL[kind]}`}>
      {children}
    </span>
  );
}

/** Every list says what an empty one means, and how it gets filled. */
export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-[var(--radius-card)] border border-dashed border-hairline-strong px-5 py-8 text-center text-[0.9375rem] text-ink-muted">
      {children}
    </div>
  );
}

/** Several endpoints return `truncated: true` rather than a cursor (§9's considered
 * exceptions). Showing a partial list silently is how a reader trusts a wrong total. */
export function TruncationNotice({ count }: { count: number }) {
  return (
    <p className="rounded-[var(--radius-control)] bg-warning-tint px-3.5 py-2.5 text-[0.8125rem] text-warning">
      Showing the first {count}. There are more rows than this view lists.
    </p>
  );
}

export function ErrorCard({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const requestId = requestIdOf(error);
  return (
    <Card>
      <div className="flex flex-col gap-3">
        <p className="text-[0.9375rem] text-ink">{explain(error)}</p>
        {requestId ? (
          <p className="text-[0.6875rem] tracking-wide text-ink-faint select-all">Reference {requestId}</p>
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
