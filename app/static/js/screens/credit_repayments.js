/* Udhaar repaid (CLAUDE.md §4.4, §5.1, §6.4).
 *
 * §4.4: not all cash comes from fuel. A customer settling an old bill brings money with no
 * corresponding sale on the day it arrives -- "if repayments are not modelled, expected cash
 * is wrong every time someone settles up."
 *
 * ## A deactivated customer can still repay, and that is deliberate
 *
 * §5.1: "You deactivate somebody precisely to stop the debt growing while they pay off what
 * they owe; refusing their money would be backwards, and would leave a balance nothing can
 * ever clear." So this screen lists inactive customers -- marked as such -- while the credit
 * *sale* screen does not. The asymmetry is the rule, not an inconsistency to tidy up.
 *
 * ## Only cash repayments reach the drawer
 *
 * §5.2 and §6.4: a customer settling by bank transfer moves no money through the locker, and
 * adding it to expected cash would invent a shortfall on the day they pay. The mode select
 * says so, because "why didn't my UPI repayment change the cash position" is otherwise a
 * question somebody has to ask.
 *
 * `credit_repayment_mode` is its own Postgres type -- cash, card, upi, bank_transfer -- and
 * notably has no `wallet`, unlike collections. Sharing an enum would have forced a later
 * phase to carry a value meaningless to one of its users (§5.2).
 */

import { el, empty, pill, render } from "../dom.js";
import { api, explain, Submission, uploadReceipt } from "../api.js";
import { format } from "../money.js";
import { field, Form, select } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { openReversalSheet, reversalBadge } from "../ui/reversal.js";
import { notify } from "../ui/toast.js";
import { satisfies } from "../ui/nav.js";
import { errorCard } from "./today.js";

const MODES = [
  { value: "cash", label: "Cash — into the locker" },
  { value: "card", label: "Card" },
  { value: "upi", label: "UPI" },
  { value: "bank_transfer", label: "Bank transfer" },
];

export async function renderCreditRepayments(container, { session, navigate, shiftId }) {
  const { shell } = session;
  shell.setTab("entry");
  shell.setTitle("Repayments");

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let page;
  let shift;
  let customers;
  try {
    [page, shift, customers] = await Promise.all([
      api.get(`/shifts/${shiftId}/credit-repayments`),
      api.get(`/shifts/${shiftId}`),
      api.get("/credit-customers", { include_inactive: true }),
    ]);
  } catch (error) {
    render(
      container,
      errorCard(error, () => renderCreditRepayments(container, { session, navigate, shiftId })),
    );
    return;
  }

  const editable = shift.status === "open";
  const context = { session, container, navigate, shiftId, editable, customers };
  const byId = new Map(customers.map((customer) => [customer.id, customer]));

  shell.setActions(
    editable
      ? el("button", {
          className: "btn btn-primary",
          text: "Add",
          attrs: { type: "button" },
          on: { click: () => repaymentSheet(null, context) },
        })
      : null,
  );

  render(
    container,
    el("div", { className: "stack" }, [
      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "Repaid this shift" }),
        el("div", { className: "t-amount", text: format(page.total) }),
        el("div", { className: "list" }, [
          el("div", { className: "list-row" }, [
            el("div", { className: "list-row-main t-body", text: "Of which cash" }),
            el("div", { className: "list-row-value t-body t-numeric", text: format(page.cash_total) }),
          ]),
        ]),
        el("p", {
          className: "t-caption",
          text: "Only the cash figure reaches the drawer. A bank transfer is recorded but never added to expected cash.",
        }),
      ]),

      page.items.length
        ? el(
            "div",
            { className: "stack" },
            page.items.map((repayment) => repaymentCard(repayment, byId, context)),
          )
        : empty("No repayments recorded for this shift."),

      page.truncated
        ? el("div", {
            className: "truncation-notice t-caption",
            text: "This shift has more repayments than this view lists.",
          })
        : null,
    ]),
  );
}

