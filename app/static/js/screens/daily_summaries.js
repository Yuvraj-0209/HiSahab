/* The daily cash summary: §6.5's rolling balance, and what makes it a snapshot.
 *
 * ## expected_closing is stored, not recomputed, and this screen shows the stored figure
 *
 * §5.2: "If a calculation bug is fixed six months from now, you still need to know what the
 * system told the manager on that day. A recomputed-on-read figure destroys that record."
 * Every component is stored for the same reason -- a manager looking at a ₹300 variance needs
 * the breakdown **as it stood**, not as recomputed after a reversal landed underneath it,
 * because otherwise the total and its own explanation disagree.
 *
 * So this screen renders what the row holds and never derives anything from it.
 *
 * ## The opening balance chains, and the source says which branch produced it
 *
 * §6.5:
 *     actual_counted(N−1)      if day N−1 was counted        -> source "counted"
 *     expected_closing(N−1)    if it was not                 -> source "carried"
 *     an admin-seeded figure   if there is no day N−1 at all -> source "seeded"
 *
 * `opening_balance_source` exists precisely so a reader never has to infer which happened, so
 * it is labelled rather than left as an enum value.
 *
 * §14: carrying from expected_closing when the day WAS counted is a listed failure mode --
 * §6.5 exists so a ₹200 shortage does not vanish into the next day's opening.
 *
 * ## Finalising is terminal
 *
 * Creating the row needs every shift closed or locked; finalising needs them all **locked**
 * (§5.2), because that is the only state in which the snapshot is guaranteed to stay true.
 * An admin may unfinalise with a mandatory reason.
 */

import { el, empty, pill, render } from "../dom.js";
import { api, explain } from "../api.js";
import { format, gapLabel } from "../money.js";
import { businessDate, todayAtOutlet } from "../time.js";
import { field, Form } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { notify } from "../ui/toast.js";
import { satisfies } from "../ui/nav.js";
import { errorCard } from "./today.js";

const SOURCE_LABEL = {
  counted: "carried from yesterday's physical count",
  carried: "carried from yesterday's expected closing — nobody counted",
  seeded: "seeded by an admin — the first day",
};

export async function renderDailySummaries(container, { session, navigate }) {
  const { shell } = session;
  shell.setTab("cash");
  shell.setTitle("Daily summaries");

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let page;
  try {
    page = await api.get("/daily-summaries", { limit: 30 });
  } catch (error) {
    render(container, errorCard(error, () => renderDailySummaries(container, { session, navigate })));
    return;
  }

  const context = { session, container, navigate };

  shell.setActions(
    el("button", {
      className: "btn btn-primary",
      text: "New",
      attrs: { type: "button" },
      on: { click: () => createSheet(context) },
    }),
  );

  render(
    container,
    el("div", { className: "stack" }, [
      page.items.length
        ? el(
            "div",
            { className: "list" },
            page.items.map((summary) =>
              el(
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
                    el("div", {
                      className: "t-caption",
                      text: summary.is_finalised ? "finalised" : "open",
                    }),
                  ]),
                  el("div", {
                    className: "list-row-value t-body t-numeric",
                    text: format(summary.expected_closing),
                  }),
                ],
              ),
            ),
          )
        : empty("No daily summaries yet. Create one once every shift on a date is closed."),

      page.truncated
        ? el("div", {
            className: "truncation-notice t-caption",
            text: "There are more days than this view lists.",
          })
        : null,
    ]),
  );
}

/* --- one day ------------------------------------------------------------------- */

