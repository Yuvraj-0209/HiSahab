/* Direct manipulation: 1:1 tracking, velocity history, and the release decision.
 *
 * §2 of the design vocabulary: "touch and content should move together." When a finger drags
 * something it must stay glued to the finger, and it must respect **where it was grabbed** --
 * snapping the element's centre to the pointer breaks the illusion in the first frame.
 *
 * ## Why velocity comes from a history rather than the last event
 *
 * The naive way to compute release velocity is to subtract the previous pointermove from the
 * current one. That reads a single inter-frame delta, which on a real touchscreen is noisy
 * enough that a steady drag can report a velocity near zero -- and the flick then dies at the
 * release point instead of being thrown. Sampling over a short window (60ms below) smooths
 * that out while staying short enough to represent what the finger is doing *now* rather than
 * what it did at the start of the drag.
 *
 * ## Why pointer capture
 *
 * Without setPointerCapture, dragging past the element's bounds -- or off the edge of the
 * screen, which is exactly what a dismiss gesture does -- stops delivering events, and the
 * sheet freezes mid-flight still attached to a finger that is still moving. Capture routes
 * every subsequent event to the element that claimed the pointer until release.
 */

/** How far back to look when computing release velocity. */
const VELOCITY_WINDOW_MS = 60;

/** Movement before a drag is committed to, in px.
 *
 * §10: require a small threshold before claiming a direction, so a tap with a slightly
 * unsteady thumb is still a tap. Below this, the gesture has not decided what it is.
 */
export const DRAG_THRESHOLD = 10;

/** Tracks pointer samples and answers "how fast is it moving right now?". */
export class VelocityTracker {
  constructor() {
    this.samples = [];
  }

  add(position, time = performance.now()) {
    this.samples.push({ position, time });
    // Keep a little more than the window so there is always a sample old enough to measure
    // against, even when events arrive in a burst.
    const cutoff = time - VELOCITY_WINDOW_MS * 2;
    while (this.samples.length > 2 && this.samples[0].time < cutoff) {
      this.samples.shift();
    }
  }

  /** px per second, signed. Zero when there is nothing to measure. */
  velocity(now = performance.now()) {
    if (this.samples.length < 2) return 0;

    const newest = this.samples[this.samples.length - 1];
    // Oldest sample still inside the window; falls back to the first we have.
    let oldest = this.samples[0];
    for (const sample of this.samples) {
      if (now - sample.time <= VELOCITY_WINDOW_MS) {
        oldest = sample;
        break;
      }
    }

    const elapsed = (newest.time - oldest.time) / 1000;
    if (elapsed <= 0) return 0;

    return (newest.position - oldest.position) / elapsed;
  }

  reset() {
    this.samples = [];
  }
}

/**
 * Attach a vertical drag to an element.
 *
 * The handlers receive offsets already resolved against the grab point, so a caller never
 * has to think about where the finger landed -- only about how far it has moved since.
 *
 * @param {HTMLElement} element        the element that receives the pointer events.
 * @param {object}      handlers
 * @param {Function}    handlers.onStart  () => void
 * @param {Function}    handlers.onMove   (offset:number) => void   offset from the grab point
 * @param {Function}    handlers.onEnd    ({offset, velocity}) => void
 * @param {Function}    handlers.canStart (event) => boolean  veto, e.g. "only when scrolled
 *                                        to the top", which is how a sheet lets its own body
 *                                        scroll without stealing the gesture.
 * @returns {Function}  detach
 */
export function verticalDrag(
  element,
  { onStart, onMove, onEnd, canStart = () => true } = {},
) {
  const tracker = new VelocityTracker();

  let pointerId = null;
  let startY = 0;
  let committed = false;

  function handleDown(event) {
    // Ignore secondary buttons and multi-touch; this app has no two-finger gesture, and
    // trying to track a second pointer mid-drag produces a jump.
    if (pointerId !== null || (event.pointerType === "mouse" && event.button !== 0)) return;
    if (!canStart(event)) return;

    pointerId = event.pointerId;
    startY = event.clientY;
    committed = false;
    tracker.reset();
    tracker.add(event.clientY, event.timeStamp);
  }

  function handleMove(event) {
    if (event.pointerId !== pointerId) return;

    const offset = event.clientY - startY;
    tracker.add(event.clientY, event.timeStamp);

    if (!committed) {
      if (Math.abs(offset) < DRAG_THRESHOLD) return;
      committed = true;
      // Capture only once the gesture is genuinely a drag. Capturing on pointerdown would
      // swallow taps on anything inside the element -- every button in a sheet header.
      element.setPointerCapture(pointerId);
      onStart?.();
    }

    // Now that we own the gesture, stop the browser from also scrolling or
    // text-selecting with it.
    if (event.cancelable) event.preventDefault();
    onMove?.(offset);
  }

  function handleUp(event) {
    if (event.pointerId !== pointerId) return;

    const offset = event.clientY - startY;
    const velocity = tracker.velocity(event.timeStamp);

    if (element.hasPointerCapture?.(pointerId)) {
      element.releasePointerCapture(pointerId);
    }
    pointerId = null;

    // A gesture that never passed the threshold was a tap. Report nothing, so the click
    // handler underneath runs normally.
    if (committed) onEnd?.({ offset, velocity });
    committed = false;
  }

  // passive:false on move because it calls preventDefault once the drag is committed.
  element.addEventListener("pointerdown", handleDown);
  element.addEventListener("pointermove", handleMove, { passive: false });
  element.addEventListener("pointerup", handleUp);
  element.addEventListener("pointercancel", handleUp);

  return function detach() {
    element.removeEventListener("pointerdown", handleDown);
    element.removeEventListener("pointermove", handleMove);
    element.removeEventListener("pointerup", handleUp);
    element.removeEventListener("pointercancel", handleUp);
  };
}

/**
 * Decide, at release, whether a gesture committed or should snap back.
 *
 * **Velocity decides, not position** (§10, and the Quick Reference's "decide reverse vs.
 * commit: use velocity *sign*, not position"). A fast flick dismisses from anywhere, even
 * two pixels in -- which is what makes the gesture feel responsive rather than pedantic
 * about how far the user bothered to drag. Position is only consulted for a slow drag, where
 * there is no meaningful velocity to read intent from.
 *
 * @param offset          distance travelled from the grab point.
 * @param velocity        px/s at release.
 * @param distance        the full travel that counts as "all the way".
 * @param velocityCutoff  px/s above which velocity alone decides.
 */
export function shouldCommit(offset, velocity, distance, velocityCutoff = 350) {
  if (velocity > velocityCutoff) return true; // thrown in the dismiss direction
  if (velocity < -velocityCutoff) return false; // thrown back
  return offset > distance / 2; // slow drag: past halfway
}
