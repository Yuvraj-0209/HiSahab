/* The Credit tab: who owes what, and one customer's account (CLAUDE.md §5.2, §6.6, §8).
 *
 * Phase 16. Before it, the only way to see a balance was `#/admin/customers`, which is an
 * admin screen for *editing customers* that happened to show a number. A manager chasing
 * udhaar had nowhere to stand.
 *
 * ## Every figure here is computed by the server and rendered as a string
 *
 * §14: JavaScript has no decimal type and `0.1 + 0.2 !== 0.3` there exactly as it does in
 * Python, so §3 rule 1 does not stop at the API boundary. That bites hardest on this tab,
 * because a ledger is nothing *but* money arithmetic -- so `balance_after` arrives per row
 * from `GET /credit-customers/{id}/ledger` and this file never adds two amounts together.
 *
 * ## `null` is not zero, and this screen has to say so out loud
 *
 * `opening_balance: null` means nobody has entered what this customer already owed;
 * `"0.00"` means somebody checked and they were square (§6.8, §14). Collapsing the two with
 * `?? 0` would tell the owner his ledger is complete when it has not been started -- which
 * is the plausible-but-wrong figure §14 opens by warning about.
 */

import { el, empty, pill, render, row } from "../dom.js";
import { api } from "../api.js";
import { format, isNegative, isZero } from "../money.js";
import { businessDate } from "../time.js";
import { satisfies } from "../ui/nav.js";
import { errorCard } from "./today.js";

const KIND_LABELS = {
  opening: "Opening balance",
  sale: "Udhaar issued",
  repayment: "Repayment",
};

/* Who owes what -- the tab's landing screen. */
export async function renderCreditHub(container, { session, navigate }) {
  const { shell } = session;
  shell.setTab("credit");
  shell.setTitle("Credit");
  shell.setActions(
    el("button", {
      className: "btn",
      text: "Record a payment",
      attrs: { type: "button" },
      on: { click: () => navigate("#/credit/repayments") },
    }),
  );

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let balances;
  try {
    balances = await api.get("/credit-opening-balances");
  } catch (error) {
    render(
      container,
      errorCard(error, () => renderCreditHub(container, { session, navigate })),
    );
    return;
  }

  // Biggest debt first. Sorting on a money *string* would be wrong, so the comparison goes
  // through the server-supplied sign helpers rather than through Number() -- see below.
  const rows = [...balances.items].sort(compareOutstandingDescending);

  const unanchored = rows.filter((entry) => entry.opening_balance === null).length;
  const isAdmin = satisfies(session.me.role, "admin");

  render(
    container,
    el("div", { className: "stack" }, [
      unanchored
        ? el("div", { className: "card stack" }, [
            el("div", { className: "t-micro", text: "Before you trust these figures" }),
            el("p", {
              className: "t-body",
              text:
                `${unanchored} of ${rows.length} customers have no opening balance entered. ` +
                "Their ledger starts at zero here, which is almost never what they actually owed.",
            }),
            isAdmin
              ? el("button", {
                  className: "btn btn-primary",
                  text: "Set opening balances",
                  attrs: { type: "button" },
                  on: { click: () => navigate("#/credit/opening-balances") },
                })
              : el("p", {
                  className: "t-caption",
                  text: "An admin can enter them.",
                }),
          ])
        : null,

      rows.length
        ? el(
            "div",
            { className: "stack" },
            rows.map((entry) => customerCard(entry, navigate)),
          )
        : empty("No credit customers yet. An admin adds them under Admin › Credit customers."),
    ]),
  );
}

function customerCard(entry, navigate) {
  // A card with a `btn-plain` inside rather than a `<button class="card">`, matching
  // `days.js`. A button wrapping this much structure reads as one enormous control to a
  // screen reader, and the app has no styling for it.
  return el("div", { className: "card stack" }, [
    el("div", { className: "row-between" }, [
      el("div", { className: "grow" }, [
        el("div", { className: "t-headline", text: entry.name }),
        el("div", { className: "t-caption", text: standingLabel(entry) }),
      ]),
      el("div", { className: "row" }, [
        entry.is_active ? null : pill("inactive", "neutral"),
        el("span", {
          className: "t-title t-numeric",
          text: format(entry.outstanding),
        }),
      ]),
    ]),
    el("button", {
      className: "btn btn-plain",
      text: "Open the ledger",
      attrs: { type: "button" },
      on: { click: () => navigate(`#/credit/customers/${entry.credit_customer_id}`) },
    }),
  ]);
}

/* The sentence under a name. It is the one place the null/zero distinction is visible to a
 * reader, so it is spelled out in words rather than left to a dash. */
