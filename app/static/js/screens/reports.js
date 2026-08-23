/* Reports: the week, one day, and what needs looking at.
 *
 * Phase 13. Three screens over three read-only endpoints, all manager-floor (§8).
 *
 * ## The one thing these screens must not soften
 *
 * §13.20 splits every day into two kinds of claim. A `snapshot` day is what a manager was
 * *told* on the day and §5.2 stores it so nothing can rewrite it. A `computed` day is an
 * estimate of a day still in motion, derived seconds ago, and it can still move.
 *
 * Rendering those identically would be the interface undoing a distinction the schema went to
 * some trouble to keep. So every row says which it is, in words, and the two never share a
 * visual treatment. §4.7's principle, one layer out: the abnormal day becomes visible rather
 * than reassigned.
 *
 * ## Nulls
 *
 * `variance: null` means nobody counted -- which under §6.5's locker model is most days -- and
 * is rendered "not counted", never ₹0.00. `gross_fuel_margin: null` means the commission has
 * never been entered, and is rendered as a prompt to enter it rather than as ₹0. §14 calls
 * `?? 0` the most dangerous two characters this project can write in a client, and a
 * reporting screen is where they would look most natural.
 *
 * ## Arithmetic
 *
 * There is none. Every figure here is rendered as the string the server sent, and the chart's
 * bar heights arrive as CSS percentages for exactly that reason (see ui/chart.js).
 */

import { el, empty, pill, render } from "../dom.js";
import { api } from "../api.js";
import { format, gapLabel } from "../money.js";
import { businessDate, todayAtOutlet } from "../time.js";
import { describeSource, salesBars, varianceStrip } from "../ui/chart.js";
import { errorCard } from "./today.js";

/** The pill kind each `source` gets. Snapshot is the only "settled" state. */
const SOURCE_PILL = {
  snapshot: "open",
  computed: "closed",
  no_trading: "neutral",
  unavailable: "review",
};

// --- the rolling window -------------------------------------------------------

export async function renderReports(container, { session, navigate }, query = {}) {
  const { shell } = session;
  shell.setTab("cash");
  shell.setTitle("Reports");
  shell.setActions(
    el("button", {
      className: "btn",
      text: "Alerts",
      attrs: { type: "button" },
      on: { click: () => navigate("#/reports/alerts") },
    }),
  );

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let report;
  try {
    // `from`/`to` omitted unless the caller supplied them: the server's default is §11's
    // 7-day window ending today *at the outlet*, and recomputing that here would put a second
    // copy of a timezone rule in the client (§3 rule 4).
    report = await api.get("/reports/range", { from: query.from, to: query.to });
  } catch (error) {
    render(
      container,
      errorCard(error, () => renderReports(container, { session, navigate }, query)),
    );
    return;
  }

  shell.setTitle("Reports", `${businessDate(report.from)} – ${businessDate(report.to)}`);

  const days = report.days;
  const reversed = [...days].reverse();

  render(
    container,
    el("div", { className: "stack" }, [
      el("div", { className: "card stack grid-wide" }, [
        el("div", { className: "t-micro", text: "Total sales" }),
        salesBars(days, {
          onSelect: (day) => navigate(`#/reports/${day.business_date}`),
        }) ?? empty("No days in this window."),
        varianceStrip(days),
        el("div", {
          className: "t-caption",
          text:
            "Bar height is relative to the tallest day in this window. " +
            `A day is flagged when its variance exceeds ${format(report.threshold)}.`,
        }),
      ]),

      el("div", { className: "section-label t-micro", text: "Day by day" }),
      days.length
        ? el("div", { className: "list" }, reversed.map((day) => dayRow(day, navigate)))
        : empty("No days in this window."),

      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "Reading this" }),
        el("p", { className: "t-caption", text: report.basis }),
      ]),
    ]),
  );
}

