/* Assertions over the sign words in money.js, run by tests/test_money_labels.py.
 *
 * The same exception tests/motion_assertions.mjs takes to §13.18: money.js is pure -- no DOM,
 * no fetch -- so node's own assert module can check it with nothing installed.
 *
 * Why these functions: a gap and a variance are both signed money figures, and they carry
 * OPPOSITE sign conventions (§6.4):
 *
 *   gap      = accountable_cash − declared_cash    positive = SHORT,   negative = surplus
 *   variance = actual_counted − expected_closing   positive = SURPLUS, negative = short
 *
 * Until this file existed every variance in the app was labelled with gapLabel, so a locker
 * holding ₹1,69,645.75 more than expected read "short" -- and a real shortage would have read
 * "surplus". Nothing threw and every figure was correct; only the word was backwards. That is
 * the plausible-but-wrong shape CLAUDE.md opens by warning about, and a person reviewing by
 * eye did not catch it for several phases.
 *
 * Run directly:  node tests/money_assertions.mjs
 */

import assert from "node:assert/strict";

const { gapLabel, varianceLabel, varianceIsShort } = await import(
  "../app/static/js/money.js"
);

let count = 0;
function check(name, fn) {
  fn();
  count += 1;
  console.log(`ok - ${name}`);
}

// --- variance: counted − expected -----------------------------------------------------------

check("a positive variance is a surplus (the locker holds more than expected)", () => {
  // The real case that found this bug: expected −1,69,190.75, counted 455.00.
  const label = varianceLabel("169645.75");
  assert.match(label.text, /surplus/);
  assert.doesNotMatch(label.text, /short/);
  assert.equal(label.className, "text-surplus");
});

check("a negative variance is short (the locker holds less than expected)", () => {
  // §6.5's worked example: a ₹200 shortage on Monday.
  const label = varianceLabel("-200.00");
  assert.match(label.text, /short/);
  assert.doesNotMatch(label.text, /surplus/);
  assert.equal(label.className, "text-short");
});

check("a zero variance is balanced, including negative zero", () => {
  assert.match(varianceLabel("0.00").text, /balanced/);
  assert.match(varianceLabel("-0.00").text, /balanced/);
});

check("a null variance means not counted, never ₹0", () => {
  const label = varianceLabel(null);
  assert.equal(label.text, "not counted");
  assert.equal(label.className, "t-absent");
});

check("varianceIsShort follows the variance convention, for the chart's direction", () => {
  assert.equal(varianceIsShort("-200.00"), true);
  assert.equal(varianceIsShort("169645.75"), false);
});

// --- gap: accountable − declared. Unchanged, and pinned so a "fix" cannot flip it ----------

check("a positive gap is still short", () => {
  assert.match(gapLabel("500.00").text, /short/);
  assert.equal(gapLabel("500.00").className, "text-short");
});

check("a negative gap is still a surplus", () => {
  assert.match(gapLabel("-500.00").text, /surplus/);
  assert.equal(gapLabel("-500.00").className, "text-surplus");
});

check("the same figure reads oppositely as a gap and as a variance", () => {
  // The whole bug in one line: one helper cannot serve both conventions.
  assert.notEqual(gapLabel("300.00").className, varianceLabel("300.00").className);
});

console.log(`${count} assertions passed`);
