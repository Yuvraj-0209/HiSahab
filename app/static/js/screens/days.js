/* A business date, and everything that is true about it.
 *
 * Phase 15. This module exists because of a report that a day had been entered, closed and
 * locked and then could not be found -- and because chasing that turned up three separate
 * ways the Cash tab could not describe a day.
 *
 * ## A missing row is not a missing day
 *
 * `GET /daily-summaries` reads one table. That is correct for what it is, and it means a
 * business date that traded and was never reconciled is *structurally* invisible there --
 * which is exactly the date somebody needs to act on. §14: do not let the absence of a row
 * read as the absence of a day.
 *
 * So the list here is a **client-side merge** of `GET /shifts` with `GET /daily-summaries`,
 * keyed on `business_date`. No new endpoint: both already exist, both are already
 * manager-floor (§8), and which rows belong to which day is a presentation question.
 *
 * ## The lifecycle is drawn rather than documented
 *
 *     Entered → Closed → Locked → Reconciled → Finalised
 *
 * Two of those steps are per *shift* and two are per *day*, which is the whole reason the
 * sequence confuses somebody meeting it for the first time. Prose in a manual does not fix
 * that; a strip of five dots on every row does, because it is in front of them at the moment
 * they are wondering.
 *
 * `actual_counted` is deliberately **not** a step. §6.5 is explicit that under this outlet's
 * locker model most days are never counted, so making it one would mark every normal day
 * incomplete and teach a reader to ignore the strip.
 *
 * ## Oldest first, and only the oldest may be reconciled
 *
 * §6.5's opening balance chains from the most recent *summary*, not from yesterday. So
 * reconciling the 4th before the 3rd chains the 4th's opening from the 2nd and skips a day's
 * cash -- permanently, since §5.2 stores `expected_closing` precisely so nothing can rewrite
 * it later. The server refuses that with 409 `EARLIER_DAY_NOT_RECONCILED`; this screen sorts
 * its worklist oldest-first and offers the button on one day only, so nobody has to be
 * refused to find out.
 *
 * ## One day, one screen
 *
 * `#/daily-summaries/{date}` and `#/reports/{date}` described the same date from two tables
 * and never linked to each other. `renderDay` is the merge: the report's provenance, cash,
 * fuel, expenses and shifts, plus the summary's review banner, count and finalise controls.
 *
 * §13.20 is untouched by that merge and must stay so. A `snapshot` day is what a manager was
 * *told*; a `computed` day is an estimate derived seconds ago and can still move. The screen
 * goes on saying which, in words, and the stored component list renders **only** for a
 * snapshot -- because for a computed day there is nothing stored to show.
 */

import { el, empty, pill, render } from "../dom.js";
import { api, ApiError, explain } from "../api.js";
import { format, gapLabel } from "../money.js";
import { businessDate } from "../time.js";
import { describeSource } from "../ui/chart.js";
import { notify } from "../ui/toast.js";
import { satisfies } from "../ui/nav.js";
import { errorCard } from "./today.js";
import { SOURCE_PILL } from "./reports.js";
import {
  SOURCE_LABEL,
  countSheet,
  createSheet,
  finaliseControls,
  termRow,
} from "./daily_summaries.js";

/** The five steps a business date passes through, in order. See the module docstring. */
export const LIFECYCLE = ["Entered", "Closed", "Locked", "Reconciled", "Finalised"];

/** How many days the hub and the list ask for in one go. */
const PAGE = 40;

// --- the merge ----------------------------------------------------------------

/**
 * Fold shifts and summaries into one row per business date, newest first.
 *
 * ISO dates compare correctly as strings, so no `Date` is constructed -- §3 rule 4 and
 * `time.js`'s rule that a business date is a calendar day and is never timezone-converted.
 */
export function mergeDays(shifts, summaries) {
  const byDate = new Map();

  const entryFor = (date) => {
    let entry = byDate.get(date);
    if (!entry) {
      entry = { business_date: date, shifts: [], summary: null };
      byDate.set(date, entry);
    }
    return entry;
  };

  for (const shift of shifts) entryFor(shift.business_date).shifts.push(shift);
  for (const summary of summaries) entryFor(summary.business_date).summary = summary;

  return [...byDate.values()].sort((a, b) =>
    a.business_date < b.business_date ? 1 : a.business_date > b.business_date ? -1 : 0,
  );
}

/** The oldest merged day that traded and has no summary, or null. Mirrors §6.5's server rule. */
export function oldestUnreconciled(days) {
  const pending = days.filter((day) => day.shifts.length > 0 && day.summary === null);
  if (!pending.length) return null;
  return pending[pending.length - 1].business_date; // days are newest-first
}

