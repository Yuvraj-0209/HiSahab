/* Charts with no arithmetic on money (Phase 13's chart.js, for React).
 *
 * **A bar chart is `value / max × height`, which is arithmetic on money.** §3 rule 1 does not
 * stop at the API boundary, so that division never happens here: the server sends
 * `bar_height_pct` as a ready-made CSS percentage computed in Decimal, and this module ASSIGNS
 * it. Because the bars and the table beneath come from one server pass, the chart cannot
 * disagree with its own numbers.
 *
 * ## Paging, and what paging must never do
 *
 * Bars come in pages of ten (Phase 19b: at 90 days a bar is a hairline). `bar_height_pct` stays
 * scaled to the tallest day in the WHOLE window -- re-scaling per page would be `value / max`
 * in JavaScript, and would also lie: every page's tallest bar would reach the top, so a quiet
 * week would look like a record one. Paging changes what is visible, never what anything means.
 * A short last page is padded with empty columns, or a 31-day window's one leftover day would
 * render as a single bar spanning the whole chart: a plausible, enormous, wrong day. A window
 * that fits on one page is not padded -- there is no other page for its bars to match.
 *
 * ## Motion
 *
 * The rail between pages is driven by the spring (interruptible: a second tap mid-turn
 * redirects it). When they come into view the bars grow from the baseline with GSAP -- `scaleY`, a
 * transform on each bar; the height itself is still the server's string. Two engines, never on
 * the same element (§14). Reduced motion: both simply arrive.
 *
 * Every plotted value also appears in the table below, so the SVG is aria-hidden and a screen
 * reader gets the real figures.
 */

import { type ReactNode, useEffect, useRef, useState } from "react";
import { CaretLeftIcon, CaretRightIcon } from "@phosphor-icons/react";
import type { Schemas } from "../api/types";
import { format, varianceIsShort, varianceLabel } from "../lib/money";
import { businessDate, businessDateShort, businessDateWeekday } from "../lib/time";
import { DURATION, EASE, gsap, useMotion } from "../motion/gsap";
import "../motion/scroll";
import { PRESETS, Spring } from "../motion/spring";

type RangeDay = Schemas["RangeDayResponse"];

/* Charts grow when they come into view, not when they mount (Phase 24 D4). A chart below the
 * fold that animates on mount grows where nobody can see it, and is simply there by the time
 * anyone scrolls to it -- so the motion that says "this is the figure" said it to nobody. Only
 * transforms move (`scaleY`, `scaleX`, a dash length); the geometry is the server's string
 * throughout, and a chart already on screen starts at once. */
const ON_VIEW = "top 92%";

/** What a sales bar needs. The range report's days carry a variance; the summary's trend days
 * do not, and the detail panel simply omits it rather than printing an absent one. */
export type BarDay = Pick<RangeDay, "business_date" | "source" | "total_sales" | "alert" | "bar_height_pct"> & { variance?: string | null };

/** The words a `source` means (§13.20), used in labels and legends alike so they cannot drift. */
export function describeSource(source: string): string {
  switch (source) {
    case "snapshot":
      return "reconciled, figures as recorded that day";
    case "computed":
      return "not reconciled, figures calculated just now";
    case "no_trading":
      return "no trading";
    case "unavailable":
      return "cannot be calculated";
    default:
      return source;
  }
}

