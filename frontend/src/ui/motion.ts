/* GSAP choreography: the motion that says something changed (Phase 23 D8).
 *
 * Every hook here runs inside `useGSAP` (so React's cleanup reverts it) and inside
 * `gsap.matchMedia()`, so under prefers-reduced-motion it does nothing and content is simply
 * there. Each animates `transform` and `opacity` only -- the two properties the compositor
 * handles without laying the page out again, which is the difference between smooth and
 * stuttering on the cheap Android phone this is actually used on.
 *
 * And each answers "what does this communicate?" (§14):
 *
 *   useScreenEntrance  the screen changed            -- a state transition
 *   useArrival         this list just arrived        -- hierarchy, read top to bottom
 *
 * Neither runs on a refetch: a list that re-animates every time the window regains focus is
 * decoration, and decoration is what the salesman at 10pm does not need.
 */

import { type RefObject, useRef } from "react";
import { useGSAP } from "@gsap/react";
import gsap from "gsap";

gsap.registerPlugin(useGSAP);

const NO_PREFERENCE = "(prefers-reduced-motion: no-preference)";

/** Cross-fade plus an 8px rise when the route changes. Killed if the user navigates again
 * before it finishes, because `useGSAP` reverts it when `key` changes. */
export function useScreenEntrance(scope: RefObject<HTMLElement | null>, key: string) {
  useGSAP(
    () => {
      const element = scope.current;
      if (!element) return;
      const mm = gsap.matchMedia();
      mm.add(NO_PREFERENCE, () => {
        gsap.fromTo(
          element,
          { opacity: 0, y: 8 },
          { opacity: 1, y: 0, duration: 0.24, ease: "power2.out", clearProps: "transform,opacity" },
        );
      });
      return () => mm.revert();
    },
    { dependencies: [key], scope },
  );
}

/**
 * Stagger the first eight `[data-arrive]` children of `scope` in, the first time `ready`
 * becomes true -- and never again for this mount.
 */
export function useArrival(scope: RefObject<HTMLElement | null>, ready: boolean) {
  const done = useRef(false);
  useGSAP(
    () => {
      if (!ready || done.current || !scope.current) return;
      done.current = true;
      const items = Array.from(scope.current.querySelectorAll("[data-arrive]")).slice(0, 8);
      if (items.length === 0) return;
      const mm = gsap.matchMedia();
      mm.add(NO_PREFERENCE, () => {
        gsap.from(items, {
          opacity: 0,
          y: 10,
          duration: 0.36,
          ease: "power2.out",
          stagger: 0.03,
          clearProps: "transform,opacity",
        });
      });
      return () => mm.revert();
    },
    { dependencies: [ready], scope },
  );
}
