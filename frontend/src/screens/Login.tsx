/* Sign in (CLAUDE.md §8, §13.28, §13.29).
 *
 * The one screen outside the shell -- there is no role to build a tab bar from until it
 * succeeds -- and the only one with no data on it, which is what lets it carry a photograph.
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
 * ## Motion (Phase 23 D8)
 *
 * The photograph drifts under a CSS Ken Burns keyframe and moves under the scroll with a
 * scrubbed GSAP parallax on its *band* -- two elements, because one `transform` cannot be
 * driven from two places. Below the sign-in card, the two statements are the rules the system
 * exists to enforce; each rises in once as it arrives. Nothing is redrawn to move (§13.29), no
 * filter sits on anything that moves, and under reduced motion the photograph holds still and
 * the statements are simply there.
 */

import { type FormEvent, useRef, useState } from "react";
import { SignInError, signIn } from "../auth/auth";
import { DURATION, gsap, useMotion } from "../motion/gsap";
import "../motion/scroll";
import { useForm, TextField } from "../ui/form";
import { Button } from "../ui/primitives";
import { notify } from "../ui/toast";

const STATEMENTS = [
  {
    title: "Confirm the meter.",
    body:
      "Every opening reading is carried forward and shown, never assumed. Somebody has to say it " +
      "matches. A reading that disagrees becomes a question, before it becomes anyone's debt.",
  },
  {
    title: "The gap has a name.",
    body:
      "Expected cash comes from the meters. Counted cash is declared separately. When the two " +
      "differ, the difference is recorded rather than quietly corrected away.",
  },
];

/* A 20px blurred copy of the photograph, so the first paint is a soft suggestion of the image
 * rather than a black rectangle. Smaller than the request it saves; the CSP allows `img-src
 * data:`. Set through the CSSOM, which `style-src 'self'` permits. */
const LQIP =
  "data:image/webp;base64,UklGRlgAAABXRUJQVlA4IEwAAABQAwCdASoUAA0APu1mqk4ppaOiMAgBMB2J" +
  "QBOgBDuPXVN8gAD9PBPp1RfIybZ5wr7vY3mF3MMjvbf0SQhcOvC7DltObWAe/1CJWpAA";

export function Login({ outletName, onSignedIn }: { outletName?: string | undefined; onSignedIn: () => Promise<void> }) {
  const form = useForm({ email: "", password: "" });
  const [busy, setBusy] = useState(false);
  // A failed attempt's error toast persists until dismissed, so it would otherwise survive into
  // a successful retry. Dismiss it the moment a new attempt starts.
  const pending = useRef<{ dismiss: () => void } | null>(null);

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
        // Each statement rises in once, when it is genuinely on screen.
        gsap.utils.toArray<HTMLElement>("[data-statement]").forEach((section) => {
          gsap.from(section.querySelectorAll("[data-reveal]"), {
            opacity: 0,
            y: 28,
            duration: DURATION.large,
            ease: "power3.out",
            stagger: 0.12,
            scrollTrigger: { trigger: section, start: "top 72%", once: true },
          });
        });
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
            src="/img/city-1280.webp"
            srcSet="/img/city-1280.webp 1280w, /img/city-2560.webp 2560w"
            sizes="100vw"
            alt=""
            decoding="async"
            fetchPriority="high"
          />
        </div>
        <div className="login-grade" />
        <div className="login-wash" />
      </div>

      <div className="relative z-10">
        {/* Absolute, not fixed: the wordmark is white on the photograph and belongs to it. Fixed,
         * it would ghost across the paper of the statements below in the light palette. */}
        <header className="pointer-events-none absolute inset-x-0 top-0 z-20 flex justify-center pt-[calc(env(safe-area-inset-top)+1.25rem)]">
          <p className="wordmark text-[1.375rem] text-on-photo">HiSahab</p>
        </header>

        <section className="mx-auto flex min-h-[88dvh] max-w-md flex-col justify-end gap-5 px-4 pt-24 pb-10">
          <p className="text-center text-[1.0625rem] leading-snug text-on-photo-muted">
            {outletName ? `${outletName}, daily stock and cash flow` : "Daily stock and cash flow"}
          </p>
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

        {/* The statements sit on the palette's own ground, so their ink is legible in light and
         * in dark alike; the photograph belongs to the first screen only. */}
        <div className="relative bg-ground">
          {STATEMENTS.map((statement, index) => (
            <section
              key={statement.title}
              data-statement
              className={`mx-auto flex max-w-2xl flex-col justify-center gap-5 px-6 ${index === 0 ? "min-h-[70dvh] pt-20" : "min-h-[80dvh]"}`}
            >
              <h2 data-reveal className="display text-[clamp(2.75rem,9vw,4.75rem)] text-ink">
                {statement.title}
              </h2>
              <p data-reveal className="max-w-[34rem] text-[1.0625rem] leading-relaxed text-ink-muted">
                {statement.body}
              </p>
              {index === STATEMENTS.length - 1 ? (
                <footer data-reveal className="mt-10 flex flex-col gap-1 text-[0.8125rem] text-ink-faint">
                  <p>{outletName ? `${outletName}, HiSahab daily stock and cash flow` : "HiSahab daily stock and cash flow"}</p>
                  <p>Trouble signing in? Your outlet admin can check your account.</p>
                </footer>
              ) : null}
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}
