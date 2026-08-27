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
import { businessDate, todayAtOutlet } from "../time.js";
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

/* --- Repayments that arrived at the bank (§5.2, Phase 16) --------------------
 *
 * The Credit tab's write screen, and the one that makes a ledger reconstructable at all.
 *
 * Before Phase 16 every repayment hung off a shift, and §5.2 forbids modifying anything
 * referencing a `locked` shift -- so a bank transfer against a day already locked could not
 * be recorded, and one arriving on a day the outlet was shut had no shift to attach to.
 * §4.7 says the whole day is typed in after the fact, which makes both the normal case here.
 *
 * **Cash is deliberately absent from this form.** Cash lands in a drawer, so it belongs to
 * the shift it arrived on -- the screen above. The server refuses `mode = cash` here with
 * 422 CASH_REPAYMENT_NEEDS_SHIFT and the database refuses it too, so omitting it from the
 * select is a courtesy rather than the control (§8).
 */

const BANK_MODES = [
  { value: "bank_transfer", label: "Bank transfer" },
  { value: "upi", label: "UPI — to a bank account, not the pump's QR" },
  { value: "card", label: "Card — not on the pump's machine" },
];

export async function renderLedgerRepayments(container, { session, navigate }) {
  const { shell } = session;
  shell.setTab("credit");
  shell.setTitle("Payments received");

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let page;
  let customers;
  try {
    [page, customers] = await Promise.all([
      api.get("/credit-repayments", { limit: 25 }),
      api.get("/credit-customers", { include_inactive: true }),
    ]);
  } catch (error) {
    render(
      container,
      errorCard(error, () => renderLedgerRepayments(container, { session, navigate })),
    );
    return;
  }

  const context = { session, container, navigate, customers };
  const byId = new Map(customers.map((customer) => [customer.id, customer]));

  shell.setActions(
    el("button", {
      className: "btn btn-primary",
      text: "Add",
      attrs: { type: "button" },
      on: { click: () => bankRepaymentSheet(context) },
    }),
  );

  render(
    container,
    el("div", { className: "stack" }, [
      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "Money that reached the bank" }),
        el("p", {
          className: "t-body",
          text:
            "Record a settlement that arrived by transfer rather than at the pump. It " +
            "reduces what the customer owes and changes no day's cash — the locker never " +
            "saw it.",
        }),
        el("p", {
          className: "t-caption",
          text: "Cash belongs on the shift it arrived on. Enter that under Entry › Repayments.",
        }),
      ]),

      page.items.length
        ? el(
            "div",
            { className: "stack" },
            page.items.map((repayment) => ledgerRepaymentCard(repayment, byId, context)),
          )
        : empty("Nothing recorded yet."),
    ]),
  );
}

function ledgerRepaymentCard(repayment, byId, context) {
  const customer = byId.get(repayment.credit_customer_id);
  return el("div", { className: "card stack" }, [
    el("div", { className: "row-between" }, [
      el("div", { className: "grow" }, [
        el("div", { className: "t-headline", text: customer?.name ?? "Unknown customer" }),
        el("div", {
          className: "t-caption",
          text:
            `${businessDate(repayment.business_date)} · ` +
            repayment.mode.replace("_", " ") +
            (repayment.shift_id === null ? "" : " · on a shift"),
        }),
      ]),
      el("div", { className: "row" }, [
        reversalBadge(repayment),
        el("span", { className: "t-title t-numeric", text: format(repayment.amount) }),
      ]),
    ]),
    repayment.reversal_reason
      ? el("p", { className: "t-caption", text: `Reason: ${repayment.reversal_reason}` })
      : null,
  ]);
}

function bankRepaymentSheet(context) {
  const { customers } = context;

  const customerSelect = select({
    name: "credit_customer_id",
    label: "Customer",
    options: [
      { value: "", label: "Choose…" },
      ...customers.map((customer) => ({
        value: customer.id,
        label: customer.is_active ? customer.name : `${customer.name} (deactivated)`,
      })),
    ],
    value: "",
    required: true,
    hint: "A deactivated customer can still pay off what they owe.",
  });

  const amount = field({
    name: "amount",
    label: "Amount",
    type: "number",
    step: "0.01",
    min: "0.01",
    value: "",
    required: true,
    inputMode: "decimal",
    hint: "More than they owe is accepted — the balance simply goes negative.",
  });

  const modeSelect = select({
    name: "mode",
    label: "How did it arrive?",
    options: [{ value: "", label: "Choose…" }, ...BANK_MODES],
    value: "",
    required: true,
    hint: "None of these touch the locker, so no day's cash position changes.",
  });

  const businessDateField = field({
    name: "business_date",
    label: "Date it arrived",
    type: "date",
    value: todayAtOutlet(),
    required: true,
    hint: "The day the money reached the account — not the day you are typing this in.",
  });

  const form = new Form({
    credit_customer_id: customerSelect,
    amount,
    mode: modeSelect,
    business_date: businessDateField,
  });

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: "Record payment",
    attrs: { type: "button" },
  });

  // §6.10 and §14: the key belongs to the *submission*, minted once and reused by every
  // retry until it succeeds. A fresh key per fetch would reintroduce the duplicate this
  // exists to prevent, on exactly the connectivity it was built for.
  const submission = new Submission("POST", "/credit-repayments");

  submit.addEventListener("click", async () => {
    form.clearErrors();
    const values = form.values();

    const missing = ["credit_customer_id", "mode"].find((name) => !values[name]);
    if (missing) {
      form.showErrors([
        {
          loc: ["body", missing],
          msg:
            missing === "credit_customer_id"
              ? "Choose a customer."
              : "Choose how the money arrived.",
        },
      ]);
      return;
    }

    submit.disabled = true;
    try {
      await submission.run({
        credit_customer_id: values.credit_customer_id,
        amount: values.amount,
        mode: values.mode,
        business_date: values.business_date,
      });
      sheet.close();
      notify.success("Payment recorded.");
      renderLedgerRepayments(context.container, context);
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
    title: "Record a bank payment",
    body: el("div", { className: "stack" }, form.nodes()),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}
