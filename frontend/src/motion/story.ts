/* The front door's scroll story (Phase 24 D7). §14: pins, scrubbed timelines and smooth scrolling
 * live here and in src/showroom/ only -- the front door has no data on it, and a salesman's data
 * screens never wait for a story.
 *
 * `useStory` is `useMotion` with a second question: is this a desktop with a fine pointer? Pins
 * and scrubs run only there, because pinning on a phone fights the collapsing address bar and a
 * thumb flick overshoots a scrub. Phones get the same beats as one-shot reveals. Under reduced
 * motion neither runs, and every section is simply there.
 *
 * ScrollSmoother is created here, on desktop only, before any story trigger, so pins land in the
 * smoothed content. It is not used anywhere inside the app (D10).
 */

import { type RefObject } from "react";
import { useGSAP } from "@gsap/react";
import { ScrollSmoother } from "gsap/ScrollSmoother";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import { gsap } from "./gsap";
import { NO_PREFERENCE } from "./preference";

gsap.registerPlugin(ScrollTrigger, ScrollSmoother);

export { ScrollSmoother, ScrollTrigger };

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
