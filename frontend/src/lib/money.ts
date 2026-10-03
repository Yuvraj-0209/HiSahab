/* Money formatting. The only module permitted to touch a money value.
 *
 * **The rule, and it is stronger than "use a library": this application performs no
 * arithmetic on money, ever** (CLAUDE.md §3 rule 1, §14).
 *
 * JavaScript has no decimal type. `0.1 + 0.2 !== 0.3` here exactly as it does in Python, and
 * §3 rule 1 exists because in a cash system that compounds into unexplainable variance. The
 * API already computes every figure this app displays -- `cash-position` returns the `gap`,
 * `credit-customers/outstanding` returns the balance, `collections` returns
 * `totals_by_mode` -- so there is genuinely nothing left for a client to add up. Anything that
 * looks like it needs a subtotal here is a signal that the wrong endpoint is being called.
 *
 * So money arrives as a **string**, stays a string, and is formatted for display without ever
 * becoming a Number. The generated API types (src/api/schema.d.ts) make every money field a
 * `string`, so the compiler now holds the line a convention used to; `tests/` scans for
 * `parseFloat` as well.
 *
 * ## The null rule, which is the dangerous one
 *
 * `?? 0` and `|| 0` are the two most dangerous characters this codebase can write in a client.
 * Several fields are *legitimately* null and mean something specific:
 *
 *   declared_cash  null = nobody has declared;  "0.00" = they counted zero (§6.8)
 *   gap            null = there is nothing to compare against, NOT "no discrepancy"
 *   variance       null = the day was never counted
 *   actual_counted null = not counted
 *   credit_limit   null = NO LIMIT -- coercing it to 0 refuses every sale to the customers who
 *                  are trusted most (§6.6)
 *
 * Every function here therefore returns a *word* for null rather than a number, and callers
 * pass what the absence means in that context.
 *
 * Ported from the Phase 12 `money.js` with its behaviour unchanged (Phase 23). One cosmetic
 * difference: the default placeholder for an unexplained null is a hyphen rather than an
 * em-dash, because the design rules ban the em-dash from anything a person reads.
 */

export type Money = string;

/** What a null renders as when the caller has not said what it means. Deliberately vague, so
 * a caller that has not thought about it produces something obviously unfinished rather than a
 * plausible "₹0.00". */
export const UNEXPLAINED = "-";

/** Indian digit grouping: 1,23,456.78 -- last three, then pairs.
 *
 * Not `Intl.NumberFormat`, which would require converting the string to a Number and is
 * exactly what this module exists to avoid. The grouping is done on the digit characters, so
 * a value with more precision than a float can hold still renders correctly.
 */
function groupIndian(digits: string): string {
  if (digits.length <= 3) return digits;
  const last3 = digits.slice(-3);
  const rest = digits.slice(0, -3);
  // Pairs, right to left, for everything above the last three.
  const grouped = rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",");
  return `${grouped},${last3}`;
}

export interface FormatOptions {
  /** What null means HERE, in words. Required in practice; see UNEXPLAINED. */
  absent?: string;
  /** Force a leading + on positive values, for a variance. */
  sign?: boolean;
}

/** Format a money string for display, e.g. "-160000.5" -> "−₹1,60,000.50". */
export function format(
  value: Money | null | undefined,
  { absent = UNEXPLAINED, sign = false }: FormatOptions = {},
): string {
  if (value === null || value === undefined) return absent;

  let text = String(value).trim();
  if (text === "") return absent;

  let negative = false;
  if (text.startsWith("-")) {
    negative = true;
    text = text.slice(1);
  } else if (text.startsWith("+")) {
    text = text.slice(1);
  }

  const [whole = "0", fraction = "00"] = text.split(".");
  const grouped = groupIndian(whole);
  const prefix = negative ? "−" : sign ? "+" : ""; // U+2212, not a hyphen: it aligns
  return `${prefix}₹${grouped}.${fraction.padEnd(2, "0").slice(0, 2)}`;
}

/** True when a money string is negative, without parsing it. */
export function isNegative(value: Money | null | undefined): boolean {
  return typeof value === "string" && value.trim().startsWith("-");
}

/** True when a money string is zero, without parsing it.
 *
 * "0", "0.00" and "-0.00" are all zero. A different question from `value === null`, and the
 * two must never be conflated -- see the null rule above.
 */
export function isZero(value: Money | null | undefined): boolean {
  if (value === null || value === undefined) return false;
  return /^-?0*(\.0*)?$/.test(String(value).trim());
}

/** A money figure with its meaning attached. `className` is one of `text-short`,
 * `text-surplus`, `t-absent` or "" -- utilities defined in styles.css. */
