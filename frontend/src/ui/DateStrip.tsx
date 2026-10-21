/* A strip of ten days with the chosen one at the centre, larger (Phase 30).
 *
 * Built for the flagged-expense queue, where the unit of work is a trading day (§4.7: the day is
 * typed after the fact, so which day is the whole question). It is a radio group of days:
 * one is chosen, and the rest are the neighbours you might want next.
 *
 * ## The window
 *
 * Five days before the chosen one and four after, so ten are visible and the chosen one is
 * always the centre. Days after `max` (today) are shown dimmed and disabled rather than
 * dropped: a strip that re-anchored near today would move the chosen day off-centre, and
 * "where am I" is the one thing it must keep answering.
 *
 * ## The motion is CSS, and why
 *
 * Fourteen chips are rendered, keyed by date; the four outside the visible ten are parked
 * invisible. Choosing another day changes each chip's offset from the centre, and CSS
 * transitions carry it there (styles.css `.date-strip-chip`) -- so a chip that stays on screen
 * glides, one entering fades in at the edge, and nothing is drawn per frame. GSAP is
 * deliberately not involved: a CSS transition is the cheaper engine for "go from this layout to
 * that one", and one engine per element is the rule (§14).
 *
 * ## Dates are strings
 *
 * Arithmetic goes through `addDays` (UTC-anchored); a business date is never turned into a
 * local `Date` (§6.1). ISO strings compare correctly as strings.
 *
 * ## Counts are counts
 *
 * A badge is a number of rows, not money, so showing it needs no server arithmetic. A day with
 * no entry is "nothing waiting" -- a true zero, and a quiet chip.
 */

import { type KeyboardEvent, type PointerEvent, useRef } from "react";
import { addDays, businessDateShort, businessDateWeekday } from "../lib/time";

const VISIBLE_BEFORE = 5;
const VISIBLE_AFTER = 4;
/** Chips rendered beyond the visible ten, each side, so one can fade in as the rail moves. */
const PARKED = 2;
/** Horizontal travel, in px, past which a swipe counts as a move rather than a tap. */
const SWIPE_THRESHOLD = 24;

export interface DateStripProps {
  /** The chosen business date, YYYY-MM-DD. */
  value: string;
  onChange: (date: string) => void;
  /** The latest date that may be chosen (today at the outlet). */
  max: string;
  /** How many rows wait on each date. A date with no entry has none. */
  badges?: Record<string, number>;
  label?: string;
}

function distanceClass(offset: number): string {
  const distance = Math.abs(offset);
  if (offset < -VISIBLE_BEFORE || offset > VISIBLE_AFTER) return "far";
  return String(Math.min(distance, 5));
}

export function DateStrip({ value, onChange, max, badges = {}, label = "Choose a day" }: DateStripProps) {
  const drag = useRef<{ x: number; moved: boolean } | null>(null);
  const suppressClick = useRef(false);

  const offsets: number[] = [];
  for (let offset = -(VISIBLE_BEFORE + PARKED); offset <= VISIBLE_AFTER + PARKED; offset++) offsets.push(offset);

  function choose(date: string) {
    if (date > max || date === value) return;
    onChange(date);
  }

  function onKeyDown(event: KeyboardEvent) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    choose(addDays(value, event.key === "ArrowLeft" ? -1 : 1));
  }

  function onPointerDown(event: PointerEvent<HTMLDivElement>) {
    drag.current = { x: event.clientX, moved: false };
  }

  function onPointerUp(event: PointerEvent<HTMLDivElement>) {
    const start = drag.current;
    drag.current = null;
    if (!start) return;
    const travelled = event.clientX - start.x;
    if (Math.abs(travelled) < SWIPE_THRESHOLD) return;
    // A swipe is a move, so the tap that ends it must not also choose the chip under the
    // finger. One step per chip-width of travel: dragging right goes back in time.
    suppressClick.current = true;
    const pitch = event.currentTarget.getBoundingClientRect().width / (VISIBLE_BEFORE + VISIBLE_AFTER + 1);
    const steps = Math.max(1, Math.round(Math.abs(travelled) / pitch));
    choose(addDays(value, travelled > 0 ? -steps : steps));
  }

  const { month } = businessDateShort(value);

  return (
    <div className="select-none">
      <p className="mb-1 text-center text-caption font-medium text-ink-muted" aria-hidden="true">
        {month} {value.slice(0, 4)}
      </p>
      <div
        role="radiogroup"
        aria-label={label}
        className="date-strip touch-pan-y"
        onKeyDown={onKeyDown}
        onPointerDown={onPointerDown}
        onPointerUp={onPointerUp}
        onPointerCancel={() => {
          drag.current = null;
        }}
      >
        {offsets.map((offset) => {
          const date = addDays(value, offset);
          const parked = offset < -VISIBLE_BEFORE || offset > VISIBLE_AFTER;
          const future = date > max;
          const chosen = offset === 0;
          const waiting = badges[date] ?? 0;
          const { day } = businessDateShort(date);
          const accessibleName = `${businessDateWeekday(date)} ${day} ${businessDateShort(date).month}${
            waiting ? `, ${waiting} flagged` : ""
          }`;
          return (
            <div
              key={date}
              className="date-strip-chip"
              data-d={distanceClass(offset)}
              data-future={future}
              style={{ "--o": offset } as React.CSSProperties}
            >
              <button
                type="button"
                role="radio"
                aria-checked={chosen}
                aria-label={accessibleName}
                aria-hidden={parked || undefined}
                disabled={future || parked}
                tabIndex={chosen ? 0 : -1}
                onClick={() => {
                  if (suppressClick.current) {
                    suppressClick.current = false;
                    return;
                  }
                  choose(date);
                }}
                className={`relative grid size-full place-items-center rounded-[var(--radius-control)] border text-center transition-colors disabled:cursor-default ${
                  chosen
                    ? "border-accent bg-surface-raised text-ink shadow-2"
                    : future
                      ? "border-transparent bg-transparent text-ink-faint"
                      : "border-transparent bg-transparent text-ink-muted"
                }`}
              >
                <span className="flex flex-col items-center leading-none">
                  <span className="text-micro font-medium uppercase tracking-wide">{businessDateWeekday(date)}</span>
                  <span className="tabular mt-1 text-subhead font-semibold">{day}</span>
                </span>
                {waiting > 0 && !parked ? (
                  <span
                    className="tabular absolute -right-2 -top-2 grid h-4 min-w-4 place-items-center rounded-full bg-warning px-1 text-micro font-semibold text-on-accent"
                    aria-hidden="true"
                  >
                    {waiting}
                  </span>
                ) : null}
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
