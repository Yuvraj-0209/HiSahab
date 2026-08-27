/* The Cash tab: what needs doing, and who owes what.
 *
 * ## Phase 15: a worklist, not a menu
 *
 * This screen used to be six links and a list of the summary rows that happened to exist. A
 * day was entered, closed and locked, and then could not be found anywhere on it -- because
 * `GET /daily-summaries` reads one table, so a date that traded and was never reconciled is
 * structurally invisible there, and nothing said it was waiting.
 *
 * So the top of the tab is now **the days that need you, oldest first**, each showing where
 * it stands and offering the one act that moves it on. §4.7's principle at the interface: the
 * abnormal day becomes visible rather than reassigned to nobody's attention. The merge and
 * the lifecycle model both live in `days.js`; this screen composes them.
 *
 * Oldest first is not a presentation choice. §6.5's opening balance chains from the most
 * recent summary, so reconciling out of order skips a day's cash permanently -- and the
 * button is offered on exactly one day for that reason.
 *
 * ## "Reconcile this shift" was a lie, and this is where it was told
 *
 * A primary button here read *"Reconcile this shift"* and its entire action was to navigate
 * to `GET /shifts/{id}/cash-position`, which §8 requires to write nothing. Somebody pressed
 * it, believed the day was settled, and no row was ever created. §14 now carries the general
 * rule; what it costs here is one honest label. The screen behind it shows a figure, so it is
 * named for the figure it shows.
 *
 * Everything reachable from here is manager-floor or above (§8) -- these are reports about
 * the people doing the data entry, and the person a shortfall would be booked against is the
 * last one who should be able to run the calculation privately before anybody else sees it.
 */

import { el, empty, render } from "../dom.js";
import { api, ApiError } from "../api.js";
import { format } from "../money.js";
import { businessDate } from "../time.js";
import { satisfies } from "../ui/nav.js";
import { errorCard } from "./today.js";
import { dayRow, loadDays, oldestUnreconciled, worklistCard } from "./days.js";

/** How many settled days the hub lists before deferring to the full list. */
const RECENT = 8;

export async function renderCash(container, { session, navigate }) {
  const { shell, me } = session;
  shell.setTab("cash");
  shell.setTitle("Cash");
  shell.setActions();

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let shift = null;
  let outstanding = null;
  let days = null;

  try {
    [shift, outstanding, days] = await Promise.all([
      api.get("/shifts/current").catch((error) => {
        if (error instanceof ApiError && error.code === "NO_OPEN_SHIFT") return null;
        throw error;
      }),
      api.get("/salesman-shortfalls/outstanding"),
      loadDays(),
    ]);
  } catch (error) {
    render(container, errorCard(error, () => renderCash(container, { session, navigate })));
    return;
  }

  const reload = () => renderCash(container, { session, navigate });
  const context = { session, navigate, container, reload };
  const unblocked = oldestUnreconciled(days);

  // Unfinished days, oldest first. The open shift has its own card above, so it is not
  // repeated here -- it is not waiting on anybody, it is being typed in.
  const needsYou = days
    .filter((day) => !(day.summary && day.summary.is_finalised))
    .filter((day) => !day.shifts.some((entry) => entry.status === "open"))
    .reverse();

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
              className: "btn btn-block",
              // Named for what it shows, never for what a reader might wish it did. §14.
              text: "Cash position",
              attrs: { type: "button" },
              on: { click: () => navigate(`#/shifts/${shift.id}/cash-position`) },
            }),
            el("p", {
              className: "t-caption",
              text: "Calculates what this salesman should be holding and shows the gap. It is a report — nothing is written, and the day is reconciled once every shift on it is closed.",
            }),
          ])
        : el("div", { className: "card" }, [
            el("p", { className: "t-caption", text: "No shift is currently open." }),
          ]),

      el("div", { className: "section-label t-micro", text: "Needs you" }),
      needsYou.length
        ? el(
            "div",
            { className: "stack" },
            needsYou.map((day) => worklistCard(day, { me, navigate, context, unblocked })),
          )
        : empty("Every day that traded has been reconciled and finalised."),

      el("div", { className: "section-label t-micro", text: "Trading days" }),
      days.length
        ? el(
            "div",
            { className: "list" },
            days.slice(0, RECENT).map((day) => dayRow(day, { me, navigate, unblocked })),
          )
        : empty("No trading day has been entered yet."),

      el("button", {
        className: "btn btn-block",
        text: "All days",
        attrs: { type: "button" },
        on: { click: () => navigate("#/days") },
      }),

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

      // Deliberately last. This hub is for doing the reconciliation; these three are for
      // looking back at it once it is done.
      el("div", { className: "section-label t-micro", text: "Look back" }),
      satisfies(me.role, "manager")
        ? el("button", {
            className: "btn btn-block",
            text: "Flagged expenses",
            attrs: { type: "button" },
            on: { click: () => navigate("#/expenses/flagged") },
          })
        : null,
      el("button", {
        className: "btn btn-block",
        text: "Reports — the latest trading week",
        attrs: { type: "button" },
        on: { click: () => navigate("#/reports") },
      }),
      el("button", {
        className: "btn btn-block",
        text: "Alerts",
        attrs: { type: "button" },
        on: { click: () => navigate("#/reports/alerts") },
      }),
    ]),
  );
}
