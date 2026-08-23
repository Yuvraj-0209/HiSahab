/* Sign in (CLAUDE.md §8).
 *
 * The one screen that exists outside the shell, because there is no role to build a tab bar
 * from until it succeeds.
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
 */

import { el, render } from "../dom.js";
import { signIn } from "../auth.js";
import { field, Form } from "../ui/field.js";
import { notify } from "../ui/toast.js";

export function renderLogin(container, { onSignedIn, outletName }) {
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

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: "Sign in",
    attrs: { type: "submit" },
  });

  const formNode = el("form", { className: "stack" }, [
    email,
    password,
    submit,
  ]);

  formNode.addEventListener("submit", async (event) => {
    // Every form in this app is submitted by fetch; index.html's CSP sets form-action 'none'
    // so a real form POST is a blocked request rather than a mysterious page reload.
    event.preventDefault();
    form.clearErrors();

    const values = form.values();
    if (!values.email || !values.password) {
      notify.warning("Enter your email and password.");
      return;
    }

    submit.disabled = true;
    submit.textContent = "Signing in…";

    try {
      await signIn(values.email.trim(), values.password);
      await onSignedIn();
    } catch (error) {
      // Supabase's own error, not the HiSahab envelope -- this call never touches our API.
      notify.error(
        error.status === 400
          ? "That email and password did not match."
          : (error.message ?? "Sign-in failed."),
      );
      submit.disabled = false;
      submit.textContent = "Sign in";
    }
  });

  render(
    container,
    el("main", { className: "screen stack" }, [
      el("div", { className: "col", style: { marginTop: "12vh", marginBottom: "2rem" } }, [
        el("h1", { className: "t-display", text: "HiSahab" }),
        el("p", {
          className: "t-caption",
          text: outletName ? `${outletName} · daily stock and cash flow` : "Daily stock and cash flow",
        }),
      ]),
      el("div", { className: "card" }, [formNode]),
    ]),
  );

  email._input.focus({ preventScroll: true });
}
