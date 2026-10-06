/* The gesture spring, sampled into a CSS easing (Phase 28 D8).
 *
 * The app moves in three engines (§14): View Transitions, GSAP, and the spring in ./spring.ts that
 * owns everything under a finger. CSS transitions -- the tab indicator, a button pressed down --
 * used a cubic-bezier that only approximated the spring, so a sheet dragged by a finger and the
 * tab pill beside it moved with two different physics.
 *
 * CSS can now carry a spring: `linear()` takes a list of points and draws straight lines between
 * them, so a curve sampled finely enough is the curve. This samples the *same* `Spring` class the
 * gestures use, stepped exactly as the frame loop steps it, from 0 to 1 until it settles. The
 * Motion skill's CSS springs are built the same way; this one is built from our presets instead
 * of a guess at them, which is the point.
 *
 * styles.css carries the result as `--spring-ui` / `--spring-snap` and their durations. A Vitest
 * test re-samples and fails if the CSS copy drifts from the physics, as pytest already holds the
 * GSAP and CSS ease tokens equal.
 */

import { Spring, type SpringPreset } from "./spring";

/** Points in the curve. Enough that the straight segments are invisible at any size a transition
 * moves here; a critically damped spring has no wobble for a coarse sampling to miss. */
const POINTS = 24;

export function cssSpring(preset: SpringPreset): { duration: string; easing: string } {
  const spring = new Spring({ damping: preset.damping, response: preset.response, value: 0 });
  spring.target = 1;
  spring.resting = false;

  // Step at 60 fps, as a browser frame would, recording the value after each frame.
  const frame = 1 / 60;
  const trace: number[] = [0];
  for (let guard = 0; guard < 600 && !spring.resting; guard += 1) {
    spring._advance(frame);
    trace.push(spring.value);
  }
  const settled = (trace.length - 1) * frame;

  // Resample the trace at evenly spaced instants across the settle time.
  const points: string[] = [];
  for (let index = 0; index <= POINTS; index += 1) {
    const at = (index / POINTS) * (trace.length - 1);
    const below = Math.floor(at);
    const above = Math.min(below + 1, trace.length - 1);
    const value = (trace[below] ?? 0) + ((trace[above] ?? 1) - (trace[below] ?? 0)) * (at - below);
    points.push(index === POINTS ? "1" : String(Math.round(value * 1000) / 1000));
  }
  return { duration: `${Math.round(settled * 1000)}ms`, easing: `linear(${points.join(", ")})` };
}