export interface Labelled {
  text: string;
  className: "" | "text-short" | "text-surplus" | "t-absent";
}

/**
 * Render a signed figure with its meaning attached, for a **gap**.
 *
 * §6.4: a positive gap means **short** and a negative one means a **surplus**. That is not
 * self-evident from a minus sign, and getting it backwards puts a debt on the wrong side of
 * somebody's name -- so the word is rendered next to the number rather than left to be
 * inferred from a colour.
 */
export function gapLabel(
  value: Money | null | undefined,
  { absent = "not declared" }: { absent?: string } = {},
): Labelled {
  if (value === null || value === undefined) return { text: absent, className: "t-absent" };
  if (isZero(value)) return { text: `${format(value)} · balanced`, className: "" };
  if (isNegative(value)) return { text: `${format(value)} · surplus`, className: "text-surplus" };
  return { text: `${format(value)} · short`, className: "text-short" };
}

/**
 * Render a day's **variance** with its meaning attached. **Not `gapLabel`** -- the sign runs
 * the other way.
 *
 *   gap      = accountable − declared   what he should hold minus what he handed over
 *   variance = counted − expected       what is in the locker minus what should be (§6.4)
 *
 * So a positive gap is short and a positive variance is a **surplus**. Every variance in the
 * app was once labelled with `gapLabel`, and 15 Sept 2026 -- counted ₹455.00 against an
 * expected −₹1,69,190.75 -- read "₹1,69,645.75 · short" for a locker holding exactly that much
 * more than the books said. `money.test.ts` pins both conventions side by side.
 */
export function varianceLabel(
  value: Money | null | undefined,
  { absent = "not counted" }: { absent?: string } = {},
): Labelled {
  if (value === null || value === undefined) return { text: absent, className: "t-absent" };
  if (isZero(value)) return { text: `${format(value)} · balanced`, className: "" };
  if (varianceIsShort(value)) return { text: `${format(value)} · short`, className: "text-short" };
  return { text: `${format(value)} · surplus`, className: "text-surplus" };
}

/** True when a variance means the locker is short: counted below expected, so negative. The
 * one place the variance sign convention lives, so a chart's direction and a label's word
 * cannot disagree. */
export function varianceIsShort(value: Money | null | undefined): boolean {
  return isNegative(value) && !isZero(value);
}

/**
 * Format a quantity with its unit.
 *
 * §4.5: a quantity is a *measure*, not necessarily a volume. CBG is sold by the kilogram
 * through the same endpoints as petrol, so the unit is read from the fuel type -- which every
 * reading, sale and credit response echoes for exactly this reason -- and never assumed.
 */
export function quantity(
  value: string | null | undefined,
  unitOfMeasure: string | null | undefined,
  { absent = UNEXPLAINED }: { absent?: string } = {},
): string {
  if (value === null || value === undefined) return absent;
  const unit = unitOfMeasure === "kilogram" ? "kg" : "L";
  return `${String(value)} ${unit}`;
}

/** A totalizer reading. No unit: a meter shows a number, and it is not a quantity sold. */
export function reading(
  value: string | null | undefined,
  { absent = UNEXPLAINED }: { absent?: string } = {},
): string {
  if (value === null || value === undefined) return absent;
  return String(value);
}

/**
 * Compare two non-negative decimal money strings without turning either into a number.
 *
 * The one ordering the client genuinely needs: whether a typed amount is over the receipt
 * threshold, so a person is warned *before* the server refuses with 422. Integer-part length
 * first (leading zeros stripped), then the digits lexically, then the fraction padded -- exact
 * for this shape of input, and only ever used to show a warning, never to decide anything the
 * server does not decide again for itself. Returns -1, 0 or 1. Ported from expenses.js and
 * credit.js, which each had a copy.
 */
export function compareMoney(a: Money, b: Money): number {
  const [aWhole = "0", aFrac = ""] = String(a).trim().split(".");
  const [bWhole = "0", bFrac = ""] = String(b).trim().split(".");
  const aw = aWhole.replace(/^0+(?=\d)/, "");
  const bw = bWhole.replace(/^0+(?=\d)/, "");
  if (aw.length !== bw.length) return aw.length > bw.length ? 1 : -1;
  if (aw !== bw) return aw > bw ? 1 : -1;
  const width = Math.max(aFrac.length, bFrac.length);
  const af = aFrac.padEnd(width, "0");
  const bf = bFrac.padEnd(width, "0");
  return af === bf ? 0 : af > bf ? 1 : -1;
}
