/* Whether motion is welcome, without GSAP (Phase 24 D1, D2).
 *
 * The shell's navigation needs to ask this on every route change, and the shell is in the first
 * paint. Asked through motion/gsap.ts, the question would bring GSAP's 28 KB core into the first
 * paint with it -- which is exactly what the bundle budget caught the first time it was asked
 * that way. So the answer lives here, with no imports at all, and gsap.ts re-exports it.
 */

/** The media query every animation runs under. Reduced motion means the content simply arrives. */
export const NO_PREFERENCE = "(prefers-reduced-motion: no-preference)";

/** Whether motion is welcome right now, for code that is not a tween (a view transition, a
 * vibration). Tweens should go through `useMotion` instead. */
export function motionAllowed(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(NO_PREFERENCE).matches;
}
