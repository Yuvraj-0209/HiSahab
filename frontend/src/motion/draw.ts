/* DrawSVG, for a stroke drawing itself (Phase 24 D5): the confirm tick, a lifecycle connector.
 * Its own module so only the chunks that draw pay for it. */

import { DrawSVGPlugin } from "gsap/DrawSVGPlugin";
import { gsap } from "./gsap";

gsap.registerPlugin(DrawSVGPlugin);

export { DrawSVGPlugin };
