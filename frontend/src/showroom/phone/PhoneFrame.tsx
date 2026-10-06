/* A phone drawn around one of the app's screens (Phase 28 D4).
 *
 * The screen inside is laid out at a real phone's width, 390 CSS px, and drawn smaller with CSS
 * `zoom` rather than a `transform: scale()`. Zoom re-lays the text out at the smaller size, so it
 * stays crisp, and it leaves `transform` free for the story to animate (§14: one element, one
 * engine). The replicas are therefore built from the app's own primitives at their own sizes,
 * and look like the app because they are made of it.
 *
 * It is a picture, not a control: the screen is `inert` and hidden from assistive technology,
 * and the figure is named by a caption that says what the screen shows. §13.46 records what a
 * replica costs: when a real screen is redesigned, this one does not follow.
 */

import type { ReactNode } from "react";

export function PhoneFrame({
  label,
  size = "story",
  className = "",
  children,
}: {
  /** What the screen shows, for a screen reader: the screen itself is hidden from one. */
  label: string;
  /** `hero` beside the sign-in card, `story` pinned beside the steps. */
  size?: "hero" | "story";
  className?: string;
  children: ReactNode;
}) {
  return (
    <figure className={`phone phone-${size} ${className}`}>
      <div className="phone-bezel">
        <span className="phone-island" aria-hidden="true" />
        <div className="phone-screen" inert aria-hidden="true">
          {children}
        </div>
      </div>
      <figcaption className="sr-only-text">{label}</figcaption>
    </figure>
  );
}
