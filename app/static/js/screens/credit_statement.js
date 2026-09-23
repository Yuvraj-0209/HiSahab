/* The billing-period statement (CLAUDE.md §6.6, §8, §13.40-42 -- Phase 21).
 *
 * This outlet bills every udhaar customer on the 16th (for the 1st-15th) and on the 1st (for
 * the rest). This screen is what a bill is prepared and checked against: for any window, per
 * customer, what they owed going in, the udhaar and payments inside it, what the bill should
 * say, what has come in since, and what they owe today.
 *
 * ## Every figure arrives computed, including the totals
 *
 * §14: JavaScript has no decimal type, and a statement is money arithmetic from top to
 * bottom. So `GET /credit-customers/statement` sends each customer's six figures *and* the
 * column totals, as strings, and this file adds nothing to anything. Even "total repaid"
 * is shown as two server figures -- in the period, and since -- rather than summed here.
 *
 * ## `paid_since` is not "this bill, paid"
 *
 * §13.40: nothing allocates a payment to a bill. The screen puts `billed` beside
 * `paid_since` and lets the owner read it, and its captions never claim a bill was cleared.
 *
 * ## Printing
 *
 * `@media print` in app.css hides the chrome and opens every detail panel. "Print this
 * customer" adds a body class that hides the others for one print, removed on `afterprint`.
 * A print stylesheet is a medium, not a second palette, so §12's one-palette rule stands.
 */

import { el, empty, pill, render, row } from "../dom.js";
import { api } from "../api.js";
import { format, isZero } from "../money.js";
import { businessDate, todayAtOutlet } from "../time.js";
import { Form, field } from "../ui/field.js";
import { notify } from "../ui/toast.js";
import { errorCard } from "./today.js";

const MODE_LABELS = {
  cash: "Cash",
  card: "Card",
  upi: "UPI",
  bank_transfer: "Bank transfer",
};

const UNIT_SHORT = { litre: "L", kilogram: "kg" };

/* The bank's verdict on one bank-transfer payment (§13.42). The pill colours reuse the
 * status tints: green for confirmed, amber for "a human must pick", red for "the bank did
 * not see it", grey for "nobody has uploaded that month yet" -- which is not a problem. */
const BANK_STATUS = {
  verified: { text: "on bank statement", kind: "open" },
  ambiguous: { text: "bank: ambiguous", kind: "closed" },
  not_on_statement: { text: "not on bank statement", kind: "review" },
  no_statement: { text: "no statement uploaded", kind: "neutral" },
};

/* The most recently completed billing half-month. Date parts only -- never a `Date` parsed
 * from a business date, which time.js explains moves it a day west of UTC. This is calendar
 * arithmetic on integers, not money. */
export function lastCompletedHalf(today) {
  const [year, month, day] = today.split("-").map(Number);
  const pad = (n) => String(n).padStart(2, "0");
  if (day >= 16) {
    return { from: `${year}-${pad(month)}-01`, to: `${year}-${pad(month)}-15` };
  }
  const prevYear = month === 1 ? year - 1 : year;
  const prevMonth = month === 1 ? 12 : month - 1;
  // Day 0 of this month is the last day of the previous one.
  const lastDay = new Date(Date.UTC(year, month - 1, 0)).getUTCDate();
  return {
    from: `${prevYear}-${pad(prevMonth)}-16`,
    to: `${prevYear}-${pad(prevMonth)}-${pad(lastDay)}`,
  };
}

