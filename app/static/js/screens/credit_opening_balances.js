/* Opening balances -- what a customer already owed when this system started (§5.2, §6.6, §8).
 *
 * Phase 16. **Admin only**, and the route meta says so as well as this file, because §8 puts
 * it there for the reason §6.5 puts the cash locker's seeded opening balance there: it is a
 * figure nothing else in the system can check. Every other money row here is corroborated by
 * something -- a meter, a machine total, a receipt -- and this one is a person's word about
 * the past.
 *
 * The client-side gate is a courtesy either way. §8: "hiding a button is UX, not a control",
 * and the server refuses a manager with 403 `INSUFFICIENT_ROLE` whether or not this screen
 * is reachable.
 *
 * ## Zero is a button, not an omission
 *
 * §6.8's rule, and the reason this screen has a "They owed nothing" affordance rather than
 * leaving the field blank: an absent row means *nobody has looked at this customer*, and an
 * entered ₹0.00 means *somebody checked and they were square*. Those are different facts and
 * the owner needs to be able to state the second one.
 */

import { el, empty, pill, render, row } from "../dom.js";
import { api, explain } from "../api.js";
import { format } from "../money.js";
import { businessDate, todayAtOutlet } from "../time.js";
import { field, Form } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { openReversalSheet } from "../ui/reversal.js";
import { notify } from "../ui/toast.js";
import { errorCard } from "./today.js";

export async function renderOpeningBalances(container, { session, navigate }) {
  const { shell } = session;
  shell.setTab("credit");
  shell.setTitle("Opening balances");
  shell.setActions(null);

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let page;
  try {
    page = await api.get("/credit-opening-balances");
  } catch (error) {
    render(
      container,
      errorCard(error, () => renderOpeningBalances(container, { session, navigate })),
    );
    return;
  }

  const context = { session, container, navigate };
  const pending = page.items.filter((entry) => entry.opening_balance === null);
  const anchored = page.items.filter((entry) => entry.opening_balance !== null);

  render(
    container,
    el("div", { className: "stack" }, [
      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "What this is" }),
        el("p", {
          className: "t-body",
          text:
            "What each customer owed before this app started counting. Without it their " +
            "ledger begins at zero, and the first payment they make drives their balance " +
            "negative — as though the pump owed them money.",
        }),
        el("p", {
          className: "t-caption",
          text:
            "It can only be set once per customer. Correcting one reverses the old figure " +
            "with a reason and records the new one, so the change stays on the record.",
        }),
      ]),

      pending.length
        ? el("div", { className: "stack" }, [
            el("div", { className: "t-micro", text: `Not entered yet · ${pending.length}` }),
            ...pending.map((entry) => pendingCard(entry, context)),
          ])
        : null,

      anchored.length
        ? el("div", { className: "stack" }, [
            el("div", { className: "t-micro", text: `Entered · ${anchored.length}` }),
            ...anchored.map((entry) => anchoredCard(entry, context)),
          ])
        : null,

      page.items.length
        ? null
        : empty("No credit customers yet. Add them under Admin › Credit customers first."),
    ]),
  );
}

function pendingCard(entry, context) {
  return el("div", { className: "card stack" }, [
    el("div", { className: "row-between" }, [
      el("div", { className: "grow" }, [
        el("div", { className: "t-headline", text: entry.name }),
        el("div", { className: "t-caption", text: "No opening balance entered" }),
      ]),
      entry.is_active ? null : pill("inactive", "neutral"),
    ]),
    el("button", {
      className: "btn btn-primary btn-block",
      text: "Enter what they owed",
      attrs: { type: "button" },
      on: { click: () => openingSheet(entry, context) },
    }),
  ]);
}

function anchoredCard(entry, context) {
  return el("div", { className: "card stack" }, [
    el("div", { className: "row-between" }, [
      el("div", { className: "grow" }, [
        el("div", { className: "t-headline", text: entry.name }),
        el("div", {
          className: "t-caption",
          text: `As of ${businessDate(entry.as_of_date)}`,
        }),
      ]),
      el("span", {
        className: "t-title t-numeric",
        text: format(entry.opening_balance),
      }),
    ]),
    el("div", { className: "list" }, [
      row("Outstanding now", format(entry.outstanding)),
    ]),
    el("button", {
      className: "btn btn-danger",
      text: "Correct this figure",
      attrs: { type: "button" },
      on: {
        click: () =>
          openReversalSheet({
            title: "Correct the opening balance",
            path: `/credit-opening-balances/${entry.opening_balance_id}/reversals`,
            amount: entry.opening_balance,
            description: entry.name,
            replacementText:
              "Corrected opening balance (leave blank to remove it entirely)",
            onDone: () => renderOpeningBalances(context.container, context),
          }),
      },
    }),
  ]);
}

function openingSheet(entry, context) {
  const amount = field({
    name: "amount",
    label: "What they owed",
    type: "number",
    step: "0.01",
    value: "",
    required: true,
    inputMode: "decimal",
    // No `min`. §6.6 permits a negative outstanding -- a customer who paid in advance is
    // owed money by the pump -- and the server carries no sign CHECK for the same reason.
    hint: "Negative if the pump owed them. Zero if you checked and they were square.",
  });

  const asOf = field({
    name: "as_of_date",
    label: "As of",
    type: "date",
    value: todayAtOutlet(),
    required: true,
    hint: "Their ledger starts here. Nothing dated before this can be entered afterwards.",
  });

  const form = new Form({ amount, as_of_date: asOf });

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: "Record it",
    attrs: { type: "button" },
  });

  const zero = el("button", {
    className: "btn btn-block",
    text: "They owed nothing",
    attrs: { type: "button" },
  });

  async function send(amountValue) {
    form.clearErrors();
    const values = form.values();
    submit.disabled = true;
    zero.disabled = true;
    try {
      // No Idempotency-Key, and none is accepted: the server refuses a second live opening
      // balance with 409, so a retry cannot duplicate one (§6.10's nozzle-reading argument).
      await api.post("/credit-opening-balances", {
        credit_customer_id: entry.credit_customer_id,
        amount: amountValue,
        as_of_date: values.as_of_date,
      });
      sheet.close();
      notify.success(`Opening balance recorded for ${entry.name}.`);
      renderOpeningBalances(context.container, context);
    } catch (error) {
      submit.disabled = false;
      zero.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    }
  }

  submit.addEventListener("click", () => {
    const values = form.values();
    if (!values.amount) {
      form.showErrors([
        { loc: ["body", "amount"], msg: "Enter a figure, or use “They owed nothing”." },
      ]);
      return;
    }
    send(values.amount);
  });

  // §6.8: zero as an answer, never zero as an omission. A separate control, so recording
  // "square" is a deliberate act rather than something that happens by leaving a field alone.
  zero.addEventListener("click", () => send("0.00"));

  const sheet = openSheet({
    title: entry.name,
    body: el("div", { className: "stack" }, [
      ...form.nodes(),
      el("p", {
        className: "t-caption",
        text:
          "This can only be set once. Getting it wrong is recoverable — you reverse it with " +
          "a reason — but the correction stays visible, so it is worth checking the register.",
      }),
    ]),
    footer: el("div", { style: { padding: "0 1rem 1rem" }, className: "stack" }, [
      submit,
      zero,
    ]),
  });
}
