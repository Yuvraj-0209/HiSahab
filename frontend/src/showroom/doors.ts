/* The front door's two ways around the page (Phase 28 D3): down to the story, and back up to the
 * sign-in form. Shared by the nav in Login.tsx and the closing section in the story, and kept
 * free of GSAP so the sign-in page can import it without paying for the story.
 *
 * **Buttons, never `href="#…"` anchors.** The app is hash-routed (§2), so `#how` is a route,
 * not a place on this page: the router would take the visitor away from the front door. A
 * structural test refuses the anchor (§14).
 */

import { motionAllowed } from "../motion/preference";

/** Where "Talk to us" goes: a build-time setting, because the owner has not chosen one yet
 * (WhatsApp, a phone number or an email). Without it there is no such button anywhere, rather
 * than a placeholder one. */
export const CONTACT: string | undefined = import.meta.env.VITE_SALES_CONTACT || undefined;

function behaviour(): ScrollBehavior {
  return motionAllowed() ? "smooth" : "auto";
}

/** Down to "A day at the pump". Until the story has loaded (it is requested once the page is
 * idle) the nearest thing is the end of the walk-in, where the story will begin. */
export function goToStory(): void {
  const target = document.querySelector<HTMLElement>('[data-section="how"]') ?? document.querySelector<HTMLElement>("[data-travel]");
  target?.scrollIntoView({ behavior: behaviour(), block: "start" });
}

/** Back to the top, with the cursor in the email field: the one thing a returning salesman
 * wants. Focus first without scrolling, so the field does not jump into view under a moving
 * page, then let the scroll carry the whole hero back. */
export function goToSignIn(): void {
  document.querySelector<HTMLInputElement>('input[autocomplete="username"]')?.focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: behaviour() });
}