export async function renderCreditStatement(container, { session, navigate }, query = {}) {
  const { shell } = session;
  shell.setTab("credit");
  shell.setTitle("Billing statement");

  const fallback = lastCompletedHalf(todayAtOutlet());
  const from = query.from || fallback.from;
  const to = query.to || fallback.to;

  shell.setActions(
    el("button", {
      className: "btn",
      text: "Print",
      attrs: { type: "button" },
      on: { click: () => printStatement(null) },
    }),
  );

  const picker = dateForm({ from, to, navigate });
  render(container, picker, el("div", { className: "t-caption", text: "Loading…" }));

  let statement;
  try {
    statement = await api.get("/credit-customers/statement", { from, to });
  } catch (error) {
    render(
      container,
      picker,
      errorCard(error, () => renderCreditStatement(container, { session, navigate }, query)),
    );
    return;
  }

  const period = `${businessDate(statement.from)} – ${businessDate(statement.to)}`;
  shell.setTitle("Billing statement", period);

  const unanchored = statement.rows.filter((entry) => !entry.opening_balance_entered).length;

  render(
    container,
    picker,
    el("div", { className: "stack" }, [
      // Only visible on paper: the screen's own title bar is hidden when printing.
      el("div", { className: "print-only" }, [
        el("div", { className: "t-title", text: "Udhaar statement" }),
        el("div", {
          className: "t-body",
          text: `${period} · printed ${businessDate(statement.today)}`,
        }),
      ]),

      statement.open_shift_count
        ? el("div", {
            className: "truncation-notice t-caption",
            text:
              `${statement.open_shift_count} shift${statement.open_shift_count === 1 ? " is" : "s are"} ` +
              "in this period still open. Its udhaar is counted, but the figures may still change " +
              "until it is closed.",
          })
        : null,

      unanchored
        ? el("div", {
            className: "truncation-notice t-caption no-print",
            text:
              `${unanchored} customer${unanchored === 1 ? " has" : "s have"} no opening balance ` +
              "entered, so “owed before” only counts what has been typed into the app.",
          })
        : null,

      statement.lines_truncated
        ? el("div", {
            className: "truncation-notice t-caption",
            text:
              "This period has more entries than the lists can show. Every figure is still " +
              "exact; choose a shorter period to see every line.",
          })
        : null,

      el("p", {
        className: "t-caption no-print",
        text:
          "Bill = owed before + udhaar − repaid, inside the dates. Paid since is every payment " +
          "after the end date; it is not matched to any particular bill.",
      }),

      statement.rows.length
        ? el(
            "div",
            { className: "stack" },
            statement.rows.map((entry) => customerCard(entry, statement, navigate)),
          )
        : empty("Nobody owed anything or had any udhaar or payments in this period."),

      statement.rows.length ? totalsCard(statement) : null,
    ]),
  );
}

function dateForm({ from, to, navigate }) {
  const fromField = field({ name: "from", label: "From", type: "date", value: from, required: true });
  const toField = field({
    name: "to",
    label: "To",
    type: "date",
    value: to,
    required: true,
    max: todayAtOutlet(),
    hint: "Both dates are included. Up to 366 days.",
  });
  const form = new Form({ from: fromField, to: toField });

  const show = el("button", {
    className: "btn btn-primary",
    text: "Show statement",
    attrs: { type: "submit" },
  });

  return el(
    "form",
    {
      className: "card stack no-print",
      on: {
        submit: (event) => {
          event.preventDefault();
          const values = form.values();
          if (!values.from || !values.to) {
            notify.warning("Pick both a start and an end date.");
            return;
          }
          // ISO dates compare correctly as strings. The server refuses this too -- this is
          // the courtesy, not the control (§8).
          if (values.from > values.to) {
            notify.warning("The start date must not be after the end date.");
            return;
          }
          navigate(`#/credit/statement?from=${values.from}&to=${values.to}`);
        },
      },
    },
    [el("div", { className: "statement-dates" }, form.nodes()), show],
  );
}

function customerCard(entry, statement, navigate) {
  const inRange = entry.lines.filter((line) => line.period === "in_range");
  const since = entry.lines.filter((line) => line.period === "since");

  const detail = el("div", { className: "statement-detail is-collapsed stack" }, [
    el("div", { className: "t-micro", text: "In the period" }),
    inRange.length
      ? el("div", { className: "list" }, inRange.map(lineRow))
      : el("p", { className: "t-caption", text: "No udhaar or payments inside these dates." }),

    el("div", { className: "t-micro", text: `Since ${businessDate(statement.to)}` }),
    el("div", { className: "list" }, [
      row("Udhaar since", format(entry.udhaar_since)),
      isZero(entry.opening_since)
        ? null
        : row("Opening balance dated after the period", format(entry.opening_since)),
      ...since.map(lineRow),
    ]),
    el("p", {
      className: "t-caption",
      text: "Owes today = bill + udhaar since − paid since.",
    }),

    el("div", { className: "row no-print", style: { flexWrap: "wrap" } }, [
      el("button", {
        className: "btn",
        text: "Print this customer",
        attrs: { type: "button" },
        on: { click: () => printStatement(card) },
      }),
      el("button", {
        className: "btn btn-plain",
        text: "Open the ledger",
        attrs: { type: "button" },
        on: { click: () => navigate(`#/credit/customers/${entry.customer_id}`) },
      }),
    ]),
  ]);

  const toggle = el("button", {
    className: "btn btn-plain no-print",
    text: `Show entries (${entry.lines.length})`,
    attrs: { type: "button", "aria-expanded": "false" },
    on: {
      click: () => {
        const collapsed = detail.classList.toggle("is-collapsed");
        toggle.textContent = collapsed
          ? `Show entries (${entry.lines.length})`
          : "Hide entries";
        toggle.setAttribute("aria-expanded", String(!collapsed));
      },
    },
  });

  const card = el("div", { className: "card stack statement-customer" }, [
    el("div", { className: "row-between" }, [
      el("div", { className: "grow" }, [
        el("div", { className: "t-headline", text: entry.name }),
        el("div", { className: "t-caption", text: entry.phone }),
      ]),
      el("div", { className: "row" }, [
        entry.is_active ? null : pill("inactive", "neutral"),
        el("div", { className: "text-right" }, [
          el("div", { className: "t-micro", text: "Bill" }),
          el("div", { className: "t-title t-numeric", text: format(entry.billed) }),
        ]),
      ]),
    ]),
    figures(entry),
    toggle,
    detail,
  ]);
  return card;
}

