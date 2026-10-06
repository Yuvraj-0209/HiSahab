/* The front door's scroll story (Phase 24 D7). §14: pins and scrubbed timelines live here and in
 * src/showroom/ only -- the front door has no data on it, and a salesman's data
 * screens never wait for a story.
 *
 * `useStory` is `useMotion` with a second question: is this a desktop with a fine pointer? Pins
 * and scrubs run only there, because pinning on a phone fights the collapsing address bar and a
 * thumb flick overshoots a scrub. Phones get the same beats as one-shot reveals. Under reduced
 * motion neither runs, and every section is simply there.
 *
 * There is no ScrollSmoother (Phase 28 D2). It moved the sign-in form, which §14 forbids, and it
 * breaks `position: sticky`, which the story's pinned phone is built on. Scrolling is the
 * browser's own; a numeric `scrub` gives a scrubbed timeline its ease.
 */

import { type RefObject } from "react";
import { useGSAP } from "@gsap/react";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import { gsap } from "./gsap";
import { NO_PREFERENCE } from "./preference";

gsap.registerPlugin(ScrollTrigger);

export { ScrollTrigger };

export const DESKTOP = "(min-width: 1024px) and (pointer: fine)";

export function useStory(scope: RefObject<Element | null>, build: (desktop: boolean) => void, dependencies: unknown[] = []): void {
  useGSAP(
    () => {
      const mm = gsap.matchMedia();
      mm.add({ desktop: DESKTOP, motion: NO_PREFERENCE }, (context) => {
        const { desktop, motion } = context.conditions as { desktop: boolean; motion: boolean };
        if (motion) build(desktop);
      });
      return () => mm.revert();
    },
    { scope, dependencies },
  );
}
