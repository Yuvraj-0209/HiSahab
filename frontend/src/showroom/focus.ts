/* Where a point of a photograph lands on screen, for the front door's push-in (Phase 25 D1).
 *
 * The backdrop photograph is drawn with `object-fit: cover`, so which part of it is visible --
 * and where the dispenser sits -- depends on the shape of the window: a phone in portrait shows a
 * narrow middle slice, a monitor shows nearly all of it. The push-in scales the photograph about
 * the dispenser, so the transform-origin has to be the dispenser's position *in this window*.
 * This is that calculation, kept pure so it can be tested. Pixels of geometry, never money.
 */

export interface Size {
  width: number;
  height: number;
}

/** A point as a fraction of the photograph (0..1 across, 0..1 down). */
export interface Point {
  x: number;
  y: number;
}

/**
 * The on-screen position, in px from the box's top-left, of `focus` in an image drawn into `box`
 * with `object-fit: cover` and the given `object-position` (fractions, 0.5 = centred).
 */
export function coverFocus(box: Size, image: Size, focus: Point, position: Point = { x: 0.5, y: 0.5 }): Point {
  const scale = Math.max(box.width / image.width, box.height / image.height);
  const drawnWidth = image.width * scale;
  const drawnHeight = image.height * scale;
  return {
    x: (box.width - drawnWidth) * position.x + focus.x * drawnWidth,
    y: (box.height - drawnHeight) * position.y + focus.y * drawnHeight,
  };
}
