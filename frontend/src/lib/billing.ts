/* This outlet's billing calendar (Phase 21): bills go out on the 16th, for the 1st to the 15th,
 * and on the 1st, for the 16th to month end.
 *
 * Calendar arithmetic on the date's integer parts -- never a Date parsed from a business date,
 * which can move it a day west of UTC (§6.1). The one Date below is UTC-anchored and asks only
 * "how many days in this month", which is zone-independent.
 */

const pad = (n: number) => String(n).padStart(2, "0");

/** The most recently completed billing half-month, as of `today` (YYYY-MM-DD at the outlet). */
export function lastCompletedHalf(today: string): { from: string; to: string } {
  const [year = 0, month = 1, day = 1] = today.split("-").map(Number);
  if (day >= 16) return { from: `${year}-${pad(month)}-01`, to: `${year}-${pad(month)}-15` };
  const prevYear = month === 1 ? year - 1 : year;
  const prevMonth = month === 1 ? 12 : month - 1;
  // Day 0 of this month is the last day of the previous one.
  const lastDay = new Date(Date.UTC(year, month - 1, 0)).getUTCDate();
  return { from: `${prevYear}-${pad(prevMonth)}-16`, to: `${prevYear}-${pad(prevMonth)}-${pad(lastDay)}` };
}
