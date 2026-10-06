/* A block whose `[data-arrive]` children arrive once, in reading order (Phase 28 D6).
 *
 * `useArrival` (ui/motion.ts) as a component, so a screen joins the arrival choreography by
 * wrapping its content rather than threading a ref through it. Twelve screens had no arrival at
 * all, and the Admin cards carried `data-arrive` that nothing ever read. The rules are
 * useArrival's: once per mount, never on a refetch, the first eight items, transform and
 * opacity only, nothing under reduced motion.
 *
 * Mark whole blocks, never the rows of a list that `useFlipList` moves: that list arrives as one
 * block, and Flip keeps the rows (§14, one element one engine at a time).
 */

import { type ReactNode, useRef } from "react";
import { useArrival } from "./motion";

export function Arrive({
  ready = true,
  items = "marked",
  className = "",
  children,
}: {
  ready?: boolean;
  /** `marked`: the `[data-arrive]` elements inside. `children`: this block's own children, so a
   * screen's column of cards arrives card by card without marking each one. */
  items?: "marked" | "children";
  className?: string;
  children: ReactNode;
}) {
  const scope = useRef<HTMLDivElement>(null);
  useArrival(scope, ready, items === "children" ? ":scope > *" : "[data-arrive]");
  return (
    <div ref={scope} className={className}>
      {children}
    </div>
  );
}
