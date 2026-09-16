/* The Bank screens: import a statement, review what it found, reconcile it (§5.3a, §8).
 *
 * Phase 20. Three screens inside the Credit tab, because feeding the udhaar ledger is what
 * this is for and §8 already puts both at the manager floor. A seventh tab would crowd a
 * phone tab bar for a screen used once a month.
 *
 * ## Every figure arrives as a string and is rendered as one
 *
 * §14: JavaScript has no decimal type. This file never adds two amounts, never divides one,
 * and never calls `parseFloat` -- the reconciliation differences, the totals, the boundary
 * settlement are all computed server-side in `Decimal` and sent as text.
 *
 * ## `null` is not zero, twice over on these screens
 *
 * `settled: null` means **Paytm has not paid yet, or the row is not in this file** --
 * emphatically not "Paytm paid nothing", which would read as a discrepancy the size of a
 * whole day. And `is_expense: "undecided"` means nobody has looked, which is not the same as
 * "not an expense" (§6.8). Both are rendered as their own state, never collapsed with `?? 0`.
 *
 * ## Why the review list has two sections
 *
 * Credits ask *who sent this*; debits ask *was this a cost*. They are different questions
 * with different consequences -- a wrong credit lands on somebody's ledger, a wrong debit
 * moves Phase 21's profit line -- so they are answered separately rather than in one long
 * list where the eye slides between two kinds of decision.
 */

import { api, explain, newIdempotencyKey, postMultipart } from "../api.js";
import { el, empty, pill, render, row } from "../dom.js";
import { checkbox, select } from "../ui/field.js";
import { format } from "../money.js";
import { businessDate } from "../time.js";
import { toast } from "../ui/toast.js";
import { errorCard } from "./today.js";

const CLASSIFICATION_LABELS = {
  paytm_settlement: "Paytm settlement",
  cash_deposit: "Cash deposit",
  udhaar_repayment: "Udhaar repayment",
  iocl_ms_hsd: "IOCL — petrol/diesel",
  iocl_cbg: "IOCL — CBG",
  bank_charge: "Bank charge",
  loan: "Loan",
  self_transfer: "Self transfer",
  other: "Other",
  unclassified: "Unclassified",
};

/* Money that moved between the owner's own pockets, rather than leaving the business. The
 * screen says so in words, because §12's rule -- an IOCL payment is not an expense -- is the
 * one a person is most likely to get wrong when ticking through a month of debits. */
const NOT_A_COST = new Set(["iocl_ms_hsd", "iocl_cbg", "self_transfer"]);

function label(classification) {
  return CLASSIFICATION_LABELS[classification] ?? classification;
}

/* --- the hub: accounts, imports, and the way in ------------------------------------- */

export async function renderBankHub(container, { session, navigate }) {
  const { shell } = session;
  shell.setTab("credit");
  shell.setTitle("Bank");
  shell.setActions();

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let accounts;
  let imports;
  try {
    [accounts, imports] = await Promise.all([
      api.get("/bank-accounts"),
      api.get("/bank-statements/imports"),
    ]);
  } catch (error) {
    render(container, errorCard(error));
    return;
  }

  const active = accounts.filter((account) => account.is_active);

  if (active.length === 0) {
    render(
      container,
      el("div", { className: "card" }, [
        el("h2", { className: "t-heading", text: "No bank account yet" }),
        el("p", {
          className: "t-caption",
          text:
            "An admin adds the outlet's bank account on the Admin tab. A statement is " +
            "imported against one account, so there has to be one first.",
        }),
      ]),
    );
    return;
  }

  render(
    container,
    el("div", { className: "stack" }, [
      importCard(active, () => renderBankHub(container, { session, navigate })),
      el("div", { className: "card" }, [
        el("h2", { className: "t-heading", text: "What to do next" }),
        el("div", { className: "row gap" }, [
          el("button", {
            className: "btn",
            text: "Review what the statement found",
            attrs: { type: "button" },
            on: { click: () => navigate("#/credit/bank/review") },
          }),
          el("button", {
            className: "btn btn-plain",
            text: "Reconciliation",
            attrs: { type: "button" },
            on: { click: () => navigate("#/credit/bank/reconciliation") },
          }),
        ]),
      ]),
      importsCard(imports.items),
    ]),
  );
}

