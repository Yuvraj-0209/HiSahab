/* Expenses: what left the drawer, and what needs paper (CLAUDE.md §5.1, §6.7, §6.11).
 *
 * ## The receipt rule is warned about, never enforced, by this screen
 *
 * §6.11: `receipt_required = category.requires_receipt OR amount > EXPENSE_RECEIPT_THRESHOLD`.
 * The threshold comes from `/client-config` rather than a constant here, because §6.11 says
 * "changing it must not require a deploy" and a hardcoded ₹5,000 would keep warning at the
 * old figure the day it moves.
 *
 * The client's copy of that rule exists purely so somebody is told *before* they fill in a
 * form and get a 422. The server decides -- and it decides against a snapshot, not the live
 * flag: §6.11 stores `expenses.receipt_required` at insert, so flipping a category to
 * receipt-required later does not retroactively declare every historical expense
 * non-compliant. §14 forbids reading the live category flag when reporting on an existing
 * expense, so this screen reads `expense.receipt_required` for saved rows and only consults
 * the category when composing a new one.
 *
 * ## mode is an answer, and it is what keeps §6.4 honest
 *
 * §5.2 gave expenses a `mode` column precisely so §6.4's `cash_expenses` line is answerable.
 * Before it, every expense was implicitly cash, and a ₹40,000 electricity bill paid online
 * read as a ₹40,000 hole in the drawer -- which this outlet books as udhaar against a
 * salesman's name. NOT NULL with no default, so the select below has no pre-selected value.
 *
 * ## What must never be filed here
 *
 * §14, twice over: a fuel restock, a tanker settlement or an IOCL/PAD payment is a BANK
 * movement, not a drawer movement, and recording one as an expense makes §6.4 invent a daily
 * cash shortage that never happened. The enum that used to make this impossible is gone --
 * categories are admin-managed data since Phase 8 -- so only discipline is left, and the
 * warning is surfaced on the form rather than left in a document nobody reads at 9pm.
 */

import { el, empty, pill, render } from "../dom.js";
import { api, explain, Submission, uploadReceipt } from "../api.js";
import { format } from "../money.js";
import { relative } from "../time.js";
import { field, Form, select } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { openReversalSheet, reversalBadge } from "../ui/reversal.js";
import { receiptButton } from "../ui/receipt.js";
import { notify } from "../ui/toast.js";
import { satisfies } from "../ui/nav.js";
import { errorCard } from "./today.js";

const MODES = [
  { value: "cash", label: "Cash — out of the drawer" },
  { value: "card", label: "Card" },
  { value: "upi", label: "UPI" },
  { value: "bank_transfer", label: "Bank transfer" },
];

export async function renderExpenses(container, { session, navigate, shiftId }) {
  const { shell } = session;
  shell.setTab("entry");
  shell.setTitle("Expenses");

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let page;
  let shift;
  let categories;
  try {
    [page, shift, categories] = await Promise.all([
      api.get(`/shifts/${shiftId}/expenses`),
      api.get(`/shifts/${shiftId}`),
      api.get("/expense-categories"),
    ]);
  } catch (error) {
    render(
      container,
      errorCard(error, () => renderExpenses(container, { session, navigate, shiftId })),
    );
    return;
  }

  const editable = shift.status === "open";
  const context = { session, container, navigate, shiftId, editable, categories };

  shell.setActions(
    editable
      ? el("button", {
          className: "btn btn-primary",
          text: "Add",
          attrs: { type: "button" },
          on: { click: () => expenseSheet(null, context) },
        })
      : null,
  );

  const totals = Object.entries(page.totals_by_category ?? {});

  render(
    container,
    el("div", { className: "stack" }, [
      totals.length
        ? el("div", { className: "card stack" }, [
            el("div", { className: "t-micro", text: "By category" }),
            el(
              "div",
              { className: "list" },
              totals.map(([code, amount]) =>
                el("div", { className: "list-row" }, [
                  el("div", { className: "list-row-main t-body", text: code }),
                  el("div", { className: "list-row-value t-body t-numeric", text: format(amount) }),
                ]),
              ),
            ),
            el("p", {
              className: "t-caption",
              text: "Every mode, not just cash. Only cash expenses reduce what the salesman should be holding.",
            }),
          ])
        : null,

      page.items.length
        ? el(
            "div",
            { className: "stack" },
            page.items.map((expense) => expenseCard(expense, context)),
          )
        : empty("No expenses recorded for this shift."),

      page.truncated
        ? el("div", {
            className: "truncation-notice t-caption",
            text: "This shift has more expenses than this view lists.",
          })
        : null,
    ]),
  );
}

