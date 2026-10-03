/* A checkbox whose tick draws itself (Phase 24 D5).
 *
 * The box that matters most in this app is §4.7's "the meter reads exactly this" confirmation:
 * the one place a person asserts they looked at a physical meter. When they tick it, the tick
 * *draws* -- a short stroke from the corner -- because the motion is saying "you did that", and a
 * box that silently flips colour says nothing.
 *
 * The rule from §14 that matters more than the motion: **nothing animates on the first render.**
 * A tick that drew itself as a sheet opened would look exactly like a pre-ticked box, which is
 * the one thing this control must never resemble. So the drawing runs only on a change the
 * person made, from unticked to ticked; unticking simply clears it.
 *
 * The input is the real, native checkbox (keyboard, screen readers and `check()` in tests all use
 * it) with its default look removed; the stroke is a two-point polyline laid over it. That is a
 * single geometric mark, not an icon, which is why it is drawn here rather than taken from the
 * icon set: an icon's filled outline cannot be drawn as a stroke.
 */

import { type InputHTMLAttributes, useRef } from "react";
import { DURATION, EASE, gsap, useMotion } from "../motion/gsap";
import "../motion/draw";

export function Tick({ checked, className = "", ...input }: InputHTMLAttributes<HTMLInputElement> & { checked: boolean }) {
  const scope = useRef<HTMLSpanElement>(null);
  const previous = useRef<boolean | null>(null);

  useMotion(
    (play) => {
      const before = previous.current;
      previous.current = checked;
      if (before === null || before || !checked) return; // first render, or not a fresh tick
      play(() => {
        gsap.fromTo("polyline", { drawSVG: "0%" }, { drawSVG: "100%", duration: DURATION.medium, ease: EASE.enter.gsap });
      });
    },
    { scope, dependencies: [checked] },
  );

  return (
    <span ref={scope} className={`relative grid size-5 shrink-0 place-items-center ${className}`}>
      <input
        {...input}
        type="checkbox"
        checked={checked}
        className="peer size-5 cursor-pointer appearance-none rounded-[6px] border-[1.5px] border-hairline-strong bg-surface-raised transition-colors duration-150 checked:border-accent checked:bg-accent disabled:cursor-not-allowed disabled:opacity-45"
      />
      <svg viewBox="0 0 20 20" aria-hidden="true" className="pointer-events-none invisible absolute inset-0 size-5 text-on-accent peer-checked:visible">
        <polyline points="5.5 10.5 8.75 13.75 14.75 6.75" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </span>
  );
}