export function SalesBars<Day extends BarDay>({ days, onSelect, perPage = 10 }: { days: Day[]; onSelect?: (day: Day) => void; perPage?: number }) {
  const pages: Day[][] = [];
  for (let index = 0; index < days.length; index += perPage) pages.push(days.slice(index, index + perPage));

  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<Day | null>(null);
  const [hovered, setHovered] = useState<Day | null>(null);
  const rail = useRef<HTMLDivElement>(null);
  const scope = useRef<HTMLDivElement>(null);
  const spring = useRef<Spring | null>(null);
  const canHover = typeof matchMedia === "function" && matchMedia("(hover: hover)").matches;

  // The rail is positioned in PAGE UNITS, so no width is ever measured.
  useEffect(() => {
    const node = rail.current;
    if (!node) return;
    const s = new Spring({ ...PRESETS.move, onChange: (value) => (node.style.transform = `translate3d(${value * -100}%, 0, 0)`) });
    spring.current = s;
    return () => {
      s.stop();
    };
  }, []);

  // Bars grow from the baseline once, on first view.
  useMotion(
    (play) =>
      play(() => {
        gsap.from("[data-bar]", {
          scaleY: 0,
          transformOrigin: "50% 100%",
          duration: DURATION.large,
          ease: EASE.enter.gsap,
          stagger: 0.025,
          scrollTrigger: { trigger: scope.current, start: ON_VIEW, once: true },
        });
      }),
    { scope },
  );

  /* A wrap (last page to first) re-seats the rail on the far side silently and springs one page,
   * so the motion always agrees with the arrow pressed. */
  function turn(direction: 1 | -1) {
    const next = (page + direction + pages.length) % pages.length;
    if (next !== page + direction) spring.current?.set(next - direction);
    setPage(next);
    spring.current?.to(next);
  }

  if (!days.length) return null;
  const shown = hovered ?? selected;
  const first = page * perPage + 1;
  const last = Math.min((page + 1) * perPage, days.length);

  return (
    <div ref={scope} className="flex flex-col gap-3">
      <div className="relative overflow-hidden" onPointerLeave={() => setHovered(null)}>
        <div ref={rail} className="flex">
          {pages.map((members, index) => (
            <div key={index} className="flex w-full shrink-0 items-end gap-1.5 px-1" aria-hidden={index !== page}>
              {members.map((day) => {
                const absent = day.total_sales === null || day.source === "unavailable";
                const isSelected = selected?.business_date === day.business_date;
                const label = businessDateShort(day.business_date);
                return (
                  <button
                    key={day.business_date}
                    type="button"
                    tabIndex={index === page ? 0 : -1}
                    aria-label={`${businessDateWeekday(day.business_date)} ${day.business_date}, ${describeSource(day.source)}`}
                    onClick={() => setSelected(day)}
                    onFocus={() => setHovered(day)}
                    onBlur={() => setHovered(null)}
                    onPointerEnter={canHover ? () => setHovered(day) : undefined}
                    className="flex min-w-0 flex-1 flex-col items-center gap-1.5"
                  >
                    <div className={`relative flex h-28 w-full items-end rounded-md ${isSelected ? "bg-accent-tint" : ""}`}>
                      {/* The one place a server-computed percentage sets geometry. Assigned, never derived. */}
                      <div
                        data-bar
                        className={`w-full rounded-md ${
                          absent ? "border border-dashed border-hairline-strong" : day.alert ? "bg-warning" : day.source === "snapshot" ? "bg-accent" : "bg-accent/45"
                        }`}
                        style={{ height: day.bar_height_pct }}
                      />
                    </div>
                    <span className="flex flex-col items-center leading-none">
                      <span className="tabular text-caption text-ink">{label.day}</span>
                      <span className="text-micro text-ink-faint uppercase">{label.month}</span>
                    </span>
                  </button>
                );
              })}
              {/* Padding keeps a bar the same width on every page; one page has nothing to match. */}
              {Array.from({ length: pages.length > 1 ? perPage - members.length : 0 }, (_, pad) => (
                <div key={`pad-${pad}`} className="min-w-0 flex-1" aria-hidden="true" />
              ))}
            </div>
          ))}
        </div>
      </div>

      {pages.length > 1 ? (
        <div className="flex items-center justify-between gap-3">
          <button type="button" aria-label="Previous days" onClick={() => turn(-1)} className="pressable rounded-full bg-surface-sunken p-2 text-ink">
            <CaretLeftIcon size={16} weight="bold" aria-hidden />
          </button>
          <span className="tabular text-caption text-ink-muted">
            {first} to {last} of {days.length}
          </span>
          <button type="button" aria-label="Next days" onClick={() => turn(1)} className="pressable rounded-full bg-surface-sunken p-2 text-ink">
            <CaretRightIcon size={16} weight="bold" aria-hidden />
          </button>
        </div>
      ) : null}

      <DayDetail day={shown} onSelect={onSelect} />
    </div>
  );
}

