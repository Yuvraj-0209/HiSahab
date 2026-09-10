/* Timestamps in, readable local time out (CLAUDE.md §3 rule 4, §6.1).
 *
 * §3 rule 4: every instant is stored in UTC, and **display conversion to the outlet's zone
 * happens in the frontend only**. This module is that conversion, and it is the only place it
 * happens.
 *
 * The zone comes from `GET /api/v1/client-config`, not from a constant here. `TZ_DISPLAY` is
 * server configuration (§13.11 notes it is global rather than a column on `outlets`, for now),
 * and a second copy in a JS constant is a second source of truth waiting to disagree.
 *
 * ## business_date is NOT converted, and that is the important part
 *
 * §5.2 makes `business_date` an explicit DATE column, never derived from a timestamp, and
 * §6.1 exists because the night shift crosses midnight. Running a business date through a
 * timezone conversion is precisely the bug that column was created to prevent: "2026-08-23"
 * parsed as an instant becomes midnight UTC, which in Asia/Kolkata is 05:30 on the 23rd --
 * fine -- but in a zone west of UTC it is the 22nd, and the register would show a day that
 * never happened. `businessDate` below formats the string's own parts and never constructs a
 * Date at all.
 *
 * ## Two wire shapes for the same thing
 *
 * The API is not consistent about this and it is not worth a breaking change: `business_date`
 * is a real date on ShiftResponse but an ISO string on ShiftSales and FlaggedExpenseResponse,
 * and `reviewed_at` is a string on ExpenseResponse but a datetime on ReadingResponse. Both
 * arrive as JSON strings, so every function here takes a string and asks nothing about which
 * endpoint produced it.
 */

/* Set once at boot from /client-config. Module-level rather than passed through every call,
 * because it is genuinely global for the session and threading it through would put a `tz`
 * parameter on every render function in the app. */
let zone = "Asia/Kolkata";

/** Called by main.js after /client-config resolves. */
export function setZone(tzDisplay) {
  if (tzDisplay) zone = tzDisplay;
}

export function currentZone() {
  return zone;
}

const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/**
 * A business date, formatted from its own parts.
 *
 * **Never parsed into a Date.** See the module header: a DATE is a calendar day, not an
 * instant, and converting it can move it by one.
 *
 * @param {string|null} value  "2026-08-23"
 */
export function businessDate(value, { absent = "—" } = {}) {
  if (!value) return absent;
  const [year, month, day] = String(value).slice(0, 10).split("-");
  if (!year || !month || !day) return String(value);
  return `${Number(day)} ${MONTHS[Number(month) - 1] ?? month} ${year}`;
}

/**
 * A business date without its year, for a chart axis where space is the scarce thing.
 *
 * Returns the two parts separately rather than one string, because the label stacks them on
 * two lines and joining them here would only mean splitting them again in the caller.
 *
 * Parts, never a `Date` -- `businessDate` above and the module header both explain why, and
 * the reason does not weaken just because the year is being dropped.
 *
 * @param {string|null} value  "2026-08-23"
 * @returns {{day: string, month: string}}  {day: "23", month: "Aug"}
 */
export function businessDateShort(value) {
  if (!value) return { day: "", month: "" };
  const [, month, day] = String(value).slice(0, 10).split("-");
  if (!month || !day) return { day: String(value), month: "" };
  return { day: String(Number(day)), month: MONTHS[Number(month) - 1] ?? month };
}

/** The weekday for a business date, again without constructing an instant in another zone.
 *
 * Uses UTC accessors on a UTC-anchored date so the arithmetic cannot drift: the point is only
 * to name the weekday of that calendar day, which is zone-independent.
 */
export function businessDateWeekday(value) {
  if (!value) return "";
  const [year, month, day] = String(value).slice(0, 10).split("-").map(Number);
  if (!year || !month || !day) return "";
  const anchored = new Date(Date.UTC(year, month - 1, day));
  return ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][anchored.getUTCDay()];
}

function formatter(options) {
  return new Intl.DateTimeFormat("en-IN", { timeZone: zone, ...options });
}

/**
 * An instant, in the outlet's local time.
 *
 * @param {string|null} value  ISO 8601 with an offset, as every timestamp field returns
 */
export function dateTime(value, { absent = "—" } = {}) {
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

/** Just the clock time, for a shift's start and end on a screen that already names the day. */
export function timeOnly(value, { absent = "—" } = {}) {
  if (!value) return absent;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return formatter({ hour: "2-digit", minute: "2-digit", hour12: true }).format(parsed);
}

/** A local-time HH:MM, for a shift template's `starts_at_local` / `ends_at_local`.
 *
 * §5.1 is explicit that these are TIME columns and not instants -- "06:00 local, every day"
 * is a recurring wall-clock time -- so this formats the string and does not convert anything.
 */
export function localTime(value, { absent = "—" } = {}) {
  if (!value) return absent;
  const [hours, minutes] = String(value).split(":");
  const hour = Number(hours);
  if (Number.isNaN(hour)) return String(value);
  const suffix = hour < 12 ? "am" : "pm";
  const twelve = hour % 12 === 0 ? 12 : hour % 12;
  return `${twelve}:${minutes ?? "00"} ${suffix}`;
}

/** "today" in the outlet's zone, as a YYYY-MM-DD business date.
 *
 * §6.1 rejects a future `business_date` and evaluates "future" in the outlet's local zone,
 * not UTC. A form defaulting to the browser's own date would offer tomorrow to anybody east
 * of the outlet -- so this asks the formatter for the date *in the outlet's zone*.
 */
export function todayAtOutlet() {
  const parts = formatter({ year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(
    new Date(),
  );
  const get = (type) => parts.find((part) => part.type === type)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")}`;
}

/** Relative phrasing for recent activity, falling back to an absolute date.
 *
 * Deliberately falls back quickly (past a day). §4.7 says the register is typed up after the
 * fact, so "3 days ago" is a worse answer than the date itself for anything being reconciled.
 */
export function relative(value, { absent = "—" } = {}) {
  if (!value) return absent;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);

  const seconds = (Date.now() - parsed.getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return dateTime(value);
}