function repaymentCard(repayment, byId, context) {
  const customer = byId.get(repayment.credit_customer_id);
  const live = !repayment.reverses_id && !repayment.is_reversed;

  return el("div", { className: "card stack" }, [
    el("div", { className: "row-between" }, [
      el("div", { className: "grow" }, [
        el("div", { className: "t-headline", text: customer?.name ?? "Unknown customer" }),
        el("div", { className: "t-caption", text: repayment.mode.replace("_", " ") }),
      ]),
      el("div", { className: "row" }, [
        reversalBadge(repayment),
        el("span", { className: "t-title t-numeric", text: format(repayment.amount) }),
      ]),
    ]),
    repayment.reversal_reason
      ? el("p", { className: "t-caption", text: `Reason: ${repayment.reversal_reason}` })
      : null,
    el("div", { className: "row" }, [
      context.editable && live
        ? el("button", {
            className: "btn grow",
            text: "Edit",
            attrs: { type: "button" },
            on: { click: () => repaymentSheet(repayment, context) },
          })
        : null,
      live && satisfies(context.session.me.role, "manager")
        ? el("button", {
            className: "btn btn-danger",
            text: "Reverse",
            attrs: { type: "button" },
            on: {
              click: () =>
                openReversalSheet({
                  title: "Reverse repayment",
                  path: `/shifts/${context.shiftId}/credit-repayments/${repayment.id}/reversals`,
                  amount: repayment.amount,
                  description: customer?.name ?? "Repayment",
                  onDone: () => renderCreditRepayments(context.container, context),
                }),
            },
          })
        : null,
    ]),
  ]);
}

function repaymentSheet(existing, context) {
  const { customers } = context;

  const customerSelect = select({
    name: "credit_customer_id",
    label: "Customer",
    options: [
      { value: "", label: "Choose…" },
      ...customers.map((customer) => ({
        value: customer.id,
        // Inactive customers are listed and labelled. §5.1: refusing their money would leave
        // a balance nothing can ever clear.
        label: customer.is_active ? customer.name : `${customer.name} (deactivated)`,
      })),
    ],
    value: existing?.credit_customer_id ?? "",
    required: true,
    hint: "A deactivated customer can still pay off what they owe.",
  });

  const amount = field({
    name: "amount",
    label: "Amount",
    type: "number",
    step: "0.01",
    min: "0.01",
    value: existing?.amount ?? "",
    required: true,
    inputMode: "decimal",
    // §6.6: a repayment larger than outstanding is ACCEPTED, not refused -- a customer who
    // pays in advance or rounds up is owed money by the pump, and the balance goes negative.
    hint: "More than they owe is accepted — the balance simply goes negative.",
  });

  const modeSelect = select({
    name: "mode",
    label: "How did it arrive?",
    options: [{ value: "", label: "Choose…" }, ...MODES],
    value: existing?.mode ?? "",
    required: true,
    hint: "Only cash reaches the locker and the shift's cash position.",
  });

  const form = new Form({
    credit_customer_id: customerSelect,
    amount,
    mode: modeSelect,
  });

  let attachmentId = existing?.attachment_id ?? null;
  const fileInput = el("input", {
    attrs: { type: "file", accept: "image/jpeg,image/png", id: "f-repayment-receipt" },
    className: "field-input",
  });
  const receiptStatus = el("p", { className: "t-caption" });

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    receiptStatus.textContent = "Uploading…";
    try {
      const result = await uploadReceipt(file, context.shiftId);
      attachmentId = result.attachment_id;
      receiptStatus.textContent = "Receipt attached.";
    } catch (error) {
      receiptStatus.textContent = "";
      notify.error(explain(error), { requestId: error.requestId });
    }
  });

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: existing ? "Save" : "Record repayment",
    attrs: { type: "button" },
  });

  const submission = existing
    ? null
    : new Submission("POST", `/shifts/${context.shiftId}/credit-repayments`);

  submit.addEventListener("click", async () => {
    form.clearErrors();
    const values = form.values();

    if (!values.credit_customer_id || !values.mode) {
      form.showErrors([
        !values.credit_customer_id
          ? { loc: ["body", "credit_customer_id"], msg: "Choose a customer." }
          : { loc: ["body", "mode"], msg: "Choose how the money arrived." },
      ]);
      return;
    }

    submit.disabled = true;
    try {
      if (existing) {
        const changes = form.changes();
        delete changes.credit_customer_id; // CreditRepaymentUpdate accepts amount and mode only
        if (Object.keys(changes).length === 0) {
          sheet.close();
          return;
        }
        await api.patch(
          `/shifts/${context.shiftId}/credit-repayments/${existing.id}`,
          changes,
        );
      } else {
        const body = {
          credit_customer_id: values.credit_customer_id,
          amount: values.amount,
          mode: values.mode,
        };
        if (attachmentId) body.attachment_id = attachmentId;
        await submission.run(body);
      }
      sheet.close();
      notify.success(existing ? "Updated." : "Repayment recorded.");
      renderCreditRepayments(context.container, context);
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
    title: existing ? "Edit repayment" : "Record a repayment",
    body: el("div", { className: "stack" }, [
      ...form.nodes(),
      el("div", { className: "field" }, [
        el("span", { className: "field-label t-caption", text: "Receipt (optional)" }),
        existing ? el("p", { className: "t-caption", text: "The receipt cannot be changed." }) : fileInput,
        receiptStatus,
      ]),
    ]),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}
