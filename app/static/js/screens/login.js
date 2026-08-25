/* Sign in (CLAUDE.md §8).
 *
 * The one screen that exists outside the shell, because there is no role to build a tab bar
 * from until it succeeds. It is also the only screen with no data on it, which is what lets
 * it carry a full-bleed drawing -- see js/backdrop/skyline.js.
 *
 * Two failure modes get a real explanation rather than a generic one, because they are
 * genuinely different problems with different fixes and a user cannot tell them apart:
 *
 *   - **Supabase rejected the credentials.** Wrong email or password. Deliberately not
 *     distinguished -- Supabase does not either, and saying which half was right is how an
 *     account list gets enumerated.
 *   - **Supabase accepted them, but this outlet has no profile for you.** `GET /api/v1/me`
 *     403s with PROFILE_NOT_PROVISIONED. The credentials are *correct*; an admin has to run
 *     app/jobs/provision_user.py. Telling somebody "sign-in failed" here would send them to
 *     retype a password that was never wrong.
 *
 * ## Three states, and why not more
 *
 * An earlier version had five things to scroll through. This has three, each a full
 * viewport with one idea in it. The two after the form are not a pitch -- nobody signing in
 * to a single-outlet internal tool needs persuading -- they are the two rules the whole
 * system is built to enforce, stated once each in the largest type on the screen.
 *
 * Entry is driven by ONE IntersectionObserver toggling a data attribute; the transition
 * itself is CSS. There is no scroll listener here at all, which is the difference between
 * text that arrives smoothly and text that arrives whenever the main thread is free.
 */

import { el, render } from "../dom.js";
import { signIn } from "../auth.js";
import { field, Form } from "../ui/field.js";
import { notify } from "../ui/toast.js";
import { mountSkyline } from "../backdrop/skyline.js";

const STATEMENTS = [
  {
    title: "Confirm the meter.",
    body:
      "Every opening reading is carried forward and shown, never assumed. Somebody has to "
      + "say it matches. A reading that disagrees becomes a question — before it becomes "
      + "anyone's debt.",
  },
  {
    title: "The gap has a name.",
    body:
      "Expected cash comes from the meters. Counted cash is declared separately. When the "
      + "two differ, the difference is recorded rather than quietly corrected away.",
  },
];

export function renderLogin(container, { onSignedIn, outletName }) {
  const backdropHost = document.getElementById("backdrop");
  // Defensive: signing out and back in must not leave two drawings stacked, each with its
  // own frame subscription.
  if (backdropHost) backdropHost.replaceChildren();
  const skyline = backdropHost ? mountSkyline(backdropHost) : null;

  const email = field({
    name: "email",
    label: "Email",
    type: "email",
    required: true,
    autocomplete: "username",
    inputMode: "email",
  });

  const password = field({
    name: "password",
    label: "Password",
    type: "password",
    required: true,
    autocomplete: "current-password",
  });

  const form = new Form({ email, password });

  // Error toasts persist until dismissed (toast.js) so a real failure doesn't vanish before
  // it's read. But that means a stale "wrong password" toast from a failed attempt survives
  // untouched into a *successful* retry -- nothing else here would ever clear it. Track it and
  // dismiss it the moment a new attempt starts.
  let pendingToast = null;
  function clearPendingToast() {
    if (pendingToast) pendingToast.dismiss();
    pendingToast = null;
  }

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: "Sign in",
    attrs: { type: "submit" },
  });

  const formNode = el("form", { className: "stack" }, [email, password, submit]);

  formNode.addEventListener("submit", async (event) => {
    // Every form in this app is submitted by fetch; index.html's CSP sets form-action 'none'
    // so a real form POST is a blocked request rather than a mysterious page reload.
    event.preventDefault();
    form.clearErrors();
    clearPendingToast();

    const values = form.values();
    if (!values.email || !values.password) {
      pendingToast = notify.warning("Enter your email and password.");
      return;
    }

    submit.disabled = true;
    submit.textContent = "Signing in…";

    try {
      await signIn(values.email.trim(), values.password);
      clearPendingToast();
      await onSignedIn();
    } catch (error) {
      // Supabase's own error, not the HiSahab envelope -- this call never touches our API.
      pendingToast = notify.error(
        error.status === 400
          ? "That email and password did not match."
          : (error.message ?? "Sign-in failed."),
      );
      submit.disabled = false;
      submit.textContent = "Sign in";
    }
  });

  const statements = STATEMENTS.map(({ title, body }) =>
    el("section", { className: "login-state" }, [
      el("h2", { className: "t-hero reveal", text: title }),
      el("p", { className: "t-lede reveal reveal-delay", text: body }),
    ]),
  );

  // The footer joins the last statement rather than becoming a fourth thing to scroll past.
  statements[statements.length - 1].appendChild(
    el("footer", { className: "login-foot stack reveal reveal-delay" }, [
      el("p", {
        className: "t-caption",
        text: outletName
          ? `${outletName} · HiSahab daily stock and cash flow`
          : "HiSahab daily stock and cash flow",
      }),
      el("p", {
        className: "t-caption",
        text: "Trouble signing in? Your outlet admin can check your account.",
      }),
    ]),
  );

  const cue = el("button", {
    className: "login-cue t-caption",
    text: "Read on  ↓",
    attrs: { type: "button" },
    on: {
      click: () => {
        // Honour reduced motion here too: a smooth scroll nobody asked for is exactly the
        // kind of movement that query is about.
        const reduced =
          typeof matchMedia === "function"
          && matchMedia("(prefers-reduced-motion: reduce)").matches;
        statements[0].scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "start" });
      },
    },
  });

  render(
    container,
    el("header", { className: "login-brand" }, [
      el("p", { className: "login-wordmark", text: "HiSahab" }),
    ]),
    el("main", { className: "screen login" }, [
      el("section", { className: "login-state login-state-signin" }, [
        el("p", {
          className: "t-lede",
          text: outletName ? `${outletName} · daily stock and cash flow` : "Daily stock and cash flow",
        }),
        el("div", { className: "card login-card" }, [formNode]),
        cue,
      ]),
      ...statements,
    ]),
  );

  // One observer for every revealing element. `threshold` is deliberately high enough that a
  // block commits only once it is genuinely on screen -- a fade that fires at 1% visibility
  // is over before the reader arrives.
  let observer = null;
  const revealing = Array.from(container.querySelectorAll(".reveal"));
  if (typeof IntersectionObserver === "function") {
    observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          entry.target.setAttribute("data-visible", "");
          // Unobserve on arrival: these are entrances, not a scrubbing effect, and leaving
          // them observed means the browser keeps testing them for the whole session.
          observer.unobserve(entry.target);
        }
      },
      { threshold: 0.35 },
    );
    for (const node of revealing) observer.observe(node);
  } else {
    // No observer: show everything rather than hiding the copy behind a missing feature.
    for (const node of revealing) node.setAttribute("data-visible", "");
  }

  email._input.focus({ preventScroll: true });

  // Returned, not optional. js/main.js calls this before it replaces #app with the shell --
  // without it the frame subscription and the observer outlive the screen.
  return {
    detach() {
      clearPendingToast();
      if (observer) observer.disconnect();
      if (skyline) skyline.detach();
      if (backdropHost) backdropHost.replaceChildren();
    },
  };
}
