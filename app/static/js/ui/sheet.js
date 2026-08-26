/* Bottom sheets: grabbable, throwable, interruptible.
 *
 * Every create-and-edit form in this app is a sheet, so this is the component that decides
 * whether the whole thing feels native or feels like a web page with rounded corners.
 *
 * The four properties that produce that difference, none of which a CSS transition can give:
 *
 *   1. **1:1 tracking with the grab offset respected** (§2). The sheet stays glued to the
 *      finger from wherever it was grabbed. Snapping its top edge to the pointer would break
 *      the illusion in the first frame.
 *   2. **Interruptibility** (§3). The sheet can be caught mid-animation -- opening, closing,
 *      settling -- and redirected, because the spring carries its live value and velocity
 *      through every retarget. There is no "animating" state that locks out input.
 *   3. **Velocity hand-off and momentum projection** (§5, §6). At release the spring
 *      continues at the finger's exact speed, and where the flick was *going* decides
 *      dismiss-versus-settle -- so a fast flick dismisses from two pixels in.
 *   4. **Rubber-banding at the top** (§9). Dragging up past the open position resists
 *      progressively instead of stopping dead, which reads as "there is nothing more here"
 *      rather than "this froze".
 *
 * Spatial consistency (§7): a sheet enters from the bottom and leaves to the bottom, along
 * the same path. It materialises rather than fading -- the scrim's opacity is driven from the
 * same spring as the sheet's position, so the dimming tracks the drag continuously.
 */

import { el } from "../dom.js";
import { PRESETS, project, Spring } from "../motion/spring.js";
import { shouldCommit, verticalDrag } from "../motion/gesture.js";

const LAYERS = () => document.getElementById("layers");

/** The sheet currently on screen, if any. One at a time -- this app never stacks. */
let current = null;

/**
 * Open a bottom sheet.
 *
 * @param {object}   options
 * @param {string}   options.title
 * @param {Node}     options.body      built by the caller
 * @param {Node}     [options.footer]  usually the submit button
 * @param {Function} [options.onClose] called once the sheet has actually left the screen
 * @returns {{close: Function, element: HTMLElement, setBody: Function}}
 */
