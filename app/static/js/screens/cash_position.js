/* §6.4's equation, term by term, and the decision that carries a person's name.
 *
 * ## Why every term is shown rather than a single figure
 *
 * `cash_position.py` returns all of them deliberately, and its docstring says why: "a manager
 * told 'you are ₹500 short' and nothing else cannot check the claim, and this is the number
 * that decides whether a debt lands on somebody's name." So this screen renders the equation
 * as a worked sum, in the order §6.4 states it, with each term labelled -- not a total with a
 * disclosure triangle.
 *
 * ## The two figures are never added together
 *
 *   accountable_cash   what the meters and the other channels say he should be holding
 *   declared_cash      what he says he counted into the locker
 *
 * §5.2 and §14: summing these double-counts the day. They are computed independently by the
 * server and only ever *subtracted*, and this screen keeps them visually separate for the
 * same reason.
 *
 * ## gap: null is not zero
 *
 * `gap: null` means nobody has declared, which is a different fact from a gap of ₹0.00. §6.8's
 * "zero as an answer, never zero as an omission" again -- and the booking control is disabled
 * with that reason rather than offered and then refused with 409 NO_CASH_DECLARED.
 *
 * ## A shortfall is booked by a human, never by this screen
 *
 * §14 forbids booking automatically, and §5.2 gives the argument with more force than
 * anywhere else in the document, because here the debt is explicit and carries a name: "a
 * ₹500 gap is more often a mistyped reading, a forgotten UPI figure or an unrecorded udhaar
 * slip than it is theft, and the software must not be the thing that decides."
 *
 * So the computed gap is *shown*, the booking form is pre-filled with it, and the amount
 * remains editable -- because §5.2 stores `computed_gap` and `amount` separately and expects
 * them to differ sometimes. A divergence logs a warning server-side and writes anyway; it
 * never refuses, because refusing a human's judgement sends the correction outside the system
 * where nothing can see it.
 */

import { el, pill, render } from "../dom.js";
import { api, explain, Submission } from "../api.js";
import { format, gapLabel, isZero } from "../money.js";
import { businessDate } from "../time.js";
import { field, Form } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { notify } from "../ui/toast.js";
import { errorCard } from "./today.js";

export async function renderCashPosition(container, { session, navigate, shiftId }) {
  const { shell } = session;
  shell.setTab("cash");
  shell.setTitle("Cash position");
  shell.setActions();

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let position;
  try {
    position = await api.get(`/shifts/${shiftId}/cash-position`);
  } catch (error) {
    render(
      container,
      errorCard(error, () => renderCashPosition(container, { session, navigate, shiftId })),
    );
    return;
  }

  shell.setTitle("Cash position", businessDate(position.business_date));

  const context = { session, container, navigate, shiftId, position };
  const gap = gapLabel(position.gap);
  const declared = position.declared_cash;

  render(
    container,
    el("div", { className: "stack" }, [
      // The comparison, kept as two figures side by side rather than one net number.
      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "The comparison" }),
        el("div", { className: "list" }, [
          term("Accountable — what the meters imply", position.accountable_cash),
          declared === null
            ? el("div", { className: "list-row" }, [
                el("div", { className: "list-row-main t-body", text: "Declared — what he counted" }),
                el("div", { className: "list-row-value t-absent", text: "not declared" }),
              ])
            : term("Declared — what he counted", declared),
        ]),
        el("div", { className: "row-between", style: { marginTop: "0.75rem" } }, [
          el("span", { className: "t-micro", text: "Gap" }),
          el("span", { className: `t-title t-numeric ${gap.className}`, text: gap.text }),
        ]),
        el("p", {
          className: "t-caption",
          text:
            declared === null
              ? "Nobody has declared cash for this shift, so there is nothing to compare against. That is not the same as a gap of zero."
              : "Positive means short, negative means a surplus. Nothing here is written — a gap becomes a debt only when a manager books it.",
        }),
      ]),

      position.incomplete
        ? el("div", { className: "card" }, [
            pill("incomplete", "review"),
            el("p", {
              className: "t-caption",
              text: "Some nozzles have no closing reading yet, so the accountable figure is partial.",
            }),
          ])
        : null,

      // §6.4 written out. The order matches the equation in the spec exactly, so somebody
      // holding the document can follow along line by line.
      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "How the accountable figure is built" }),
        el("div", { className: "list" }, [
          term("Metered fuel sales", position.metered_fuel_sales),
          term("Non-fuel sales", position.non_fuel_sales),
          divider("less what did not arrive as cash"),
          term("Card", position.card_total, true),
          term("UPI", position.upi_total, true),
          term("Wallet", position.wallet_total, true),
          term("Credit sales (udhaar)", position.credit_sales_total, true),
          divider("plus cash that arrived from elsewhere"),
          term("Credit repayments in cash", position.cash_credit_repayments),
          term("Shortfall settlements in cash", position.cash_shortfall_settlements),
          divider("less cash that left"),
          term("Cash expenses", position.cash_expenses, true),
        ]),
        el("p", {
          className: "t-caption",
          text: "Only cash-mode expenses and repayments appear here. A bank-paid bill is on the record but never leaves the drawer.",
        }),
      ]),

      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "Booked against this shift" }),
        el("div", { className: "list" }, [
          term("Shortfalls booked", position.shortfalls_booked),
        ]),
        el("p", {
          className: "t-caption",
          text: "Subtracted from the day's expected closing, so the same money is not counted both as a debt and as cash in the locker.",
        }),
        bookingControl(position, context),
      ]),
    ]),
  );
}

