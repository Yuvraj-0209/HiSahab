import { describe, expect, it } from "vitest";
import { businessDateRange } from "./time";

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
