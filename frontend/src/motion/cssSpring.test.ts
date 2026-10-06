/* The CSS copy of the gesture spring must be the spring (Phase 28 D8). */

import { describe, expect, it } from "vitest";
// The stylesheet as text, through Vite's `?raw`: no Node API, so the typecheck needs no Node types.
import styles from "../styles.css?raw";
import { cssSpring } from "./cssSpring";
import { PRESETS } from "./spring";

function token(name: string): string | undefined {
  return styles.match(new RegExp(`--${name}:\\s*([^;]+);`))?.[1]?.trim();
}

describe("cssSpring", () => {
  it("starts at rest, ends on its target, and never overshoots a critically damped preset", () => {
    for (const preset of [PRESETS.ui, PRESETS.snap]) {
      const points = cssSpring(preset).easing.slice("linear(".length, -1).split(", ").map(Number);
      expect(points[0]).toBe(0);
      expect(points.at(-1)).toBe(1);
      for (let index = 1; index < points.length; index += 1) expect(points[index]).toBeGreaterThanOrEqual(points[index - 1]!);
      expect(Math.max(...points)).toBeLessThanOrEqual(1);
    }
  });

  it("settles faster for a snappier preset", () => {
    expect(parseInt(cssSpring(PRESETS.snap).duration)).toBeLessThan(parseInt(cssSpring(PRESETS.ui).duration));
  });

  // If this fails after a change to PRESETS, paste the `expected` values into styles.css: the CSS
  // copy is generated from the physics, never tuned by hand.
  it.each([
    ["spring-ui", PRESETS.ui],
    ["spring-snap", PRESETS.snap],
  ] as const)("styles.css carries --%s exactly as the spring samples it", (name, preset) => {
    const expected = cssSpring(preset);
    expect(token(name)).toBe(expected.easing);
    expect(token(`${name}-duration`)).toBe(expected.duration);
  });
});
