/* The day's lifecycle as five dots: Entered, Closed, Locked, Reconciled, Finalised (Phase 15).
 *
 * Moved here from screens/days.tsx in Phase 28 so the front door's phone can draw the same strip
 * the Cash tab does, without importing a whole screen and its queries into the front door's
 * chunk. It takes only the two fields it reads, so a sample day can pass literals.
 */

import { useRef } from "react";
import { type DayState, LIFECYCLE } from "../lib/days";
import { DURATION, EASE, gsap, useMotion } from "../motion/gsap";

/** Five dots, filled up to `reached`. When a day moves on, each newly reached step's connector
 * draws in from the dot before it and then the dot lands -- a state transition the reader just
 * caused, told in the order it happened. */
export function LifecycleStrip({ state }: { state: Pick<DayState, "reached" | "label"> }) {
  const scope = useRef<HTMLDivElement>(null);
  const previous = useRef(state.reached);

  useMotion(
    (play) => {
      const before = previous.current;
      previous.current = state.reached;
      if (state.reached <= before) return;
      play(() => {
        // Each newly reached step: its connector draws from the previous dot, then the dot lands.
        const links = Array.from(scope.current?.querySelectorAll("[data-link]") ?? []);
        const dots = Array.from(scope.current?.querySelectorAll("[data-step]") ?? []);
        const timeline = gsap.timeline();
        for (let index = before; index < state.reached; index += 1) {
          const link = links[index - 1]; // the connector before step `index`; step 0 has none
          const dot = dots[index];
          if (link) timeline.from(link, { scaleX: 0, transformOrigin: "0% 50%", duration: DURATION.small, ease: EASE.move.gsap });
          if (dot) timeline.from(dot, { scale: 0.3, opacity: 0, duration: DURATION.small, ease: EASE.settle.gsap }, "<0.12");
        }
      });
    },
    { dependencies: [state.reached], scope },
  );

  return (
    <div ref={scope} className="flex flex-wrap items-center gap-x-3 gap-y-1" aria-label={`${state.label}: step ${state.reached} of ${LIFECYCLE.length}`}>
      <div className="flex items-center" aria-hidden="true">
        {LIFECYCLE.map((step, index) => (
          <span key={step} className="flex items-center">
            {index > 0 ? <span data-link className={`h-0.5 w-3 ${index < state.reached ? "bg-accent" : "bg-hairline-strong"}`} /> : null}
            <span className="relative grid size-3 place-items-center rounded-full border border-hairline-strong" title={step}>
              {index < state.reached ? <span data-step className="absolute inset-[-1px] rounded-full bg-accent" /> : null}
            </span>
          </span>
        ))}
      </div>
      <span className="text-footnote whitespace-nowrap text-ink-muted">{state.label}</span>
    </div>
  );
}