export async function renderDailySummary(container, { session, navigate, businessDate: date }) {
  const { shell } = session;
  shell.setTab("cash");
  shell.setTitle("Day", businessDate(date));
  shell.setActions();

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let summary;
  try {
    summary = await api.get(`/daily-summaries/${date}`);
  } catch (error) {
    render(
      container,
      errorCard(error, () => renderDailySummary(container, { session, navigate, businessDate: date })),
    );
    return;
  }

  const context = { session, container, navigate, businessDate: date, summary };
  const variance = gapLabel(summary.variance, { absent: "not counted" });
  const isAdmin = satisfies(session.me.role, "admin");

  render(
    container,
    el("div", { className: "stack" }, [
      summary.requires_review
        ? el("div", { className: "card stack flagged" }, [
            pill("needs review", "review"),
            el("p", {
              className: "t-caption",
              text:
                summary.review_note ??
                "A shift beneath this day was reopened after it was finalised. Nothing was recomputed — the figures below are as they stood.",
            }),
          ])
        : null,

      el("div", { className: "card stack" }, [
        el("div", { className: "row-between" }, [
          el("div", { className: "t-micro", text: "Expected closing" }),
          summary.is_finalised ? pill("finalised", "locked") : pill("open", "open"),
        ]),
        el("div", { className: "t-amount", text: format(summary.expected_closing) }),
        el("p", {
          className: "t-caption",
          text: "The figure the system computed and showed on the day. It is stored, not recomputed — so it still says what the manager was told.",
        }),
      ]),

      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "The count" }),
        el("div", { className: "list" }, [
          el("div", { className: "list-row" }, [
            el("div", { className: "list-row-main t-body", text: "Actually counted" }),
            summary.actual_counted === null
              ? el("div", { className: "list-row-value t-absent", text: "not counted" })
              : el("div", {
                  className: "list-row-value t-body t-numeric",
                  text: format(summary.actual_counted),
                }),
          ]),
          el("div", { className: "list-row" }, [
            el("div", { className: "list-row-main t-body", text: "Variance" }),
            el("div", {
              className: `list-row-value t-body t-numeric ${variance.className}`,
              text: variance.text,
            }),
          ]),
        ]),
        el("p", {
          className: "t-caption",
          text: "The variance is recorded, never corrected into the expected figure. The variance is the signal.",
        }),
        !summary.is_finalised
          ? el("button", {
              className: "btn btn-block",
              text: summary.actual_counted === null ? "Record a count" : "Update the count",
              attrs: { type: "button" },
              on: { click: () => countSheet(context) },
            })
          : null,
      ]),

      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "Opening balance" }),
        el("div", { className: "t-title t-numeric", text: format(summary.opening_balance) }),
        el("p", {
          className: "t-caption",
          text: SOURCE_LABEL[summary.opening_balance_source] ?? summary.opening_balance_source,
        }),
      ]),

      // The component snapshot, stored per §5.2 so the total and its explanation cannot
      // disagree six months later.
      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "As it stood on the day" }),
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
      ]),

      summary.notes ? el("div", { className: "card" }, [el("p", { className: "t-body", text: summary.notes })]) : null,

      isAdmin ? finaliseControls(summary, context) : null,
    ]),
  );
}

function termRow(label, value) {
  return el("div", { className: "list-row" }, [
    el("div", { className: "list-row-main t-body", text: label }),
    el("div", { className: "list-row-value t-body t-numeric", text: format(value) }),
  ]);
}

function finaliseControls(summary, context) {
  if (!summary.is_finalised) {
    return el("div", { className: "card stack" }, [
      el("button", {
        className: "btn btn-primary btn-block",
        text: "Finalise this day",
        attrs: { type: "button" },
        on: {
          click: async () => {
            try {
              await api.patch(`/daily-summaries/${context.businessDate}/finalise`, {});
              notify.success("Day finalised.");
              renderDailySummary(context.container, context);
            } catch (error) {
              notify.error(explain(error), { requestId: error.requestId });
            }
          },
        },
      }),
      el("p", {
        className: "t-caption",
        text: "Every shift on this date must be locked first, and the previous day finalised — the opening balance chains from it.",
      }),
    ]);
  }

  return el("div", { className: "card stack" }, [
    el("button", {
      className: "btn btn-block",
      text: "Unfinalise",
      attrs: { type: "button" },
      on: { click: () => unfinaliseSheet(context) },
    }),
    el("p", {
      className: "t-caption",
      text: "Finalising is otherwise terminal. Unfinalising takes a mandatory reason and is audit-logged.",
    }),
  ]);
}