export function openSheet({ title, body, footer = null, onClose = null }) {
  // Only one at a time. Closing the old one first keeps the scrim stack honest and means a
  // caller never has to think about what was already open.
  if (current) current.close({ immediate: true });

  const scrim = el("div", { className: "scrim", attrs: { "aria-hidden": "true" } });

  const grip = el("div", { className: "sheet-grip" });
  const bodyNode = el("div", { className: "sheet-body" }, [body]);
  // Wrapped the same way bodyNode wraps `body`, so setBody below can replace either without
  // touching the sheet's own child list (grip/head/bodyNode/footerNode never change).
  const footerNode = el("div", { className: "sheet-footer" }, [footer]);

  const head = el("div", { className: "sheet-head" }, [
    el("h2", { className: "sheet-title t-title", text: title }),
    el("button", {
      className: "btn btn-plain",
      text: "Close",
      attrs: { type: "button", "aria-label": "Close" },
      on: { click: () => close() },
    }),
  ]);

  const sheet = el(
    "div",
    {
      className: "sheet",
      attrs: { role: "dialog", "aria-modal": "true", "aria-label": title },
    },
    [grip, head, bodyNode, footerNode],
  );

  LAYERS().append(scrim, sheet);

  // Measured after insertion, because the sheet's height depends on its content and the
  // travel distance is that height. Using a guess here would make the dismiss threshold and
  // the projection wrong for every sheet that is not the size of the guess.
  //
  // `let`, not `const`: setBody() below can swap in taller or shorter content after the sheet
  // is already open (readings.js's entry sheet does this to move from a chooser to a form
  // without a close-then-reopen jump-cut). Every use of `height` below reads it from this
  // closure, so reassigning it there is all a swap needs -- but a stale height left as `const`
  // would leave the sheet unable to fully leave the screen on close whenever the swapped-in
  // content is taller than what was measured at open.
  let height = sheet.offsetHeight;

  let closing = false;

  /* One spring drives BOTH the sheet's position and the scrim's opacity. They cannot drift
   * apart, and the dimming tracks a drag continuously rather than switching at a threshold --
   * which is what makes the scrim feel attached to the gesture rather than to the state. */
  const spring = new Spring({
    ...PRESETS.sheet,
    value: height, // off-screen
    onChange: (y) => {
      sheet.style.transform = `translate3d(0, ${y}px, 0)`;
      // 0 when fully open, 1 when fully dismissed.
      const progress = Math.min(1, Math.max(0, y / height));
      scrim.style.opacity = String(1 - progress);
    },
    onRest: (y) => {
      if (y >= height - 0.5 && closing) teardown();
    },
  });

  function teardown() {
    scrim.remove();
    sheet.remove();
    document.removeEventListener("keydown", handleKey);
    detachDrag();
    if (current === instance) current = null;
    onClose?.();
  }

  function close({ immediate = false, velocity } = {}) {
    closing = true;
    if (immediate) {
      teardown();
      return;
    }
    // Leaves along the path it entered (§7). Velocity is passed through when the user threw
    // it, so the dismissal continues at the speed of their finger.
    spring.to(height, velocity !== undefined ? { velocity } : undefined);
  }

  function handleKey(event) {
    if (event.key === "Escape") close();
  }

  /* --- the gesture ---------------------------------------------------------
   *
   * Attached to the whole sheet rather than only the grip, because a sheet that can only be
   * dragged by a 36px handle is a sheet most people never discover is draggable. `canStart`
   * is what makes that safe: when the body is scrolled, the drag belongs to the body.
   */
  let dragStart = 0;

  const detachDrag = verticalDrag(sheet, {
    canStart: (event) => {
      // A drag beginning inside the scrollable body only becomes a sheet drag when the body
      // is already at the top -- otherwise the user is scrolling content, and stealing that
      // gesture is the single most irritating thing a sheet can do.
      if (bodyNode.contains(event.target) && bodyNode.scrollTop > 0) return false;
      // Never hijack a press on a control.
      if (event.target.closest("button, input, select, textarea, a")) return false;
      return true;
    },
    onStart: () => {
      // Interruptibility: take over from wherever the sheet *currently is*, mid-flight or
      // at rest. Reading the target here instead of the live value is what causes a sheet
      // to jump under the finger when it is caught while animating.
      spring.stop();
      dragStart = spring.value;
    },
    onMove: (offset) => {
      let y = dragStart + offset;
      if (y < 0) {
        // Above the open position: resist progressively rather than clamping to 0 (§9).
        y = -rubberbandUp(-y, height);
      }
      // track() records the implied velocity as it goes, so the hand-off at release has a
      // real figure to use rather than recomputing one after the fact.
      spring.track(y, 0);
    },
    onEnd: ({ offset, velocity }) => {
      const released = dragStart + offset;

      // Where the flick was *going*, not where the finger let go (§6). This is what makes a
      // small fast movement throw the sheet instead of nudging it.
      const projected = released + project(velocity);

      if (shouldCommit(projected, velocity, height)) {
        close({ velocity });
      } else {
        // Snap home, carrying the release velocity so there is no seam between the drag and
        // the animation (§5).
        spring.to(0, { velocity });
      }
    },
  });

  function rubberbandUp(overshoot, dimension) {
    const constant = 0.55;
    return (overshoot * dimension * constant) / (dimension + constant * overshoot);
  }

  /**
   * Replace the sheet's content in place -- a multi-step flow inside ONE sheet (e.g. a choice
   * of buttons that reveals a form) rather than closing this sheet and opening another, which
   * would jump-cut with no slide-out (see the comment on `current.close({ immediate: true })`
   * above). The sheet stays exactly where it is; only what's inside it changes.
   */
  function setBody(newBody, newFooter = null) {
    bodyNode.replaceChildren(newBody);
    footerNode.replaceChildren(...(newFooter ? [newFooter] : []));
    // Reading offsetHeight forces the layout this replaceChildren just invalidated, so this
    // reflects the new content's real height, not the old one's.
    height = sheet.offsetHeight;

    const firstField = bodyNode.querySelector("input, select, textarea, button");
    firstField?.focus({ preventScroll: true });
  }

  scrim.addEventListener("click", () => close());
  document.addEventListener("keydown", handleKey);

  // Enter. The spring starts at `height` (off-screen) and is retargeted to 0 -- so the sheet
  // is grabbable from the very first frame of its entrance, not once it has arrived.
  spring.to(0);

  // Focus the first control so a keyboard user is inside the dialog immediately, and a phone
  // keyboard opens on the field they came here to fill in.
  const firstField = bodyNode.querySelector("input, select, textarea, button");
  firstField?.focus({ preventScroll: true });

  const instance = { close, element: sheet, setBody };
  current = instance;
  return instance;
}

/** Close whatever is open. Used by the router, so navigating away never strands a sheet. */
export function closeAnySheet() {
  current?.close({ immediate: true });
}
