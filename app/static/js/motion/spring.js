/* Springs, in about a hundred lines and with no dependency (CLAUDE.md §14).
 *
 * §14 forbids npm, so Motion / Framer Motion / Vaul are all out and Apple's model is
 * implemented directly. That turns out to be affordable, because the model is small: two
 * designer-facing parameters, one integrator, and a shared frame loop.
 *
 * ## Why a spring at all, rather than a CSS transition
 *
 * A fixed-duration animation cannot respond to new input. Once it starts, it owns the value
 * until it finishes -- so a user who grabs a closing sheet must wait for it to finish closing
 * before it can reopen. That single behaviour is what separates an interface that feels alive
 * from one that feels like a form.
 *
 * A spring has no duration. It has a *target*, and retargeting mid-flight is the normal case
 * rather than an interruption: the value and velocity carry straight through, so motion stays
 * continuous and there is never a visible jump. This is why `to()` below does not restart
 * anything -- it assigns a field.
 *
 * ## The two parameters
 *
 * Apple deliberately replaced mass/stiffness/damping with two numbers a designer can reason
 * about, and the mapping to the physical triplet is exact:
 *
 *     stiffness k = (2π / response)²
 *     damping   c = 4π · dampingRatio / response
 *
 *   damping (ζ)  1.0 = critically damped, settles with no overshoot. Below 1.0 it
 *                overshoots and oscillates; lower is bouncier.
 *   response     roughly how long it takes to reach the target, in seconds. NOT a duration
 *                -- settle time emerges from the parameters -- but it is the dial that reads
 *                as "snappier" or "lazier".
 *
 * House values live in PRESETS below. The rule they encode: **bounce is earned by momentum.**
 * Overshoot on a panel that merely appeared feels wrong; overshoot on a card somebody flicked
 * feels right, because the finger supplied the energy.
 */

/** Apple's shipped pairings, plus the one this app adds for chrome. */
export const PRESETS = {
  /** Default for anything that was not thrown. Critically damped: no overshoot. */
  ui: { damping: 1.0, response: 0.35 },
  /** Sheets and drawers released from a real gesture. */
  sheet: { damping: 0.8, response: 0.3 },
  /** A repositioning move (Apple ships 1.0 / 0.4 for picture-in-picture). */
  move: { damping: 1.0, response: 0.4 },
  /** Snappy, for small affordances that should feel immediate. */
  snap: { damping: 1.0, response: 0.22 },
};

/* Reduced motion is read once per spring at construction rather than cached at module load,
 * so a user changing the setting mid-session gets the new behaviour on the next interaction
 * without a reload. §14 of the design vocabulary: reduced motion means *gentler*, not dead --
 * the value still arrives, it simply arrives immediately instead of travelling. Gesture
 * tracking is unaffected, because a drag that stops following the finger is broken, not calm.
 */