/* --- sheets --------------------------------------------------------------------- */

function createSheet(context) {
  const date = field({
    name: "business_date",
    label: "Business date",
    type: "date",
    value: todayAtOutlet(),
    max: todayAtOutlet(),
    required: true,
  });

  const opening = field({
    name: "opening_balance",
    label: "Opening balance (first day only)",
    type: "number",
    step: "0.01",
    inputMode: "decimal",
    // §6.5: supplying one when a prior day exists is 409 OPENING_BALANCE_IS_CHAINED -- the
    // figure is derived, not typed. Only the very first day is seeded, admin only.
    hint: "Leave blank unless this is the very first day ever. Otherwise it chains from yesterday and supplying one is refused.",
  });

  const notes = field({ name: "notes", label: "Notes (optional)" });

  const form = new Form({ business_date: date, opening_balance: opening, notes });

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: "Create",
    attrs: { type: "button" },
  });

  submit.addEventListener("click", async () => {
    form.clearErrors();
    submit.disabled = true;
    try {
      const values = form.values();
      const body = { business_date: values.business_date };
      if (values.opening_balance) body.opening_balance = values.opening_balance;
      if (values.notes) body.notes = values.notes;
      await api.post("/daily-summaries", body);
      sheet.close();
      notify.success("Summary created.");
      context.navigate(`#/daily-summaries/${values.business_date}`);
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    }
  });

  const sheet = openSheet({
    title: "New daily summary",
    body: el("div", { className: "stack" }, [
      el("p", {
        className: "t-caption",
        text: "Every shift on the date must already be closed or locked.",
      }),
      ...form.nodes(),
    ]),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}

function countSheet(context) {
  const counted = field({
    name: "actual_counted",
    label: "Cash actually counted",
    type: "number",
    step: "0.01",
    value: context.summary.actual_counted ?? "",
    required: true,
    inputMode: "decimal",
    hint: "A physical count re-anchors the chain: tomorrow opens at this figure rather than the arithmetic one.",
  });

  const notes = field({
    name: "notes",
    label: "Notes (optional)",
    value: context.summary.notes ?? "",
  });

  const form = new Form({ actual_counted: counted, notes });

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: "Save count",
    attrs: { type: "button" },
  });

  submit.addEventListener("click", async () => {
    form.clearErrors();
    submit.disabled = true;
    try {
      const changes = form.changes();
      if (Object.keys(changes).length === 0) {
        sheet.close();
        return;
      }
      await api.patch(`/daily-summaries/${context.businessDate}`, changes);
      sheet.close();
      notify.success("Count recorded.");
      renderDailySummary(context.container, context);
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    }
  });

  const sheet = openSheet({
    title: "Record the count",
    body: el("div", { className: "stack" }, [
      el("p", {
        className: "t-caption",
        text: "The physical cash in the locker is what carries forward, not the theoretical figure — so a shortage stays visible instead of disappearing into tomorrow.",
      }),
      ...form.nodes(),
    ]),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}

function unfinaliseSheet(context) {
  const reason = field({
    name: "reason",
    label: "Why is this being unfinalised?",
    required: true,
    hint: "Audit-logged. 3–500 characters.",
  });
  const form = new Form({ reason });

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: "Unfinalise",
    attrs: { type: "button" },
  });

  submit.addEventListener("click", async () => {
    form.clearErrors();
    submit.disabled = true;
    try {
      await api.patch(`/daily-summaries/${context.businessDate}/unfinalise`, form.values());
      sheet.close();
      notify.success("Unfinalised.");
      // Nothing is recomputed (§13.16). Said out loud, because the absence of a change is
      // itself the thing somebody might not expect.
      notify.info("Nothing was recomputed — every stored figure is exactly as it was.");
      renderDailySummary(context.container, context);
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    }
  });

  const sheet = openSheet({
    title: "Unfinalise day",
    body: el("div", { className: "stack" }, [reason]),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}
