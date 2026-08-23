/* Collections: how the money arrived (CLAUDE.md §5.2, §6.4, §6.8).
 *
 * ## The cash row is a declaration, not an input to the cash equation
 *
 * This is the distinction the whole screen is built around, and §14 forbids getting it wrong
 * by name: "Sum the `cash` collection row together with §6.4's derived `cash_sales`" is a
 * listed failure mode, because doing so double-counts the entire day's cash and the result
 * looks plausible.
 *
 * §6.4 derives cash as the *residual* -- total sales minus card, UPI, wallet and udhaar --
 * and never reads a cash collection row at all. The cash row is the **independent
 * observation** that derived figure gets checked against:
 *
 *   derived cash    what the meters say the salesman should be holding
 *   declared cash   what he says he counted into the locker
 *
 * and the gap between them is what a manager may book as a shortfall against his name (§14).
 * Same shape as §4.7's chain: the system predicts, a human confirms, both are stored, and a
 * disagreement leaves a trace instead of being absorbed. So this screen shows the declaration
 * and never shows a running total that mixes it with anything derived.
 *
 * ## null and "0.00" are different answers
 *
 * `declared_cash: null` means nobody has declared; `"0.00"` means they counted zero. §6.8
 * makes that distinction load-bearing -- an explicit zero satisfies the MISSING_COLLECTIONS
 * close precondition and a blank does not -- and `collections.py`'s own docstring says a
 * client "must not coalesce them". Rendered as words, never as ₹0.00.
 *
 * ## One live row per mode
 *
 * Enforced in the service layer rather than by a unique constraint, because §6.9's reversals
 * make a unique index impossible (a replacement collides with the reversed original). So a
 * second POST for a mode is a 409 COLLECTION_ALREADY_EXISTS and the client PATCHes instead --
 * which is why each mode below is either "declare" or "edit", never "add another".
 */

import { el, empty, render, row } from "../dom.js";
import { api, explain, Submission } from "../api.js";
import { format, isZero } from "../money.js";
import { field, Form, select } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { openReversalSheet, reversalBadge } from "../ui/reversal.js";
import { notify } from "../ui/toast.js";
import { satisfies } from "../ui/nav.js";
import { errorCard } from "./today.js";

const MODES = [
  { value: "cash", label: "Cash", hint: "What was counted into the locker" },
  { value: "card", label: "Card", hint: "The card machine's settlement total" },
  { value: "upi", label: "UPI", hint: "The QR total" },
  { value: "wallet", label: "Wallet", hint: "Paytm, Phonepe and similar" },
];

export async function renderCollections(container, { session, navigate, shiftId }) {
  const { shell } = session;
  shell.setTab("entry");
  shell.setTitle("Collections");
  shell.setActions();

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let page;
  let shift;
  try {
    [page, shift] = await Promise.all([
      api.get(`/shifts/${shiftId}/collections`),
      api.get(`/shifts/${shiftId}`),
    ]);
  } catch (error) {
    render(
      container,
      errorCard(error, () => renderCollections(container, { session, navigate, shiftId })),
    );
    return;
  }

  const editable = shift.status === "open";
  const context = { session, container, navigate, shiftId, editable };

  // Live rows only, for the per-mode view: a reversed row and the reversal that cancels it
  // are history, not the current declaration.
  const live = page.items.filter((item) => !item.reverses_id && !item.is_reversed);
  const byMode = new Map(live.map((item) => [item.mode, item]));

  render(
    container,
    el("div", { className: "stack" }, [
      declarationCard(page),

      el("div", { className: "section-label t-micro", text: "By mode" }),
      el(
        "div",
        { className: "stack" },
        MODES.map((mode) => modeCard(mode, byMode.get(mode.value), context)),
      ),

      page.items.length > live.length
        ? el("div", { className: "stack" }, [
            el("div", { className: "section-label t-micro", text: "Corrections" }),
            el(
              "div",
              { className: "list" },
              page.items
                .filter((item) => item.reverses_id || item.is_reversed)
                .map((item) => historyRow(item)),
            ),
          ])
        : null,

      page.truncated
        ? el("div", {
            className: "truncation-notice t-caption",
            text: "This shift has more collection rows than this view lists.",
          })
        : null,
    ]),
  );
}

/** The declared-cash card. Deliberately separate from every derived figure on the Cash tab. */
function declarationCard(page) {
  const declared = page.declared_cash;

  return el("div", { className: "card stack" }, [
    el("div", { className: "t-micro", text: "Cash declared" }),
    declared === null
      ? el("div", { className: "t-title t-absent", text: "not declared" })
      : el("div", { className: "t-amount", text: format(declared) }),

    el("p", {
      className: "t-caption",
      text:
        declared === null
          ? "Nobody has declared cash for this shift yet. That is different from declaring zero — the shift cannot close until somebody answers."
          : isZero(declared)
            ? "Zero was declared as an answer. That is a different fact from having entered nothing."
            : "What the salesman says he counted into the locker. The Cash tab compares this against what the meters imply.",
    }),
  ]);
}

