/* Timestamps in, readable local time out (CLAUDE.md §3 rule 4, §6.1).
 *
 * §3 rule 4: every instant is stored in UTC, and **display conversion to the outlet's zone
 * happens in the frontend only**. This module is that conversion, and the only place it
 * happens. The zone comes from `GET /api/v1/client-config`, never a constant here: `TZ_DISPLAY`
 * is server configuration (§13.11), and a second copy would be a second source of truth.
 *
 * ## business_date is NOT converted, and that is the important part
 *
 * §5.2 makes `business_date` an explicit DATE column, never derived from a timestamp, and §6.1
 * exists because the night shift crosses midnight. Running a business date through a timezone
 * conversion is precisely the bug that column was created to prevent: "2026-08-23" parsed as an
 * instant is midnight UTC, which west of UTC is the 22nd. `businessDate` formats the string's
 * own parts and never constructs a Date at all.
 *
 * Ported from the Phase 12 `time.js`, behaviour unchanged (Phase 23).
 */

import { UNEXPLAINED } from "./money";

/* Set once at boot from /client-config. Module-level because it is genuinely global for the
 * session; threading it through would put a `tz` parameter on every screen. */
let zone = "Asia/Kolkata";

export function setZone(tzDisplay: string | null | undefined): void {
  if (tzDisplay) zone = tzDisplay;
}

export function currentZone(): string {
  return zone;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

interface Absent {
  absent?: string;
}

/** A business date, formatted from its own parts. **Never parsed into a Date.** */
export function businessDate(value: string | null | undefined, { absent = UNEXPLAINED }: Absent = {}): string {
  if (!value) return absent;
  const [year, month, day] = String(value).slice(0, 10).split("-");
  if (!year || !month || !day) return String(value);
  return `${Number(day)} ${MONTHS[Number(month) - 1] ?? month} ${year}`;
}

/** A business date without its year, for a chart axis where space is scarce. Parts, never a
 * Date -- the reason does not weaken because the year is being dropped. */
export function businessDateShort(value: string | null | undefined): { day: string; month: string } {
  if (!value) return { day: "", month: "" };
  const [, month, day] = String(value).slice(0, 10).split("-");
  if (!month || !day) return { day: String(value), month: "" };
  return { day: String(Number(day)), month: MONTHS[Number(month) - 1] ?? month };
}

/** The weekday of a business date. UTC accessors on a UTC-anchored date, so the arithmetic is
 * zone-independent: it only names the weekday of that calendar day. */
export function businessDateWeekday(value: string | null | undefined): string {
  if (!value) return "";
  const [year, month, day] = String(value).slice(0, 10).split("-").map(Number);
  if (!year || !month || !day) return "";
  const anchored = new Date(Date.UTC(year, month - 1, day));
  return WEEKDAYS[anchored.getUTCDay()] ?? "";
}

function formatter(options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  return new Intl.DateTimeFormat("en-IN", { timeZone: zone, ...options });
}

/** An instant, in the outlet's local time. */
export function dateTime(value: string | null | undefined, { absent = UNEXPLAINED }: Absent = {}): string {
  if (!value) return absent;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return formatter({
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
  }).format(parsed);
}

/** Just the clock time, for a shift on a screen that already names the day. */
export function timeOnly(value: string | null | undefined, { absent = UNEXPLAINED }: Absent = {}): string {
  if (!value) return absent;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return formatter({ hour: "2-digit", minute: "2-digit", hour12: true }).format(parsed);
}

/** A shift template's `starts_at_local`. §5.1: a TIME is a recurring wall-clock time, not an
 * instant, so this formats the string and converts nothing. */
export function localTime(value: string | null | undefined, { absent = UNEXPLAINED }: Absent = {}): string {
  if (!value) return absent;
  const [hours, minutes] = String(value).split(":");
  const hour = Number(hours);
  if (Number.isNaN(hour)) return String(value);
  const suffix = hour < 12 ? "am" : "pm";
  const twelve = hour % 12 === 0 ? 12 : hour % 12;
  return `${twelve}:${minutes ?? "00"} ${suffix}`;
}

/** "today" in the outlet's zone, as YYYY-MM-DD. §6.1 evaluates "future" in the outlet's zone,
 * so a form defaulting to the browser's own date would offer tomorrow to anybody east of it. */
export function todayAtOutlet(): string {
  const parts = formatter({ year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date());
  const get = (type: string) => parts.find((part) => part.type === type)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")}`;
}

/** Relative phrasing for recent activity, falling back quickly to the date: §4.7 says the
 * register is typed up after the fact, so "3 days ago" is worse than the date itself. */
export function relative(value: string | null | undefined, { absent = UNEXPLAINED }: Absent = {}): string {
  if (!value) return absent;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  const seconds = (Date.now() - parsed.getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return dateTime(value);
}

/** A `datetime-local` value as an ISO instant with an offset. Every timestamp the API accepts
 * rejects a naive value with 422 NAIVE_TIMESTAMP (§3 rule 4); the browser parses a zoneless
 * string as local time, which is what the person typing meant. */
export function toOffsetISO(localValue: string | null | undefined): string | null {
  if (!localValue) return null;
  const parsed = new Date(localValue);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toISOString();
}

/** A `datetime-local` value for "now", for pre-filling a field. */
export function nowLocalValue(): string {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}