/**
 * Where a day stands, and the one act that would move it on.
 *
 * `reached` counts completed steps out of `LIFECYCLE.length`. `action` is null when the day
 * is either finished or waiting on somebody else's turn -- never a button that would be
 * refused, which §8 says is politeness rather than a control, and the server enforces anyway.
 */
export function dayState(day, { role = "manager", unblocked = null } = {}) {
  const { shifts, summary } = day;
  const open = shifts.find((shift) => shift.status === "open");
  const unlocked = shifts.filter((shift) => shift.status !== "locked");

  if (open) {
    return {
      reached: 0,
      label: "Entry in progress",
      kind: "open",
      hint: "This shift is still open. Finish typing it in, then close it.",
      action: { text: "Continue entry", hash: `#/shifts/${open.id}` },
    };
  }

  if (shifts.length && unlocked.length) {
    return {
      reached: 2,
      label: "Closed",
      kind: "closed",
      hint: "Entry is finished and figures can now only be corrected by reversal. An admin locks the shift to make it permanent.",
      action: satisfies(role, "admin")
        // Named for what pressing it does -- it navigates. §14: a verb on a button is a
        // promise about what the server will be asked to do, and locking happens on the
        // shift screen, one tap further on.
        ? { text: "Open the shift to lock it", hash: `#/shifts/${unlocked[0].id}` }
        : null,
    };
  }

  if (summary === null) {
    // The 2 July case: every shift locked, and nothing carried the day's cash forward.
    return {
      reached: 3,
      label: "Not reconciled",
      kind: "closed",
      hint: "Every shift is locked. Reconciling stores this day's cash position and carries the balance into the next day.",
      action:
        unblocked === null || unblocked === day.business_date
          ? { text: "Reconcile this day", reconcile: true, primary: true }
          : null,
      blockedBy: unblocked && unblocked !== day.business_date ? unblocked : null,
    };
  }

  if (summary.is_finalised) {
    return {
      reached: 5,
      label: "Finalised",
      kind: "locked",
      hint: "This day is settled and its figures are frozen.",
      action: null,
    };
  }

  return {
    reached: 4,
    label: summary.requires_review ? "Needs review" : "Reconciled",
    kind: summary.requires_review ? "review" : "open",
    hint: summary.requires_review
      ? "A shift beneath this day moved after it was reconciled. Nothing was recomputed — a human reconciles the two."
      : "The cash position is stored. An admin finalises the day to freeze it.",
    action: satisfies(role, "admin")
      ? { text: "Open the day to finalise it", hash: `#/days/${day.business_date}` }
      : null,
  };
}

/** Five dots, filled up to `reached`, with the state named beside them. */
export function lifecycleStrip(state) {
  return el("div", { className: "lifecycle", attrs: { "aria-label": `${state.label} — step ${state.reached} of ${LIFECYCLE.length}` } }, [
    el(
      "div",
      { className: "lifecycle-dots", attrs: { "aria-hidden": "true" } },
      LIFECYCLE.map((step, index) =>
        el("span", {
          className: `lifecycle-dot ${index < state.reached ? "is-done" : ""}`,
          attrs: { title: step },
        }),
      ),
    ),
    el("span", { className: "t-caption", text: state.label }),
  ]);
}

/**
 * Reconcile a day, straight from the row.
 *
 * §6.5's anchor is the one case that cannot be a one-tap act: the very first day at an outlet
 * has no predecessor to chain from, so an admin has to type what was in the locker. The server
 * says so with `OPENING_BALANCE_REQUIRED`, and the sheet is the only thing that can ask.
 */
export async function reconcileDay(date, context) {
  try {
    await api.post("/daily-summaries", { business_date: date });
    notify.success("Day reconciled.");
    context.navigate(`#/days/${date}`);
  } catch (error) {
    if (error instanceof ApiError && error.code === "OPENING_BALANCE_REQUIRED") {
      createSheet({ ...context, businessDate: date });
      return;
    }
    notify.error(explain(error), { requestId: error.requestId });
  }
}

// --- the list -----------------------------------------------------------------

export async function renderDays(container, { session, navigate }) {
  const { shell, me } = session;
  shell.setTab("cash");
  shell.setTitle("Days");
  shell.setActions();

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let days;
  try {
    days = await loadDays();
  } catch (error) {
    render(container, errorCard(error, () => renderDays(container, { session, navigate })));
    return;
  }

  const unblocked = oldestUnreconciled(days);

  render(
    container,
    el("div", { className: "stack" }, [
      days.length
        ? el("div", { className: "list" }, days.map((day) => dayRow(day, { me, navigate, unblocked })))
        : empty("No trading day has been entered yet."),

      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "Reading this" }),
        el("p", {
          className: "t-caption",
          text: `Every date the outlet traded appears here, whether or not it has been reconciled — the ${LIFECYCLE.join(" · ")} strip says how far each one has got.`,
        }),
      ]),
    ]),
  );
}

