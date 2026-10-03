/* The pure motion functions, ported from tests/motion_assertions.mjs (Phase 23).
 *
 * These are the functions where a wrong sign or a misplaced factor produces motion that is
 * subtly wrong rather than broken -- a flick that undershoots, a spring that takes two seconds
 * to settle -- which a person reviewing by eye will not catch reliably. Every test drives the
 * integrator by hand; a real rAF clock would make them timing-dependent.
 */

import { beforeAll, describe, expect, it } from "vitest";
import { PRESETS, project, rubberband, Spring } from "./spring";

beforeAll(() => {
  // jsdom has matchMedia undefined; the animating path is the one under test.
  window.matchMedia = ((query: string) => ({ matches: false, media: query })) as unknown as typeof window.matchMedia;
  globalThis.matchMedia = window.matchMedia;
});

function advance(spring: Spring, seconds: number, fps = 60) {
  const dt = 1 / fps;
  for (let elapsed = 0; elapsed < seconds; elapsed += dt) {
    if (spring.resting) break;
    spring._advance(dt);
  }
}

describe("spring", () => {
  it("a critically damped spring reaches its target", () => {
    const spring = new Spring({ ...PRESETS.ui, value: 0 });
    spring.to(100);
    advance(spring, 2);
    expect(spring.resting).toBe(true);
    expect(Math.abs(spring.value - 100)).toBeLessThan(0.1);
  });

  it("damping 1.0 does not overshoot", () => {
    const spring = new Spring({ damping: 1.0, response: 0.35, value: 0 });
    spring.to(100);
    let peak = 0;
    for (let i = 0; i < 240 && !spring.resting; i += 1) {
      spring._advance(1 / 60);
      peak = Math.max(peak, spring.value);
    }
    expect(peak).toBeLessThanOrEqual(100.5);
  });

  it("damping below 1.0 does overshoot", () => {
    const spring = new Spring({ damping: 0.6, response: 0.35, value: 0 });
    spring.to(100);
    let peak = 0;
    for (let i = 0; i < 240 && !spring.resting; i += 1) {
      spring._advance(1 / 60);
      peak = Math.max(peak, spring.value);
    }
    expect(peak).toBeGreaterThan(101);
  });

  it("a lower response settles sooner", () => {
    function framesToSettle(response: number) {
      const spring = new Spring({ damping: 1.0, response, value: 0 });
      spring.to(100);
      let frames = 0;
      while (!spring.resting && frames < 1000) {
        spring._advance(1 / 60);
        frames += 1;
      }
      return frames;
    }
    expect(framesToSettle(0.2)).toBeLessThan(framesToSettle(0.5));
  });

  it("retargeting mid-flight does not restart from the target", () => {
    const spring = new Spring({ ...PRESETS.ui, value: 0 });
    spring.to(100);
    advance(spring, 0.1);
    const valueBefore = spring.value;
    const velocityBefore = spring.velocity;
    expect(valueBefore).toBeGreaterThan(0);
    expect(valueBefore).toBeLessThan(100);
    expect(velocityBefore).toBeGreaterThan(0);

    spring.to(0);
    // THE interruptibility property: value and velocity carry through untouched.
    expect(spring.value).toBe(valueBefore);
    expect(spring.velocity).toBe(velocityBefore);
  });

  it("a reversal decelerates through zero once, and never teleports", () => {
    const spring = new Spring({ ...PRESETS.ui, value: 0 });
    spring.to(100);
    advance(spring, 0.12);
    spring.to(0);

    let previousValue = spring.value;
    let previousVelocity = spring.velocity;
    let signChanges = 0;
    let worstStep = 0;
    for (let i = 0; i < 240 && !spring.resting; i += 1) {
      spring._advance(1 / 60);
      if (previousVelocity > 0 && spring.velocity <= 0) signChanges += 1;
      worstStep = Math.max(worstStep, Math.abs(spring.value - previousValue));
      previousValue = spring.value;
      previousVelocity = spring.velocity;
    }
    expect(signChanges).toBe(1);
    expect(worstStep).toBeLessThan(12);
    expect(Math.abs(spring.value)).toBeLessThan(0.1);
  });

  it("velocity hand-off is honoured", () => {
    const spring = new Spring({ ...PRESETS.sheet, value: 0 });
    spring.to(100, { velocity: 2000 });
    expect(spring.velocity).toBe(2000);
    spring._advance(1 / 60);
    expect(spring.value).toBeGreaterThan(20);
  });

  it("track() records velocity without animating", () => {
    const spring = new Spring({ ...PRESETS.ui, value: 0 });
    spring.track(42, 900);
    expect(spring.value).toBe(42);
    expect(spring.target).toBe(42);
    expect(spring.velocity).toBe(900);
  });

  it("a large timestep does not go unstable", () => {
    const spring = new Spring({ damping: 1.0, response: 0.2, value: 0 });
    spring.to(100);
    spring._advance(0.064);
    expect(Number.isFinite(spring.value)).toBe(true);
    expect(Math.abs(spring.value)).toBeLessThanOrEqual(200);
  });

  it("set() jumps with no residual motion", () => {
    const spring = new Spring({ ...PRESETS.ui, value: 0 });
    spring.to(100);
    advance(spring, 0.1);
    spring.set(7);
    expect(spring.value).toBe(7);
    expect(spring.velocity).toBe(0);
    expect(spring.resting).toBe(true);
  });

  it("onRest fires exactly once", () => {
    let rests = 0;
    const spring = new Spring({ ...PRESETS.ui, value: 0, onRest: () => (rests += 1) });
    spring.to(100);
    advance(spring, 3);
    expect(rests).toBe(1);
  });
});

describe("project", () => {
  it("grows with velocity and keeps its sign", () => {
    expect(project(1000)).toBeGreaterThan(project(500));
    expect(project(-1000)).toBeLessThan(0);
    expect(project(0)).toBe(0);
  });

  it("uses Apple's exponential form, not v²/2a", () => {
    expect(Math.abs(project(1000) - 499)).toBeLessThan(0.001);
  });

  it("a snappier deceleration rate projects less far", () => {
    expect(project(1000, 0.99)).toBeLessThan(project(1000, 0.998));
  });
});

describe("rubberband", () => {
  it("resistance is sublinear and monotonic", () => {
    const small = rubberband(50, 800);
    const large = rubberband(400, 800);
    expect(small).toBeLessThan(50);
    expect(large).toBeGreaterThan(small);
    expect(large / 400).toBeLessThan(small / 50);
  });

  it("is signed and safe at zero", () => {
    expect(rubberband(-100, 800)).toBeLessThan(0);
    expect(rubberband(100, 0)).toBe(0);
  });
});
