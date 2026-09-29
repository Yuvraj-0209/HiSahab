/* Money formatting. The only module permitted to touch a money value.
 *
 * **The rule, and it is stronger than "use a library": this application performs no
 * arithmetic on money, ever** (CLAUDE.md §3 rule 1, §14).
 *
 * JavaScript has no decimal type. `0.1 + 0.2 !== 0.3` here exactly as it does in Python, and
 * §3 rule 1 exists because in a cash system that compounds into unexplainable variance. The
 * API already computes every figure this app displays -- `cash-position` returns the `gap`,
 * `credit-customers/outstanding` returns the balance, `collections` returns
 * `totals_by_mode` -- so there is genuinely nothing left for a client to add up. Anything
 * that looks like it needs a subtotal here is a signal that the wrong endpoint is being
 * called.
 *
 * So money arrives as a **string**, stays a string, and is formatted for display without ever
 * becoming a Number. `format` below inserts separators by manipulating the digits directly.
 * `tests/test_frontend_assets.py` asserts `parseFloat` appears nowhere.
 *
 * ## The null rule, which is the dangerous one
 *
 * `?? 0` and `|| 0` are the two most dangerous characters this codebase can write in a
 * client. Several fields are *legitimately* null and mean something specific:
 *
 *   declared_cash  null = nobody has declared;  "0.00" = they counted zero (§6.8)
 *   gap            null = there is nothing to compare against, NOT "no discrepancy"
 *   variance       null = the day was never counted
 *   actual_counted null = not counted
 *   credit_limit   null = NO LIMIT -- coercing it to 0 refuses every sale to the customers
 *                  who are trusted most (§6.6)
 *
 * Every function here therefore returns a *word* for null rather than a number, and callers
 * pass what the absence means in that context.
 */

/** Indian digit grouping: 1,23,456.78 -- last three, then pairs.
 *
 * Not `Intl.NumberFormat`, which would require converting the string to a Number and is
 * exactly what this module exists to avoid. The grouping is done on the digit characters,
 * so a value with more precision than a float can hold still renders correctly.
 */
function groupIndian(digits) {
  if (digits.length <= 3) return digits;
  const last3 = digits.slice(-3);
  const rest = digits.slice(0, -3);
  // Pairs, right to left, for everything above the last three.
  const grouped = rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",");
  return `${grouped},${last3}`;
}

/**
 * Format a money string for display.
 *
 * @param {string|null} value   as it came off the wire, e.g. "60000.00"
 * @param {object} [options]
 * @param {string} [options.absent]  what null means HERE, in words. Required in practice --
 *                                   the default is deliberately vague so a caller that has
 *                                   not thought about it produces something obviously
 *                                   unfinished rather than a plausible "₹0.00".
 * @param {boolean} [options.sign]   force a leading + on positive values, for a variance
 */
export function format(value, { absent = "—", sign = false } = {}) {
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

  const [whole, fraction = "00"] = text.split(".");
  const grouped = groupIndian(whole);
  const prefix = negative ? "−" : sign ? "+" : ""; // U+2212, not a hyphen: it aligns
  return `${prefix}₹${grouped}.${fraction.padEnd(2, "0").slice(0, 2)}`;
}

/** True when a money string is negative, without parsing it.
 *
 * Used to colour a variance or a gap. String inspection rather than `< 0`, for the reason
 * this whole module exists.
 */
export function isNegative(value) {
  return typeof value === "string" && value.trim().startsWith("-");
}

/** True when a money string is zero, without parsing it.
 *
 * "0", "0.00" and "-0.00" are all zero. Note this is a different question from `value ===
 * null`, and the two must never be conflated -- see the null rule above.
 */
export function isZero(value) {
  if (value === null || value === undefined) return false;
  return /^-?0*(\.0*)?$/.test(String(value).trim());
}

/**
 * Render a signed figure with its meaning attached, for a gap or a variance.
 *
 * §6.4: a positive gap means **short** and a negative one means a **surplus**. That is not
 * self-evident from a minus sign, and getting it backwards puts a debt on the wrong side of
 * somebody's name -- so the word is rendered next to the number rather than left to be
 * inferred from a colour.
 *
 * @returns {{text: string, className: string}}
 */
export function gapLabel(value, { absent = "not declared" } = {}) {
  if (value === null || value === undefined) {
    return { text: absent, className: "t-absent" };
  }
  if (isZero(value)) {
    return { text: `${format(value)} · balanced`, className: "" };
  }
  if (isNegative(value)) {
    return { text: `${format(value)} · surplus`, className: "text-surplus" };
  }
  return { text: `${format(value)} · short`, className: "text-short" };
}

/**
 * Render a day's variance with its meaning attached. **Not `gapLabel`** -- the sign runs the
 * other way.
 *
 *   gap      = accountable − declared   what he should hold minus what he handed over
 *   variance = counted − expected       what is in the locker minus what should be (§6.4)
 *
 * So a positive gap is short and a positive variance is a **surplus**. Every variance in the
 * app was once labelled with `gapLabel`, and 15 Sept 2026 -- counted ₹455.00 against an
 * expected −₹1,69,190.75 -- read "₹1,69,645.75 · short" for a locker holding exactly that
 * much more than the books said. A real shortage would have read "surplus".
 * `tests/money_assertions.mjs` pins both conventions side by side.
 *
 * @returns {{text: string, className: string}}
 */
export function varianceLabel(value, { absent = "not counted" } = {}) {
  if (value === null || value === undefined) {
    return { text: absent, className: "t-absent" };
  }
  if (isZero(value)) {
    return { text: `${format(value)} · balanced`, className: "" };
  }
  if (varianceIsShort(value)) {
    return { text: `${format(value)} · short`, className: "text-short" };
  }
  return { text: `${format(value)} · surplus`, className: "text-surplus" };
}

/** True when a variance means the locker is short: counted below expected, so negative.
 *
 * The one place the variance sign convention lives, so the chart's direction and the label's
 * word cannot disagree.
 */
export function varianceIsShort(value) {
  return isNegative(value) && !isZero(value);
}

/**
 * Format a quantity with its unit.
 *
 * §4.5: a quantity is a *measure*, not necessarily a volume. CBG is sold by the kilogram
 * through the same endpoints as petrol, so the unit is read from the fuel type -- which
 * every reading, sale and credit response echoes for exactly this reason -- and never
 * assumed to be litres.
 */
export function quantity(value, unitOfMeasure, { absent = "—" } = {}) {
  if (value === null || value === undefined) return absent;
  const unit = unitOfMeasure === "kilogram" ? "kg" : "L";
  return `${String(value)} ${unit}`;
}

/** A totalizer reading. No unit: a meter shows a number, and it is not a quantity sold. */
export function reading(value, { absent = "—" } = {}) {
  if (value === null || value === undefined) return absent;
  return String(value);
}
