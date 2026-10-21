import { describe, expect, it } from "vitest";
import { addDays, businessDateRange, nextTradingDate } from "./time";

describe("businessDateRange", () => {
  it("says the month and year once when they are shared", () => {
    expect(businessDateRange("2026-09-16", "2026-09-30")).toBe("16 to 30 Sep 2026");
    expect(businessDateRange("2026-09-22", "2026-10-02")).toBe("22 Sep to 2 Oct 2026");
  });

  it("spells both out across a year end, and collapses a single day", () => {
    expect(businessDateRange("2026-12-16", "2027-01-15")).toBe("16 Dec 2026 to 15 Jan 2027");
    expect(businessDateRange("2026-10-02", "2026-10-02")).toBe("2 Oct 2026");
  });
});

describe("nextTradingDate", () => {
  it("is the day after the last shift entered", () => {
    expect(nextTradingDate("2026-10-10", "2026-10-12")).toBe("2026-10-11");
    expect(nextTradingDate("2026-09-30", "2026-10-12")).toBe("2026-10-01");
    expect(nextTradingDate("2026-12-31", "2027-01-05")).toBe("2027-01-01");
  });

  it("never offers a future day: caught up, it is today", () => {
    expect(nextTradingDate("2026-10-11", "2026-10-12")).toBe("2026-10-12");
    expect(nextTradingDate("2026-10-12", "2026-10-12")).toBe("2026-10-12");
  });

  it("is today when nothing has ever been opened", () => {
    expect(nextTradingDate(null, "2026-10-12")).toBe("2026-10-12");
  });
});

describe("addDays", () => {
  it("steps forward and back across a month and a year end", () => {
    expect(addDays("2026-10-13", 1)).toBe("2026-10-14");
    expect(addDays("2026-10-31", 1)).toBe("2026-11-01");
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
    expect(addDays("2027-01-01", -1)).toBe("2026-12-31");
    expect(addDays("2026-03-01", -1)).toBe("2026-02-28");
  });

  it("knows a leap day", () => {
    expect(addDays("2028-02-28", 1)).toBe("2028-02-29");
    expect(addDays("2028-03-01", -1)).toBe("2028-02-29");
  });

  it("moves several days at once, and zero is the same day", () => {
    expect(addDays("2026-10-13", -5)).toBe("2026-10-08");
    expect(addDays("2026-10-13", 0)).toBe("2026-10-13");
  });
});
