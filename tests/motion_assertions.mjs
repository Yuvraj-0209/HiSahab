/* Assertions over the pure motion functions, run by tests/test_motion.py.
 *
 * §13.18 records that the frontend has no automated behavioural tests, because §14 forbids
 * npm and therefore Jest and Vitest. This file is the exception that rule permits: the
 * *pure* parts of the motion system -- the spring integrator, the momentum projection and
 * the rubber-band curve -- are ordinary arithmetic with no DOM, so they can be checked with
 * node's own assert module and nothing installed at all. No package.json, no dependency, no
 * build step; if node is absent the Python test skips.
 *
 * These are the functions where a wrong sign or a misplaced factor produces motion that is
 * subtly wrong rather than obviously broken -- a flick that undershoots, a spring that takes
 * two seconds to settle -- which is exactly the class of bug a person reviewing by eye will
 * not catch reliably.
 *
 * Run directly:  node tests/motion_assertions.mjs
 */

import assert from "node:assert/strict";

/* The module reads matchMedia and requestAnimationFrame at construction and per frame. Node
 * has neither, so provide the minimum: reduced motion off (we are testing the animating
 * path), and a frame loop we never start -- every test below drives _advance by hand, which
 * is deliberate. Testing against a real rAF clock would make these tests timing-dependent
 * and flaky; stepping the integrator manually tests the physics rather than the browser. */
globalThis.matchMedia = () => ({ matches: false });
globalThis.requestAnimationFrame = () => 0;
globalThis.performance = globalThis.performance ?? { now: () => 0 };

const { Spring, PRESETS, project, rubberband } = await import(
  "../app/static/js/motion/spring.js"
);

let checks = 0;
function check(name, fn) {
  fn();
  checks += 1;
  console.log(`  ok  ${name}`);
}

/** Step a spring forward by `seconds`, in browser-sized frames. */
function advance(spring, seconds, fps = 60) {
  const dt = 1 / fps;
  for (let elapsed = 0; elapsed < seconds; elapsed += dt) {
    if (spring.resting) break;
    spring._advance(dt);
  }
}

console.log("spring");

check("a critically damped spring reaches its target", () => {
  const spring = new Spring({ ...PRESETS.ui, value: 0 });
  spring.to(100);
  advance(spring, 2);

  assert.equal(spring.resting, true, "should have settled");
  assert.ok(Math.abs(spring.value - 100) < 0.1, `landed at ${spring.value}`);
});

check("damping 1.0 does not overshoot", () => {
  const spring = new Spring({ damping: 1.0, response: 0.35, value: 0 });
  spring.to(100);

  let peak = 0;
  for (let i = 0; i < 240 && !spring.resting; i += 1) {
    spring._advance(1 / 60);
    peak = Math.max(peak, spring.value);
  }

  // A tiny tolerance: the integrator is discrete, so a critically damped spring can cross
  // its target by a fraction of a pixel. Anything visible would be a real overshoot.
  assert.ok(peak <= 100.5, `critically damped spring peaked at ${peak}`);
});

check("damping below 1.0 does overshoot", () => {
  const spring = new Spring({ damping: 0.6, response: 0.35, value: 0 });
  spring.to(100);

  let peak = 0;
  for (let i = 0; i < 240 && !spring.resting; i += 1) {
    spring._advance(1 / 60);
    peak = Math.max(peak, spring.value);
  }

  // The counterpart to the test above: if this fails, the damping parameter is not reaching
  // the integrator and every "bouncy" preset in the app is silently critically damped.
  assert.ok(peak > 101, `underdamped spring should overshoot, peaked at ${peak}`);
});

check("a lower response settles sooner", () => {
  function framesToSettle(response) {
    const spring = new Spring({ damping: 1.0, response, value: 0 });
    spring.to(100);
    let frames = 0;
    while (!spring.resting && frames < 1000) {
      spring._advance(1 / 60);
      frames += 1;
    }
    return frames;
  }

  assert.ok(
    framesToSettle(0.2) < framesToSettle(0.5),
    "response is meant to be the 'snappiness' dial",
  );
});

check("retargeting mid-flight does not restart from the target", () => {
  const spring = new Spring({ ...PRESETS.ui, value: 0 });
  spring.to(100);
  advance(spring, 0.1);

  const valueBefore = spring.value;
  const velocityBefore = spring.velocity;
  assert.ok(valueBefore > 0 && valueBefore < 100, "should be mid-flight");
  assert.ok(velocityBefore > 0, "should be moving");

  spring.to(0);

  // THE interruptibility property. The value and velocity carry through untouched; only the
  // target changed. Reading the target instead of the presentation value here is the classic
  // mistake, and it shows up as a visible jump the instant a user grabs a moving sheet.
  assert.equal(spring.value, valueBefore, "value must carry through a retarget");
  assert.equal(spring.velocity, velocityBefore, "velocity must carry through a retarget");
});