/** The panel under the bars. It teaches the interaction while empty: a phone has no hover. */
function DayDetail<Day extends BarDay>({ day, onSelect }: { day: Day | null; onSelect?: ((day: Day) => void) | undefined }) {
  if (!day) return <p className="text-footnote text-ink-muted">Tap a bar for that day's figures.</p>;
  const variance = day.variance === undefined ? null : varianceLabel(day.variance);
  return (
    <div className="flex items-start justify-between gap-3 rounded-[var(--radius-control)] bg-surface-sunken px-3.5 py-3">
      <div className="min-w-0">
        <p className="text-body text-ink">
          {businessDateWeekday(day.business_date)}, {businessDate(day.business_date)}
        </p>
        <p className="text-footnote text-ink-muted">{describeSource(day.source)}</p>
        {onSelect ? (
          <button type="button" onClick={() => onSelect(day)} className="mt-1 text-footnote font-medium text-accent">
            Open this day
          </button>
        ) : null}
      </div>
      <div className="flex shrink-0 flex-col items-end">
        <span className="tabular text-body text-ink">{format(day.total_sales, { absent: "not known" })}</span>
        {variance ? <span className={`tabular text-footnote ${variance.className}`}>{variance.text}</span> : null}
      </div>
    </div>
  );
}

/**
 * A signed strip: a mark per counted day, above or below a baseline. A surplus draws up, a
 * shortage down -- and the direction comes from `varianceIsShort`, the one place the variance
 * sign convention lives, so the chart and the label cannot disagree.
 */
export function VarianceStrip({ days }: { days: RangeDay[] }) {
  if (!days.some((day) => day.variance !== null)) return null;
  const width = Math.max(days.length * 24, 120);
  const height = 64;
  const mid = height / 2;
  return (
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true" focusable="false" className="h-16 w-full">
      <line x1={0} y1={mid} x2={width} y2={mid} className="stroke-hairline-strong" strokeWidth={1} />
      {days.map((day, index) => {
        if (day.variance === null) return null;
        const short = varianceIsShort(day.variance);
        const x = index * 24 + 12;
        const reach = day.alert ? mid - 6 : mid / 2;
        return (
          <line
            key={day.business_date}
            x1={x}
            y1={mid}
            x2={x}
            y2={short ? mid + reach : mid - reach}
            strokeWidth={6}
            strokeLinecap="round"
            className={short ? "stroke-short" : "stroke-surplus"}
          />
        );
      })}
    </svg>
  );
}

/* --- shares: the donut and the share bars (Phase 19) ------------------------------------- */

/* Colour by position in a stable list, never by hashing a code and never by size. Petrol must
 * be the same colour in the donut and its legend, and still that colour tomorrow: a palette
 * keyed on rank would repaint the chart whenever diesel overtook petrol, and the eye trusts
 * colour more than it trusts a label. Literal class names, so Tailwind can see them. */
const CAT_BG = ["bg-cat-1", "bg-cat-2", "bg-cat-3", "bg-cat-4", "bg-cat-5", "bg-cat-6"] as const;
const CAT_STROKE = ["stroke-cat-1", "stroke-cat-2", "stroke-cat-3", "stroke-cat-4", "stroke-cat-5", "stroke-cat-6"] as const;

/** The category colour for the n-th series (0-based); a seventh wraps. */
export const categoryColour = (index: number) => index % CAT_BG.length;

/** A swatch for a legend row, in the series' own colour. */
export function Swatch({ colour }: { colour: number }) {
  return <span aria-hidden="true" className={`inline-block size-2.5 shrink-0 rounded-[3px] ${CAT_BG[colour % CAT_BG.length]}`} />;
}

/** A server percentage string as a number, for GEOMETRY only.
 *
 * Not `parseFloat`, which the structural tests ban outright. The input is never money: it is a
 * share the server produced by dividing in Decimal, and the output only places an arc on a
 * circle. `null` (unknowable) stays null, so an unknown slice is not drawn as a zero one. */
function percentForGeometry(value: string | null): number | null {
  if (typeof value !== "string") return null;
  const digits = value.trim().replace("%", "");
  if (!/^-?\d+(\.\d+)?$/.test(digits)) return null;
  return Number(digits);
}

