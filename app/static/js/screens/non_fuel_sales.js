/* Non-fuel sales: the money no meter counts (CLAUDE.md §5.2, §6.4).
 *
 * A bottle of oil, a coolant top-up. §5.2 added this table in Phase 10 because §6.4 named
 * `other_cash_income` from the beginning and nothing ever gave it a column.
 *
 * ## Why it is per shift, and why that matters to the person using this screen
 *
 * §5.2's argument, worth repeating on the screen where the row gets typed: a ₹500 bottle of
 * oil is in the salesman's hand and **not** in the meter-derived figure, but it *is* in the
 * cash he counts into the locker. Compare derived fuel cash against his declaration without
 * it and he shows a ₹500 **surplus** every day he sells one -- a phantom in his name.
 *
 * ## It is added to sales, never to the cash side
 *
 * §6.4 and §14 both state this, because the intuitive placement is wrong. A card-paid oil
 * sale is already inside the card collections total; putting the sale on the cash side would
 * understate derived cash by exactly its amount. On the sales side the arithmetic is correct
 * **regardless of how it was paid**, which is why this row needs no mode column of its own --
 * the collections rows already record how the money arrived.
 *
 * That is also why this screen does not ask how it was paid. Somebody will want to; the
 * answer is that the collections screen already has it.
 */

import { el, empty, render } from "../dom.js";
import { api, explain, Submission } from "../api.js";
import { format } from "../money.js";
import { field, Form } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { openReversalSheet, reversalBadge } from "../ui/reversal.js";
import { notify } from "../ui/toast.js";
import { satisfies } from "../ui/nav.js";
import { errorCard } from "./today.js";

export async function renderNonFuelSales(container, { session, navigate, shiftId }) {
  const { shell } = session;
  shell.setTab("entry");
  shell.setTitle("Non-fuel sales");

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let page;
  let shift;
  try {
    [page, shift] = await Promise.all([
      api.get(`/shifts/${shiftId}/non-fuel-sales`),
      api.get(`/shifts/${shiftId}`),
    ]);
  } catch (error) {
    render(
      container,
      errorCard(error, () => renderNonFuelSales(container, { session, navigate, shiftId })),
    );
    return;
  }

  const editable = shift.status === "open";
  const context = { session, container, navigate, shiftId, editable };

  shell.setActions(
    editable
      ? el("button", {
          className: "btn btn-primary",
          text: "Add",
          attrs: { type: "button" },
          on: { click: () => saleSheet(null, context) },
        })
      : null,
  );

  render(
    container,
    el("div", { className: "stack" }, [
      el("div", { className: "card stack" }, [
        el("div", { className: "t-micro", text: "Total this shift" }),
        el("div", { className: "t-amount", text: format(page.total) }),
        el("p", {
          className: "t-caption",
          text: "Added to sales, not to cash. However it was paid, the collections rows already record that.",
        }),
      ]),

      page.items.length
        ? el(
            "div",
            { className: "stack" },
            page.items.map((sale) => saleCard(sale, context)),
          )
        : empty("Nothing recorded. If a bottle of oil was sold, it belongs here — otherwise the salesman shows a surplus that is not real."),

      page.truncated
        ? el("div", {
            className: "truncation-notice t-caption",
            text: "This shift has more rows than this view lists.",
          })
        : null,
    ]),
  );
}

function saleCard(sale, context) {
  const live = !sale.reverses_id && !sale.is_reversed;
  const buttons = [];

  if (context.editable && live) {
    buttons.push(
      el("button", {
        className: "btn grow",
        text: "Edit",
        attrs: { type: "button" },
        on: { click: () => saleSheet(sale, context) },
      }),
    );
  }
  if (live && satisfies(context.session.me.role, "manager")) {
    buttons.push(
      el("button", {
        className: "btn btn-danger",
        text: "Reverse",
        attrs: { type: "button" },
        on: {
          click: () =>
            openReversalSheet({
              title: "Reverse non-fuel sale",
              path: `/shifts/${context.shiftId}/non-fuel-sales/${sale.id}/reversals`,
              amount: sale.amount,
              description: sale.description ?? "Non-fuel sale",
              replacementText: {
                name: "replacement_description",
                label: "Corrected description (optional)",
                value: sale.description ?? "",
              },
              onDone: () => renderNonFuelSales(context.container, context),
            }),
        },
      }),
    );
  }

  return el("div", { className: "card stack" }, [
    el("div", { className: "row-between" }, [
      el("div", { className: "grow" }, [
        el("div", { className: "t-body", text: sale.description ?? "Non-fuel sale" }),
        sale.reversal_reason
          ? el("div", { className: "t-caption", text: `Reason: ${sale.reversal_reason}` })
          : null,
      ]),
      el("div", { className: "row" }, [
        reversalBadge(sale),
        el("span", { className: "t-title t-numeric", text: format(sale.amount) }),
      ]),
    ]),
    buttons.length ? el("div", { className: "row" }, buttons) : null,
  ]);
}

function saleSheet(existing, context) {
  const amount = field({
    name: "amount",
    label: "Amount",
    type: "number",
    step: "0.01",
    min: "0.01",
    value: existing?.amount ?? "",
    required: true,
    inputMode: "decimal",
  });

  const description = field({
    name: "description",
    label: "What was sold (optional)",
    value: existing?.description ?? "",
    hint: "An amount and a note. There is no product catalogue and no stock in V1.",
  });

  const form = new Form({ amount, description });

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: existing ? "Save" : "Record sale",
    attrs: { type: "button" },
  });

  const submission = existing
    ? null
    : new Submission("POST", `/shifts/${context.shiftId}/non-fuel-sales`);

  submit.addEventListener("click", async () => {
    form.clearErrors();
    submit.disabled = true;
    try {
      if (existing) {
        const changes = form.changes();
        if (Object.keys(changes).length === 0) {
          sheet.close();
          return;
        }
        await api.patch(
          `/shifts/${context.shiftId}/non-fuel-sales/${existing.id}`,
          changes,
        );
      } else {
        const values = form.values();
        const body = { amount: values.amount };
        if (values.description) body.description = values.description;
        await submission.run(body);
      }
      sheet.close();
      notify.success(existing ? "Updated." : "Recorded.");
      renderNonFuelSales(context.container, context);
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), {
          requestId: error.requestId,
          action:
            error.name === "NetworkError"
              ? { label: "Retry", onClick: () => submit.click() }
              : undefined,
        });
      }
    }
  });

  const sheet = openSheet({
    title: existing ? "Edit non-fuel sale" : "Record a non-fuel sale",
    body: el("div", { className: "stack" }, form.nodes()),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}
