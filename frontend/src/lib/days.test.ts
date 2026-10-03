/* §6.5's ordering rule and the day lifecycle, as behaviour (Phase 23). */

import { describe, expect, it } from "vitest";
import { dayState, mergeDays, oldestUnreconciled } from "./days";

const shift = (business_date: string, status: string, id = `s-${business_date}`) => ({ id, business_date, status });
const summary = (business_date: string, extra: Partial<{ is_finalised: boolean; requires_review: boolean }> = {}) => ({
  business_date,
  is_finalised: false,
  requires_review: false,
  ...extra,
});

describe("mergeDays", () => {
  it("shows a day that traded but was never reconciled -- the Phase 15 bug", () => {
    const days = mergeDays([shift("2026-07-02", "locked")], []);
    expect(days).toHaveLength(1);
    expect(days[0]?.summary).toBeNull();
  });

  it("orders newest first by business date, never by entry order", () => {
    const days = mergeDays([shift("2026-07-01", "locked"), shift("2026-07-03", "locked"), shift("2026-07-02", "locked")], []);
    expect(days.map((day) => day.business_date)).toEqual(["2026-07-03", "2026-07-02", "2026-07-01"]);
  });
});

describe("oldestUnreconciled", () => {
  it("is the earliest traded day with no summary", () => {
    const days = mergeDays(
      [shift("2026-07-01", "locked"), shift("2026-07-02", "locked"), shift("2026-07-03", "locked")],
      [summary("2026-07-01")],
    );
    expect(oldestUnreconciled(days)).toBe("2026-07-02");
  });

  it("a shut day (no shifts) is no obstacle", () => {
    const days = mergeDays([shift("2026-07-03", "locked")], [summary("2026-07-01")]);
    expect(oldestUnreconciled(days)).toBe("2026-07-03");
  });
});

describe("dayState", () => {
  it("offers reconcile only on the oldest unreconciled day (§6.5)", () => {
    const days = mergeDays([shift("2026-07-02", "locked"), shift("2026-07-03", "locked")], []);
    const unblocked = oldestUnreconciled(days);
    const [third, second] = days;
    expect(dayState(second!, { unblocked }).action?.reconcile).toBe(true);
    const blocked = dayState(third!, { unblocked });
    expect(blocked.action).toBeNull();
    expect(blocked.blockedBy).toBe("2026-07-02");
  });

  it("never offers lock to a manager, and names the navigation honestly for an admin", () => {
    const day = mergeDays([shift("2026-07-02", "closed")], [])[0]!;
    expect(dayState(day, { role: "manager" }).action).toBeNull();
    expect(dayState(day, { role: "admin" }).action).toEqual({ text: "Open the shift to lock it", path: "/shifts/s-2026-07-02" });
  });

  it("a counted day is not a lifecycle step: reconciled is step four whether counted or not", () => {
    const day = mergeDays([shift("2026-07-02", "locked")], [summary("2026-07-02")])[0]!;
    expect(dayState(day).reached).toBe(4);
  });

  it("a finalised day is finished and offers nothing", () => {
    const day = mergeDays([shift("2026-07-02", "locked")], [summary("2026-07-02", { is_finalised: true })])[0]!;
    expect(dayState(day, { role: "admin" })).toMatchObject({ reached: 5, action: null });
  });
});
