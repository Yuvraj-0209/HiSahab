/* Sign in, and the front door (CLAUDE.md §8, §13.28, §13.29, §14; Phase 24 D7).
 *
 * The one screen outside the shell -- there is no role to build a tab bar from until it
 * succeeds -- and the only one with no data on it, which is what lets it carry a photograph and,
 * below the sign-in card, the product's story for a pump owner deciding whether to buy it.
 *
 * Two failure modes get a real explanation, because they are different problems with different
 * fixes and a user cannot tell them apart:
 *
 *   - **Supabase rejected the credentials.** Deliberately not split into "wrong password" vs
 *     "no account": saying which half was right is how an account list gets enumerated.
 *   - **Supabase accepted them, but this outlet has no profile for you.** That is
 *     PROFILE_NOT_PROVISIONED from /me, explained by the boot sequence in App.tsx -- telling
 *     somebody "sign-in failed" there would send them to retype a password that was never wrong.
 *
 * ## The salesman comes first (§14)
 *
 * The sign-in card is inside the first viewport on a 390x844 phone, in both palettes, and
 * Playwright asserts it. The story below it is a separate chunk (src/showroom/), requested only
 * once the page is idle, so a salesman on Slow 4G gets the form without paying for the story --
 * and staff restored from a refresh token never see this screen at all.
 *
 * ## Motion
 *
 * The photograph breathes under a slow CSS Ken Burns drift. **Scrolling walks into the station**
 * (Phase 25 D1): the fixed photograph pushes in towards the dispenser while a cut-out of the
 * canopy and pillars, in front of it, grows faster and passes overhead -- two depths, so it reads
 * as moving under a roof rather than zooming into a picture. Nothing is pinned, so a phone's
 * collapsing address bar has nothing to fight; the hero simply scrolls away over the moving
 * photograph, and a line rises over the dispenser before the story begins.
 *
 * Each element owns one transform (§14): the drift wrapper, each depth layer, the headline words.
 * Nothing is redrawn to move (§13.29), no filter sits on anything that moves, and under reduced
 * motion the photograph holds still and every word is simply there.
 *
 * The drift stops once the walk begins (Phase 28 B3): two transforms breathing and pushing on one
 * photograph stack into a zoom nobody asked for. It resumes at the very top, where the photograph
 * is at rest again. The drift belongs to CSS; the walk only flips an attribute that pauses it.
 *
 * Nothing smooths the scroll (Phase 28 D2): ScrollSmoother moved this form, which §14 forbids.
 */

import { type FormEvent, lazy, Suspense, useEffect, useRef, useState } from "react";
import { SignInError, signIn } from "../auth/auth";
import { DURATION, EASE, gsap, useMotion } from "../motion/gsap";
import "../motion/scroll";
import { coverFocus } from "../showroom/focus";
import { Words } from "../showroom/Words";
import { useForm, TextField } from "../ui/form";
import { Button } from "../ui/primitives";
import { notify } from "../ui/toast";

const Showroom = lazy(() => import("../showroom/Showroom"));

/* A 20px copy of the photograph, so the first paint is a soft suggestion of the image rather
 * than a black rectangle. Smaller than the request it saves; the CSP allows `img-src data:`.
 * Set through the CSSOM, which `style-src 'self'` permits. */
const LQIP =
  "data:image/webp;base64,UklGRooAAABXRUJQVlA4IH4AAAAwBACdASoUAA0APu1iqk2ppaQiMAgBMB2JZAC7H8Agthf4leG0zc2DLygA/vQfsqKyhTa6bLFl2k" +
  "vub4xwuH/8ya/Eg0437N3miVGp9hsDsyUsW0gpGbDfn4hTaFWG/eV7QuAWA3XkLRMK3yB5UahDdRPROwYLeFACsr0AAAA=";

/* The push-in's aim: the central dispenser of the forecourt photograph (Adobe Stock 969116629,
 * 3000 x 2002), as fractions of the photograph, and the `object-position` it is drawn with. */
const PHOTO = { width: 3000, height: 2002 };
const DISPENSER = { x: 0.57, y: 0.7 };
const DRAWN_AT = { x: 0.5, y: 0.38 };
/** How far each depth has grown when the walk ends: the canopy, nearer, grows faster. */
const FAR_SCALE = 2.3;
const NEAR_SCALE = 3.4;
/** Where the dispenser ends up on screen, as a fraction of the window's height. */
const ARRIVE_AT = 0.56;

/** An element's distance from the top of the document, in layout px (unaffected by transforms,
 * which matters once the story's smooth scrolling is moving the content by transform). */
function documentTop(element: HTMLElement): number {
  let top = 0;
  for (let node: HTMLElement | null = element; node; node = node.offsetParent as HTMLElement | null) top += node.offsetTop;
  return top;
}

