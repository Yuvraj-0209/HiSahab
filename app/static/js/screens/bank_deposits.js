/* Bank deposits: cash leaving the locker (CLAUDE.md §5.2, §6.4).
 *
 * Subtracted from expected closing, because the money is genuinely gone from the drawer.
 *
 * `business_date` is **not** a field on this form. §5.2 stores it on the row -- kept for the
 * reason `expected_closing` is kept, so the most-queried column is not one you can only reach
 * through a join -- but §3 rule 7 applies in full: "the server recomputes it from
 * shifts.business_date and refuses to take the client's word for it, so the two cannot
 * drift." BankDepositCreate forbids it outright, and sending one is a 422.
 *
 * Manager floor even for reads (§8: "Record bank deposits" is a manager row, and the read
 * follows it).
 */

import { el, empty, render } from "../dom.js";
import { api, explain, Submission, uploadReceipt } from "../api.js";
import { format } from "../money.js";
import { businessDate } from "../time.js";
import { field, Form } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { openReversalSheet, reversalBadge } from "../ui/reversal.js";
import { receiptButton } from "../ui/receipt.js";
import { notify } from "../ui/toast.js";
import { errorCard } from "./today.js";

export async function renderBankDeposits(container, { session, navigate, shiftId }) {
  const { shell } = session;
  shell.setTab("entry");
  shell.setTitle("Bank deposits");

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let page;
  let shift;
  try {
    [page, shift] = await Promise.all([
      api.get(`/shifts/${shiftId}/bank-deposits`),
      api.get(`/shifts/${shiftId}`),
    ]);
  } catch (error) {
    render(
      container,
      errorCard(error, () => renderBankDeposits(container, { session, navigate, shiftId })),
    );
    return;
  }

  const editable = shift.status === "open";
  const context = { session, container, navigate, shiftId, editable };

  shell.setActions(
    editable
      ? el("button", {
          className: "btn btn-primary",
          text: "Add",
          attrs: { type: "button" },
          on: { click: () => depositSheet(null, context) },
        })
      : null,
  );

  render(
    container,
    el("div", { className: "stack" }, [
      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "Deposited this shift" }),
        el("div", { className: "t-amount", text: format(page.total) }),
        el("p", {
          className: "t-caption",
          text: "Subtracted from what should be in the locker at the end of the day.",
        }),
      ]),

      page.items.length
        ? el(
            "div",
            { className: "stack" },
            page.items.map((deposit) => depositCard(deposit, context)),
          )
        : empty("No deposits recorded for this shift."),

      page.truncated
        ? el("div", {
            className: "truncation-notice t-caption",
            text: "This shift has more deposits than this view lists.",
          })
        : null,
    ]),
  );
}

function depositCard(deposit, context) {
  const live = !deposit.reverses_id && !deposit.is_reversed;

  return el("div", { className: "card stack" }, [
    el("div", { className: "row-between" }, [
      el("div", { className: "grow" }, [
        el("div", { className: "t-body", text: businessDate(deposit.business_date) }),
        deposit.bank_reference
          ? el("div", { className: "t-caption", text: `Reference ${deposit.bank_reference}` })
          : null,
      ]),
      el("div", { className: "row" }, [
        reversalBadge(deposit),
        el("span", { className: "t-title t-numeric", text: format(deposit.amount) }),
      ]),
    ]),
    deposit.reversal_reason
      ? el("p", { className: "t-caption", text: `Reason: ${deposit.reversal_reason}` })
      : null,
    el("div", { className: "row" }, [
      receiptButton(deposit.attachment_id, { label: "View slip" }),
      context.editable && live
        ? el("button", {
            className: "btn grow",
            text: "Edit",
            attrs: { type: "button" },
            on: { click: () => depositSheet(deposit, context) },
          })
        : null,
      live
        ? el("button", {
            className: "btn btn-danger",
            text: "Reverse",
            attrs: { type: "button" },
            on: {
              click: () =>
                openReversalSheet({
                  title: "Reverse deposit",
                  path: `/shifts/${context.shiftId}/bank-deposits/${deposit.id}/reversals`,
                  amount: deposit.amount,
                  description: deposit.bank_reference ?? "Bank deposit",
                  replacementText: {
                    name: "replacement_reference",
                    label: "Corrected reference (optional)",
                    value: deposit.bank_reference ?? "",
                  },
                  onDone: () => renderBankDeposits(context.container, context),
                }),
            },
          })
        : null,
    ]),
  ]);
}

function depositSheet(existing, context) {
  const amount = field({
    name: "amount",
    label: "Amount deposited",
    type: "number",
    step: "0.01",
    min: "0.01",
    value: existing?.amount ?? "",
    required: true,
    inputMode: "decimal",
  });

  const reference = field({
    name: "bank_reference",
    label: "Bank reference (optional)",
    value: existing?.bank_reference ?? "",
  });

  const form = new Form({ amount, bank_reference: reference });

  let attachmentId = existing?.attachment_id ?? null;
  const fileInput = el("input", {
    attrs: { type: "file", accept: "image/jpeg,image/png", id: "f-deposit-slip" },
    className: "field-input",
  });
  const slipStatus = el("p", { className: "t-caption" });

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    slipStatus.textContent = "Uploading…";
    try {
      const result = await uploadReceipt(file, context.shiftId);
      attachmentId = result.attachment_id;
      slipStatus.textContent = "Slip attached.";
    } catch (error) {
      slipStatus.textContent = "";
      notify.error(explain(error), { requestId: error.requestId });
    }
  });

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: existing ? "Save" : "Record deposit",
    attrs: { type: "button" },
  });

  const submission = existing
    ? null
    : new Submission("POST", `/shifts/${context.shiftId}/bank-deposits`);

  submit.addEventListener("click", async () => {
    form.clearErrors();
    submit.disabled = true;
    try {
      if (existing) {
        const changes = form.changes();
        if (Object.keys(changes).length === 0) {
          sheet.close();
          return;
        }
        await api.patch(`/shifts/${context.shiftId}/bank-deposits/${existing.id}`, changes);
      } else {
        const values = form.values();
        // No business_date: the server takes it from the shift and refuses a client value.
        const body = { amount: values.amount };
        if (values.bank_reference) body.bank_reference = values.bank_reference;
        if (attachmentId) body.attachment_id = attachmentId;
        await submission.run(body);
      }
      sheet.close();
      notify.success(existing ? "Updated." : "Deposit recorded.");
      renderBankDeposits(context.container, context);
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), {
          requestId: error.requestId,
          action:
            error.name === "NetworkError"
              ? { label: "Retry", onClick: () => submit.click() }
              : undefined,
        });
      }
    }
  });

  const sheet = openSheet({
    title: existing ? "Edit deposit" : "Record a deposit",
    body: el("div", { className: "stack" }, [
      ...form.nodes(),
      el("div", { className: "field" }, [
        el("span", { className: "field-label t-caption", text: "Deposit slip (optional)" }),
        existing ? el("p", { className: "t-caption", text: "The slip cannot be changed." }) : fileInput,
        slipStatus,
      ]),
      el("p", {
        className: "t-caption",
        text: "The business date comes from the shift — it is never typed, so the two cannot drift.",
      }),
    ]),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}
