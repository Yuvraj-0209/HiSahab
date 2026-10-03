/* Direct manipulation: 1:1 tracking, velocity history, and the release decision (Phase 12,
 * ported in Phase 23 with behaviour unchanged).
 *
 * A dragged thing stays glued to the finger and respects **where it was grabbed** -- snapping
 * its edge to the pointer breaks the illusion in the first frame.
 *
 * Release velocity comes from a 60ms history, not the last event: a single inter-frame delta
 * on a real touchscreen is noisy enough that a steady drag can report ~0 and the flick dies at
 * the release point. Pointer capture is taken only once a drag is committed, so taps on the
 * controls inside a sheet still work, and dragging past the edge of the screen keeps
 * delivering events.
 */

const VELOCITY_WINDOW_MS = 60;

/** Movement before a drag is committed to, in px: a tap with an unsteady thumb is still a tap. */
export const DRAG_THRESHOLD = 10;

interface Sample {
  position: number;
  time: number;
}

export class VelocityTracker {
  private samples: Sample[] = [];

  add(position: number, time: number = performance.now()): void {
    this.samples.push({ position, time });
    const cutoff = time - VELOCITY_WINDOW_MS * 2;
    while (this.samples.length > 2 && (this.samples[0]?.time ?? time) < cutoff) this.samples.shift();
  }

  /** px per second, signed. Zero when there is nothing to measure. */
  velocity(now: number = performance.now()): number {
    if (this.samples.length < 2) return 0;
    const newest = this.samples[this.samples.length - 1] as Sample;
    let oldest = this.samples[0] as Sample;
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

  reset(): void {
    this.samples = [];
  }
}

export interface DragHandlers {
  onStart?: () => void;
  /** Offset from the grab point, in px. */
  onMove?: (offset: number) => void;
  onEnd?: (release: { offset: number; velocity: number }) => void;
  /** Veto, e.g. "only when the body is scrolled to the top". */
  canStart?: (event: PointerEvent) => boolean;
}

/** Attach a vertical drag to an element. Returns `detach`. */
export function verticalDrag(element: HTMLElement, handlers: DragHandlers = {}): () => void {
  const { onStart, onMove, onEnd, canStart = () => true } = handlers;
  const tracker = new VelocityTracker();
  let pointerId: number | null = null;
  let startY = 0;
  let committed = false;

  function handleDown(event: PointerEvent) {
    // One pointer only: this app has no two-finger gesture, and a second pointer mid-drag jumps.
    if (pointerId !== null || (event.pointerType === "mouse" && event.button !== 0)) return;
    if (!canStart(event)) return;
    pointerId = event.pointerId;
    startY = event.clientY;
    committed = false;
    tracker.reset();
    tracker.add(event.clientY, event.timeStamp);
  }

  function handleMove(event: PointerEvent) {
    if (event.pointerId !== pointerId) return;
    const offset = event.clientY - startY;
    tracker.add(event.clientY, event.timeStamp);
    if (!committed) {
      if (Math.abs(offset) < DRAG_THRESHOLD) return;
      committed = true;
      element.setPointerCapture(event.pointerId);
      onStart?.();
    }
    if (event.cancelable) event.preventDefault();
    onMove?.(offset);
  }

  function handleUp(event: PointerEvent) {
    if (event.pointerId !== pointerId) return;
    const offset = event.clientY - startY;
    const velocity = tracker.velocity(event.timeStamp);
    if (element.hasPointerCapture?.(event.pointerId)) element.releasePointerCapture(event.pointerId);
    pointerId = null;
    // Never past the threshold: a tap. Report nothing, so the click underneath runs normally.
    if (committed) onEnd?.({ offset, velocity });
    committed = false;
  }

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

/** At release: commit or snap back? **Velocity decides, not position** -- a fast flick
 * dismisses from two pixels in; position only decides a slow drag. */
export function shouldCommit(offset: number, velocity: number, distance: number, velocityCutoff = 350): boolean {
  if (velocity > velocityCutoff) return true;
  if (velocity < -velocityCutoff) return false;
  return offset > distance / 2;
}
