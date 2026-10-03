/* Flip, for state that changes shape in place (Phase 24 D5).
 *
 * Flip records where elements are, lets React re-render them somewhere else, then animates the
 * difference with transforms only -- so a list closing ranks after a row leaves, or a tile
 * whose contents changed, moves on the compositor without React knowing anything happened. Its
 * own module so only the chunks that use it pay for it.
 */

import { type RefObject, useLayoutEffect, useRef } from "react";
import { Flip } from "gsap/Flip";
import { DURATION, EASE, gsap } from "./gsap";
import { motionAllowed } from "./preference";

gsap.registerPlugin(Flip);

export { Flip };

/**
 * Rows move to their new places when a list changes, instead of jumping (Phase 24 D5).
 *
 * Saving an expense adds a row; a reversal adds two; a reading landing makes its tile taller and
 * pushes the grid. Without this, every row below the change teleports. With it, they slide, and
 * the new row rises in -- which is the sentence "your entry is in, here" said without words.
 *
 * How: after every commit the layout of `scope`'s direct children is recorded. When `signature`
 * changes (the caller passes something that changes exactly when the list's contents do), Flip
 * animates each child from where it was recorded to where it is now; children that did not
 * exist before fade up. A row that left simply goes, and the rest close ranks over its space.
 *
 * Never on the first load, which is useArrival's job, and never under reduced motion. Children
 * keep their identity because React keeps the same DOM node for the same key; Flip tags each with
 * a `data-flip-id` of its own.
 */
export function useFlipList(scope: RefObject<HTMLElement | null>, signature: string): void {
  const recorded = useRef<Flip.FlipState | null>(null);
  const seen = useRef(signature);
  useLayoutEffect(() => {
    const node = scope.current;
    if (!node) return;
    const children = Array.from(node.children) as HTMLElement[];
    const before = recorded.current;
    if (before && seen.current !== signature && before.elementStates.length > 0 && motionAllowed()) {
      Flip.from(before, {
        targets: children,
        duration: DURATION.medium,
        ease: EASE.move.gsap,
        onEnter: (entering) =>
          gsap.fromTo(entering, { opacity: 0, y: 10 }, { opacity: 1, y: 0, duration: DURATION.medium, ease: EASE.enter.gsap }),
      });
    }
    seen.current = signature;
    recorded.current = Flip.getState(children);
  });
}