/** Ask for the story once the browser has nothing better to do. Safari has no idle callback, so
 * there it waits for a short beat after the first paint instead. */
function useWhenIdle(): boolean {
  const [idle, setIdle] = useState(false);
  useEffect(() => {
    if ("requestIdleCallback" in window) {
      const handle = window.requestIdleCallback(() => setIdle(true), { timeout: 1500 });
      return () => window.cancelIdleCallback(handle);
    }
    const timer = setTimeout(() => setIdle(true), 400);
    return () => clearTimeout(timer);
  }, []);
  return idle;
}

/** Whether to fetch the full-resolution photograph for the walk-in. Pushing in 2.3x magnifies
 * whatever was drawn, and the first paint deliberately draws a light file (the largest paint on
 * the page, on rural 4G). So the 3000 px original arrives afterwards, when the page is idle --
 * and not at all on data saver or a slow connection, where a softer walk is the right trade. */
function useFullResolution(ready: boolean): boolean {
  const connection = (navigator as Navigator & { connection?: { saveData?: boolean; effectiveType?: string } }).connection;
  const thrifty = Boolean(connection?.saveData) || ["slow-2g", "2g", "3g"].includes(connection?.effectiveType ?? "");
  return ready && !thrifty;
}

export function Login({ outletName, onSignedIn }: { outletName?: string | undefined; onSignedIn: () => Promise<void> }) {
  const form = useForm({ email: "", password: "" });
  const [busy, setBusy] = useState(false);
  // A failed attempt's error toast persists until dismissed, so it would otherwise survive into
  // a successful retry. Dismiss it the moment a new attempt starts.
  const pending = useRef<{ dismiss: () => void } | null>(null);
  const story = useWhenIdle();

  const scope = useRef<HTMLDivElement>(null);
  const drift = useRef<HTMLDivElement>(null);
  const far = useRef<HTMLDivElement>(null);
  const near = useRef<HTMLDivElement>(null);
  const wash = useRef<HTMLDivElement>(null);
  const travel = useRef<HTMLElement>(null);
  const [nearReady, setNearReady] = useState(false);
  const [sharpFar, setSharpFar] = useState(false);
  const [sharpNear, setSharpNear] = useState(false);
  const fullResolution = useFullResolution(story);

  useMotion(
    (play) =>
      play(() => {
        // Walking in: from the top of the page until the travel section has scrolled past, both
        // depths grow about the dispenser and carry it up towards the middle of the window. Scrubbed,
        // so the scroll *is* the camera; recomputed on resize, because where the dispenser sits on
        // screen depends on the window's shape (focus.ts).
        const focus = () => coverFocus({ width: window.innerWidth, height: window.innerHeight }, PHOTO, DISPENSER, DRAWN_AT);
        const origin = () => `${focus().x}px ${focus().y}px`;
        const lift = () => ARRIVE_AT * window.innerHeight - focus().y;
        const walk = gsap.timeline({
          defaults: { ease: "power1.inOut" },
          scrollTrigger: {
            trigger: document.documentElement,
            start: 0,
            end: () => (travel.current ? documentTop(travel.current) + travel.current.offsetHeight - window.innerHeight : window.innerHeight),
            scrub: 0.5,
            invalidateOnRefresh: true,
            // An attribute, written only when it changes: CSS owns the drift's transform.
            onUpdate: (self) => {
              const walking = self.progress > 0 ? "true" : "false";
              if (drift.current && drift.current.dataset.walking !== walking) drift.current.dataset.walking = walking;
            },
          },
        });
        walk
          .fromTo(far.current, { scale: 1, y: 0, transformOrigin: origin }, { scale: FAR_SCALE, y: lift, transformOrigin: origin, duration: 1 }, 0)
          .fromTo(near.current, { scale: 1, y: 0, transformOrigin: origin }, { scale: NEAR_SCALE, y: lift, transformOrigin: origin, duration: 1 }, 0)
          // The wash that seats the sign-in card on the ground colour lifts, so the forecourt is
          // fully lit as you walk into it.
          .to(wash.current, { opacity: 0, ease: "none", duration: 0.3 }, 0)
          .fromTo("[data-travel-line]", { opacity: 0, y: 28 }, { opacity: 1, y: 0, ease: EASE.enter.gsap, duration: 0.25 }, 0.62);
        // The headline arrives once, word by word: the first thing a visitor reads.
        gsap.from("[data-hero] [data-word]", { yPercent: 110, duration: DURATION.large, ease: EASE.enter.gsap, stagger: 0.07, delay: 0.15 });
        gsap.from("[data-hero-sub]", { opacity: 0, y: 12, duration: DURATION.large, ease: EASE.enter.gsap, delay: 0.45 });
      }),
    { scope },
  );

  async function submit(event: FormEvent) {
    // Every form is submitted by fetch; the CSP's `form-action 'none'` makes a real form POST a
    // blocked request rather than a mysterious page reload.
    event.preventDefault();
    form.clearErrors();
    pending.current?.dismiss();
    pending.current = null;

    const email = form.values.email.trim();
    if (!email || !form.values.password) {
      pending.current = notify.warning("Enter your email and password.");
      return;
    }

    setBusy(true);
    try {
      await signIn(email, form.values.password);
      await onSignedIn();
    } catch (error) {
      // Supabase's own error, not the HiSahab envelope: this call never touches our API.
      pending.current = notify.error(
        error instanceof SignInError && error.status === 400
          ? "That email and password did not match."
          : error instanceof Error
            ? error.message
            : "Sign-in failed.",
      );
      setBusy(false);
    }
  }

  return (
    <div ref={scope}>
      <div className="login-backdrop" aria-hidden="true">
        <div ref={drift} className="login-drift">
          <div ref={far} className="login-layer" style={{ backgroundImage: `url("${LQIP}")` }}>
            <img
              src="/img/forecourt-1280.webp"
              srcSet="/img/forecourt-1280.webp 1280w, /img/forecourt-2560.webp 2560w"
              sizes="100vw"
              alt=""
              decoding="async"
              fetchPriority="high"
            />
            {fullResolution ? (
              <img className="login-sharp" data-ready={sharpFar ? "true" : "false"} src="/img/forecourt-3000.webp" alt="" decoding="async" onLoad={() => setSharpFar(true)} />
            ) : null}
          </div>
          <div ref={near} className="login-layer login-near" data-ready={nearReady ? "true" : "false"}>
            <img
              src="/img/forecourt-near-1280.webp"
              srcSet="/img/forecourt-near-1280.webp 1280w, /img/forecourt-near-2560.webp 2560w"
              sizes="100vw"
              alt=""
              decoding="async"
              fetchPriority="low"
              onLoad={() => setNearReady(true)}
            />
            {fullResolution ? (
              <img className="login-sharp" data-ready={sharpNear ? "true" : "false"} src="/img/forecourt-near-3000.webp" alt="" decoding="async" onLoad={() => setSharpNear(true)} />
            ) : null}
          </div>
        </div>
        <div className="login-grade" />
        <div ref={wash} className="login-wash" />
      </div>

      <div className="relative z-10">
        {/* Absolute, not fixed: the wordmark is white on the photograph and belongs to it. Fixed,
         * it would ghost across the paper of the story below in the light palette. */}
        <header className="pointer-events-none absolute inset-x-0 top-0 z-20 flex justify-center pt-[calc(env(safe-area-inset-top)+1.25rem)]">
          <p className="wordmark text-[1.375rem] text-on-photo">HiSahab</p>
        </header>

        <section className="mx-auto flex min-h-[100dvh] max-w-md flex-col justify-end gap-5 px-4 pt-24 pb-8">
          <div data-hero className="text-center">
            <Words as="h1" text="The day's cash, checked against the meters." className="display block text-[clamp(2.25rem,9vw,3.25rem)] text-on-photo" />
            <p data-hero-sub className="mx-auto mt-3 max-w-[22rem] text-[1rem] leading-snug text-on-photo-muted">
              {outletName ? `${outletName}: readings` : "Readings"}, collections, udhaar and the bank statement, reconciled every night.
            </p>
          </div>
          <form
            onSubmit={submit}
            noValidate
            className="flex flex-col gap-4 rounded-[var(--radius-sheet)] border border-hairline bg-surface-raised p-5 shadow-3 sm:p-6"
          >
            <TextField form={form} name="email" label="Email" type="email" inputMode="email" autoComplete="username" required />
            <TextField form={form} name="password" label="Password" type="password" autoComplete="current-password" required />
            <Button type="submit" variant="primary" block disabled={busy}>
              {busy ? "Signing in…" : "Sign in"}
            </Button>
          </form>
        </section>

        {/* The walk in: a stretch of page with nothing on it but the forecourt moving beneath, and
         * one line that rises over the dispenser as you arrive. */}
        <section ref={travel} data-travel className="relative flex min-h-[120dvh] items-end justify-center px-6 pb-[14dvh] lg:min-h-[150dvh]">
          <p data-travel-line className="display travel-line max-w-[20ch] text-center text-[clamp(2rem,6vw,3.75rem)] text-on-photo">
            Every night, somebody reads this meter.
          </p>
        </section>

        {story ? (
          <Suspense fallback={null}>
            <Showroom outletName={outletName} />
          </Suspense>
        ) : null}
      </div>
    </div>
  );
}
