/* The front door's typographic parts, shared by every section (Phase 24 D7, Phase 28). */

import type { ReactNode } from "react";
import { Words } from "./Words";

/** A section headline in the serif voice. Balanced, so it never strands its last word; its words
 * rise into place as it arrives (Showroom.tsx `revealHeadlines`). */
export function Headline({ text, className = "" }: { text: string; className?: string }) {
  return <Words as="h2" text={text} className={`serif block text-[clamp(2.5rem,7vw,4.5rem)] text-balance text-ink ${className}`} />;
}

export function Body({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <p data-reveal className={`mt-5 max-w-[34rem] text-lead leading-relaxed text-ink-muted ${className}`}>
      {children}
    </p>
  );
}

/** One of the page's two eyebrows (Phase 24 D7 allows at most two). */
export function Eyebrow({ children }: { children: ReactNode }) {
  return <p className="mb-4 text-caption font-semibold tracking-[0.12em] text-accent uppercase">{children}</p>;
}

/** Said beside every figure on this page: these are not anybody's real books (§14). */
export function SampleNote({ className = "" }: { className?: string }) {
  return <p className={`mt-3 text-caption text-ink-faint ${className}`}>Sample figures</p>;
}