check("a reversal decelerates through zero once, and never teleports", () => {
  const spring = new Spring({ ...PRESETS.ui, value: 0 });
  spring.to(100);
  advance(spring, 0.12);
  spring.to(0);

  // Note what is NOT asserted here: a bound on per-frame velocity *change*. A reversing
  // spring legitimately pulls hard -- k*displacement alone reaches ~32,000 px/s^2 at these
  // parameters, or ~537 px/s of velocity change per 60fps frame -- so a small bound would
  // be measuring physics and calling it a defect. Continuity at the moment of retarget is
  // already proven exactly, by the test above: value and velocity carry through untouched.
  //
  // What this checks instead are the two things that would actually look wrong on screen.
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

  // One turn, not a wobble. A critically damped spring redirected home decelerates, crosses
  // zero once, and comes back -- more crossings would mean the damping is not reaching the
  // integrator and every "calm" preset in the app is secretly bouncy.
  assert.equal(signChanges, 1, `velocity crossed zero ${signChanges} times`);
  // No frame moves further than a fast finger could: position stays continuous, so nothing
  // ever appears to jump between frames.
  assert.ok(worstStep < 12, `position jumped ${worstStep}px in one frame`);
  assert.ok(Math.abs(spring.value) < 0.1, "should have returned home");
});

check("velocity hand-off is honoured", () => {
  const spring = new Spring({ ...PRESETS.sheet, value: 0 });
  spring.to(100, { velocity: 2000 });

  assert.equal(spring.velocity, 2000);

  spring._advance(1 / 60);
  // Moving fast in the direction it was thrown, rather than easing in from a standstill --
  // this is the seam between the finger and the animation.
  assert.ok(spring.value > 20, `expected a fast first frame, got ${spring.value}`);
});

check("track() records velocity without animating", () => {
  const spring = new Spring({ ...PRESETS.ui, value: 0 });
  spring.track(42, 900);

  assert.equal(spring.value, 42);
  assert.equal(spring.target, 42, "tracking must not leave a target behind the finger");
  assert.equal(spring.velocity, 900);
});

check("a large timestep does not go unstable", () => {
  // A backgrounded tab delivers one enormous frame. Explicit Euler on a stiff spring
  // diverges to infinity here; the substepping is what prevents it. A NaN or a runaway
  // value would put a sheet somewhere off-screen with no way back.
  const spring = new Spring({ damping: 1.0, response: 0.2, value: 0 });
  spring.to(100);
  spring._advance(0.064);

  assert.ok(Number.isFinite(spring.value), "value went non-finite");
  assert.ok(Math.abs(spring.value) <= 200, `value ran away to ${spring.value}`);
});

check("set() jumps with no residual motion", () => {
  const spring = new Spring({ ...PRESETS.ui, value: 0 });
  spring.to(100);
  advance(spring, 0.1);
  spring.set(7);

  assert.equal(spring.value, 7);
  assert.equal(spring.velocity, 0);
  assert.equal(spring.resting, true);
});

check("onRest fires exactly once", () => {
  let rests = 0;
  const spring = new Spring({ ...PRESETS.ui, value: 0, onRest: () => (rests += 1) });
  spring.to(100);
  advance(spring, 3);

  assert.equal(rests, 1, `onRest fired ${rests} times`);
});

console.log("project");

check("projection grows with velocity and keeps its sign", () => {
  assert.ok(project(1000) > project(500));
  assert.ok(project(-1000) < 0, "a flick upward must project upward");
  assert.equal(project(0), 0);
});

check("projection uses Apple's exponential form, not v^2/2a", () => {
  // (v/1000) * d / (1 - d), d = 0.998  ->  1000 -> 1 * 0.998 / 0.002 = 499
  assert.ok(
    Math.abs(project(1000) - 499) < 0.001,
    `expected ~499px of travel for 1000px/s, got ${project(1000)}`,
  );
  // The textbook form would give a wildly different figure, and the difference is the whole
  // reason a flick in this app feels like iOS rather than like a physics demo.
});

check("a snappier deceleration rate projects less far", () => {
  assert.ok(project(1000, 0.99) < project(1000, 0.998));
});

console.log("rubberband");

check("resistance is sublinear and monotonic", () => {
  const small = rubberband(50, 800);
  const large = rubberband(400, 800);

  assert.ok(small < 50, "must resist rather than follow 1:1");
  assert.ok(large > small, "must still move -- a hard stop reads as frozen");
  assert.ok(large / 400 < small / 50, "resistance must increase with distance");
});

check("rubberband is signed and safe at zero", () => {
  assert.ok(rubberband(-100, 800) < 0, "must resist in both directions");
  assert.equal(rubberband(100, 0), 0, "no dimension, no movement -- and no divide by zero");
});

console.log(`\n${checks} assertions passed`);
