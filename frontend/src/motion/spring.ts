/* Springs: the physics behind every gesture in the app (Phase 12, ported in Phase 23).
 *
 * GSAP choreographs (route changes, lists arriving, charts); this module owns anything under a
 * finger -- sheets and toasts. GSAP's eases have a duration, and a fixed-duration animation
 * cannot respond to new input: once it starts, it owns the value until it finishes, so a user
 * who grabs a closing sheet must wait for it to finish closing. A spring has no duration. It
 * has a *target*, and retargeting mid-flight is the normal case: value and velocity carry
 * straight through, so motion stays continuous and nothing ever jumps.
 *
 * **One rule (§14): an element is driven by this spring or by GSAP, never both.** They would
 * write the same `transform` on the same frame.
 *
 * ## The two parameters
 *
 * Apple replaced mass/stiffness/damping with two numbers a designer can reason about, and the
 * mapping is exact:
 *
 *     stiffness k = (2π / response)²
 *     damping   c = 4π · dampingRatio / response
 *
 *   damping (ζ)  1.0 = critically damped, no overshoot. Below 1.0 it overshoots.
 *   response     roughly how long it takes to reach the target, in seconds.
 *
 * **Bounce is earned by momentum.** Overshoot on a panel that merely appeared feels wrong;
 * overshoot on a card somebody flicked feels right, because the finger supplied the energy.
 *
 * Behaviour is unchanged from `app/static/js/motion/spring.js`; `spring.test.ts` is the port of
 * `tests/motion_assertions.mjs` and pins it.
 */

export interface SpringPreset {
  damping: number;
  response: number;
}

export const PRESETS = {
  /** Default for anything that was not thrown. Critically damped: no overshoot. */
  ui: { damping: 1.0, response: 0.35 },
  /** Sheets and drawers released from a real gesture. */
  sheet: { damping: 0.8, response: 0.3 },
  /** A repositioning move (Apple ships 1.0 / 0.4 for picture-in-picture). */
  move: { damping: 1.0, response: 0.4 },
  /** Snappy, for small affordances that should feel immediate. */
  snap: { damping: 1.0, response: 0.22 },
} as const satisfies Record<string, SpringPreset>;

/* Read once per spring at construction rather than at module load, so changing the setting
 * mid-session takes effect on the next interaction. Reduced motion means *gentler*, not dead:
 * the value still arrives, immediately instead of travelling. Gesture tracking is unaffected,
 * because a drag that stops following the finger is broken, not calm. */
function prefersReducedMotion(): boolean {
  return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/* --- the shared frame loop ---------------------------------------------------------------
 * One requestAnimationFrame for every spring in the app: a single loop cannot drift out of
 * phase with itself, and the browser schedules one callback instead of N. */
const active = new Set<Spring>();
let frame: number | null = null;
let lastTime = 0;

function tick(now: number): void {
  const dt = Math.min((now - lastTime) / 1000, 0.064); // clamp: see _advance
  lastTime = now;
  for (const spring of active) spring._advance(dt);
  frame = active.size > 0 ? requestAnimationFrame(tick) : null;
}

function start(spring: Spring): void {
  active.add(spring);
  if (frame === null) {
    lastTime = performance.now();
    frame = requestAnimationFrame(tick);
  }
}

export interface SpringOptions extends Partial<SpringPreset> {
  value?: number;
  onChange?: ((value: number) => void) | null;
  onRest?: ((value: number) => void) | null;
}

export class Spring {
  damping: number;
  response: number;
  value: number;
  target: number;
  velocity = 0;
  resting = true;
  onChange: ((value: number) => void) | null;
  onRest: ((value: number) => void) | null;
  private readonly reduced: boolean;

  constructor({
    damping = PRESETS.ui.damping,
    response = PRESETS.ui.response,
    value = 0,
    onChange = null,
    onRest = null,
  }: SpringOptions = {}) {
    this.damping = damping;
    this.response = response;
    this.value = value;
    this.target = value;
    this.onChange = onChange;
    this.onRest = onRest;
    this.reduced = prefersReducedMotion();
  }

  /** Jump to a value with no animation. */
  set(value: number): this {
    this.value = value;
    this.target = value;
    this.velocity = 0;
    this.halt();
    this.onChange?.(this.value);
    return this;
  }

  /** Follow the finger: set the value directly and *record the implied velocity*, so the
   * hand-off at release continues at exactly the speed the finger was moving. */
  track(value: number, velocity: number): this {
    this.value = value;
    this.target = value;
    this.velocity = velocity;
    this.onChange?.(this.value);
    return this;
  }

  /** Retarget. **This is not a restart**: value and velocity are left exactly as they are, so
   * a spring redirected mid-flight continues from where it visibly is. `velocity` overrides the
   * carried value, for the hand-off at the end of a gesture. */
  to(target: number, { velocity }: { velocity?: number } = {}): this {
    this.target = target;
    if (velocity !== undefined) this.velocity = velocity;

    if (this.reduced) {
      // Arrive immediately. Still calls onChange/onRest, so callers need no branch of their own.
      this.value = target;
      this.velocity = 0;
      this.onChange?.(this.value);
      this.settle();
      return this;
    }

    this.resting = false;
    start(this);
    return this;
  }

  /** Halt where it stands, keeping the current value. */
  stop(): this {
    this.velocity = 0;
    this.target = this.value;
    this.halt();
    return this;
  }

  private halt(): void {
    active.delete(this);
    this.resting = true;
  }

  private settle(): void {
    this.value = this.target;
    this.velocity = 0;
    this.halt();
    this.onChange?.(this.value);
    this.onRest?.(this.value);
  }

  /** One frame of integration. Semi-implicit Euler, substepped at a fixed 1/240s: a
   * backgrounded tab delivers a huge dt, and a stiff spring integrated in one large step goes
   * numerically unstable. Public only so the tests can drive it frame by frame. */
  _advance(dt: number): void {
    const k = ((2 * Math.PI) / this.response) ** 2;
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

    // Thresholds relative to the distance, so a 900px sheet and a 0..1 opacity both stop when
    // they are imperceptibly close.
    const scale = Math.max(1, Math.abs(this.target));
    if (Math.abs(this.value - this.target) < 0.0005 * scale && Math.abs(this.velocity) < 0.005 * scale) {
      this.settle();
      return;
    }
    this.onChange?.(this.value);
  }
}

/** Where a flick was going -- Apple's exponential projection, not the textbook v²/2a. Use it to
 * choose the snap target *before* animating, so a flick throws rather than drops. */
export function project(velocity: number, decelerationRate = 0.998): number {
  return ((velocity / 1000) * decelerationRate) / (1 - decelerationRate);
}

/** Progressive resistance past a boundary. A hard stop reads as "frozen"; resistance reads as
 * "there is nothing more here", which is the honest message. */
export function rubberband(overshoot: number, dimension: number, constant = 0.55): number {
  if (dimension <= 0) return 0;
  return (overshoot * dimension * constant) / (dimension + constant * Math.abs(overshoot));
}