function prefersReducedMotion() {
  return (
    typeof matchMedia === "function" &&
    matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/* --- the shared frame loop --------------------------------------------------
 *
 * One requestAnimationFrame for every spring in the app, rather than one each. Two reasons:
 * a single loop cannot drift out of phase with itself, so two springs started in the same
 * frame stay in lockstep; and the browser gets one callback to schedule instead of N.
 */
const active = new Set();
let frame = null;
let lastTime = 0;

function tick(now) {
  const dt = Math.min((now - lastTime) / 1000, 0.064); // clamp: see below
  lastTime = now;

  for (const spring of active) spring._advance(dt);

  frame = active.size > 0 ? requestAnimationFrame(tick) : null;
}

function start(spring) {
  active.add(spring);
  if (frame === null) {
    lastTime = performance.now();
    frame = requestAnimationFrame(tick);
  }
}

export class Spring {
  /**
   * @param {object}   options
   * @param {number}   options.damping   ζ. 1.0 = no overshoot.
   * @param {number}   options.response  seconds to reach the target, roughly.
   * @param {number}   options.value     starting value.
   * @param {Function} options.onChange  called every frame with the current value.
   * @param {Function} options.onRest    called once when the spring settles.
   */
  constructor({
    damping = PRESETS.ui.damping,
    response = PRESETS.ui.response,
    value = 0,
    onChange = null,
    onRest = null,
  } = {}) {
    this.damping = damping;
    this.response = response;
    this.value = value;
    this.target = value;
    this.velocity = 0;
    this.onChange = onChange;
    this.onRest = onRest;
    this.resting = true;
    this._reduced = prefersReducedMotion();
  }

  /** Jump to a value with no animation. Used to seed a gesture from its current position. */
  set(value) {
    this.value = value;
    this.target = value;
    this.velocity = 0;
    this._stop();
    this.onChange?.(this.value);
    return this;
  }

  /** Follow the finger: set the value directly and *record the implied velocity*.
   *
   * The velocity matters more than it looks. It is what §5 of the design vocabulary calls
   * the seam between dragging and animating -- when the gesture ends, the spring continues
   * at exactly the speed the finger was moving, so there is no visible discontinuity. A
   * drag that resets velocity to zero produces a small dead stop at release that reads as
   * cheap even when nobody can say why.
   */
  track(value, velocity) {
    this.value = value;
    this.target = value;
    this.velocity = velocity;
    this.onChange?.(this.value);
    return this;
  }

  /** Retarget. **This is not a restart.**
   *
   * The current value and velocity are left exactly as they are, which is the whole point:
   * a spring redirected mid-flight continues from where it visibly is, at the speed it is
   * visibly moving. Reading the *target* instead of the presentation value here -- the
   * classic mistake -- is what produces a jump on interrupt.
   *
   * `velocity` overrides the carried value, for the hand-off at the end of a gesture.
   */
  to(target, { velocity } = {}) {
    this.target = target;
    if (velocity !== undefined) this.velocity = velocity;

    if (this._reduced) {
      // Arrive immediately rather than travelling. Still calls onChange/onRest, so callers
      // need no branch of their own and every state transition still happens.
      this.value = target;
      this.velocity = 0;
      this.onChange?.(this.value);
      this._settle();
      return this;
    }

    this.resting = false;
    start(this);
    return this;
  }

  /** Halt where it stands, keeping the current value. */
  stop() {
    this.velocity = 0;
    this.target = this.value;
    this._stop();
    return this;
  }

  _stop() {
    active.delete(this);
    this.resting = true;
  }

  _settle() {
    this.value = this.target;
    this.velocity = 0;
    this._stop();
    this.onChange?.(this.value);
    this.onRest?.(this.value);
  }

  /** One frame of integration.
   *
   * Semi-implicit Euler (velocity updated first, then position) rather than explicit Euler,
   * because it is stable for oscillators at the timesteps a browser actually delivers.
   *
   * Substepped at a fixed 1/240s. A backgrounded tab or a slow frame delivers a large dt,
   * and a stiff spring integrated in one large step does not merely look wrong -- it goes
   * numerically unstable and the value runs away to infinity. The clamp in tick() bounds the
   * worst case at ~4 frames of catch-up; the substepping keeps each one small.
   */
  _advance(dt) {
    const k = (2 * Math.PI / this.response) ** 2;
    const c = (4 * Math.PI * this.damping) / this.response;

    const step = 1 / 240;
    let remaining = dt;

    while (remaining > 0) {
      const h = Math.min(step, remaining);
      remaining -= h;

      const displacement = this.value - this.target;
      const acceleration = -k * displacement - c * this.velocity;

      this.velocity += acceleration * h;
      this.value += this.velocity * h;
    }

    // Settle thresholds are relative to the distance travelled, so a spring animating a
    // 900px sheet and one animating a 0..1 opacity both stop when they are imperceptibly
    // close rather than one of them spinning for hundreds of frames.
    const scale = Math.max(1, Math.abs(this.target));
    if (
      Math.abs(this.value - this.target) < 0.0005 * scale &&
      Math.abs(this.velocity) < 0.005 * scale
    ) {
      this._settle();
      return;
    }

    this.onChange?.(this.value);
  }
}

/** Where a flick was going -- Apple's projection, not the physics-textbook one.
 *
 * Given a release velocity, this is the distance the value would still travel under
 * exponential deceleration. Use it to choose the snap target *before* animating, so a flick
 * throws an element rather than dropping it at the nearest edge to wherever the finger
 * happened to leave the glass.
 *
 * The textbook `v² / (2a)` is **not** what iOS ships and does not feel the same; this is the
 * form from Apple's own Designing Fluid Interfaces sample code.
 *
 * @param velocity px/s at release.
 * @param decelerationRate 0.998 for normal scroll feel; 0.99 is snappier.
 */
export function project(velocity, decelerationRate = 0.998) {
  return ((velocity / 1000) * decelerationRate) / (1 - decelerationRate);
}

/** Progressive resistance past a boundary.
 *
 * A hard stop reads as "frozen -- did it break?". Continuous resistance reads as
 * "responsive, but there is nothing more here", which is the honest message. The further
 * past the bound, the less the element follows, exactly like a real thing slowing before it
 * stops.
 *
 * @param overshoot how far past the boundary the finger has gone.
 * @param dimension the size of the surface being dragged, which sets the scale.
 */
export function rubberband(overshoot, dimension, constant = 0.55) {
  if (dimension <= 0) return 0;
  return (
    (overshoot * dimension * constant) / (dimension + constant * Math.abs(overshoot))
  );
}

/** A spring bound to a DOM element's translateY, in the units the compositor wants.
 *
 * Only `transform` and `opacity` are ever animated in this app (§11 of the design
 * vocabulary) -- they are the two properties the compositor can handle without laying the
 * page out again, which is the difference between 60fps and visible stutter on the cheap
 * Android phone this is actually used on.
 */
export function translateYSpring(element, options = {}) {
  return new Spring({
    ...options,
    onChange: (value) => {
      element.style.transform = `translate3d(0, ${value}px, 0)`;
      options.onChange?.(value);
    },
  });
}
