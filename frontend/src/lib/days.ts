/* A business date, and everything that is true about it (Phase 15's days.js, as pure functions).
 *
 * ## A missing row is not a missing day
 *
 * `GET /daily-summaries` reads one table, so a date that traded and was never reconciled is
 * structurally invisible there -- and that is exactly the date somebody needs to act on (§14).
 * So the day list is a client-side merge of `GET /shifts` with `GET /daily-summaries`, keyed on
 * `business_date`. No new endpoint: both exist and both are manager-floor (§8).
 *
 * ## The lifecycle, drawn rather than documented
 *
 *     Entered → Closed → Locked → Reconciled → Finalised
 *
 * Two steps are per shift and two per day, which is why the sequence confuses a newcomer.
 * `actual_counted` is deliberately NOT a step: under §6.5's locker model most days are never
 * counted, and making it one would mark every normal day incomplete.
 *
 * ## Oldest first, and only the oldest may be reconciled
 *
 * §6.5's opening balance chains from the most recent *summary*, not from yesterday, so
 * reconciling the 4th before the 3rd skips a day's cash permanently. The server refuses with 409
 * EARLIER_DAY_NOT_RECONCILED; the client offers the act on one day only, so nobody has to be
 * refused to find out.
 *
 * Business dates are compared as ISO strings, which order correctly. No Date is constructed:
 * a business date is a calendar day and is never timezone-converted (§6.1).
 */

import { satisfies } from "./roles";

export const LIFECYCLE = ["Entered", "Closed", "Locked", "Reconciled", "Finalised"] as const;

export interface DayShift {
  id: string;
  business_date: string;
  status: string;
  sequence?: number;
}

export interface DaySummary {
  business_date: string;
  is_finalised: boolean;
  requires_review: boolean;
  expected_closing?: string | null;
  variance?: string | null;
}

export interface Day<S extends DayShift = DayShift, M extends DaySummary = DaySummary> {
  business_date: string;
  shifts: S[];
  summary: M | null;
}

/** One row per business date, newest first. */
export function mergeDays<S extends DayShift, M extends DaySummary>(shifts: S[], summaries: M[]): Day<S, M>[] {
  const byDate = new Map<string, Day<S, M>>();
  const entry = (date: string) => {
    let day = byDate.get(date);
    if (!day) {
      day = { business_date: date, shifts: [], summary: null };
      byDate.set(date, day);
    }
    return day;
  };
  for (const shift of shifts) entry(shift.business_date).shifts.push(shift);
  for (const summary of summaries) entry(summary.business_date).summary = summary;
  return [...byDate.values()].sort((a, b) => (a.business_date < b.business_date ? 1 : a.business_date > b.business_date ? -1 : 0));
}

/** The oldest day that traded and has no summary, or null. Mirrors §6.5's server rule. */
export function oldestUnreconciled(days: Day[]): string | null {
  const pending = days.filter((day) => day.shifts.length > 0 && day.summary === null);
  return pending.length ? (pending[pending.length - 1]?.business_date ?? null) : null;
}

export type DayKind = "open" | "closed" | "locked" | "review";

export interface DayAction {
  text: string;
  /** Navigate here, or... */
  path?: string;
  /** ...reconcile the day in place. */
  reconcile?: true;
  primary?: boolean;
}

export interface DayState {
  reached: number;
  label: string;
  kind: DayKind;
  hint: string;
  action: DayAction | null;
  /** Set when an earlier day must be reconciled first. */
  blockedBy: string | null;
}

/**
 * Where a day stands, and the one act that would move it on. `action` is null when the day is
 * finished or waiting on somebody else -- never a button that would be refused. Every verb is a
 * promise about what will happen (§14): "Open the shift to lock it" navigates, it does not lock.
 */
export function dayState(day: Day, { role = "manager", unblocked = null }: { role?: string; unblocked?: string | null } = {}): DayState {
  const open = day.shifts.find((shift) => shift.status === "open");
  const unlocked = day.shifts.filter((shift) => shift.status !== "locked");

  if (open) {
    return {
      reached: 0,
      label: "Entry in progress",
      kind: "open",
      hint: "This shift is still open. Finish typing it in, then close it.",
      action: { text: "Continue entry", path: `/shifts/${open.id}` },
      blockedBy: null,
    };
  }

  if (day.shifts.length && unlocked.length) {
    return {
      reached: 2,
      label: "Closed",
      kind: "closed",
      hint: "Entry is finished and figures can now only be corrected by reversal. An admin locks the shift to make it permanent.",
      action: satisfies(role, "admin") && unlocked[0] ? { text: "Open the shift to lock it", path: `/shifts/${unlocked[0].id}` } : null,
      blockedBy: null,
    };
  }

  if (day.summary === null) {
    const blocked = unblocked !== null && unblocked !== day.business_date;
    return {
      reached: 3,
      label: "Not reconciled",
      kind: "closed",
      hint: "Every shift is locked. Reconciling stores this day's cash position and carries the balance into the next day.",
      action: blocked ? null : { text: "Reconcile this day", reconcile: true, primary: true },
      blockedBy: blocked ? unblocked : null,
    };
  }

  if (day.summary.is_finalised) {
    return { reached: 5, label: "Finalised", kind: "locked", hint: "This day is settled and its figures are frozen.", action: null, blockedBy: null };
  }

  const review = day.summary.requires_review;
  return {
    reached: 4,
    label: review ? "Needs review" : "Reconciled",
    kind: review ? "review" : "open",
    hint: review
      ? "A shift beneath this day moved after it was reconciled. Nothing was recomputed: a person reconciles the two."
      : "The cash position is stored. An admin finalises the day to freeze it.",
    action: satisfies(role, "admin") ? { text: "Open the day to finalise it", path: `/days/${day.business_date}` } : null,
    blockedBy: null,
  };
}