function expenseCard(expense, context) {
  const flagged = expense.requires_review;

  return el("div", { className: `card stack ${flagged ? "flagged" : ""}` }, [
    el("div", { className: "row-between" }, [
      el("div", { className: "grow" }, [
        el("div", { className: "t-headline", text: expense.description }),
        el("div", {
          className: "t-caption",
          text: `${expense.category_code} · ${expense.mode.replace("_", " ")}${
            expense.paid_to ? ` · ${expense.paid_to}` : ""
          }`,
        }),
      ]),
      el("div", { className: "text-right" }, [
        el("div", { className: "t-title t-numeric", text: format(expense.amount) }),
      ]),
    ]),

    el("div", { className: "row" }, [
      reversalBadge(expense),
      // Read from the ROW's snapshot, never from the live category flag (§14, §6.11).
      expense.receipt_required
        ? pill(expense.attachment_id ? "receipt attached" : "receipt required", expense.attachment_id ? "open" : "review")
        : null,
      flagged ? pill(expense.reviewed_at ? "reviewed" : "needs review", expense.reviewed_at ? "neutral" : "review") : null,
    ]),

    flagged && !expense.reviewed_at
      ? el("p", {
          className: "t-caption",
          text: "Flagged for a manager's eyes. A shift cannot be locked while this is unreviewed.",
        })
      : null,
    expense.review_note
      ? el("p", { className: "t-caption", text: `Review: ${expense.review_note}` })
      : null,
    expense.reversal_reason
      ? el("p", { className: "t-caption", text: `Reason: ${expense.reversal_reason}` })
      : null,

    actionRow(expense, context),
  ]);
}

function actionRow(expense, context) {
  const { editable, session } = context;
  const buttons = [];
  const isManager = satisfies(session.me.role, "manager");

  // A reversal row is itself a correction and cannot be edited or re-reversed.
  const live = !expense.reverses_id && !expense.is_reversed;

  if (editable && live) {
    buttons.push(
      el("button", {
        className: "btn grow",
        text: "Edit",
        attrs: { type: "button" },
        on: { click: () => expenseSheet(expense, context) },
      }),
    );
  }

  if (live && isManager) {
    buttons.push(
      el("button", {
        className: "btn btn-danger",
        text: "Reverse",
        attrs: { type: "button" },
        on: {
          click: () =>
            openReversalSheet({
              title: "Reverse expense",
              path: `/shifts/${context.shiftId}/expenses/${expense.id}/reversals`,
              amount: expense.amount,
              description: expense.description,
              replacementText: {
                name: "replacement_paid_to",
                label: "Corrected payee (optional)",
                value: expense.paid_to ?? "",
              },
              onDone: () => renderExpenses(context.container, context),
            }),
        },
      }),
    );
  }

  const view = receiptButton(expense.attachment_id);
  if (view) buttons.push(view);

  if (expense.requires_review && !expense.reviewed_at && isManager) {
    buttons.push(
      el("button", {
        className: "btn",
        text: "Review",
        attrs: { type: "button" },
        on: { click: () => reviewSheet(expense, context) },
      }),
    );
  }

  return buttons.length ? el("div", { className: "row" }, buttons) : null;
}

/* --- creating and editing ------------------------------------------------------ */