function standingLabel(entry) {
  if (entry.opening_balance === null) {
    return "No opening balance entered — starts from zero";
  }
  if (isNegative(entry.outstanding)) {
    return `In credit · opening ${format(entry.opening_balance)} on ${businessDate(entry.as_of_date)}`;
  }
  if (isZero(entry.outstanding)) {
    return `Settled up · opening ${format(entry.opening_balance)} on ${businessDate(entry.as_of_date)}`;
  }
  return `Since ${businessDate(entry.as_of_date)}`;
}

/* Sort biggest debt first.
 *
 * `parseFloat` is forbidden (§14) and a structural test enforces it, so this compares the
 * *strings* by sign, then by length, then lexically -- which is a correct ordering for fixed
 * two-decimal-place values and needs no arithmetic. A customer in credit sorts below zero,
 * which is where a reader expects them. */
function compareOutstandingDescending(left, right) {
  return compareMoneyStrings(right.outstanding, left.outstanding);
}

function compareMoneyStrings(a, b) {
  const negA = isNegative(a);
  const negB = isNegative(b);
  if (negA !== negB) return negA ? -1 : 1;

  const digitsA = a.replace(/[^0-9]/g, "");
  const digitsB = b.replace(/[^0-9]/g, "");
  const magnitude =
    digitsA.length !== digitsB.length
      ? digitsA.length - digitsB.length
      : digitsA.localeCompare(digitsB);

  // Among negatives, the larger magnitude is the smaller number.
  return negA ? -magnitude : magnitude;
}

/* One customer's account, newest first, with the balance beside every line. */
export async function renderCustomerLedger(container, { session, navigate, customerId }) {
  const { shell } = session;
  shell.setTab("credit");
  shell.setTitle("Ledger");

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let customer;
  let ledger;
  try {
    [customer, ledger] = await Promise.all([
      api.get(`/credit-customers/${customerId}`),
      api.get(`/credit-customers/${customerId}/ledger`),
    ]);
  } catch (error) {
    render(
      container,
      errorCard(error, () =>
        renderCustomerLedger(container, { session, navigate, customerId }),
      ),
    );
    return;
  }

  shell.setTitle("Ledger", customer.name);
  shell.setActions(
    el("button", {
      className: "btn",
      text: "Record a payment",
      attrs: { type: "button" },
      on: { click: () => navigate("#/credit/repayments") },
    }),
  );

  render(
    container,
    el("div", { className: "stack" }, [
      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "Outstanding" }),
        el("div", { className: "t-amount", text: format(ledger.outstanding) }),
        el("div", { className: "list" }, [
          row(
            "Opening balance",
            // Never `?? 0`. `format`'s own docstring says the word for null is required in
            // practice: "not entered" and "0.00" are different facts (§6.8, §14).
            format(ledger.opening_balance, { absent: "not entered" }),
          ),
          row("Credit limit", format(customer.credit_limit, { absent: "no limit" })),
        ]),
        el("p", {
          className: "t-caption",
          text:
            ledger.opening_balance === null
              ? "No opening balance has been entered, so this account starts from zero here — not from what they actually owed."
              : "Computed from the opening balance plus every sale and repayment, reversals included. Never stored, so it cannot drift.",
        }),
      ]),

      ledger.items.length
        ? el(
            "div",
            { className: "list" },
            ledger.items.map((entry) => ledgerRow(entry)),
          )
        : empty("Nothing on this account yet."),

      ledger.truncated
        ? el("div", {
            className: "truncation-notice t-caption",
            text: "Older entries are not listed. The balances shown are still exact — each one is worked back from today.",
          })
        : null,
    ]),
  );
}

function ledgerRow(entry) {
  return el("div", { className: "list-row" }, [
    el("div", { className: "list-row-main" }, [
      el("div", { className: "t-body", text: KIND_LABELS[entry.kind] ?? entry.kind }),
      el("div", {
        className: "t-caption",
        text:
          businessDate(entry.business_date) +
          (entry.shift_id === null && entry.kind === "repayment" ? " · to the bank" : ""),
      }),
    ]),
    el("div", { className: "row" }, [
      entry.is_reversal ? pill("reversal", "neutral") : null,
      el("div", { className: "list-row-value" }, [
        el("div", {
          className: "t-body t-numeric",
          text: format(entry.balance_delta, { sign: true }),
        }),
        // The running balance under the movement -- the column this screen exists for.
        // Computed server-side; §14 forbids adding two money values here.
        el("div", {
          className: "t-micro t-numeric",
          text: format(entry.balance_after),
        }),
      ]),
    ]),
  ]);
}
