import { describe, expect, it } from "vitest";
import { lastCompletedHalf } from "./billing";

describe("lastCompletedHalf", () => {
  it("from the 16th onwards, the bill is the 1st to the 15th", () => {
    expect(lastCompletedHalf("2026-10-16")).toEqual({ from: "2026-10-01", to: "2026-10-15" });
    expect(lastCompletedHalf("2026-10-31")).toEqual({ from: "2026-10-01", to: "2026-10-15" });
  });

  it("before the 16th, the bill is the second half of last month, to its real last day", () => {
    expect(lastCompletedHalf("2026-10-03")).toEqual({ from: "2026-09-16", to: "2026-09-30" });
    expect(lastCompletedHalf("2026-03-01")).toEqual({ from: "2026-02-16", to: "2026-02-28" });
    expect(lastCompletedHalf("2028-03-15")).toEqual({ from: "2028-02-16", to: "2028-02-29" });
  });

  it("January reaches back into December of the year before", () => {
    expect(lastCompletedHalf("2027-01-10")).toEqual({ from: "2026-12-16", to: "2026-12-31" });
  });
});
