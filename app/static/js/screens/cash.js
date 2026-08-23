/* The Cash tab: reconciliation, and who owes what.
 *
 * A hub rather than a single report, because §6.4 and §6.5 answer different questions at
 * different grains: a *shift's* cash position is about one salesman's drawer, and a *day's*
 * summary is about the locker balance that rolls forward. §5.4: "A daily cash summary
 * aggregates one business date across every shift on it."
 *
 * Everything reachable from here is manager-floor or above (§8) -- these are reports about
 * the people doing the data entry, and the person a shortfall would be booked against is the
 * last one who should be able to run the calculation privately before anybody else sees it.
 */

import { el, empty, pill, render } from "../dom.js";
import { api, ApiError } from "../api.js";
import { format, gapLabel } from "../money.js";
import { businessDate } from "../time.js";
import { satisfies } from "../ui/nav.js";
import { errorCard } from "./today.js";

export async function renderCash(container, { session, navigate }) {
  const { shell, me } = session;
  shell.setTab("cash");
  shell.setTitle("Cash");
  shell.setActions();

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let shift = null;
  let outstanding = null;
  let summaries = null;

  try {
    [shift, outstanding, summaries] = await Promise.all([
      api.get("/shifts/current").catch((error) => {
        if (error instanceof ApiError && error.code === "NO_OPEN_SHIFT") return null;
        throw error;
      }),
      api.get("/salesman-shortfalls/outstanding"),
      api.get("/daily-summaries", { limit: 5 }),
    ]);
  } catch (error) {
    render(container, errorCard(error, () => renderCash(container, { session, navigate })));
    return;
  }

  const owed = (outstanding?.items ?? []).filter(
    (entry) => !entry.outstanding.trim().startsWith("-") && entry.outstanding !== "0.00",
  );

  render(
    container,
    el("div", { className: "stack" }, [
      shift
        ? el("div", { className: "card stack" }, [
            el("div", { className: "t-micro", text: "Open shift" }),
            el("div", {
              className: "t-title",
              text: `${businessDate(shift.business_date)} · shift ${shift.sequence}`,
            }),
            el("button", {
              className: "btn btn-primary btn-block",
              text: "Reconcile this shift",
              attrs: { type: "button" },
              on: { click: () => navigate(`#/shifts/${shift.id}/cash-position`) },
            }),
          ])
        : el("div", { className: "card" }, [
            el("p", { className: "t-caption", text: "No shift is currently open." }),
          ]),

      el("div", { className: "section-label t-micro", text: "Salesman balances" }),
      owed.length
        ? el(
            "div",
            { className: "list" },
            owed.map((entry) =>
              el(
                "button",
                {
                  className: "list-row",
                  attrs: { type: "button" },
                  on: { click: () => navigate(`#/salesmen/${entry.salesman_id}/ledger`) },
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
                  el("div", { className: "list-row-main t-body", text: entry.full_name }),
                  el("div", {
                    className: "list-row-value t-body t-numeric text-short",
                    text: format(entry.outstanding),
                  }),
                ],
              ),
            ),
          )
        : empty("Nobody owes a shortfall."),

      // §13.15, said where somebody will see it rather than only in the document: a gap
      // nobody will ever chase stays on that balance permanently, because V1 has no
      // write-off. The balance only ever grows.
      owed.length
        ? el("p", {
            className: "t-caption",
            text: "A shortfall can only be repaid in cash — V1 has no way to write one off, so a small figure nobody will chase stays here.",
          })
        : null,

      el("div", { className: "section-label t-micro", text: "Recent days" }),
      summaries.items.length
        ? el(
            "div",
            { className: "list" },
            summaries.items.map((summary) => summaryRow(summary, navigate)),
          )
        : empty("No daily summaries yet."),

      el("button", {
        className: "btn btn-block",
        text: "All daily summaries",
        attrs: { type: "button" },
        on: { click: () => navigate("#/daily-summaries") },
      }),

      satisfies(me.role, "manager")
        ? el("button", {
            className: "btn btn-block",
            text: "Flagged expenses",
            attrs: { type: "button" },
            on: { click: () => navigate("#/expenses/flagged") },
          })
        : null,
    ]),
  );
}

function summaryRow(summary, navigate) {
  const variance = gapLabel(summary.variance, { absent: "not counted" });

  return el(
    "button",
    {
      className: `list-row ${summary.requires_review ? "flagged" : ""}`,
      attrs: { type: "button" },
      on: { click: () => navigate(`#/daily-summaries/${summary.business_date}`) },
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
        el("div", { className: "t-body", text: businessDate(summary.business_date) }),
        el("div", { className: "t-caption", text: summary.is_finalised ? "finalised" : "open" }),
      ]),
      el("div", { className: `list-row-value t-body t-numeric ${variance.className}`, text: variance.text }),
    ],
  );
}
