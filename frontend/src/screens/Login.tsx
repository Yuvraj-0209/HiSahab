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
 * The photograph drifts under a CSS Ken Burns keyframe and moves under the scroll with a
 * scrubbed GSAP parallax on its *band* -- two elements, because one `transform` cannot be
 * driven from two places. The headline's words rise once on arrival. Nothing is redrawn to move
 * (§13.29), no filter sits on anything that moves, and under reduced motion the photograph holds
 * still and the words are simply there.
 *
 * `#smooth-wrapper` / `#smooth-content` are where the story's ScrollSmoother attaches on
 * desktop; until then (and on a phone, always) they are plain blocks. The backdrop stays outside
 * them, because a fixed element inside smoothed content would scroll with it.
 */

import { type FormEvent, lazy, Suspense, useEffect, useRef, useState } from "react";
import { SignInError, signIn } from "../auth/auth";
import { DURATION, EASE, gsap, useMotion } from "../motion/gsap";
import "../motion/scroll";
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

export function Login({ outletName, onSignedIn }: { outletName?: string | undefined; onSignedIn: () => Promise<void> }) {
  const form = useForm({ email: "", password: "" });
  const [busy, setBusy] = useState(false);
  // A failed attempt's error toast persists until dismissed, so it would otherwise survive into
  // a successful retry. Dismiss it the moment a new attempt starts.
  const pending = useRef<{ dismiss: () => void } | null>(null);
  const story = useWhenIdle();

  const scope = useRef<HTMLDivElement>(null);
  const band = useRef<HTMLDivElement>(null);

  useMotion(
    (play) =>
      play(() => {
        // The photograph moves slower than the page over it.
        gsap.to(band.current, {
          yPercent: -6,
          ease: "none",
          scrollTrigger: { trigger: document.documentElement, start: "top top", end: "bottom bottom", scrub: true },
        });
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
        <div ref={band} className="login-band" style={{ backgroundImage: `url("${LQIP}")` }}>
          <img
            className="login-plate"
            src="/img/forecourt-1280.webp"
            srcSet="/img/forecourt-1280.webp 1280w, /img/forecourt-2560.webp 2560w"
            sizes="100vw"
            alt=""
            decoding="async"
            fetchPriority="high"
          />
        </div>
        <div className="login-grade" />
        <div className="login-wash" />
      </div>

      <div id="smooth-wrapper">
        <div id="smooth-content" className="relative z-10">
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

          {story ? (
            <Suspense fallback={null}>
              <Showroom outletName={outletName} />
            </Suspense>
          ) : null}
        </div>
      </div>
    </div>
  );
}
