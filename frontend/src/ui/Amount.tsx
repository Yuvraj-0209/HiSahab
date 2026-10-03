/* A money figure, rendered as received -- and, when it changes after a save, rolled.
 *
 * **What moves is the characters of the server's own string, and nothing else** (§14: never
 * tween a money value). Counting ₹0 up to ₹1,23,456 would compute rupee figures in JavaScript
 * that never existed and put them on screen -- plausible for a few hundred milliseconds, which
 * is the worst kind of wrong. Here each character position is compared with the previous string,
 * aligned by place value from the right, and only the characters that differ slide in. No
 * character is ever derived from another; every glyph drawn is one the server sent.
 *
 * The first render never animates: a figure arriving with its screen is not news. Under
 * prefers-reduced-motion the new figure simply appears. The full string is always present for
 * a screen reader; the per-character spans are aria-hidden.
 */

import { useRef } from "react";
import { format, type Money } from "../lib/money";
import { DURATION, EASE, gsap, useMotion } from "../motion/gsap";

export interface AmountProps {
  value: Money | null | undefined;
  /** What null means here, in words. */
  absent?: string;
  sign?: boolean;
  className?: string;
}

export function Amount({ value, absent, sign = false, className = "" }: AmountProps) {
  const text = format(value, { ...(absent !== undefined ? { absent } : {}), sign });
  const isAbsent = value === null || value === undefined || String(value).trim() === "";
  const scope = useRef<HTMLSpanElement>(null);
  const previous = useRef<string | null>(null);

  useMotion(
    (play) => {
      const before = previous.current;
      previous.current = text;
      if (before === null || before === text || isAbsent) return;

      // Positions counted from the right, so a figure gaining a digit rolls only what changed.
      const changed: Element[] = [];
      const glyphs = scope.current?.querySelectorAll("[data-glyph]") ?? [];
      glyphs.forEach((glyph, index) => {
        const fromRight = text.length - 1 - index;
        if (before[before.length - 1 - fromRight] !== text[index]) changed.push(glyph);
      });
      if (changed.length === 0) return;

      play(() => {
        gsap.from(changed.reverse(), {
          yPercent: 80,
          opacity: 0,
          duration: DURATION.medium,
          ease: EASE.enter.gsap,
          stagger: 0.028,
        });
      });
    },
    { scope, dependencies: [text] },
  );

  if (isAbsent) {
    return <span className={`t-absent ${className}`}>{text}</span>;
  }

  return (
    <span ref={scope} className={`tabular inline-flex overflow-hidden align-baseline leading-[1.25] ${className}`}>
      <span className="sr-only-text">{text}</span>
      <span aria-hidden="true" className="inline-flex">
        {Array.from(text).map((glyph, index) => (
          // Keyed by place value and glyph, so a changed character is a new element.
          <span key={`${text.length - index}:${glyph}`} data-glyph className="inline-block">
            {glyph}
          </span>
        ))}
      </span>
    </span>
  );
}