function term(label, value, negative = false) {
  return el("div", { className: "list-row" }, [
    el("div", { className: "list-row-main t-body", text: label }),
    el("div", {
      className: "list-row-value t-body t-numeric",
      text: `${negative ? "− " : ""}${format(value)}`,
    }),
  ]);
}

function divider(text) {
  return el("div", { className: "list-row" }, [
    el("div", { className: "list-row-main t-micro", text }),
  ]);
}

function bookingControl(position, context) {
  // 409 NO_CASH_DECLARED is what the server returns when there is no declaration to compare
  // against. Disabling with the reason is better than offering a button that always fails.
  if (position.gap === null) {
    return el("p", {
      className: "t-caption",
      text: "Booking a shortfall needs a declared cash figure first — there is no gap to book against a blank.",
    });
  }

  if (isZero(position.gap)) {
    return el("p", {
      className: "t-caption",
      text: "The drawer balances. Nothing to book.",
    });
  }

  // A surplus is not a shortfall, and V1 has no record type for one. §14's open questions
  // list "is a surplus ever booked?" as still unanswered, so this says so rather than
  // offering a control that would put a negative amount somewhere it does not belong.
  if (position.gap.trim().startsWith("-")) {
    return el("p", {
      className: "t-caption",
      text: "This shift shows a surplus rather than a shortfall. V1 has no record type for a surplus — it stays visible here and in the day's variance.",
    });
  }

  return el("button", {
    className: "btn btn-primary btn-block",
    text: "Book a shortfall",
    attrs: { type: "button" },
    on: { click: () => bookingSheet(position, context) },
  });
}

function bookingSheet(position, context) {
  const amount = field({
    name: "amount",
    label: "Amount to book",
    type: "number",
    step: "0.01",
    min: "0.01",
    // Pre-filled with the computed gap, and deliberately still editable. §5.2 stores both
    // `computed_gap` and `amount` because a manager may know part of the gap is a slip he
    // has already corrected.
    value: position.gap,
    required: true,
    inputMode: "decimal",
    hint: `The system computed ${format(position.gap)}. You may book a different figure — both are stored.`,
  });

  const reason = field({
    name: "reason",
    label: "Why is this being booked?",
    required: true,
    hint: "Mandatory. This becomes a debt in a named person's ledger, so the reason is part of the record. 3–500 characters.",
  });

  const form = new Form({ amount, reason });

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: "Book shortfall",
    attrs: { type: "button" },
  });

  const submission = new Submission("POST", `/shifts/${context.shiftId}/shortfalls`);

  submit.addEventListener("click", async () => {
    form.clearErrors();
    const values = form.values();

    if (!values.reason || values.reason.trim().length < 3) {
      form.showErrors([{ loc: ["body", "reason"], msg: "A reason is required." }]);
      return;
    }

    submit.disabled = true;
    try {
      // No salesman_id. §14: it is read from shifts.attendant_id -- the one name §5.2 says
      // carries the drawer -- and a client-supplied value would let a typo put a debt on the
      // wrong person with no second source of truth to catch it.
      await submission.run({ amount: values.amount, reason: values.reason.trim() });
      sheet.close();
      notify.success("Shortfall booked.");
      renderCashPosition(context.container, context);
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    }
  });

  const sheet = openSheet({
    title: "Book a shortfall",
    body: el("div", { className: "stack" }, [
      el("div", { className: "card stack" }, [
        el("p", {
          className: "t-caption",
          text: "This records a debt against the salesman who carried this shift's drawer. It is repaid in cash.",
        }),
        el("p", {
          className: "t-caption",
          text: "A gap is more often a mistyped reading, a forgotten UPI figure or an unrecorded udhaar slip than it is theft. Check those first.",
        }),
      ]),
      ...form.nodes(),
    ]),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}