function dayRow(day, navigate) {
  const variance = gapLabel(day.variance, { absent: "not counted" });

  return el(
    "button",
    {
      className: `list-row ${day.requires_review || day.alert ? "flagged" : ""}`,
      attrs: { type: "button" },
      on: { click: () => navigate(`#/reports/${day.business_date}`) },
      style: {
        width: "100%",
        background: "none",
        border: 0,
        textAlign: "left",
        font: "inherit",
        color: "inherit",
        cursor: "pointer",
      },
    },
    [
      el("div", { className: "list-row-main" }, [
        el("div", { className: "t-body", text: businessDate(day.business_date) }),
        el("div", { className: "row", style: { gap: "0.375rem", alignItems: "center" } }, [
          pill(day.source.replace("_", " "), SOURCE_PILL[day.source] ?? "neutral"),
          day.is_finalised ? pill("finalised", "locked") : null,
        ]),
      ]),
      el("div", { className: "col", style: { alignItems: "flex-end" } }, [
        el("div", {
          className: "t-body t-numeric",
          text: format(day.total_sales, { absent: "—" }),
        }),
        el("div", { className: `t-caption t-numeric ${variance.className}`, text: variance.text }),
      ]),
    ],
  );
}

// --- one day ------------------------------------------------------------------

export async function renderDailyReport(container, { session, navigate, businessDate: day }) {
  const { shell } = session;
  shell.setTab("cash");
  shell.setTitle("Day report");
  shell.setActions(
    el("button", {
      className: "btn",
      text: "Back to week",
      attrs: { type: "button" },
      on: { click: () => navigate("#/reports") },
    }),
  );

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let report;
  try {
    report = await api.get(`/reports/daily/${day}`);
  } catch (error) {
    render(
      container,
      errorCard(error, () =>
        renderDailyReport(container, { session, navigate, businessDate: day }),
      ),
    );
    return;
  }

  shell.setTitle("Day report", businessDate(report.business_date));

  const cash = report.cash;

  render(
    container,
    el("div", { className: "grid" }, [
      // --- provenance, first and unmissable -----------------------------------
      el("div", { className: "card stack grid-wide" }, [
        el("div", { className: "t-micro", text: "These figures are" }),
        el("div", { className: "row", style: { gap: "0.5rem", alignItems: "center" } }, [
          pill(cash.source.replace("_", " "), SOURCE_PILL[cash.source] ?? "neutral"),
          el("span", { className: "t-body", text: describeSource(cash.source) }),
        ]),
        cash.unavailable_reason
          ? el("p", {
              className: "t-caption",
              text:
                "This day cannot be calculated: " +
                `${cash.unavailable_reason}. Enter the missing rate and it will appear.`,
            })
          : null,
        cash.requires_review
          ? el("p", { className: "t-caption text-short", text: cash.review_note ?? "Flagged for review." })
          : null,
      ]),

      // --- §13.22, and it goes above the fold when it fires --------------------
      report.breakdown_reconciles === false
        ? el("div", { className: "card stack grid-wide flagged" }, [
            el("div", { className: "t-micro", text: "The fuel breakdown does not match" }),
            el("p", {
              className: "t-body",
              text:
                "This day was reconciled at one figure, but pricing it again now gives " +
                "another. A fuel price was probably backdated beneath it.",
            }),
            row("Recorded that day", report.snapshot_metered_fuel_sales),
            row("Priced again now", report.fuel_sales_total),
            el("p", {
              className: "t-caption",
              text:
                "Neither figure has been changed. The recorded one is what the day was " +
                "reconciled against; check the audit log for who revised the price.",
            }),
          ])
        : null,

      // --- cash ---------------------------------------------------------------
      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "Expected closing" }),
        el("div", {
          className: "t-amount t-numeric",
          text: format(cash.expected_closing, { absent: "not known" }),
        }),
        cash.expected_closing === null && cash.source === "computed"
          ? el("p", {
              className: "t-caption",
              text:
                "No earlier day has been reconciled, so there is no opening balance to " +
                "carry from. An admin seeds the first one.",
            })
          : null,
        divider(),
        row("Opening balance", cash.opening_balance),
        row("Metered fuel sales", cash.metered_fuel_sales),
        row("Non-fuel sales", cash.non_fuel_sales_total),
        row("Total sales", cash.total_sales),
        divider(),
        row("Card", cash.card_total),
        row("UPI", cash.upi_total),
        row("Wallet", cash.wallet_total),
        row("Udhaar issued", cash.credit_sales_total),
        row("Cash repayments", cash.cash_credit_repayments),
        row("Shortfall settlements", cash.cash_shortfall_settlements),
        row("Cash expenses", cash.cash_expenses),
        row("Bank deposits", cash.bank_deposits_total),
        row("Shortfalls booked", cash.shortfalls_booked),
        divider(),
        row("Counted", cash.actual_counted, { absent: "not counted" }),
        varianceRow(cash.variance),
      ]),

      // --- fuel ---------------------------------------------------------------
      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "Fuel" }),
        report.fuel.length
          ? el("div", { className: "list" }, report.fuel.map(fuelRow))
          : empty("Nothing was dispensed on this day."),
        divider(),
        row("Fuel sales", report.fuel_sales_total),
        el("div", { className: "list-row" }, [
          el("div", { className: "list-row-main t-body", text: "Gross fuel margin" }),
          el("div", {
            className: `list-row-value t-body t-numeric${
              report.gross_fuel_margin_total === null ? " t-absent" : ""
            }`,
            text: format(report.gross_fuel_margin_total, { absent: "not known" }),
          }),
        ]),
        report.fuels_missing_margin.length
          ? el("p", {
              className: "t-caption",
              text:
                "No total, because no dealer commission has been entered for " +
                `${report.fuels_missing_margin.join(", ")}. The figure is unknown, not zero.`,
            })
          : null,
        el("p", { className: "t-caption", text: report.profit_basis }),
      ]),

      // --- expenses and shifts ------------------------------------------------
      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "Expenses" }),
        Object.keys(report.expenses_by_category).length
          ? el(
              "div",
              { className: "list" },
              Object.entries(report.expenses_by_category).map(([code, amount]) =>
                el("div", { className: "list-row" }, [
                  el("div", { className: "list-row-main t-body", text: code }),
                  el("div", { className: "list-row-value t-body t-numeric", text: format(amount) }),
                ]),
              ),
            )
          : empty("No expenses on this day."),
        divider(),
        row("Total", report.expenses_total),
      ]),

      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "Shifts" }),
        report.shifts.length
          ? el(
              "div",
              { className: "list" },
              report.shifts.map((shift) =>
                el("div", { className: "list-row" }, [
                  el("div", { className: "list-row-main t-body", text: `Shift ${shift.sequence}` }),
                  el("div", { className: "list-row-value" }, [pill(shift.status, "neutral")]),
                ]),
              ),
            )
          : empty("No shifts on this day."),
      ]),
    ]),
  );
}

