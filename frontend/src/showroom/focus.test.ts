/* The push-in aims at the dispenser in whatever shape of window it is shown in. A wrong origin is
 * a camera that drifts past the pumps into the car park. */

import { describe, expect, it } from "vitest";
import { coverFocus } from "./focus";

const PHOTO = { width: 3000, height: 2002 };
const DISPENSER = { x: 0.57, y: 0.7 };

describe("coverFocus", () => {
  it("in a box the photograph's own shape, a point is just its fraction of the box", () => {
    const point = coverFocus({ width: 1500, height: 1001 }, PHOTO, DISPENSER);
    expect(point.x).toBeCloseTo(855);
    expect(point.y).toBeCloseTo(700.7);
  });

  it("on a wide monitor the sides fit and the top and bottom are cropped by object-position", () => {
    // 1440/3000 = 0.48 against 900/2002 = 0.4496: the box is relatively wider, so its width
    // decides the scale and the photograph overflows top and bottom.
    const point = coverFocus({ width: 1440, height: 900 }, PHOTO, DISPENSER, { x: 0.5, y: 0.38 });
    const drawnHeight = 2002 * 0.48;
    expect(point.x).toBeCloseTo(0.57 * 1440);
    expect(point.y).toBeCloseTo((900 - drawnHeight) * 0.38 + 0.7 * drawnHeight);
  });

  it("on a phone in portrait the height fits and the sides are cropped about the centre", () => {
    const point = coverFocus({ width: 390, height: 844 }, PHOTO, DISPENSER, { x: 0.5, y: 0.38 });
    const scale = 844 / 2002;
    const drawnWidth = 3000 * scale;
    expect(point.y).toBeCloseTo(0.7 * 844);
    expect(point.x).toBeCloseTo((390 - drawnWidth) / 2 + 0.57 * drawnWidth);
    // Slightly right of centre, as the dispenser is in the photograph.
    expect(point.x).toBeGreaterThan(195);
    expect(point.x).toBeLessThan(390);
  });
});
