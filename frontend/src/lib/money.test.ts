/* The sign words, ported from tests/money_assertions.mjs (Phase 23).
 *
 * A gap and a variance are both signed money figures with OPPOSITE conventions (§6.4):
 *
 *   gap      = accountable_cash − declared_cash    positive = SHORT,   negative = surplus
 *   variance = actual_counted − expected_closing   positive = SURPLUS, negative = short
 *
 * Until these assertions existed every variance was labelled with gapLabel, so a locker holding
 * ₹1,69,645.75 more than expected read "short". Nothing threw and every figure was correct; only
 * the word was backwards.
 */

import { describe, expect, it } from "vitest";
import { compareMoney, compareSignedMoney, format, gapLabel, isZero, quantity, varianceIsShort, varianceLabel } from "./money";

describe("variance: counted − expected", () => {
  it("a positive variance is a surplus", () => {
    // The real case that found the bug: expected −1,69,190.75, counted 455.00.
    const label = varianceLabel("169645.75");
    expect(label.text).toMatch(/surplus/);
    expect(label.text).not.toMatch(/short/);
    expect(label.className).toBe("text-surplus");
  });

  it("a negative variance is short", () => {
    const label = varianceLabel("-200.00");
    expect(label.text).toMatch(/short/);
    expect(label.className).toBe("text-short");
  });

  it("zero is balanced, including negative zero", () => {
    expect(varianceLabel("0.00").text).toMatch(/balanced/);
    expect(varianceLabel("-0.00").text).toMatch(/balanced/);
  });

  it("null means not counted, never ₹0", () => {
    expect(varianceLabel(null)).toEqual({ text: "not counted", className: "t-absent" });
  });

  it("varianceIsShort follows the variance convention", () => {
    expect(varianceIsShort("-200.00")).toBe(true);
    expect(varianceIsShort("169645.75")).toBe(false);
  });
});

describe("gap: accountable − declared", () => {
  it("a positive gap is short", () => {
    expect(gapLabel("500.00")).toMatchObject({ className: "text-short" });
    expect(gapLabel("500.00").text).toMatch(/short/);
  });

  it("a negative gap is a surplus", () => {
    expect(gapLabel("-500.00")).toMatchObject({ className: "text-surplus" });
  });

  it("the same figure reads oppositely as a gap and as a variance", () => {
    expect(gapLabel("300.00").className).not.toBe(varianceLabel("300.00").className);
  });

  it("null means nobody declared", () => {
    expect(gapLabel(null)).toEqual({ text: "not declared", className: "t-absent" });
  });
});

describe("format never converts money to a number", () => {
  it("groups Indian style and keeps two decimals", () => {
    expect(format("12345678.5")).toBe("₹1,23,45,678.50");
    expect(format("999")).toBe("₹999.00");
  });

  it("renders a negative with a true minus sign", () => {
    expect(format("-1500.00")).toBe("−₹1,500.00");
  });

  it("keeps precision a float would lose", () => {
    // 2^53 + 1 cannot be represented as a double; the digits must survive untouched.
    expect(format("9007199254740993.00")).toBe("₹9,00,71,99,25,47,40,993.00");
  });

  it("says what null means instead of printing ₹0.00", () => {
    expect(format(null, { absent: "no limit" })).toBe("no limit");
    expect(format(null)).not.toMatch(/₹/);
  });

  it("zero is an answer, not an omission", () => {
    expect(format("0.00")).toBe("₹0.00");
    expect(isZero("0.00")).toBe(true);
    expect(isZero(null)).toBe(false);
  });
});

describe("quantity reads the unit, never assumes litres (§4.5)", () => {
  it("CBG is kilograms", () => {
    expect(quantity("12.500", "kilogram")).toBe("12.500 kg");
    expect(quantity("40.000", "litre")).toBe("40.000 L");
  });
});

describe("compareMoney orders strings without parsing them", () => {
  it("compares by magnitude, not by text", () => {
    expect(compareMoney("10000.00", "5000.00")).toBe(1);
    expect(compareMoney("999.99", "1000.00")).toBe(-1);
  });
  it("is exact at the boundary §6.11 cares about", () => {
    expect(compareMoney("5000.00", "5000.00")).toBe(0);
    expect(compareMoney("5000", "5000.00")).toBe(0);
    expect(compareMoney("5000.01", "5000.00")).toBe(1);
  });
  it("ignores leading zeros", () => {
    expect(compareMoney("0500.00", "500")).toBe(0);
  });
});

describe("compareSignedMoney", () => {
  it("orders a negative balance below zero and below any debt", () => {
    const sorted = ["1200.00", "-50.00", "0.00", "12400.00", "-900.00"].sort(compareSignedMoney);
    expect(sorted).toEqual(["-900.00", "-50.00", "0.00", "1200.00", "12400.00"]);
  });
  it("treats negative zero as zero", () => {
    expect(compareSignedMoney("-0.00", "0.00")).toBe(0);
  });
});