function importCard(accounts, reload) {
  const accountField = select({
    name: "bank_account_id",
    label: "Account",
    options: accounts.map((account) => ({
      value: account.id,
      label: `${account.label} — ${account.bank_name}`,
    })),
    value: accounts[0].id,
    required: true,
  });

  const fileInput = el("input", {
    className: "input",
    attrs: { type: "file", accept: ".csv,text/csv", name: "file" },
  });

  const status = el("p", { className: "t-caption" });

  /* §14: the key belongs to the *submission*, not to the fetch call. Minted once here and
   * reused by every retry, so a timed-out upload cannot become two import rows. */
  let key = newIdempotencyKey();

  const submit = el("button", {
    className: "btn",
    text: "Import",
    attrs: { type: "submit" },
  });

  const form = el(
    "form",
    {
      className: "stack",
      on: {
        submit: async (event) => {
          event.preventDefault();
          const file = fileInput.files?.[0];
          if (!file) {
            status.textContent = "Choose the CSV your bank gave you.";
            return;
          }
          submit.disabled = true;
          status.textContent = "Reading…";
          try {
            const result = await postMultipart(
              "/bank-statements/imports",
              { file, bank_account_id: accountField._input.value },
              { extraHeaders: { "Idempotency-Key": key } },
            );
            toast({
              message:
                `Imported ${result.imported_count} of ${result.row_count} lines` +
                (result.skipped_count
                  ? `, skipped ${result.skipped_count} already seen.`
                  : "."),
              kind: "success",
            });
            // A fresh key: the next import is a new submission, not a retry of this one.
            key = newIdempotencyKey();
            reload();
          } catch (error) {
            status.textContent = explain(error);
          } finally {
            submit.disabled = false;
          }
        },
      },
    },
    [
      accountField,
      el("label", { className: "field" }, [
        el("span", { className: "t-caption", text: "Statement CSV" }),
        fileInput,
      ]),
      el("p", {
        className: "t-caption",
        text:
          "Download a few days past the month end — the last day's card and UPI settles " +
          "the next morning, and lines you have already imported are skipped.",
      }),
      el("div", { className: "row gap" }, [submit]),
      status,
    ],
  );

  return el("div", { className: "card" }, [
    el("h2", { className: "t-heading", text: "Import a statement" }),
    form,
  ]);
}

function importsCard(items) {
  if (items.length === 0) {
    return el("div", { className: "card" }, [
      el("h2", { className: "t-heading", text: "Imports" }),
      empty("Nothing imported yet."),
    ]);
  }

  return el("div", { className: "card" }, [
    el("h2", { className: "t-heading", text: "Imports" }),
    ...items.map((item) =>
      el("div", { className: "list-row" }, [
        el("div", { className: "grow" }, [
          el("div", {
            className: "t-body",
            text: `${businessDate(item.period_from)} → ${businessDate(item.period_to)}`,
          }),
          el("div", {
            className: "t-caption",
            text:
              `${item.imported_count} imported` +
              (item.skipped_count ? `, ${item.skipped_count} skipped` : "") +
              (item.original_filename ? ` · ${item.original_filename}` : ""),
          }),
        ]),
        // Rendered as received. `null` shows as a dash rather than as ₹0.00 -- a statement
        // with no balance column is a real case (§5.3a).
        el("div", { className: "t-mono", text: format(item.closing_balance) }),
      ]),
    ),
  ]);
}

/* --- the review list: two sections, two kinds of question --------------------------- */

export async function renderBankReview(container, { session, navigate }) {
  const { shell } = session;
  shell.setTab("credit");
  shell.setTitle("Review statement");
  shell.setActions(
    el("button", {
      className: "btn btn-plain",
      text: "Back",
      attrs: { type: "button" },
      on: { click: () => navigate("#/credit/bank") },
    }),
  );

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let customers;
  let credits;
  let debits;
  try {
    [customers, credits, debits] = await Promise.all([
      api.get("/credit-customers"),
      api.get("/bank-transactions?direction=credit&classification=udhaar_repayment&limit=200"),
      api.get("/bank-transactions?direction=debit&limit=200"),
    ]);
  } catch (error) {
    render(container, errorCard(error));
    return;
  }

  const reload = () => renderBankReview(container, { session, navigate });
  const unconfirmed = credits.items.filter((item) => !item.credit_repayment_id);
  const undecided = debits.items.filter((item) => item.is_expense === "undecided");

  render(
    container,
    el("div", { className: "stack" }, [
      creditsSection(unconfirmed, customers, reload),
      debitsSection(undecided, reload),
    ]),
  );
}

