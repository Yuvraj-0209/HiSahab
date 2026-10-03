import { describe, expect, it } from "vitest";
import { lastMonth, lastThreeMonths, thisMonth, thisYear } from "./calendar";

describe("summary range presets", () => {
  it("this month runs from the 1st to today", () => {
    expect(thisMonth("2026-10-03")).toEqual({ from: "2026-10-01", to: "2026-10-03" });
  });

  it("last month ends on last month's own last day, never this month's (the Phase 19 bug)", () => {
    // The old sheet took the month from TODAY and the day from last month: 30 Oct, a future date.
    expect(lastMonth("2026-10-03")).toEqual({ from: "2026-09-01", to: "2026-09-30" });
    expect(lastMonth("2026-03-31")).toEqual({ from: "2026-02-01", to: "2026-02-28" });
    expect(lastMonth("2028-03-01")).toEqual({ from: "2028-02-01", to: "2028-02-29" });
    expect(lastMonth("2027-01-15")).toEqual({ from: "2026-12-01", to: "2026-12-31" });
  });

  it("the last three months start on the 1st two months back, across a year end", () => {
    expect(lastThreeMonths("2026-10-03")).toEqual({ from: "2026-08-01", to: "2026-10-03" });
    expect(lastThreeMonths("2027-01-20")).toEqual({ from: "2026-11-01", to: "2027-01-20" });
  });

  it("this year starts on 1 January", () => {
    expect(thisYear("2026-10-03")).toEqual({ from: "2026-01-01", to: "2026-10-03" });
  });
});