function fuelRow(line) {
  const noMargin = line.gross_fuel_margin === null;
  const unit = line.unit_of_measure === "kilogram" ? "kg" : "L";

  return el("div", { className: "list-row" }, [
    el("div", { className: "list-row-main" }, [
      el("div", { className: "t-body", text: line.display_name }),
      el("div", {
        className: "t-caption t-numeric",
        // The unit is read from the fuel, never assumed (§4.5). CBG reads kg.
        text:
          line.quantity === null
            ? "not entered"
            : `${line.quantity} ${unit} · ${format(line.rate_per_unit, { absent: "mixed rate" })}/${unit}`,
      }),
    ]),
    el("div", { className: "col", style: { alignItems: "flex-end" } }, [
      el("div", {
        className: "t-body t-numeric",
        text: format(line.sale_value, { absent: "—" }),
      }),
      el("div", {
        className: `t-caption t-numeric${noMargin ? " t-absent" : ""}`,
        text: noMargin
          ? "no commission entered"
          : `${format(line.gross_fuel_margin)} margin`,
      }),
    ]),
  ]);
}

function row(label, value, { absent = "—" } = {}) {
  return el("div", { className: "list-row" }, [
    el("div", { className: "list-row-main t-body", text: label }),
    el("div", {
      className: `list-row-value t-body t-numeric${value === null ? " t-absent" : ""}`,
      text: format(value, { absent }),
    }),
  ]);
}