function modeCard(mode, existing, context) {
  const { editable } = context;

  return el("div", { className: "card stack" }, [
    el("div", { className: "row-between" }, [
      el("div", {}, [
        el("div", { className: "t-headline", text: mode.label }),
        el("div", { className: "t-caption", text: mode.hint }),
      ]),
      el("div", {
        className: "t-title t-numeric",
        text: existing ? format(existing.amount) : "—",
      }),
    ]),

    existing?.reference
      ? el("div", { className: "t-caption", text: `Reference ${existing.reference}` })
      : null,

    editable
      ? el("div", { className: "row" }, [
          el("button", {
            className: "btn grow",
            text: existing ? "Edit" : `Declare ${mode.label.toLowerCase()}`,
            attrs: { type: "button" },
            on: { click: () => declareSheet(mode, existing, context) },
          }),
          existing && satisfies(context.session.me.role, "manager")
            ? el("button", {
                className: "btn btn-danger",
                text: "Reverse",
                attrs: { type: "button" },
                on: {
                  click: () =>
                    openReversalSheet({
                      title: `Reverse ${mode.label.toLowerCase()}`,
                      path: `/shifts/${context.shiftId}/collections/${existing.id}/reversals`,
                      amount: existing.amount,
                      description: `${mode.label} collection`,
                      replacementText: {
                        name: "replacement_reference",
                        label: "Corrected reference (optional)",
                        value: existing.reference ?? "",
                      },
                      onDone: () => renderCollections(context.container, context),
                    }),
                },
              })
            : null,
        ])
      : null,
  ]);
}

function historyRow(item) {
  return el("div", { className: "list-row" }, [
    el("div", { className: "list-row-main" }, [
      el("div", { className: "t-body", text: item.mode }),
      item.reversal_reason
        ? el("div", { className: "t-caption", text: item.reversal_reason })
        : null,
    ]),
    el("div", { className: "row" }, [
      reversalBadge(item),
      el("span", { className: "t-body t-numeric", text: format(item.amount) }),
    ]),
  ]);
}

/* --- declaring --------------------------------------------------------------- */

function declareSheet(mode, existing, context) {
  const amount = field({
    name: "amount",
    label: `${mode.label} amount`,
    type: "number",
    step: "0.01",
    // §5.2: `>= 0` on an ordinary collection row, unlike expenses which are strictly > 0.
    // An explicit ₹0 cash declaration is meaningful and §6.8 accepts it to close a shift.
    min: "0",
    value: existing?.amount ?? "",
    required: true,
    inputMode: "decimal",
    hint:
      mode.value === "cash"
        ? "Enter 0 if the shift genuinely took no cash. Zero is an answer; leaving it blank is not."
        : "The settlement total for this channel.",
  });

  const reference = field({
    name: "reference",
    label: "Reference (optional)",
    value: existing?.reference ?? "",
    hint: "Settlement or batch number, if there is one.",
  });

  const form = new Form({ amount, reference });

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: existing ? "Save" : "Declare",
    attrs: { type: "button" },
  });

  // A PATCH needs no key -- it is naturally idempotent -- so a Submission is built only for
  // the create path.
  const submission = existing
    ? null
    : new Submission("POST", `/shifts/${context.shiftId}/collections`);

  submit.addEventListener("click", async () => {
    form.clearErrors();
    const values = form.values();

    if (values.amount === "") {
      form.showErrors([{ loc: ["body", "amount"], msg: "Enter an amount. Zero is valid." }]);
      return;
    }

    submit.disabled = true;
    try {
      if (existing) {
        // Only what changed, never explicit nulls -- see ui/field.js on why that matters.
        const changes = form.changes();
        if (Object.keys(changes).length === 0) {
          sheet.close();
          return;
        }
        await api.patch(
          `/shifts/${context.shiftId}/collections/${existing.id}`,
          changes,
        );
      } else {
        const body = { mode: mode.value, amount: values.amount };
        if (values.reference) body.reference = values.reference;
        await submission.run(body);
      }
      sheet.close();
      notify.success(`${mode.label} recorded.`);
      renderCollections(context.container, context);
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
    title: existing ? `Edit ${mode.label.toLowerCase()}` : `Declare ${mode.label.toLowerCase()}`,
    body: el("div", { className: "stack" }, form.nodes()),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}