function creditsSection(items, customers, reload) {
  if (items.length === 0) {
    return el("div", { className: "card" }, [
      el("h2", { className: "t-heading", text: "Money in" }),
      empty("Every incoming line has been dealt with."),
    ]);
  }

  const options = [
    { value: "", label: "— choose a customer —" },
    ...customers.map((customer) => ({ value: customer.id, label: customer.name })),
  ];

  const rows = items.map((item) => {
    const picker = select({
      name: `customer-${item.id}`,
      label: "Customer",
      options,
      value: "",
    });
    const remember = checkbox({
      name: `remember-${item.id}`,
      label: "Remember this sender",
      checked: true,
      hint: "Next month this narration will pre-select them — you still confirm.",
    });
    const tick = checkbox({ name: `confirm-${item.id}`, label: "Confirm" });

    return {
      item,
      picker,
      remember,
      tick,
      node: el("div", { className: "list-row stack" }, [
        el("div", { className: "row gap" }, [
          el("div", { className: "grow" }, [
            el("div", { className: "t-body", text: item.narration }),
            el("div", { className: "t-caption", text: businessDate(item.txn_date) }),
          ]),
          el("div", { className: "t-mono", text: format(item.amount) }),
        ]),
        picker,
        remember,
        tick,
      ]),
    };
  });

  const status = el("p", { className: "t-caption" });
  let key = newIdempotencyKey();

  const confirm = el("button", {
    className: "btn",
    text: "Confirm ticked",
    attrs: { type: "button" },
    on: {
      click: async () => {
        const chosen = rows
          .filter((entry) => entry.tick._input.checked)
          .map((entry) => ({
            transaction_id: entry.item.id,
            credit_customer_id: entry.picker._input.value,
            remember_sender: entry.remember._input.checked,
          }));

        if (chosen.length === 0) {
          status.textContent = "Tick the lines you want to record.";
          return;
        }
        if (chosen.some((entry) => !entry.credit_customer_id)) {
          status.textContent = "Every ticked line needs a customer.";
          return;
        }

        confirm.disabled = true;
        status.textContent = "Recording…";
        try {
          const result = await api.post(
            "/bank-transactions/confirm-repayments",
            { items: chosen },
            { idempotencyKey: key },
          );
          key = newIdempotencyKey();
          // Partial success is a real outcome, so it is reported rather than hidden behind
          // a generic success message (§6.8: a form that hides a problem teaches people to
          // stop looking for one).
          toast({
            message:
              `Recorded ${result.created.length}` +
              (result.failed.length ? `, ${result.failed.length} could not be recorded.` : "."),
            kind: result.failed.length ? "warning" : "success",
          });
          reload();
        } catch (error) {
          status.textContent = explain(error);
        } finally {
          confirm.disabled = false;
        }
      },
    },
  });

  return el("div", { className: "card" }, [
    el("h2", { className: "t-heading", text: "Money in" }),
    el("p", {
      className: "t-caption",
      text:
        `${items.length} incoming line${items.length === 1 ? "" : "s"} that look like a ` +
        "customer settling up. Nothing is recorded until you tick it.",
    }),
    ...rows.map((entry) => entry.node),
    el("div", { className: "row gap" }, [confirm]),
    status,
  ]);
}

function debitsSection(items, reload) {
  if (items.length === 0) {
    return el("div", { className: "card" }, [
      el("h2", { className: "t-heading", text: "Money out" }),
      empty("Every outgoing line has been answered."),
    ]);
  }

  return el("div", { className: "card" }, [
    el("h2", { className: "t-heading", text: "Money out" }),
    el("p", {
      className: "t-caption",
      text:
        "Was this a cost, or money moved between your own pockets? An IOCL payment is not " +
        "an expense — it sits with them as an advance and comes back as fuel.",
    }),
    ...items.map((item) => debitRow(item, reload)),
  ]);
}

function debitRow(item, reload) {
  const status = el("span", { className: "t-caption" });

  const answer = async (value) => {
    status.textContent = "Saving…";
    try {
      await api.patch(`/bank-transactions/${item.id}`, { is_expense: value });
      reload();
    } catch (error) {
      status.textContent = explain(error);
    }
  };

  return el("div", { className: "list-row stack" }, [
    el("div", { className: "row gap" }, [
      el("div", { className: "grow" }, [
        el("div", { className: "t-body", text: item.narration }),
        el("div", { className: "row gap" }, [
          el("span", { className: "t-caption", text: businessDate(item.txn_date) }),
          pill(label(item.classification), NOT_A_COST.has(item.classification) ? "info" : "neutral"),
        ]),
      ]),
      el("div", { className: "t-mono", text: format(item.amount) }),
    ]),
    el("div", { className: "row gap" }, [
      el("button", {
        className: item.suggested_expense === "yes" ? "btn" : "btn btn-plain",
        text: "An expense",
        attrs: { type: "button" },
        on: { click: () => answer("yes") },
      }),
      el("button", {
        className: item.suggested_expense === "no" ? "btn" : "btn btn-plain",
        text: "Not an expense",
        attrs: { type: "button" },
        on: { click: () => answer("no") },
      }),
      status,
    ]),
  ]);
}