/** The two reads the merge needs. Shared by this screen and the Cash hub. */
export async function loadDays() {
  const [shifts, summaries] = await Promise.all([
    api.get("/shifts", { limit: PAGE }),
    api.get("/daily-summaries", { limit: PAGE }),
  ]);
  return mergeDays(shifts.items, summaries.items);
}

/** One row in a day list. Used by this screen and by the Cash hub's "Trading days". */
export function dayRow(day, { me, navigate, unblocked }) {
  const state = dayState(day, { role: me.role, unblocked });
  const variance = gapLabel(day.summary?.variance ?? null, { absent: "not counted" });

  return el(
    "button",
    {
      className: `list-row ${day.summary?.requires_review ? "flagged" : ""}`,
      attrs: { type: "button" },
      on: { click: () => navigate(`#/days/${day.business_date}`) },
      style: rowButton,
    },
    [
      el("div", { className: "list-row-main" }, [
        el("div", { className: "t-body", text: businessDate(day.business_date) }),
        lifecycleStrip(state),
      ]),
      el("div", { className: "col", style: { alignItems: "flex-end" } }, [
        el("div", {
          className: "t-body t-numeric",
          text: format(day.summary?.expected_closing ?? null, { absent: "—" }),
        }),
        el("div", { className: `t-caption t-numeric ${variance.className}`, text: variance.text }),
      ]),
    ],
  );
}

/** A row's worth of "and here is the thing to do about it", for the worklist. */
export function worklistCard(day, { me, navigate, context, unblocked }) {
  const state = dayState(day, { role: me.role, unblocked });

  return el("div", { className: `card stack ${day.summary?.requires_review ? "flagged" : ""}` }, [
    el("div", { className: "row-between" }, [
      el("div", { className: "t-title", text: businessDate(day.business_date) }),
      pill(state.label, state.kind ?? "neutral"),
    ]),
    lifecycleStrip(state),
    el("p", { className: "t-caption", text: state.hint }),

    state.blockedBy
      ? el("p", {
          className: "t-caption",
          // §6.5: not a permission problem, an ordering one -- so it names the day to do first.
          text: `Reconcile ${businessDate(state.blockedBy)} first — the opening balance chains from the day before.`,
        })
      : null,

    state.action ? actionButton(day, state, { navigate, context }) : null,

    el("button", {
      className: "btn btn-plain",
      text: "Open the day",
      attrs: { type: "button" },
      on: { click: () => navigate(`#/days/${day.business_date}`) },
    }),
  ]);
}

/** The worklist's one act, with an in-flight guard.
 *
 * §6.10 gives `POST /daily-summaries` no `Idempotency-Key`, correctly: `UNIQUE (outlet_id,
 * business_date)` makes it naturally idempotent, and a retry gets 409 SUMMARY_ALREADY_EXISTS
 * and creates nothing. That is the right server answer and a confusing thing to read on a
 * double-tap, right after a success toast for the same act -- so the button disables itself
 * while the request is out. Nothing about correctness rests on it.
 */
function actionButton(day, state, { navigate, context }) {
  const button = el("button", {
    className: `btn btn-block ${state.action.primary ? "btn-primary" : ""}`,
    text: state.action.text,
    attrs: { type: "button" },
  });

  button.addEventListener("click", async () => {
    if (!state.action.reconcile) {
      navigate(state.action.hash);
      return;
    }
    button.disabled = true;
    try {
      await reconcileDay(day.business_date, context);
    } finally {
      button.disabled = false;
    }
  });

  return button;
}

const rowButton = {
  width: "100%",
  background: "none",
  border: 0,
  textAlign: "left",
  font: "inherit",
  color: "inherit",
  cursor: "pointer",
};