/* The six figures, in the order a bill reads. */
function figures(entry) {
  return el("div", { className: "statement-figures" }, [
    figure("Owed before", owedBefore(entry)),
    figure("Udhaar", format(entry.udhaar_in)),
    figure("Repaid", format(entry.repaid_in)),
    figure("Bill", format(entry.billed), { strong: true }),
    figure("Paid since", format(entry.paid_since)),
    figure("Owes today", format(entry.owes_today), { strong: true }),
  ]);
}

/* §6.8, §14: a customer with no opening balance entered and nothing before the window sums
 * to ₹0.00, and that is not the same fact as "owed nothing". Say "not entered" instead. When
 * there *are* earlier rows, the figure is real but partial -- the caption under the card list
 * already says so. */
function owedBefore(entry) {
  if (!entry.opening_balance_entered && isZero(entry.owed_before)) {
    return "not entered";
  }
  return format(entry.owed_before);
}

function figure(label, value, { strong = false } = {}) {
  return el("div", { className: "statement-figure" }, [
    el("div", { className: "t-micro", text: label }),
    el("div", {
      className: `t-numeric ${strong ? "t-headline" : "t-body"}`,
      text: value,
    }),
  ]);
}

function lineRow(line) {
  const status = line.bank_status ? BANK_STATUS[line.bank_status] : null;
  return el("div", { className: "list-row" }, [
    el("div", { className: "list-row-main" }, [
      el("div", { className: "t-body", text: lineTitle(line) }),
      el("div", { className: "t-caption", text: lineCaption(line) }),
    ]),
    el("div", { className: "row", style: { flexWrap: "wrap", justifyContent: "flex-end" } }, [
      line.is_reversed ? pill("cancelled", "neutral") : null,
      line.is_reversal ? pill("reversal", "neutral") : null,
      status ? pill(status.text, status.kind) : null,
      el("div", { className: "list-row-value t-body t-numeric", text: format(line.amount) }),
    ]),
  ]);
}

function lineTitle(line) {
  if (line.kind === "opening") return "Opening balance (in owed before)";
  if (line.kind === "repayment") return `Payment · ${MODE_LABELS[line.mode] ?? line.mode}`;
  if (line.fuel_display_name === null) return "Udhaar · non-fuel";
  // §4.5: a quantity is shown with its own unit, never assumed to be litres.
  const quantity =
    line.quantity === null
      ? ""
      : ` ${line.quantity} ${UNIT_SHORT[line.unit_of_measure] ?? line.unit_of_measure}`;
  return `Udhaar · ${line.fuel_display_name}${quantity}`;
}

function lineCaption(line) {
  const parts = [businessDate(line.business_date)];
  if (line.vehicle_number) parts.push(line.vehicle_number);
  if (line.bank_reference) parts.push(line.bank_reference);
  if (line.kind === "repayment" && line.shift_id === null) parts.push("to the bank");
  if (line.reversal_reason) parts.push(line.reversal_reason);
  return parts.join(" · ");
}

function totalsCard(statement) {
  const totals = statement.totals;
  return el("div", { className: "card stack statement-totals" }, [
    el("div", { className: "t-micro", text: `Totals · ${statement.rows.length} customers` }),
    el("div", { className: "list" }, [
      row("Owed before", format(totals.owed_before)),
      row("Udhaar in the period", format(totals.udhaar_in)),
      row("Repaid in the period", format(totals.repaid_in)),
      row("Bills", format(totals.billed), { valueClass: "list-row-value-strong" }),
      row("Udhaar since", format(totals.udhaar_since)),
      row("Repaid since", format(totals.paid_since)),
      row("Owed today", format(totals.owes_today), { valueClass: "list-row-value-strong" }),
    ]),
  ]);
}

/* Print the whole statement, or one customer's card. Every detail panel is opened by the
 * print stylesheet, so nothing here has to toggle state and put it back. */
function printStatement(card) {
  if (card) {
    document.body.classList.add("print-one");
    card.classList.add("print-target");
    window.addEventListener(
      "afterprint",
      () => {
        document.body.classList.remove("print-one");
        card.classList.remove("print-target");
      },
      { once: true },
    );
  }
  window.print();
}