function varianceRow(variance) {
  const label = gapLabel(variance, { absent: "not counted" });

  return el("div", { className: "list-row" }, [
    el("div", { className: "list-row-main t-body", text: "Variance" }),
    el("div", {
      className: `list-row-value t-body t-numeric ${label.className}`,
      text: label.text,
    }),
  ]);
}

function divider(text = "") {
  // The same shape `cash_position.js` uses, rather than a new rule in the stylesheet: a
  // divider that is a labelled row reads as a section break in a list of figures, and an
  // unlabelled one still separates without inventing a second visual language.
  return el("div", { className: "list-row" }, [
    el("div", { className: "list-row-main t-micro", text }),
  ]);
}

// --- alerts -------------------------------------------------------------------

const ALERT_LABEL = {
  variance_exceeds_threshold: "Cash variance",
  day_not_reconciled: "Never reconciled",
  summary_requires_review: "Summary flagged",
  unreviewed_flagged_expenses: "Expenses awaiting review",
  reading_requires_review: "Reading flagged",
  open_shift_on_a_past_date: "Shift still open",
};

export async function renderAlerts(container, { session, navigate }, query = {}) {
  const { shell } = session;
  shell.setTab("cash");
  shell.setTitle("Alerts");
  shell.setActions(
    el("button", {
      className: "btn",
      text: "Week",
      attrs: { type: "button" },
      on: { click: () => navigate("#/reports") },
    }),
  );

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let report;
  try {
    report = await api.get("/reports/variance-alerts", {
      from: query.from,
      to: query.to,
    });
  } catch (error) {
    render(
      container,
      errorCard(error, () => renderAlerts(container, { session, navigate }, query)),
    );
    return;
  }

  shell.setTitle("Alerts", `${businessDate(report.from)} – ${businessDate(report.to)}`);

  render(
    container,
    el("div", { className: "stack" }, [
      report.items.length
        ? el("div", { className: "list" }, report.items.map((alert) => alertRow(alert, navigate)))
        : empty("Nothing needs attention in this window."),

      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "Reading this" }),
        el("p", { className: "t-caption", text: report.basis }),
        el("p", {
          className: "t-caption",
          // §13.23's stated limitation, on the screen rather than only in the spec. A user
          // who cannot find a dismiss button should be told there isn't one and why.
          text:
            "There is no dismiss. An alert clears when the thing it points at is dealt " +
            "with — review the expense, reconcile the day, close the shift.",
        }),
      ]),
    ]),
  );
}

function alertRow(alert, navigate) {
  return el(
    "button",
    {
      className: "list-row flagged",
      attrs: { type: "button" },
      on: { click: () => navigate(`#/reports/${alert.business_date}`) },
      style: {
        width: "100%",
        background: "none",
        border: 0,
        textAlign: "left",
        font: "inherit",
        color: "inherit",
        cursor: "pointer",
      },
    },
    [
      el("div", { className: "list-row-main" }, [
        el("div", { className: "t-body", text: ALERT_LABEL[alert.kind] ?? alert.kind }),
        el("div", { className: "t-caption", text: alert.detail }),
        el("div", { className: "t-micro", text: businessDate(alert.business_date) }),
      ]),
      alert.amount !== null
        ? el("div", {
            className: "list-row-value t-body t-numeric",
            text: format(alert.amount),
          })
        : null,
    ],
  );
}

/** Exported so the Cash hub can offer "this week" without duplicating the date rule. */
export function currentWindowEnd() {
  return todayAtOutlet();
}