/* --- reconciliation: three comparisons, discrepancies first ------------------------- */

export async function renderBankReconciliation(container, { session, navigate }) {
  const { shell } = session;
  shell.setTab("credit");
  shell.setTitle("Reconciliation");
  shell.setActions(
    el("button", {
      className: "btn btn-plain",
      text: "Back",
      attrs: { type: "button" },
      on: { click: () => navigate("#/credit/bank") },
    }),
  );

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let accounts;
  let imports;
  try {
    [accounts, imports] = await Promise.all([
      api.get("/bank-accounts"),
      api.get("/bank-statements/imports"),
    ]);
  } catch (error) {
    render(container, errorCard(error));
    return;
  }

  if (imports.items.length === 0) {
    render(container, el("div", { className: "card" }, [empty("Import a statement first.")]));
    return;
  }

  const latest = imports.items[0];
  let report;
  try {
    report = await api.get(
      `/bank-statements/reconciliation?bank_account_id=${latest.bank_account_id}` +
        `&date_from=${latest.period_from}&date_to=${latest.period_to}`,
    );
  } catch (error) {
    render(container, errorCard(error));
    return;
  }

  render(
    container,
    el("div", { className: "stack" }, [
      el("div", { className: "card" }, [
        el("h2", { className: "t-heading", text: "Period" }),
        row("From", businessDate(report.date_from)),
        row("To", businessDate(report.date_to)),
        // The money-in-transit figure Phase 21's bridge will add to the closing balance.
        row(
          "Settling the next morning",
          format(report.boundary_settlement),
          { valueClass: "t-mono" },
        ),
      ]),
      settlementsCard(report.settlements),
      depositsCard(report.deposits),
    ]),
  );
}

function settlementsCard(items) {
  const mismatched = items.filter((item) => !item.matches);

  return el("div", { className: "card" }, [
    el("h2", { className: "t-heading", text: "Card & UPI vs Paytm" }),
    el("p", {
      className: "t-caption",
      text:
        `${items.length - mismatched.length} of ${items.length} days settled exactly. ` +
        "Paytm pays a whole day's card and UPI together the next morning.",
    }),
    ...(mismatched.length === 0
      ? [empty("Every day agrees.")]
      : mismatched.map((item) =>
          el("div", { className: "list-row" }, [
            el("div", { className: "grow" }, [
              el("div", { className: "t-body", text: businessDate(item.business_date) }),
              el("div", {
                className: "t-caption",
                // `settled: null` is its own sentence. "Nothing arrived" and "₹0 arrived"
                // are different facts and only one of them is a discrepancy (§6.8).
                text:
                  item.settled === null
                    ? `Nothing has arrived yet for this day (expected ${format(item.expected)})`
                    : `Expected ${format(item.expected)}, received ${format(item.settled)}`,
              }),
            ]),
            el("div", {
              className: "t-mono",
              text: format(item.difference, { sign: true }),
            }),
          ]),
        )),
  ]);
}

function depositsCard(items) {
  const problems = items.filter((item) => item.kind !== "matched");
  // Matched but slow: worth a glance, not an alarm. Cash normally reaches the branch the
  // next morning, so anything longer is a deposit that sat somewhere.
  const slow = items.filter((item) => item.kind === "matched" && (item.days_late ?? 0) > 1);
  const KINDS = {
    missing_from_books: "In the bank, not in the app",
    missing_from_bank: "In the app, never reached the bank",
  };

  return el("div", { className: "card" }, [
    el("h2", { className: "t-heading", text: "Cash deposits" }),
    el("p", {
      className: "t-caption",
      text:
        `${items.length - problems.length} of ${items.length} matched` +
        (slow.length ? `, ${slow.length} took more than a day to reach the bank.` : "."),
    }),
    ...(problems.length === 0
      ? [empty("Every deposit agrees.")]
      : problems.map((item) =>
          el("div", { className: "list-row" }, [
            el("div", { className: "grow" }, [
              el("div", { className: "t-body", text: businessDate(item.txn_date) }),
              el("div", { className: "t-caption", text: KINDS[item.kind] ?? item.kind }),
            ]),
            el("div", { className: "t-mono", text: format(item.amount) }),
          ]),
        )),
  ]);
}