function expenseSheet(existing, context) {
  const { categories, session } = context;
  const threshold = session.config?.expense_receipt_threshold ?? null;

  const active = categories.filter((category) => category.is_active);

  const categorySelect = select({
    name: "category_id",
    label: "Category",
    // No pre-selected category: picking the first alphabetically for the user is how an
    // expense ends up filed under whatever happened to sort first.
    options: [{ value: "", label: "Choose…" }, ...active.map((category) => ({
      value: category.id,
      label: category.display_name,
    }))],
    value: existing?.category_id ?? "",
    required: true,
    // §14: a fuel restock or IOCL settlement must never be filed as an expense. The enum
    // that made this impossible is gone, so the warning lives where the choice is made.
    hint: "Never file a tanker delivery or an IOCL/PAD settlement here — that money leaves the bank, not the drawer.",
  });

  const modeSelect = select({
    name: "mode",
    label: "How was it paid?",
    options: [{ value: "", label: "Choose…" }, ...MODES],
    value: existing?.mode ?? "",
    required: true,
    hint: "Only cash reduces what the salesman should be holding. A bank-paid bill is recorded but never subtracted from the drawer.",
  });

  const amount = field({
    name: "amount",
    label: "Amount",
    type: "number",
    step: "0.01",
    // §5.2: strictly > 0 for an expense, unlike a collection. A ₹0 expense records nothing.
    min: "0.01",
    value: existing?.amount ?? "",
    required: true,
    inputMode: "decimal",
  });

  const description = field({
    name: "description",
    label: "What was it for?",
    value: existing?.description ?? "",
    required: true,
    hint: "3–500 characters.",
  });

  const paidTo = field({
    name: "paid_to",
    label: "Paid to (optional)",
    value: existing?.paid_to ?? "",
  });

  const form = new Form({
    category_id: categorySelect,
    mode: modeSelect,
    amount,
    description,
    paid_to: paidTo,
  });

  /* --- the receipt --------------------------------------------------------- */

  let attachmentId = existing?.attachment_id ?? null;

  const fileInput = el("input", {
    attrs: { type: "file", accept: "image/jpeg,image/png", id: "f-receipt" },
    className: "field-input",
  });

  const receiptStatus = el("p", { className: "t-caption" });
  const receiptNotice = el("p", { className: "t-caption" });

  function updateReceiptNotice() {
    const categoryId = categorySelect._input.value;
    const category = active.find((entry) => entry.id === categoryId);
    const typed = amount._input.value;

    // A string comparison, not a numeric one: §14 forbids parseFloat on money, and the
    // threshold arrives as a string too. Comparing digit counts then lexically is exact for
    // non-negative decimal strings and needs no float at all.
    const overThreshold = threshold !== null && typed !== "" && isGreater(typed, threshold);
    const required = Boolean(category?.requires_receipt) || overThreshold;

    receiptNotice.textContent = required
      ? attachmentId
        ? "A receipt is required for this expense, and one is attached."
        : "A receipt is required for this expense — either its category demands one, or the amount is over the threshold."
      : "No receipt is required for this expense, but you may attach one.";
    receiptNotice.className = `t-caption ${required && !attachmentId ? "text-short" : ""}`;
  }

  categorySelect._input.addEventListener("change", updateReceiptNotice);
  amount._input.addEventListener("input", updateReceiptNotice);

  // §5.2: attachment_id is immutable once set. A PATCH may supply one only while it is still
  // null; swapping is refused with 409 ATTACHMENT_ALREADY_SET, and the correction path is a
  // reversal like everything else here.
  const receiptLocked = Boolean(existing?.attachment_id);

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files?.[0];
    if (!file) return;

    receiptStatus.textContent = "Uploading…";
    try {
      const result = await uploadReceipt(file, context.shiftId);
      attachmentId = result.attachment_id;
      receiptStatus.textContent = "Receipt uploaded.";
      updateReceiptNotice();
    } catch (error) {
      receiptStatus.textContent = "";
      // §7.2: HEIC gets a specific, actionable message from the server. Surfacing `detail`
      // verbatim is right here -- it already tells an iPhone user which setting to change.
      notify.error(explain(error), { requestId: error.requestId });
    }
  });

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: existing ? "Save" : "Record expense",
    attrs: { type: "button" },
  });

  const submission = existing
    ? null
    : new Submission("POST", `/shifts/${context.shiftId}/expenses`);

  submit.addEventListener("click", async () => {
    form.clearErrors();
    const values = form.values();

    if (!values.category_id || !values.mode) {
      form.showErrors([
        !values.category_id
          ? { loc: ["body", "category_id"], msg: "Choose a category." }
          : { loc: ["body", "mode"], msg: "Choose how it was paid." },
      ]);
      return;
    }

    submit.disabled = true;
    try {
      if (existing) {
        const changes = form.changes();
        // category_id is not editable (ExpenseUpdate forbids it) -- the category a row was
        // filed under is part of what history said.
        delete changes.category_id;
        if (attachmentId && !existing.attachment_id) changes.attachment_id = attachmentId;
        if (Object.keys(changes).length === 0) {
          sheet.close();
          return;
        }
        await api.patch(`/shifts/${context.shiftId}/expenses/${existing.id}`, changes);
      } else {
        const body = {
          category_id: values.category_id,
          mode: values.mode,
          amount: values.amount,
          description: values.description,
        };
        if (values.paid_to) body.paid_to = values.paid_to;
        if (attachmentId) body.attachment_id = attachmentId;
        await submission.run(body);
      }
      sheet.close();
      notify.success(existing ? "Expense updated." : "Expense recorded.");
      renderExpenses(context.container, context);
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

  updateReceiptNotice();

  const sheet = openSheet({
    title: existing ? "Edit expense" : "Record an expense",
    body: el("div", { className: "stack" }, [
      ...form.nodes(),
      el("div", { className: "field" }, [
        el("span", { className: "field-label t-caption", text: "Receipt" }),
        receiptLocked
          ? el("p", {
              className: "t-caption",
              text: "A receipt is already attached and cannot be swapped. Correct the expense with a reversal instead.",
            })
          : fileInput,
        receiptStatus,
        receiptNotice,
      ]),
    ]),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}

/** Compare two non-negative decimal strings without turning either into a float.
 *
 * §14 forbids parseFloat in the money path, and this is the one place the client genuinely
 * needs an ordering: whether a typed amount is over the receipt threshold. Comparing by
 * integer-part length first, then lexically, is exact for this shape of input -- and the
 * comparison is only ever used to show a warning, never to decide anything the server does
 * not decide again for itself.
 */
function isGreater(a, b) {
  const [aWhole = "0", aFrac = ""] = String(a).trim().split(".");
  const [bWhole = "0", bFrac = ""] = String(b).trim().split(".");

  const aw = aWhole.replace(/^0+(?=\d)/, "");
  const bw = bWhole.replace(/^0+(?=\d)/, "");
  if (aw.length !== bw.length) return aw.length > bw.length;
  if (aw !== bw) return aw > bw;

  const width = Math.max(aFrac.length, bFrac.length);
  return aFrac.padEnd(width, "0") > bFrac.padEnd(width, "0");
}

/* --- review -------------------------------------------------------------------- */

function reviewSheet(expense, context) {
  const note = field({
    name: "review_note",
    label: "Review note",
    required: true,
    hint: "3–500 characters. Clearing a flag is a decision somebody signs their name to.",
  });
  const form = new Form({ review_note: note });

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: "Mark reviewed",
    attrs: { type: "button" },
  });

  submit.addEventListener("click", async () => {
    form.clearErrors();
    submit.disabled = true;
    try {
      await api.patch(
        `/shifts/${context.shiftId}/expenses/${expense.id}/review`,
        form.values(),
      );
      sheet.close();
      notify.success("Reviewed.");
      renderExpenses(context.container, context);
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    }
  });

  const sheet = openSheet({
    title: "Review expense",
    body: el("div", { className: "stack" }, [
      el("div", { className: "card stack" }, [
        el("p", { className: "t-title t-numeric", text: format(expense.amount) }),
        el("p", { className: "t-caption", text: expense.description }),
      ]),
      el("p", {
        className: "t-caption",
        text: "Flags are never cleared automatically — not even when a reversal drops the day's total back under the threshold.",
      }),
      note,
    ]),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}
