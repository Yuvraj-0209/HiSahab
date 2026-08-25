/* The login backdrop: one photograph, drifting (CLAUDE.md §13.28, §13.29).
 *
 * ## What this module does, and how little that is
 *
 * It builds four nodes, subscribes one callback to the shared frame loop, and stops. There
 * is no drawing here at all. The photograph is an <img>; its Ken Burns drift is a CSS
 * keyframe; the colour grade is stacked gradients. The only JavaScript that runs after mount
 * is a single `style.transform` write per changed scroll position.
 *
 * ## Two earlier versions of this file stuttered, and the reason generalises
 *
 * The first painted a full-viewport canvas every frame -- five `drawImage` blits, three
 * radial gradients and ~46 colour strings per tick -- underneath a `backdrop-filter` on the
 * sign-in card, so the browser re-ran a 30px gaussian over changing pixels sixty times a
 * second. The second moved to CSS transforms and was smooth, but drew procedural line art
 * where the brief wanted photography.
 *
 * §13.29 is the rule that survived both: **anything that moves continuously moves under
 * `transform` or `opacity`, and is never redrawn to say so.**
 *
 * ## The nesting, which looks redundant and is not
 *
 *     div.band   <- JS writes translate3d() here, and only here (parallax)
 *       img      <- CSS animates transform here, and only here (Ken Burns)
 *
 * A CSS animation and a JS assignment cannot share the `transform` property: whichever wrote
 * last wins, and the result is a fight at 60Hz. Two elements let them compose.
 *
 * ## There is no resize handler, and that is not an omission
 *
 * `object-fit: cover` re-crops the photograph on its own. The previous version needed a
 * resize listener because it had to repaint a canvas at the new size; there is nothing left
 * to repaint.
 */

import { el } from "../dom.js";
import { onFrame } from "../motion/spring.js";

/** Multiplier on scroll offset. The photograph drifts slower than the content over it. */
const PARALLAX = 0.22;

/* A 20px-wide blurred copy of the photograph, inlined so the very first paint is a soft
 * suggestion of the image rather than a black rectangle. 128 base64 characters -- smaller
 * than the HTTP request it saves. index.html's CSP allows `img-src data:`. */
const LQIP =
  "data:image/webp;base64,UklGRlgAAABXRUJQVlA4IEwAAABQAwCdASoUAA0APu1mqk4ppaOiMAgBMB2J"
  + "QBOgBDuPXVN8gAD9PBPp1RfIybZ5wr7vY3mF3MMjvbf0SQhcOvC7DltObWAe/1CJWpAA";

/**
 * Build the backdrop inside `host` and start tracking scroll.
 *
 * @param   {HTMLElement} host
 * @returns {{detach: () => void}} `detach` unsubscribes from the frame loop and empties the
 *   host. Calling it is not optional -- the loop stays alive while any subscriber exists,
 *   so a leaked one keeps the tab painting for the rest of the session. See `onFrame`.
 */
export function mountSkyline(host) {
  const reduced =
    typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

  // srcset, so a phone never downloads the 2560px file. `sizes` is 100vw because the image
  // always covers the viewport -- there is no layout in which it is smaller.
  const plate = el("img", {
    className: "plate",
    attrs: {
      src: "/img/city-1280.webp",
      srcset: "/img/city-1280.webp 1280w, /img/city-2560.webp 2560w",
      sizes: "100vw",
      // Empty alt, not a description: this is decoration, and a screen reader announcing
      // "aerial view of London at dusk" before the sign-in form is noise, not access.
      alt: "",
      decoding: "async",
      fetchpriority: "high",
      "aria-hidden": "true",
    },
  });

  const band = el("div", { className: "band", style: { backgroundImage: `url("${LQIP}")` } }, [
    plate,
  ]);

  host.appendChild(band);
  host.appendChild(el("div", { className: "backdrop-grade" }));
  host.appendChild(el("div", { className: "backdrop-wash" }));

  let lastScroll = -1;

  function applyParallax() {
    // Read first, write second. Reading scrollY *after* writing a transform forces a
    // synchronous layout every frame, which is the other classic way to make scroll stutter.
    const y = window.scrollY;
    if (y === lastScroll) return;
    lastScroll = y;
    band.style.transform = `translate3d(0, ${(-y * PARALLAX).toFixed(2)}px, 0)`;
  }

  applyParallax();

  // Reduced motion means gentler, not dead (app.css §12): the photograph is shown in full,
  // the CSS drift is frozen by the global rule there, and parallax simply never runs --
  // nothing subscribes to the frame loop at all.
  const unsubscribe = reduced ? null : onFrame(applyParallax);

  return {
    detach() {
      if (unsubscribe) unsubscribe();
      host.replaceChildren();
    },
  };
}
