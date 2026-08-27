/* Reports: the week, and what needs looking at.
 *
 * Phase 13, trimmed in Phase 15. Two screens now, not three: the per-day report moved to
 * `days.js`, because `#/reports/{date}` and `#/daily-summaries/{date}` were describing the
 * same business date from two tables with no link between them (§11 phase 15). What is left
 * here is the two *windowed* reads -- the week, and the alerts over it. Both are manager-floor
 * (§8) and both are read-only.
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
import { businessDate } from "../time.js";
import { salesBars, varianceStrip } from "../ui/chart.js";
import { errorCard } from "./today.js";

/** The pill kind each `source` gets. Snapshot is the only "settled" state.
 *
 * Exported since Phase 15: `days.js` renders the same provenance on the day screen, and two
 * copies of this map would eventually disagree about what `unavailable` looks like. */
export const SOURCE_PILL = {
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
          onSelect: (day) => navigate(`#/days/${day.business_date}`),
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
      on: { click: () => navigate(`#/days/${day.business_date}`) },
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
      on: { click: () => navigate(`#/days/${alert.business_date}`) },
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

