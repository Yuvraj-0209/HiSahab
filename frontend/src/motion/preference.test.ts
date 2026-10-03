/* The reduced-motion gate for motion that is not a tween (Phase 24 D2). The token mirror between
 * gsap.ts and styles.css is checked from pytest (tests/test_frontend_assets.py), because Vitest
 * hands a test an empty string for any CSS import. */

import { describe, expect, it, vi } from "vitest";
import { motionAllowed } from "./preference";

describe("motionAllowed", () => {
  it("is false when the phone asks for reduced motion", () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: !query.includes("no-preference") }));
    expect(motionAllowed()).toBe(false);
    vi.unstubAllGlobals();
  });

  it("is true when the phone has no preference", () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("no-preference") }));
    expect(motionAllowed()).toBe(true);
    vi.unstubAllGlobals();
  });
});
