/* Flip, for state that changes shape in place (Phase 24 D5).
 *
 * Flip records where elements are, lets React re-render them somewhere else, then animates the
 * difference with transforms only -- so a list closing ranks after a row leaves, or a tile
 * whose contents changed, moves on the compositor without React knowing anything happened. Its
 * own module so only the chunks that use it pay for it.
 */

import { Flip } from "gsap/Flip";
import { gsap } from "./gsap";

gsap.registerPlugin(Flip);

export { Flip };
