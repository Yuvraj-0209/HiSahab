/* The phone beside the sign-in card (Phase 28 D3): the Today screen at the end of the sample day,
 * so the first thing a buyer sees is the product rather than a password field.
 *
 * Loaded with the story, after the form has painted, and only drawn on a wide screen; on a phone
 * the form owns the first viewport (§14) and the story shows each screen in its turn.
 *
 * Motion, desktop only, transform and opacity only:
 *   - it rises into place when it arrives, which also hides that it arrived a moment late;
 *   - as the hero scrolls away it drifts up a little slower than the page and settles back,
 *     the Motion skill's "hero parallax layers" rebuilt in GSAP: the phone is a layer behind
 *     the words, leaving after them.
 * Two elements, one tween each (§14): the wrapper drifts, the frame inside it rises.
 */

import { useRef } from "react";
import { DURATION, EASE, gsap } from "../motion/gsap";
import { useStory } from "../motion/story";
import { MiniApp } from "./phone/MiniApp";
import { PhoneFrame } from "./phone/PhoneFrame";
import { TodayFocus } from "./phone/screens";
import { DAY } from "./samples";

export function HeroPhone() {
  const scope = useRef<HTMLDivElement>(null);

  useStory(scope, (desktop) => {
    if (!desktop) return;
    gsap.from("[data-rise]", { y: 56, opacity: 0, duration: DURATION.large, ease: EASE.enter.gsap });
    gsap.to("[data-drift]", {
      y: () => window.innerHeight * 0.14,
      scale: 0.96,
      ease: "none",
      scrollTrigger: { trigger: scope.current, start: "top top+=10%", end: "bottom top", scrub: 0.6, invalidateOnRefresh: true },
    });
  });

  return (
    <div ref={scope} className="flex flex-col items-center">
      <div data-drift>
        <div data-rise>
          <PhoneFrame size="hero" label={`The Today screen on ${DAY.date}: metered sales ${DAY.metered}, card ${DAY.cardTaken}, UPI ${DAY.upiTaken}, cash counted ${DAY.cashCounted}.`}>
            <MiniApp title="Today" subtitle={`${DAY.date} · shift 1`} tab="today">
              <TodayFocus />
            </MiniApp>
          </PhoneFrame>
        </div>
      </div>
      <p className="mt-4 text-[0.75rem] text-on-photo-muted">Sample figures</p>
    </div>
  );
}