export interface Slice {
  key: string;
  share_pct: string | null;
  colour: number;
}

/**
 * A donut: one arc per slice, sized by the server's `share_pct`. A donut rather than a pie
 * because the hole holds the total, so the figure and its decomposition are read in one place.
 *
 * Each arc is a circle with `pathLength="100"`, so the percentage IS the dash length: nothing
 * is divided, by the server's string or anything else (§14). The arcs draw in once with GSAP,
 * tweening the dash from zero; reduced motion shows them whole. The SVG is aria-hidden because
 * every slice is also a labelled row in the legend beside it.
 */
export function Donut({
  slices,
  children,
  active = null,
  size = "md",
}: {
  slices: Slice[];
  children?: ReactNode;
  /** The slice being pointed at, if any (Phase 26). The others recede; nothing is resized. */
  active?: string | null;
  size?: "md" | "lg";
}) {
  const scope = useRef<HTMLDivElement>(null);
  const drawable = slices
    .map((slice) => ({ ...slice, pct: percentForGeometry(slice.share_pct) }))
    .filter((slice): slice is Slice & { pct: number } => slice.pct !== null && slice.pct > 0);

  useMotion(
    (play) => {
      if (!scope.current) return;
      play(() => {
        gsap.from("[data-slice]", {
          attr: { "stroke-dasharray": "0 100" },
          duration: DURATION.large,
          ease: EASE.enter.gsap,
          stagger: 0.07,
          scrollTrigger: { trigger: scope.current, start: ON_VIEW, once: true },
        });
      });
    },
    { scope },
  );

  if (!drawable.length) return null;

  // A hairline of ground between neighbours, taken from each arc's own length. Geometry on
  // percentages, never on rupees.
  const gap = drawable.length > 1 ? 0.6 : 0;
  let cursor = 0;
  return (
    <div ref={scope} className="relative grid place-items-center py-2">
      <svg
        viewBox="0 0 168 168"
        className={`-rotate-90 ${size === "lg" ? "size-60 sm:size-72" : "size-52"}`}
        aria-hidden="true"
        focusable="false"
      >
        <circle cx={84} cy={84} r={64} fill="none" strokeWidth={24} className="stroke-surface-sunken" />
        {drawable.map((slice) => {
          const start = cursor;
          cursor += slice.pct;
          // The receding is opacity on a wrapping group, so GSAP's dash tween and this CSS
          // transition never write to the same element (§14's one-engine rule).
          const receded = active !== null && active !== slice.key;
          return (
            <g key={slice.key} className={`transition-opacity duration-200 ${receded ? "opacity-25" : "opacity-100"}`}>
              <circle
                data-slice
                cx={84}
                cy={84}
                r={64}
                fill="none"
                pathLength={100}
                strokeWidth={24}
                strokeDasharray={`${Math.max(slice.pct - gap, 0.2)} 100`}
                strokeDashoffset={-start}
                className={CAT_STROKE[slice.colour % CAT_STROKE.length]}
              />
            </g>
          );
        })}
      </svg>
      <div className={`pointer-events-none absolute grid place-items-center text-center ${size === "lg" ? "max-w-40" : "max-w-32"}`}>{children}</div>
    </div>
  );
}

/* --- selectable category bars (Phase 26) -------------------------------------------------- */

export interface CategoryBar {
  key: string;
  label: string;
  /** Pre-formatted by the caller, from the server's string. */
  value: string;
  /** This category's part of the whole, the label beside the bar. */
  share_pct: string | null;
  /** The bar's LENGTH, relative to the largest category -- the server's string, assigned. */
  bar_pct: string;
}

/**
 * Horizontal bars a reader can choose between: the list behind the chosen one is shown beside
 * it. One series, so one hue (the dataviz rule: a single series needs no legend and no
 * categorical palette); the chosen bar is the accent and the rest recede.
 *
 * A radio group, because exactly one category is chosen at a time: arrow keys move the choice,
 * Tab leaves the group. `onPoint` fires as a pointer arrives, so the caller can fetch ahead and
 * choose after a short intent delay; `onChoose` is a deliberate pick.
 */
