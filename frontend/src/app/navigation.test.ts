/* Which way a navigation goes (Phase 24 D3). The direction is what the transition communicates,
 * so a wrong one is a screen arriving from the side it did not come from. */

import { describe, expect, it } from "vitest";
import { directionOf } from "./navigation";

describe("directionOf", () => {
  it("a tab to the right slides right, to the left slides left", () => {
    expect(directionOf({ path: "/today", tab: "today" }, "/cash")).toBe("right");
    expect(directionOf({ path: "/summary", tab: "summary" }, "/credit")).toBe("left");
  });

  it("leaving a tab's root for anything inside it is forward, even at the same depth", () => {
    // /cash and /days are both one segment deep; /days is opened from /cash.
    expect(directionOf({ path: "/cash", tab: "cash" }, "/days")).toBe("forward");
    expect(directionOf({ path: "/credit", tab: "credit" }, "/credit/customers/c1")).toBe("forward");
  });

  it("deeper is forward, shallower is back", () => {
    expect(directionOf({ path: "/days", tab: "cash" }, "/days/2026-10-01")).toBe("forward");
    expect(directionOf({ path: "/days/2026-10-01", tab: "cash" }, "/days")).toBe("back");
  });

  it("returning to your own tab's root is back; another tab's root is sideways", () => {
    expect(directionOf({ path: "/days/2026-10-01", tab: "cash" }, "/cash")).toBe("back");
    expect(directionOf({ path: "/days/2026-10-01", tab: "cash" }, "/summary")).toBe("right");
    expect(directionOf({ path: "/days/2026-10-01", tab: "cash" }, "/today")).toBe("left");
  });

  it("the same depth elsewhere is a crossfade, with no direction to claim", () => {
    expect(directionOf({ path: "/days/2026-10-01", tab: "cash" }, "/days/2026-10-02")).toBe("fade");
  });

  it("a query change on the same screen is not a move at all", () => {
    expect(directionOf({ path: "/credit/statement", tab: "credit" }, "/credit/statement?from=2026-09-01&to=2026-09-15")).toBe("none");
  });

  it("from a screen with no tab, a tab root crossfades", () => {
    expect(directionOf({ path: "/nowhere", tab: undefined }, "/today")).toBe("fade");
  });
});
