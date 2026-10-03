/* A tick under the thumb when something irreversible lands (Phase 24 D5).
 *
 * Closing a shift, locking it, reconciling or finalising a day, recording a repayment: each is a
 * commitment with somebody's name on it, and on an Android phone a short vibration says "that
 * happened" to a hand that may not be looking at the screen. Eight milliseconds -- a tap, not a
 * buzz. iPhones do not expose vibration to the web, so there it is simply silent.
 *
 * Only on those commits, never on ordinary saves: a phone that ticks at everything teaches the
 * hand to ignore it. And not under reduced motion, which some people set precisely because they
 * want the phone to stop moving.
 */

import { motionAllowed } from "./preference";

export function commitTick(): void {
  if (!motionAllowed() || typeof navigator === "undefined" || typeof navigator.vibrate !== "function") return;
  navigator.vibrate(8);
}
