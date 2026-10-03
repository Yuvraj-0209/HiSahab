/* The one door GSAP comes in through (Phase 24 D2, CLAUDE.md §14).
 *
 * Phase 23 imported GSAP in six files and each remembered, separately, to wrap its tweens in
 * `gsap.matchMedia("(prefers-reduced-motion: no-preference)")`. Six copies of a rule are six
 * chances to forget it, so the rule lives here once: `useMotion` hands its callback a `play`
 * function, and the only way to start a tween through it is under that media query. A structural
 * test refuses a `gsap` import anywhere outside `src/motion/`.
 *
 * Plugins live in sibling modules (`scroll.ts`, `flip.ts`, `draw.ts`, `story.ts`), so a chunk
 * pays only for what it uses: importing this file costs GSAP's core and nothing else.
 *
 * ## One physics
 *
 * Every duration and ease the app uses is named below and mirrored as a CSS custom property in
 * styles.css (`--dur-*`, `--ease-*`), for the motion CSS does on its own: the screen entrance,
 * the tab indicator, view transitions. A pytest test holds the two copies equal. "Premium" is
 * mostly this: the same thing moving the same way everywhere.
 */

import { type RefObject } from "react";
import { useGSAP } from "@gsap/react";
import gsap from "gsap";

gsap.registerPlugin(useGSAP);

export { gsap };

/** The media query every tween runs under. Reduced motion means the content simply arrives. */
export const NO_PREFERENCE = "(prefers-reduced-motion: no-preference)";

/** Seconds, as GSAP takes them. CSS mirrors these in milliseconds. */
export const DURATION = {
  /** A press acknowledged. */
  tap: 0.14,
  /** A screen arriving, a pill changing. */
  small: 0.24,
  /** A figure rolling, a tile settling, a row leaving. */
  medium: 0.42,
  /** A chart growing, a story beat on the front door. */
  large: 0.7,
} as const;

/** Each ease has a GSAP name and the CSS curve that matches it, so CSS and GSAP motion feel the
 * same. The curves are the standard published equivalents of GSAP's eases. */
export const EASE = {
  /** Arriving: fast, then settling. */
  enter: { gsap: "expo.out", css: "cubic-bezier(0.16, 1, 0.3, 1)" },
  /** Leaving: gathers speed, gets out of the way. */
  exit: { gsap: "power2.in", css: "cubic-bezier(0.55, 0.085, 0.68, 0.53)" },
  /** Moving between two places on screen. */
  move: { gsap: "power3.inOut", css: "cubic-bezier(0.645, 0.045, 0.355, 1)" },
  /** A state landing, with a small overshoot. */
  settle: { gsap: "back.out(1.8)", css: "cubic-bezier(0.34, 1.56, 0.64, 1)" },
} as const;

/** Whether motion is welcome right now, for code that is not a tween (a view transition, a
 * vibration). Tweens should go through `useMotion` instead. */
export function motionAllowed(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(NO_PREFERENCE).matches;
}

/** Starts the tweens inside it only when motion is welcome. */
export type Play = (animate: () => void) => void;

/**
 * `useGSAP` with the reduced-motion gate built in.
 *
 * `build` runs on every dependency change, so bookkeeping (the previous value, which rows are new)
 * happens whatever the user's setting; only what is passed to `play` is gated. Everything it
 * creates is reverted when the dependencies change or the component unmounts, which is what
 * keeps an interrupted animation from leaving an element half-moved.
 */
export function useMotion(
  build: (play: Play) => void,
  config: { scope: RefObject<Element | null>; dependencies?: unknown[] },
): void {
  useGSAP(
    () => {
      const mm = gsap.matchMedia();
      build((animate) => mm.add(NO_PREFERENCE, animate));
      return () => mm.revert();
    },
    { scope: config.scope, dependencies: config.dependencies ?? [] },
  );
}
