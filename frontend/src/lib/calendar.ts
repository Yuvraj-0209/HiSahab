/* The Summary tab's date-range presets: the four windows somebody actually asks for.
 *
 * Calendar arithmetic on the date's integer parts -- never a Date parsed from a business date,
 * which can move it a day west of UTC (§6.1). The one Date below is UTC-anchored and asks only
 * "how many days in this month", which is zone-independent. Months are integers, not money, so
 * nothing here is the arithmetic §14 forbids.
 */

export interface Range {
  from: string;
  to: string;
}

const pad = (n: number, width = 2) => String(n).padStart(width, "0");

function parts(iso: string): [number, number, number] {
  const [year = 0, month = 1, day = 1] = iso.split("-").map(Number);
  return [year, month, day];
}

/** The 1st of the month `offset` months from the one `iso` falls in. */
function monthStart(iso: string, offset = 0): string {
  const [year, month] = parts(iso);
  const index = year * 12 + (month - 1) + offset;
  return `${pad(Math.floor(index / 12), 4)}-${pad((index % 12) + 1)}-01`;
}

function lastDayOf(firstOfMonth: string): string {
  const [year, month] = parts(firstOfMonth);
  // Day 0 of the next month is the last day of this one.
  const last = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return `${firstOfMonth.slice(0, 8)}${pad(last)}`;
}

export const thisMonth = (today: string): Range => ({ from: monthStart(today), to: today });

export function lastMonth(today: string): Range {
  const first = monthStart(today, -1);
  return { from: first, to: lastDayOf(first) };
}

export const lastThreeMonths = (today: string): Range => ({ from: monthStart(today, -2), to: today });

export const thisYear = (today: string): Range => ({ from: `${today.slice(0, 4)}-01-01`, to: today });