// --- one day ------------------------------------------------------------------
export async function renderDay(container, { session, navigate, businessDate: day }) {
  const { shell, me } = session;
  shell.setTab("cash");
  shell.setTitle("Day");
  shell.setActions(
    el("button", {
      className: "btn",
      text: "All days",
      attrs: { type: "button" },
      on: { click: () => navigate("#/days") },
    }),
  );

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  const reload = () => renderDay(container, { session, navigate, businessDate: day });

  let report;
  let days;
  try {
    // The report is the figures; the merge is what tells this screen where the day *stands*
    // and therefore which act to offer. Two reads for the second half is the cost of not
    // adding an endpoint, and they are both cheap capped lists.
    [report, days] = await Promise.all([api.get(`/reports/daily/${day}`), loadDays()]);
  } catch (error) {
    render(container, errorCard(error, reload));
    return;
  }

  shell.setTitle("Day", businessDate(report.business_date));

  const cash = report.cash;
  const merged =
    days.find((entry) => entry.business_date === day) ??
    { business_date: day, shifts: [], summary: null };
  const summary = merged.summary;
  const state = dayState(merged, { role: me.role, unblocked: oldestUnreconciled(days) });
  const context = { session, navigate, container, businessDate: day, summary, reload };

  render(
    container,
    el("div", { className: "grid" }, [
      // --- where this day stands, and the one act that moves it on ------------
      el("div", { className: "card stack grid-wide" }, [
        el("div", { className: "row-between" }, [
          el("div", { className: "t-micro", text: "This day is" }),
          pill(state.label, state.kind ?? "neutral"),
        ]),
        lifecycleStrip(state),
        el("p", { className: "t-caption", text: state.hint }),
        state.blockedBy
          ? el("p", {
              className: "t-caption",
              text: `Reconcile ${businessDate(state.blockedBy)} first — the opening balance chains from the day before.`,
            })
          : null,
        state.action && state.action.reconcile
          ? el("button", {
              className: "btn btn-primary btn-block",
              text: state.action.text,
              attrs: { type: "button" },
              on: { click: () => reconcileDay(day, context) },
            })
          : null,
      ]),

      // --- §13.16's flag, and it says out loud that nothing moved --------------
      summary?.requires_review
        ? el("div", { className: "card stack grid-wide flagged" }, [
            pill("needs review", "review"),
            el("p", {
              className: "t-caption",
              text:
                summary.review_note ??
                "A shift beneath this day was reopened after it was finalised. Nothing was recomputed — the figures below are as they stood.",
            }),
          ])
        : null,
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
                el(
                  "button",
                  {
                    className: "list-row",
                    attrs: { type: "button" },
                    on: { click: () => navigate(`#/shifts/${shift.id}`) },
                    style: rowButton,
                  },
                  [
                    el("div", { className: "list-row-main t-body", text: `Shift ${shift.sequence}` }),
                    el("div", { className: "list-row-value" }, [pill(shift.status, "neutral")]),
                  ],
                ),
              ),
            )
          : empty("No shifts on this day."),
      ]),

      // --- the acts, and only the ones this day can actually take -------------
      //
      // Everything below needs a summary to exist. A day with none is not "a day with a
      // blank count" -- it has never been reconciled at all, and offering a count field
      // against nothing would invite exactly the §14 confusion between an absent figure and
      // a zero one.
      summary
        ? el("div", { className: "card stack" }, [
            el("div", { className: "t-micro", text: "The count" }),
            el("p", {
              className: "t-caption",
              text: "The physical cash in the locker is what carries forward, not the theoretical figure — so a shortage stays visible instead of disappearing into tomorrow. Most days are never counted, and that is normal.",
            }),
            !summary.is_finalised
              ? el("button", {
                  className: "btn btn-block",
                  text: summary.actual_counted === null ? "Record a count" : "Update the count",
                  attrs: { type: "button" },
                  on: { click: () => countSheet(context) },
                })
              : null,
          ])
        : null,

      // The stored component snapshot -- §5.2 -- and **only** for a snapshot day. On a
      // computed day there is nothing stored to show, and rendering the live figures under
      // this heading would claim a permanence they do not have (§13.20).
      summary && cash.source === "snapshot"
        ? el("div", { className: "card stack grid-wide" }, [
            el("div", { className: "t-micro", text: "As it stood on the day" }),
            el("p", {
              className: "t-caption",
              text: `Opening balance ${format(summary.opening_balance)} — ${
                SOURCE_LABEL[summary.opening_balance_source] ?? summary.opening_balance_source
              }.`,
            }),
            el("div", { className: "list" }, [
              termRow("Metered fuel sales", summary.metered_fuel_sales),
              termRow("Non-fuel sales", summary.non_fuel_sales_total),
              termRow("Card", summary.card_total),
              termRow("UPI", summary.upi_total),
              termRow("Wallet", summary.wallet_total),
              termRow("Credit sales", summary.credit_sales_total),
              termRow("Cash repayments", summary.cash_credit_repayments),
              termRow("Cash settlements", summary.cash_shortfall_settlements),
              termRow("Cash expenses", summary.cash_expenses),
              termRow("Bank deposits", summary.bank_deposits_total),
              termRow("Shortfalls booked", summary.shortfalls_booked),
            ]),
            el("p", {
              className: "t-caption",
              text: "Stored, not recomputed — so the total and its own explanation cannot drift apart six months from now.",
            }),
          ])
        : null,

      summary?.notes
        ? el("div", { className: "card" }, [el("p", { className: "t-body", text: summary.notes })])
        : null,

      summary && satisfies(me.role, "admin") ? finaliseControls(summary, context) : null,
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