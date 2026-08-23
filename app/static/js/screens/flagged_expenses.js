/* The flagged-expense queue, and the month-end summary (CLAUDE.md §6.7, §8).
 *
 * ## Why a flag is never cleared by anything but a person
 *
 * §6.7: "Flags are never auto-cleared, including when a reversal drops a group back under the
 * threshold. Auto-clearing would erase a control signal silently; a human clears a flag
 * through the review route."
 *
 * That is what makes this a *queue* rather than a report. A shift cannot be locked while it
 * holds an unreviewed flagged expense (409 UNREVIEWED_EXPENSES_EXIST), so this list is the
 * thing standing between a day and being closed out.
 *
 * ## Two rules produce a flag, and the second is the one that matters
 *
 * An amount over `EXPENSE_REVIEW_THRESHOLD`, **or** a whole category's daily total crossing
 * it. §6.7: "A single ₹1,000 rule is trivially defeated by two ₹600 entries; without this,
 * the control is theatre." And when the aggregate rule fires it flags **every live unreviewed
 * row in that group**, not only the one that crossed the line -- flagging only the last one
 * "shows a manager a trivial amount and hides the ₹1,300 pattern the rule exists to surface."
 *
 * So rows are grouped by date and category below, because that grouping *is* the signal. A
 * flat list of amounts loses exactly the pattern the aggregate rule was written to reveal.
 *
 * Reviewing happens on the expense screen for its shift, which is where the full row, its
 * receipt and its reversal history are -- this screen routes there rather than duplicating a
 * review form with less context around it.
 */

import { el, empty, pill, render } from "../dom.js";
import { api, explain } from "../api.js";
import { format } from "../money.js";
import { businessDate, todayAtOutlet } from "../time.js";
import { field, Form } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { notify } from "../ui/toast.js";
import { errorCard } from "./today.js";

export async function renderFlaggedExpenses(container, { session, navigate }) {
  const { shell } = session;
  shell.setTab("cash");
  shell.setTitle("Flagged expenses");
  shell.setActions();

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let page;
  try {
    page = await api.get("/expenses/flagged", { limit: 100 });
  } catch (error) {
    render(
      container,
      errorCard(error, () => renderFlaggedExpenses(container, { session, navigate })),
    );
    return;
  }

  // Grouped by (business_date, category) -- the same key §6.7's aggregate rule uses.
  const groups = new Map();
  for (const expense of page.items) {
    const key = `${expense.business_date}|${expense.category_code}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(expense);
  }

  render(
    container,
    el("div", { className: "stack" }, [
      el("div", { className: "card stack" }, [
        el("p", {
          className: "t-caption",
          text: "A shift cannot be locked while it holds an unreviewed flagged expense. Reviewing one is a decision with your name on it — flags are never cleared automatically, not even when a reversal drops the day back under the threshold.",
        }),
        el("button", {
          className: "btn btn-block",
          text: "Month-end summary",
          attrs: { type: "button" },
          on: { click: () => summarySheet() },
        }),
      ]),

      page.items.length
        ? el("div", { className: "stack" }, [...groups.entries()].map(([key, expenses]) => {
            const [date, category] = key.split("|");
            return el("div", { className: "card stack" }, [
              el("div", { className: "row-between" }, [
                el("div", {}, [
                  el("div", { className: "t-headline", text: category }),
                  el("div", { className: "t-caption", text: businessDate(date) }),
                ]),
                expenses.length > 1 ? pill(`${expenses.length} together`, "review") : null,
              ]),
              expenses.length > 1
                ? el("p", {
                    className: "t-caption",
                    text: "These were flagged as a group: individually small, together over the threshold.",
                  })
                : null,
              el("div", { className: "list" }, expenses.map((expense) =>
                el(
                  "button",
                  {
                    className: "list-row",
                    attrs: { type: "button" },
                    on: { click: () => navigate(`#/shifts/${expense.shift_id}/expenses`) },
                    style: {
                      width: "100%", background: "none", border: 0, textAlign: "left",
                      font: "inherit", color: "inherit", cursor: "pointer",
                    },
                  },
                  [
                    el("div", { className: "list-row-main" }, [
                      el("div", { className: "t-body truncate", text: expense.description }),
                      el("div", { className: "t-caption", text: "Review on its shift ›" }),
                    ]),
                    el("div", {
                      className: "list-row-value t-body t-numeric",
                      text: format(expense.amount),
                    }),
                  ],
                ),
              )),
            ]);
          }))
        : empty("Nothing flagged. Every expense is either under the threshold or already reviewed."),

      page.next_cursor
        ? el("p", {
            className: "t-caption",
            text: "There are more flagged expenses than this page lists.",
          })
        : null,
    ]),
  );
}

/* --- month-end summary ---------------------------------------------------------
 *
 * `GET /expenses/summary` takes literal `from` / `to` query names -- aliased server-side
 * around the reserved word -- and echoes them back the same way. Renaming them to
 * start/end here would 422.
 */

function summarySheet() {
  const today = todayAtOutlet();
  const monthStart = `${today.slice(0, 8)}01`;

  const from = field({
    name: "from",
    label: "From",
    type: "date",
    value: monthStart,
    required: true,
  });
  const to = field({
    name: "to",
    label: "To",
    type: "date",
    value: today,
    required: true,
    hint: "Up to 366 days.",
  });

  const form = new Form({ from, to });
  const results = el("div", { className: "stack" });

  const run = el("button", {
    className: "btn btn-primary btn-block",
    text: "Show totals",
    attrs: { type: "button" },
  });

  run.addEventListener("click", async () => {
    form.clearErrors();
    run.disabled = true;
    try {
      const values = form.values();
      const summary = await api.get("/expenses/summary", {
        from: values.from,
        to: values.to,
      });
      const rows = Object.entries(summary.totals_by_category ?? {});
      results.replaceChildren(
        el("div", { className: "list" }, [
          ...rows.map(([code, amount]) =>
            el("div", { className: "list-row" }, [
              el("div", { className: "list-row-main t-body", text: code }),
              el("div", { className: "list-row-value t-body t-numeric", text: format(amount) }),
            ]),
          ),
          el("div", { className: "list-row" }, [
            el("div", { className: "list-row-main t-headline", text: "Total" }),
            el("div", {
              className: "list-row-value t-headline t-numeric",
              text: format(summary.total),
            }),
          ]),
        ]),
        el("p", {
          className: "t-caption",
          text: "Every mode, not only cash. A bank-paid bill is a real expense even though it never touched the drawer.",
        }),
      );
    } catch (error) {
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    } finally {
      run.disabled = false;
    }
  });

  openSheet({
    title: "Month-end expense summary",
    body: el("div", { className: "stack" }, [...form.nodes(), run, results]),
  });
}
