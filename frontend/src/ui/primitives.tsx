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
 * landing-page eyebrow: it is how a reader finds their place in a long day. */
export function SectionLabel({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <h3 className={`mb-2 text-[0.75rem] font-semibold tracking-[0.06em] text-ink-faint uppercase ${className}`}>
      {children}
    </h3>
  );
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

/** A loading placeholder in the shape of what is coming, rather than a spinner. */
export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="flex flex-col gap-3" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="h-[4.5rem] animate-pulse rounded-[var(--radius-card)] bg-surface-sunken" />
      ))}
    </div>
  );
}
