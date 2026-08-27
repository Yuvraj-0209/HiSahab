/* The daily cash summary: §6.5's rolling balance, and what makes it a snapshot.
 *
 * **Phase 15: this module no longer renders a screen.** It held two of them --
 * `#/daily-summaries` and `#/daily-summaries/{date}` -- and the second described the same
 * business date as `#/reports/{date}` from a different table, with no link between them. §11
 * phase 15: one day, one screen. Both now live in `days.js`, which imports the sheets and
 * controls below.
 *
 * What is left is the *acts* a day supports -- reconcile it, count it, finalise it, unfinalise
 * it -- because those are what carry the reasoning worth keeping, and they were already
 * separable from the markup around them.
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

import { el } from "../dom.js";
import { api, explain } from "../api.js";
import { format } from "../money.js";
import { todayAtOutlet } from "../time.js";
import { field, Form } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { notify } from "../ui/toast.js";

export const SOURCE_LABEL = {
  counted: "carried from yesterday's physical count",
  carried: "carried from yesterday's expected closing — nobody counted",
  seeded: "seeded by an admin — the first day",
};


export function termRow(label, value) {
  return el("div", { className: "list-row" }, [
    el("div", { className: "list-row-main t-body", text: label }),
    el("div", { className: "list-row-value t-body t-numeric", text: format(value) }),
  ]);
}

export function finaliseControls(summary, context) {
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
              context.reload();
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

/**
 * Reconcile a day: §6.4 computed once and stored (§5.2).
 *
 * `context.businessDate` pre-fills the date when the caller already knows which day needs
 * reconciling -- which, since Phase 15, is every caller: the worklist offers the act *on* a
 * day rather than asking somebody to type one. Defaulting to today was right when this sheet
 * was reached from a "New" button in the chrome and wrong everywhere else, because §4.7 says
 * the day being entered is rarely today.
 */
export function createSheet(context) {
  const date = field({
    name: "business_date",
    label: "Business date",
    type: "date",
    value: context.businessDate ?? todayAtOutlet(),
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
      notify.success("Day reconciled.");
      context.navigate(`#/days/${values.business_date}`);
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

export function countSheet(context) {
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
      context.reload();
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

export function unfinaliseSheet(context) {
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
      context.reload();
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
