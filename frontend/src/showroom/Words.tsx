/* A headline whose words can rise one by one (Phase 24 D7, D9).
 *
 * GSAP's SplitText would do this, and was rejected for a React reason rather than a CSP one: it
 * replaces a heading's text nodes with spans of its own and restores the original by assigning
 * `innerHTML` -- inside DOM that React believes it owns. Here React renders the spans itself:
 * each word sits in a clipping box, and the story slides the inner span up into it. Nothing is
 * measured, nothing is re-parsed, and the full sentence is one text node for a screen reader.
 */

import type { ElementType } from "react";

export function Words({ text, as: Tag = "span", className = "" }: { text: string; as?: ElementType; className?: string }) {
  const words = text.split(" ");
  return (
    <Tag className={className}>
      <span className="sr-only-text">{text}</span>
      <span aria-hidden="true">
        {words.map((word, index) => (
          // A clipping box per word; the descender reserve keeps "g", "y" and "p" whole.
          <span key={index} className="inline-block overflow-hidden pb-[0.12em] align-top">
            <span data-word className="inline-block">
              {word}
            </span>
            {index < words.length - 1 ? " " : null}
          </span>
        ))}
      </span>
    </Tag>
  );
}
