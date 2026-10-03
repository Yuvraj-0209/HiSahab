/* GSAP choreography: the motion that says something changed (Phase 23 D8).
 *
 * Every hook here runs through `useMotion` (src/motion/gsap.ts), so React's cleanup reverts it
 * and under prefers-reduced-motion it does nothing and content is simply there. Each animates `transform` and `opacity` only -- the two properties the compositor
 * handles without laying the page out again, which is the difference between smooth and
 * stuttering on the cheap Android phone this is actually used on.
 *
 * And each answers "what does this communicate?" (§14):
 *
 *   useArrival         this list just arrived        -- hierarchy, read top to bottom
 *
 * (The screen entrance moved to CSS in Phase 24 -- `.screen-enter` -- so GSAP stays out of the
 * shell's first paint.)
 *
 * It never runs on a refetch: a list that re-animates every time the window regains focus is
 * decoration, and decoration is what the salesman at 10pm does not need.
 */

import { type RefObject, useRef } from "react";
import { DURATION, EASE, gsap, useMotion } from "../motion/gsap";

/**
 * Stagger the first eight `[data-arrive]` children of `scope` in, the first time `ready`
 * becomes true -- and never again for this mount.
 */
export function useArrival(scope: RefObject<HTMLElement | null>, ready: boolean) {
  const done = useRef(false);
  useMotion(
    (play) => {
      if (!ready || done.current || !scope.current) return;
      done.current = true;
      const items = Array.from(scope.current.querySelectorAll("[data-arrive]")).slice(0, 8);
      if (items.length === 0) return;
      play(() => {
        gsap.from(items, {
          opacity: 0,
          y: 10,
          duration: DURATION.medium,
          ease: EASE.enter.gsap,
          stagger: 0.03,
          clearProps: "transform,opacity",
        });
      });
    },
    { scope, dependencies: [ready] },
  );
}