export function CategoryBars({
  rows,
  selected,
  onChoose,
  onPoint,
  onUnpoint,
  label,
}: {
  rows: CategoryBar[];
  selected: string | null;
  onChoose: (key: string) => void;
  onPoint?: (key: string) => void;
  onUnpoint?: () => void;
  label: string;
}) {
  const scope = useRef<HTMLDivElement>(null);
  useMotion(
    (play) => {
      if (!scope.current) return;
      play(() => {
        gsap.from("[data-catbar]", {
          scaleX: 0,
          transformOrigin: "0% 50%",
          duration: DURATION.large,
          ease: EASE.enter.gsap,
          stagger: 0.05,
          scrollTrigger: { trigger: scope.current, start: ON_VIEW, once: true },
        });
      });
    },
    { scope },
  );

  function step(from: number, direction: 1 | -1) {
    const next = (from + direction + rows.length) % rows.length;
    const row = rows[next];
    if (!row) return;
    onChoose(row.key);
    scope.current?.querySelectorAll<HTMLButtonElement>("[role=radio]")[next]?.focus();
  }

  return (
    <div ref={scope} role="radiogroup" aria-label={label} className="flex flex-col gap-1" onPointerLeave={onUnpoint}>
      {rows.map((row, index) => {
        const isSelected = row.key === selected;
        return (
          <button
            key={row.key}
            type="button"
            role="radio"
            aria-checked={isSelected}
            tabIndex={isSelected || (selected === null && index === 0) ? 0 : -1}
            onClick={() => onChoose(row.key)}
            onPointerEnter={onPoint ? () => onPoint(row.key) : undefined}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown" || event.key === "ArrowRight") {
                event.preventDefault();
                step(index, 1);
              } else if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
                event.preventDefault();
                step(index, -1);
              }
            }}
            className={`group rounded-[var(--radius-control)] px-3 py-2.5 text-left transition-colors duration-200 ${
              isSelected ? "bg-accent-tint" : "hover:bg-surface-sunken"
            }`}
          >
            <span className="flex items-baseline justify-between gap-3">
              <span className={`truncate text-body ${isSelected ? "font-semibold text-ink" : "text-ink"}`}>{row.label}</span>
              <span className="tabular shrink-0 text-body font-medium text-ink">{row.value}</span>
            </span>
            <span className="mt-2 flex items-center gap-2.5">
              <span className="h-2.5 grow overflow-hidden rounded-full bg-surface-sunken">
                {/* The server's length, assigned; the first-view growth is a scaleX on top. */}
                <span
                  data-catbar
                  className={`block h-full rounded-full transition-colors duration-200 ${isSelected ? "bg-accent" : "bg-accent/35 group-hover:bg-accent/55"}`}
                  style={{ width: row.bar_pct }}
                />
              </span>
              <span className={`tabular w-14 shrink-0 text-right text-caption ${row.share_pct === null ? "t-absent" : "text-ink-muted"}`}>
                {row.share_pct ?? "unknown"}
              </span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

/* --- the udhaar bridge (Phase 26) --------------------------------------------------------- */

export interface BridgeRow {
  key: string;
  /** What this step does, in words: the sign is a word's job here, not a colour's. */
  label: string;
  /** Pre-formatted by the caller, from the server's string. */
  value: string;
  offset_pct: string | null;
  width_pct: string | null;
  emphasis?: boolean;
}

const BRIDGE_TONE: Record<string, string> = {
  start: "bg-cat-5",
  given: "bg-cat-1",
  collected: "bg-cat-2",
  end: "bg-cat-5",
};

/**
 * Owed at start, plus given, minus collected, equals owed at end: a waterfall a reader follows
 * top to bottom. Where each bar starts and how long it is are the server's strings (§14): the
 * client never subtracts one figure from another to place a bar.
 *
 * On first view the bars draw in order, as a walk: the balance, then what was added growing
 * rightwards, then what came back growing LEFTWARDS from where the addition ended -- the motion
 * says "taken away" -- then where it landed. When the server withholds the geometry (a negative
 * balance has no honest bar), the rows still carry every figure and no track is drawn.
 */
export function Bridge({ rows }: { rows: BridgeRow[] }) {
  const scope = useRef<HTMLDivElement>(null);
  useMotion(
    (play) => {
      if (!scope.current) return;
      play(() => {
        const bars = gsap.utils.toArray<HTMLElement>("[data-bridge]");
        const timeline = gsap.timeline({ scrollTrigger: { trigger: scope.current, start: ON_VIEW, once: true } });
        bars.forEach((bar, index) => {
          timeline.from(
            bar,
            {
              scaleX: 0,
              transformOrigin: bar.dataset.bridge === "collected" ? "100% 50%" : "0% 50%",
              duration: DURATION.medium,
              ease: EASE.enter.gsap,
            },
            index * 0.16,
          );
        });
      });
    },
    { scope },
  );

  /* Every track is the full width of the card, beneath its own label and figure. That is what
   * makes the bars comparable: one scale needs one track length, and a track squeezed between a
   * label and a figure gets a different length on every row whose figure is wider. */
  const drawn = rows.some((row) => row.width_pct !== null);
  return (
    <div ref={scope} className="flex flex-col">
      {rows.map((row) => (
        <div key={row.key} className={`py-2.5 ${row.emphasis ? "mt-1 border-t border-hairline-strong pt-3.5" : ""}`}>
          <div className="flex items-baseline justify-between gap-3">
            <span className={`text-body ${row.emphasis ? "font-semibold text-ink" : "text-ink-muted"}`}>{row.label}</span>
            <span className={`tabular text-right ${row.emphasis ? "text-headline text-ink" : "text-lead text-ink"}`}>
              {row.value}
            </span>
          </div>
          {drawn ? (
            <span className="relative mt-2 block h-2.5 overflow-hidden rounded-full bg-surface-sunken" aria-hidden="true">
              {row.width_pct !== null && row.offset_pct !== null ? (
                <span
                  data-bridge={row.key}
                  className={`absolute inset-y-0 rounded-full ${BRIDGE_TONE[row.key] ?? "bg-cat-5"}`}
                  style={{ left: row.offset_pct, width: row.width_pct }}
                />
              ) : null}
            </span>
          ) : null}
        </div>
      ))}
    </div>
  );
}

export interface ShareRow {
  key: string;
  label: string;
  /** Pre-formatted by the caller, from the server's string. */
  value: string;
  share_pct: string | null;
  colour: number;
}

/**
 * Horizontal share bars, for what a donut cannot carry: many categories, or values lopsided
 * enough that small slices become slivers. The WIDTH is the server's `share_pct`, assigned;
 * the growth on first view is a GSAP `scaleX` on top of it, never a change to it.
 */
export function ShareBars({ rows }: { rows: ShareRow[] }) {
  const scope = useRef<HTMLDivElement>(null);
  useMotion(
    (play) => {
      if (!scope.current) return;
      play(() => {
        gsap.from("[data-share]", {
          scaleX: 0,
          transformOrigin: "0% 50%",
          duration: DURATION.large,
          ease: EASE.enter.gsap,
          stagger: 0.04,
          scrollTrigger: { trigger: scope.current, start: ON_VIEW, once: true },
        });
      });
    },
    { scope },
  );

  if (!rows.length) return null;
  return (
    <div ref={scope} className="flex flex-col gap-3.5">
      {rows.map((row) => (
        <div key={row.key}>
          <div className="flex items-baseline justify-between gap-3">
            <span className="flex min-w-0 items-center gap-2">
              <Swatch colour={row.colour} />
              <span className="truncate text-callout text-ink">{row.label}</span>
            </span>
            <span className="tabular shrink-0 text-callout text-ink">{row.value}</span>
          </div>
          <div className="mt-1.5 flex items-center gap-2.5">
            <div className="h-2 grow overflow-hidden rounded-full bg-surface-sunken">
              {row.share_pct !== null ? (
                <div data-share className={`h-full rounded-full ${CAT_BG[row.colour % CAT_BG.length]}`} style={{ width: row.share_pct }} />
              ) : null}
            </div>
            {/* An unknowable share says so, rather than rendering an empty bar that reads as
             * zero (§6.8). */}
            <span className={`tabular w-14 shrink-0 text-right text-caption ${row.share_pct === null ? "t-absent" : "text-ink-muted"}`}>
              {row.share_pct ?? "unknown"}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}
