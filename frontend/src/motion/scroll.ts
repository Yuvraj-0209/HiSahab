/* ScrollTrigger, for the motion that waits until you can see it (Phase 24 D2, D4).
 *
 * Its own module so only the chunks that scroll-trigger pay its 18 KB: the Summary's charts, and
 * the front door. Import `gsap` and `useMotion` from ./gsap as usual; this file only registers
 * the plugin and re-exports it.
 */

import { ScrollTrigger } from "gsap/ScrollTrigger";
import { gsap } from "./gsap";

gsap.registerPlugin(ScrollTrigger);

export { ScrollTrigger };
