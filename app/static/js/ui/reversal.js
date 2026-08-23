/* §6.9's correction path, written once.
 *
 * "No UPDATE and no DELETE on financial rows in a closed or locked shift. Corrections create
 * a reversal entry: a new row with the negated amount, a `reverses_id` FK to the original,
 * and a mandatory reason. Both rows remain visible."
 *
 * Six tables carry this shape identically -- collections, expenses, credit sales, credit
 * repayments, non-fuel sales, bank deposits, plus shortfalls and their settlements -- and the
 * request body is the same three fields every time: a mandatory reason, an optional
 * replacement amount, and sometimes one optional replacement text field. Writing the sheet
 * once is the difference between eight consistent correction flows and eight that drift.
 *
 * ## Why the replacement is offered in the same sheet
 *
 * A correction is almost never "cancel this". It is "this said ₹600 and it should have said
 * ₹800", which is a reversal *plus* a replacement -- two rows, one intent. Making the user
 * reverse and then remember to re-enter invites the second half being forgotten, and a
 * cancelled expense that was meant to be corrected is a silent ₹800 hole.
 *
 * The API already understands this: every reversal endpoint takes an optional
 * `replacement_amount` and creates both rows in one transaction, returning
 * {reversal, replacement, original}.
 *
 * ## What this deliberately does not do
 *
 * It does not ask for a receipt. §6.11: "A reversal never needs a receipt. A reversal row is
 * a cancellation, not a spend; there is nothing to photograph." Its replacement inherits the
 * original's attachment rather than demanding a second photo of the same paper (§5.3).
 */

import { el } from "../dom.js";
import { api, explain, Submission } from "../api.js";
import { field, Form } from "./field.js";
import { openSheet } from "./sheet.js";
import { notify } from "./toast.js";
import { format } from "../money.js";

/**
 * Open the reversal sheet for one row.
 *
 * @param {object}   options
 * @param {string}   options.title          e.g. "Reverse collection"
 * @param {string}   options.path           the /reversals endpoint
 * @param {string}   options.amount         the original's amount, for display
 * @param {string}   [options.description]  what is being reversed, in words
 * @param {object}   [options.replacementText]  {name, label, value} for the one extra field
 *                   an endpoint accepts (replacement_reference, replacement_paid_to,
 *                   replacement_description)
 * @param {Function} options.onDone
 */
export function openReversalSheet({
  title,
  path,
  amount,
  description = null,
  replacementText = null,
  onDone,
}) {
  const reason = field({
    name: "reason",
    label: "Why is this being reversed?",
    required: true,
    hint: "Mandatory, and stored on the row itself — so this correction can never be an unexplained figure. 3–500 characters.",
  });

  const replacementAmount = field({
    name: "replacement_amount",
    label: "Corrected amount (optional)",
    type: "number",
    step: "0.01",
    min: "0",
    inputMode: "decimal",
    hint: "Leave blank to cancel outright. Enter a figure to cancel and re-record in one step.",
  });

  const fields = { reason, replacement_amount: replacementAmount };

  if (replacementText) {
    fields[replacementText.name] = field({
      name: replacementText.name,
      label: replacementText.label,
      value: replacementText.value ?? "",
      hint: "Used only when a corrected amount is entered.",
    });
  }

  const form = new Form(fields);

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: "Record reversal",
    attrs: { type: "button" },
  });

  // Minted once, here, and reused by every retry. §6.10: a key per fetch call would make a
  // timeout-then-retry create two reversals, and a double reversal is exactly what the
  // uq_<table>_reverses_id constraint exists to lose loudly.
  const submission = new Submission("POST", path);

  submit.addEventListener("click", async () => {
    form.clearErrors();
    const values = form.values();

    if (!values.reason || values.reason.trim().length < 3) {
      form.showErrors([
        { loc: ["body", "reason"], msg: "A reason is required — at least 3 characters." },
      ]);
      return;
    }

    const body = { reason: values.reason.trim() };
    if (values.replacement_amount) {
      body.replacement_amount = values.replacement_amount;
      if (replacementText && values[replacementText.name]) {
        body[replacementText.name] = values[replacementText.name];
      }
    }

    submit.disabled = true;
    submit.textContent = "Recording…";
    try {
      const result = await submission.run(body);
      sheet.close();
      notify.success(
        result?.replacement
          ? "Reversed and re-recorded. Both rows stay in the trail."
          : "Reversed. The original row stays visible.",
      );
      onDone?.();
    } catch (error) {
      submit.disabled = false;
      submit.textContent = "Record reversal";
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), {
          requestId: error.requestId,
          // The retry reuses the same submission and therefore the same key, so pressing it
          // after a network failure cannot create a second reversal.
          action:
            error.name === "NetworkError"
              ? { label: "Retry", onClick: () => submit.click() }
              : undefined,
        });
      }
    }
  });

  const sheet = openSheet({
    title,
    body: el("div", { className: "stack" }, [
      el("div", { className: "card stack" }, [
        el("p", { className: "t-micro", text: "Reversing" }),
        el("p", { className: "t-title t-numeric", text: format(amount) }),
        description ? el("p", { className: "t-caption", text: description }) : null,
      ]),
      el("p", {
        className: "t-caption",
        text: "Nothing is deleted. A negative row is added pointing back at the original, and both stay on the record.",
      }),
      ...form.nodes(),
    ]),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}

/** The badge shown on a row that has been reversed, or that is itself a reversal.
 *
 * Both stay visible (§6.9), so both need to be legible at a glance -- otherwise a reader
 * scanning a list sees two amounts and has to work out which one still counts.
 */
export function reversalBadge(row) {
  if (row.reverses_id) {
    return el("span", { className: "pill pill-neutral", text: "reversal" });
  }
  if (row.is_reversed) {
    return el("span", { className: "pill pill-review", text: "reversed" });
  }
  return null;
}
