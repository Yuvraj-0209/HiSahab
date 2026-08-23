/* One salesman's shortfall ledger, and settling it (CLAUDE.md §5.2, §13.14, §13.15).
 *
 * §13.14: a shortfall is its own record type and **never a credit sale**. It has no receipt
 * to satisfy `credit_sales.attachment_id`, and putting it there "would pollute a real
 * customer's outstanding balance with staff debt -- nobody could answer 'what does this
 * customer owe me' again." So this screen lives beside the credit screens and shares none of
 * their tables.
 *
 * ## outstanding is computed, never stored
 *
 *     outstanding = SUM(shortfalls) − SUM(settlements)
 *
 * over **every** row, reversals included -- they carry negative amounts and net out (§5.2).
 * §14 forbids a denormalised running total on a salesman row or anywhere else.
 *
 * ## A settlement names the salesman; booking a shortfall does not
 *
 * The asymmetry looks like an inconsistency and is not. Booking reads `shifts.attendant_id`,
 * because §14 says a client-supplied value "lets a typo put a debt on the wrong person, with
 * no second source of truth to catch it". A settlement takes one, because the money arriving
 * during this shift may pay down anybody's balance.
 *
 * ## §13.15, said on the screen
 *
 * There is no write-off in V1. Every settlement is cash, so "a ₹20 gap nobody will ever chase
 * stays on that salesman's outstanding balance permanently, and the balance only ever grows."
 * Recorded here where somebody will actually meet it, not only in the document.
 */

import { el, empty, pill, render } from "../dom.js";
import { api, explain, Submission, ApiError } from "../api.js";
import { format } from "../money.js";
import { dateTime } from "../time.js";
import { field, Form } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { openReversalSheet } from "../ui/reversal.js";
import { notify } from "../ui/toast.js";
import { errorCard } from "./today.js";

export async function renderShortfallLedger(container, { session, navigate, salesmanId }) {
  const { shell } = session;
  shell.setTab("cash");
  shell.setTitle("Shortfall ledger");
  shell.setActions();

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let ledger;
  let outstanding;
  let shift = null;
  try {
    [ledger, outstanding] = await Promise.all([
      api.get(`/salesman-shortfalls/${salesmanId}/ledger`, { limit: 100 }),
      api.get("/salesman-shortfalls/outstanding"),
    ]);
    // A settlement is recorded against the shift the money arrived in, so one has to be open.
    shift = await api.get("/shifts/current").catch((error) => {
      if (error instanceof ApiError && error.code === "NO_OPEN_SHIFT") return null;
      throw error;
    });
  } catch (error) {
    render(
      container,
      errorCard(error, () =>
        renderShortfallLedger(container, { session, navigate, salesmanId }),
      ),
    );
    return;
  }

  const row = outstanding.items.find((entry) => entry.salesman_id === salesmanId);
  const reload = () => renderShortfallLedger(container, { session, navigate, salesmanId });

  shell.setTitle("Shortfall ledger", row?.full_name ?? "");

  render(
    container,
    el("div", { className: "stack" }, [
      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "Outstanding" }),
        el("div", {
          className: "t-amount",
          text: row ? format(row.outstanding) : format("0.00"),
        }),
        el("p", {
          className: "t-caption",
          text: "Booked shortfalls less settlements, reversals included. Computed on every read, never stored.",
        }),
        shift
          ? el("button", {
              className: "btn btn-primary btn-block",
              text: "Record a settlement",
              attrs: { type: "button" },
              on: { click: () => settlementSheet(salesmanId, shift, reload) },
            })
          : el("p", {
              className: "t-caption",
              text: "A settlement is filed against the shift the cash arrived in, so one has to be open.",
            }),
      ]),

      el("p", {
        className: "t-caption",
        text: "A shortfall can only be repaid in cash — V1 has no way to write one off, so a small figure nobody will chase stays on this balance and it only ever grows.",
      }),

      ledger.items.length
        ? el("div", { className: "list" }, ledger.items.map((entry) =>
            el("div", { className: "list-row" }, [
              el("div", { className: "list-row-main" }, [
                el("div", {
                  className: "t-body",
                  text: entry.kind === "shortfall" ? "Shortfall booked" : "Settlement",
                }),
                el("div", { className: "t-caption", text: dateTime(entry.created_at) }),
              ]),
              el("div", { className: "row" }, [
                entry.is_reversal ? pill("reversal", "neutral") : null,
                el("span", {
                  className: `t-body t-numeric ${
                    entry.balance_delta.trim().startsWith("-") ? "text-surplus" : "text-short"
                  }`,
                  text: format(entry.balance_delta, { sign: true }),
                }),
              ]),
            ]),
          ))
        : empty("Nothing on this ledger."),
    ]),
  );
}

function settlementSheet(salesmanId, shift, onDone) {
  const amount = field({
    name: "amount",
    label: "Amount repaid",
    type: "number",
    step: "0.01",
    min: "0.01",
    required: true,
    inputMode: "decimal",
    // §5.2: over-payment is accepted and the balance goes negative, same convention as a
    // credit repayment. Stated so a minus sign later does not read as a bug.
    hint: "More than they owe is accepted — the balance simply goes negative.",
  });

  const form = new Form({ amount });
  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: "Record settlement",
    attrs: { type: "button" },
  });

  const submission = new Submission(
    "POST",
    `/shifts/${shift.id}/shortfall-settlements`,
  );

  submit.addEventListener("click", async () => {
    form.clearErrors();
    submit.disabled = true;
    try {
      // salesman_id IS sent here, unlike when booking -- see the module header.
      await submission.run({
        salesman_id: salesmanId,
        amount: form.values().amount,
      });
      sheet.close();
      notify.success("Settlement recorded.");
      onDone();
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    }
  });

  const sheet = openSheet({
    title: "Record a settlement",
    body: el("div", { className: "stack" }, [
      el("p", {
        className: "t-caption",
        text: "Cash handed back by the salesman. It increases the expected cash for the shift it arrives in.",
      }),
      ...form.nodes(),
    ]),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}
