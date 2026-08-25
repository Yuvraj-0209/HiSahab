/* Today: the shift spine (CLAUDE.md §4.7, §5.2, §6.8).
 *
 * §5.2 calls `shifts` "the spine -- everything hangs off this", and this screen is that spine
 * made visible: which shift is open, what it is worth so far, and the lifecycle actions
 * available on it.
 *
 * ## The status transitions, and why each button says what it says
 *
 * open -> closed -> locked, and never backwards without an admin action that is itself
 * audit-logged (§5.2). The buttons are role-gated for politeness only -- §8's floors are
 * enforced server-side, and every action here handles a real 403.
 *
 * **Closing can legitimately fail, and those failures are the feature.** §6.8's three
 * preconditions -- MISSING_NOZZLE_READINGS, MISSING_COLLECTIONS, CREDIT_SALE_MISSING_RECEIPT
 * -- fire on *absence*, never on a mismatch. A shift whose collections do not equal its sales
 * closes normally, because that gap is §6.4's variance and §6.6's udhaar. §14 is explicit
 * that blocking on it "teaches staff to type figures that balance", so this screen never
 * pre-checks the numbers and never disables Close on the strength of them. It sends the
 * request and reports what the server said.
 *
 * ## Profit is labelled, every time it appears
 *
 * §13.7: what this reports is *gross fuel margin on quantity sold*, not business profit. It
 * excludes stock revaluation entirely -- holding 12 kL when the rate rises ₹1 is a real
 * ₹12,000 gain this system will never see. §13.7 requires the label wherever the figure is
 * displayed, because an unlabelled "profit" here is exactly the plausible-but-wrong number
 * this project exists to prevent.
 */

import { el, empty, pill, render, row } from "../dom.js";
import { api, explain, ApiError } from "../api.js";
import { format, isNegative, isZero, quantity, reading } from "../money.js";
import { businessDate, businessDateWeekday, todayAtOutlet } from "../time.js";
import { field, Form, select } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { notify } from "../ui/toast.js";
import { satisfies } from "../ui/nav.js";

export async function renderToday(container, { session, navigate }) {
  const { shell } = session;
  shell.setTab("today");
  shell.setTitle("Today");
  shell.setActions();

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let shift = null;
  try {
    shift = await api.get("/shifts/current");
  } catch (error) {
    if (!(error instanceof ApiError) || error.code !== "NO_OPEN_SHIFT") {
      // NOT_YOUR_SHIFT reaches an attendant when somebody else's shift is the open one --
      // a real state, not an error to hide behind a generic message.
      render(container, errorCard(error, () => renderToday(container, { session, navigate })));
      return;
    }
  }

  if (!shift) {
    renderNoShift(container, { session, navigate });
    return;
  }

  await renderShiftDetail(container, shift, { session, navigate });
}

// A shift stops being reachable through `renderToday` the moment it is no longer open --
// `/shifts/current` 404s, and there was previously no other route to it at all, so a
// `closed` shift waiting to be locked (or reviewed, or reopened) had no page. This is that
// page: fetch by id, works for a shift in any status, wired to `#/shifts/:shiftId`.
export async function renderShiftById(container, { session, navigate, shiftId }) {
  const { shell } = session;
  shell.setTab("today");
  shell.setTitle("Today", "Loading…");
  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let shift;
  try {
    shift = await api.get(`/shifts/${shiftId}`);
  } catch (error) {
    render(
      container,
      errorCard(error, () => renderShiftById(container, { session, navigate, shiftId })),
    );
    return;
  }

  await renderShiftDetail(container, shift, { session, navigate });
}

// Shared by `renderToday` (which fetches `/shifts/current`, the *open* shift only) and the
// close/lock/reopen handlers below, which already hold the server's updated shift straight
// from the PATCH response. That sharing matters: `/shifts/current` 404s the instant a shift
// is no longer open, so re-fetching it right after Close would strand an admin on "no open
// shift" with no way back to the very shift that still needs locking. Passing the response
// body through directly is the fix, not another round trip.
async function renderShiftDetail(container, shift, { session, navigate }) {
  const { shell, me } = session;
  shell.setTab("today");
  shell.setTitle(
    "Today",
    `${businessDateWeekday(shift.business_date)} ${businessDate(shift.business_date)} · shift ${shift.sequence}`,
  );
  shell.setActions();

  // All manager-floor reads, so an attendant simply does not make them -- §8 for sales
  // (a report, not a data-entry sheet), and the same reasoning covers reading
  // collections/expenses/credit/deposits/day-cash/readings for a shift that is not the
  // caller's own.
  let sales = null;
  let collectionsPage = null;
  let expensesPage = null;
  let creditSalesPage = null;
  let creditRepaymentsPage = null;
  let bankDepositsPage = null;
  let dayCash = null;
  let readingsWorksheet = null;
  const customersById = new Map();
  if (satisfies(me.role, "manager")) {
    try {
      sales = await api.get(`/shifts/${shift.id}/sales`);
    } catch (error) {
      // A missing price refuses valuation outright (§6.3) and that is correct behaviour --
      // a day valued at zero would reconcile to a surplus nobody can explain. Reported as a
      // note on the card rather than as a failure of the whole screen.
      sales = { error };
    }
    // Each of these is independently caught, the same way `sales` is above -- one section
    // failing to load (a network blip, a stale permission) must not blank the whole page.
    try {
      collectionsPage = await api.get(`/shifts/${shift.id}/collections`);
    } catch {
      collectionsPage = null;
    }
    try {
      expensesPage = await api.get(`/shifts/${shift.id}/expenses`);
    } catch {
      expensesPage = null;
    }
    try {
      creditSalesPage = await api.get(`/shifts/${shift.id}/credit-sales`);
      creditRepaymentsPage = await api.get(`/shifts/${shift.id}/credit-repayments`);
      for (const customer of await api.get("/credit-customers", { include_inactive: true })) {
        customersById.set(customer.id, customer);
      }
    } catch {
      creditSalesPage = null;
      creditRepaymentsPage = null;
    }
    try {
      bankDepositsPage = await api.get(`/shifts/${shift.id}/bank-deposits`);
    } catch {
      bankDepositsPage = null;
    }
    try {
      // Never /daily-summaries/{date} -- that 404s until someone has created a summary.
      // /reports/daily/{date} always answers, live-computing when nothing is stored yet
      // (§13.20's `source` field says which), which is what makes it safe to call for any
      // shift regardless of whether its day has ever been reconciled.
      const report = await api.get(`/reports/daily/${shift.business_date}`);
      dayCash = report.cash;
    } catch {
      dayCash = null;
    }
    try {
      // Read-only use of the same worksheet `readings.js` edits -- surfaced here as the
      // Metered sales card's "Details" popout instead of a separate link-out row. The
      // editable worksheet, with its confirm/mismatch entry actions, stays exclusively at
      // #/shifts/{id}/readings (reached from the Entry tab).
      readingsWorksheet = await api.get(`/shifts/${shift.id}/readings`);
    } catch {
      readingsWorksheet = null;
    }
  }

  const unreviewed = expensesUnreviewedCount(expensesPage);

  render(
    container,
    el("div", { className: "stack" }, [
      actionsCard(shift, { session, container, navigate }),

      // One card per domain, in the app's standard `.grid` of `.card stack` -- the same shape
      // `admin_customers.js::customerCard` uses, and for the same reason: a title and status in
      // the header, the figures in a grouped `.list`, actions in a row at the foot. "Details"
      // opens the full list in this app's existing grabbable, spring-animated bottom sheet
      // (ui/sheet.js). Nothing on this screen is a data-entry surface.
      el("div", { className: "grid" }, [
        domainCard({
          title: "Metered sales",
          subtitle: sales?.error ? "not valued" : format(sales?.total_sale_value),
          rows: salesRows(sales),
          sheetTitle: "Metered sales",
          onDetails: () => openSalesSheet(sales, readingsWorksheet),
        }),
        domainCard({
          title: "Collections",
          subtitle: "What was taken, and how",
          rows: collectionsRows(collectionsPage),
          sheetTitle: "Collections",
        }),
        domainCard({
          title: "Expenses",
          subtitle: countLabel(expensesPage?.items.length, "item"),
          badge: unreviewed ? pill(`${unreviewed} needs review`, "review") : null,
          rows: expensesRows(expensesPage),
          sheetTitle: "Expenses",
        }),
        domainCard({
          title: "Credit",
          subtitle: `${countLabel(creditSalesPage?.items.length, "sale")} · ${countLabel(creditRepaymentsPage?.items.length, "repayment")}`,
          rows: creditSummaryRows(creditSalesPage, creditRepaymentsPage),
          sheetTitle: "Credit",
          onDetails: () => openCreditSheet(creditSalesPage, creditRepaymentsPage, customersById),
        }),
        domainCard({
          title: "Bank deposits",
          subtitle: countLabel(bankDepositsPage?.items.length, "deposit"),
          rows: bankDepositsRows(bankDepositsPage),
          sheetTitle: "Bank deposits",
        }),
        domainCard({
          title: "Day cash",
          subtitle: businessDate(shift.business_date),
          badge: dayCash ? pill(DAY_SOURCE_PILL[dayCash.source] ?? dayCash.source, dayCash.source === "snapshot" ? "locked" : "neutral") : null,
          rows: dayCashRows(dayCash),
          sheetTitle: `Day cash — ${businessDate(shift.business_date)}`,
        }),
      ]),
    ]),
  );
}

/** One domain's card, in the shape `admin_customers.js::customerCard` established: a header
 * carrying the title, a secondary line and an optional status pill; a grouped `.list` of the
 * figures; an action row at the foot.
 *
 * A `div` rather than a `<button>`, deliberately -- the action lives on a real button *inside*
 * the card (as the reference's Edit/Ledger row does), and a button nested inside a button is
 * invalid HTML.
 *
 * Shows up to `PREVIEW_CAP` `{label, value}` rows. `{note}` entries -- §13.7's margin
 * disclaimer, an empty-state sentence -- are sheet-only: they are explanations, not figures,
 * and a card face is the wrong place to read one. */
const PREVIEW_CAP = 4;

function domainCard({ title, subtitle, badge = null, rows, sheetTitle, onDetails }) {
  const values = rows.filter((item) => "value" in item);
  const shown = values.slice(0, PREVIEW_CAP);
  const hidden = values.length - shown.length;
  // Most cards open the generic flat-list sheet; a card whose detail view needs its own
  // shape (Credit's Issued/Repaid split, Metered sales' nozzle-readings section) passes
  // `onDetails` instead.
  const openDetails = onDetails ?? (() => openDetailSheet(sheetTitle, rows));

  return el("div", { className: "card stack" }, [
    el("div", { className: "row-between" }, [
      el("div", { className: "grow" }, [
        el("div", { className: "t-headline", text: title }),
        subtitle ? el("div", { className: "t-caption", text: subtitle }) : null,
      ]),
      badge,
    ]),

    shown.length
      ? el(
          "div",
          { className: "list" },
          shown.map((item) => row(item.label, item.value, { valueClass: item.valueClass ?? "" })),
        )
      : null,

    // `card-footer` pins this row to the card's bottom edge (margin-top: auto) so every
    // card's action lines up regardless of how many rows sit above it -- what makes the
    // `.grid`'s stretch-to-equal-height cards actually look equal, not just measure equal.
    el("div", { className: "row card-footer" }, [
      el("button", {
        className: "btn grow",
        text: hidden > 0 ? `View all (${values.length})` : "Details",
        attrs: { type: "button" },
        on: { click: openDetails },
      }),
    ]),
  ]);
}

/** "4 sales", "1 deposit", or "none" -- the secondary identity line under a card's title, the
 * way the reference card puts a phone number under a customer's name. */
function countLabel(count, noun) {
  if (count === undefined || count === null) return "unavailable";
  if (count === 0) return `no ${noun}s`;
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

/** Every card's "Details" opens the same way: a view-only sheet, no footer. `{label, value}`
 * becomes a list row; `{note}` becomes a plain explanatory line -- the same distinction
 * `domainCard` uses to decide what belongs on a card face and what does not. */
function openDetailSheet(title, items) {
  const rows = items.map((item) =>
    "note" in item
      ? el("p", { className: "t-caption list-row", text: item.note })
      : row(item.label, item.value, { valueClass: item.valueClass ?? "" }),
  );
  openSheet({ title, body: el("div", { className: "list" }, rows) });
}

function expensesUnreviewedCount(page) {
  return page ? page.items.filter((e) => e.requires_review && !e.reviewed_at).length : 0;
}

function salesRows(sales) {
  if (!sales) return [{ label: "Metered sales", value: "—" }];
  if (sales.error) {
    return [
      { note: explain(sales.error) },
      {
        note: "Sales cannot be valued until a price exists for every fuel sold. Nothing is wrong with the readings.",
      },
    ];
  }

  const quantities = Object.entries(sales.quantity_by_unit ?? {});
  return [
    { label: "Total", value: format(sales.total_sale_value), valueClass: "list-row-value-strong" },
    ...quantities.map(([unit, amount]) => ({ label: unit, value: quantity(amount, unit) })),
    {
      label: "Gross fuel margin",
      value: format(sales.total_gross_fuel_margin, { absent: "no margin entered" }),
    },
    // §13.7, and this label is not optional.
    { note: "Gross fuel margin on quantity sold — not business profit. It excludes stock revaluation." },
    sales.incomplete
      ? { note: "Some nozzles have no closing reading yet, so this total is partial." }
      : null,
  ].filter(Boolean);
}

/** Metered sales' "Details" -- the sales summary above, and every nozzle's readings below
 * it, so the box that reports the total is also where the meters behind it can be checked.
 * Read-only: no confirm/mismatch controls, no entry. The editable worksheet stays exclusively
 * at #/shifts/{id}/readings (reached from the Entry tab), per §4.7's confirm-don't-assume
 * rule -- this sheet cannot be where an opening gets confirmed. */
function openSalesSheet(sales, worksheet) {
  const salesSection = el(
    "div",
    { className: "list" },
    salesRows(sales).map((item) =>
      "note" in item
        ? el("p", { className: "t-caption list-row", text: item.note })
        : row(item.label, item.value, { valueClass: item.valueClass ?? "" }),
    ),
  );

  const readingsSection = worksheet?.lines.length
    ? el("div", { className: "stack" }, [
        el("div", { className: "section-label t-micro", text: "Nozzle readings" }),
        ...worksheet.lines.map(nozzleReadingRows),
      ])
    : el("p", { className: "t-caption", text: "Nozzle readings unavailable." });

  openSheet({
    title: "Metered sales",
    body: el("div", { className: "stack" }, [salesSection, readingsSection]),
  });
}

/** One nozzle's readings, read-only. Mirrors `readings.js::savedBody`'s figures without its
 * entry controls -- opening, closing, testing and quantity sold, the four numbers §4.2 and
 * §6.2 exist to get right. */
function nozzleReadingRows(line) {
  const saved = line.reading;
  return el("div", { className: "list" }, [
    el("div", { className: "list-row" }, [
      el("div", { className: "list-row-main" }, [
        el("span", { className: "t-body", text: line.nozzle_label }),
        el("div", { className: "t-caption", text: `${line.dispenser_label} · ${line.fuel_type_code}` }),
      ]),
    ]),
    saved
      ? row("Opening", reading(saved.opening_reading))
      : row("Reading", "not recorded"),
    saved ? row("Closing", reading(saved.closing_reading, { absent: "not entered" })) : null,
    saved ? row("Testing", quantity(saved.testing_quantity, line.unit_of_measure)) : null,
    saved
      ? row("Sold", quantity(saved.quantity_sold, line.unit_of_measure, { absent: "awaiting closing" }))
      : null,
  ].filter(Boolean));
}

const COLLECTION_MODES = [
  { value: "card", label: "Card" },
  { value: "upi", label: "UPI" },
  { value: "wallet", label: "Wallet" },
];

function collectionsRows(page) {
  if (!page) return [{ label: "Collections", value: "—" }];
  // Live rows only -- a reversed row and the reversal that cancels it are history, not the
  // current declaration (same filter `collections.js` uses for its per-mode view).
  const live = page.items.filter((item) => !item.reverses_id && !item.is_reversed);
  const byMode = new Map(live.map((item) => [item.mode, item]));

  return [
    // "not declared" and ₹0.00 are different facts (§6.8) -- never coalesced.
    { label: "Cash declared", value: format(page.declared_cash, { absent: "not declared" }) },
    ...COLLECTION_MODES.map((mode) => ({
      label: mode.label,
      value: format(byMode.get(mode.value)?.amount),
    })),
  ];
}

function expensesRows(page) {
  if (!page) return [{ label: "Expenses", value: "—" }];
  // `total` is server-computed (§14 forbids summing money in JS) -- see ExpensePage.total.
  const total = { label: "Total", value: format(page.total), valueClass: "list-row-value-strong" };
  const totals = Object.entries(page.totals_by_category ?? {}).map(([code, amount]) => ({
    label: code,
    value: format(amount),
  }));

  const items = page.items.length
    ? page.items.map((expense) => {
        const flagged = expense.requires_review && !expense.reviewed_at;
        return {
          label: `${expense.description} · ${expense.category_code}${flagged ? " ⚑" : ""}`,
          value: format(expense.amount),
        };
      })
    : [{ note: "No expenses recorded for this shift." }];

  return [
    total,
    ...totals,
    ...items,
    page.truncated ? { note: "This shift has more expenses than shown here." } : null,
  ].filter(Boolean);
}

/** The server-computed total first -- it is the figure the card is read for -- then a line per
 * customer. `totalLabel` differs by direction ("Issued" vs "Repaid") because "Total" on two
 * adjacent cards showing opposite movements of money is the kind of ambiguity this project
 * spends its whole spec avoiding. */
function creditTotalRows(page, customersById, totalLabel) {
  if (!page) return [{ label: totalLabel, value: "—" }];

  const rows = [{ label: totalLabel, value: format(page.total) }];
  // Repayments carry a second server-computed figure: only the cash half reaches the drawer
  // (§6.4), so a card that showed the gross total alone would overstate what the locker got.
  if (page.cash_total !== undefined) {
    rows.push({ label: "Of which cash", value: format(page.cash_total) });
  }
  if (!page.items.length) return [...rows, { note: "Nothing recorded this shift." }];

  return [
    ...rows,
    ...page.items.map((entry) => ({
      label: customersById.get(entry.credit_customer_id)?.name ?? "Unknown customer",
      value: format(entry.amount),
    })),
  ];
}

/** The merged Credit card's face: udhaar issued and repaid are opposite movements of the
 * same money, so both totals sit on one card rather than two (§6.6's ledger, read together
 * the way a manager actually thinks about it). "Issued" is bolded -- it's the figure that
 * grows what a customer owes, the one a manager scans for first. */
function creditSummaryRows(salesPage, repayPage) {
  return [
    { label: "Issued", value: salesPage ? format(salesPage.total) : "—", valueClass: "list-row-value-strong" },
    { label: "Repaid", value: repayPage ? format(repayPage.total) : "—" },
    repayPage?.cash_total !== undefined
      ? { label: "Of which cash", value: format(repayPage.cash_total) }
      : null,
  ].filter(Boolean);
}

/** Credit's "Details": two labelled sections, Issued then Repaid, each in the same
 * `creditTotalRows` shape the two former separate cards used -- so nothing about the
 * per-customer breakdown is lost by merging the cards, only the second card face. */
function openCreditSheet(salesPage, repayPage, customersById) {
  const section = (label, rows) =>
    el("div", { className: "stack" }, [
      el("div", { className: "section-label t-micro", text: label }),
      el(
        "div",
        { className: "list" },
        rows.map((item) =>
          "note" in item
            ? el("p", { className: "t-caption list-row", text: item.note })
            : row(item.label, item.value, { valueClass: item.valueClass ?? "" }),
        ),
      ),
    ]);

  openSheet({
    title: "Credit",
    body: el("div", { className: "stack" }, [
      section("Issued", creditTotalRows(salesPage, customersById, "Issued")),
      section("Repaid", creditTotalRows(repayPage, customersById, "Repaid")),
    ]),
  });
}

function bankDepositsRows(page) {
  if (!page) return [{ label: "Deposited", value: "—" }];

  const total = { label: "Deposited", value: format(page.total) };
  if (!page.items.length) return [total, { note: "No deposits recorded for this shift." }];

  const rows = [
    total,
    ...page.items.map((deposit) => ({
      label: deposit.bank_reference ?? "Deposit",
      value: format(deposit.amount),
    })),
  ];
  return page.truncated
    ? [...rows, { note: "This shift has more deposits than shown here." }]
    : rows;
}

/* §13.20's provenance, in the two registers this screen needs it: a short pill for the card
 * header, and a full sentence for the sheet. A snapshot is a record of what a manager was
 * shown; a computed figure is an estimate of a day still in motion. Reading them as the same
 * number is the mistake `source` exists to prevent, so neither register hides which it is. */
const DAY_SOURCE_PILL = {
  snapshot: "finalised",
  computed: "live",
  no_trading: "no trading",
  unavailable: "unavailable",
};

const DAY_SOURCE_LABEL = {
  snapshot: "Finalised — these are the figures as they were recorded on the day.",
  computed: "Live estimate — this day has not been reconciled, so these are derived now.",
  no_trading: "No trading recorded for this date.",
  unavailable: "Not available.",
};

/** `daily_cash_summaries.variance` is a generated column, `actual_counted − expected_closing`
 * (app/models/cash.py:217) -- so a NEGATIVE variance is a shortage and a positive one is a
 * surplus.
 *
 * That is the exact opposite of `cash_position`'s `gap`, which is `accountable_cash −
 * declared_cash` and whose own docstring says "positive means short". `money.js::gapLabel` is
 * written for the latter, so reusing it here would print "surplus" across a shortage -- a
 * plausible, confident, wrong label on the one figure a day is judged by. Hence a separate
 * function, and this comment, rather than the tempting import. */
function varianceLabel(value) {
  if (value === null || value === undefined) return { text: "not counted", className: "t-absent" };
  if (isZero(value)) return { text: `${format(value)} · balanced`, className: "" };
  if (isNegative(value)) return { text: `${format(value)} · short`, className: "text-short" };
  return { text: `${format(value)} · surplus`, className: "text-surplus" };
}

function dayCashRows(dayCash) {
  if (!dayCash) return [{ label: "Day cash", value: "—" }];
  const variance = varianceLabel(dayCash.variance);
  return [
    { label: "Opening balance", value: format(dayCash.opening_balance) },
    { label: "Expected closing", value: format(dayCash.expected_closing) },
    { label: "Actual counted", value: format(dayCash.actual_counted, { absent: "not counted" }) },
    { label: "Variance", value: variance.text, valueClass: variance.className },
    { note: DAY_SOURCE_LABEL[dayCash.source] ?? dayCash.source },
    dayCash.unavailable_reason ? { note: dayCash.unavailable_reason } : null,
  ].filter(Boolean);
}

function linkRow(label, hint, onClick) {
  return el(
    "button",
    {
      className: "list-row",
      attrs: { type: "button" },
      on: { click: onClick },
      style: { width: "100%", background: "none", border: 0, textAlign: "left", font: "inherit", color: "inherit", cursor: "pointer" },
    },
    [
      el("div", { className: "list-row-main" }, [
        el("div", { className: "t-body", text: label }),
        el("div", { className: "t-caption", text: hint }),
      ]),
      el("div", { className: "t-body", text: "›", attrs: { "aria-hidden": "true" } }),
    ],
  );
}

function actionsCard(shift, { session, container, navigate }) {
  const { me } = session;
  const actions = [];

  if (shift.status === "open" && satisfies(me.role, "manager")) {
    actions.push(
      el("button", {
        className: "btn btn-primary btn-block",
        text: "Close shift",
        attrs: { type: "button" },
        on: { click: () => closeShift(shift, { session, container, navigate }) },
      }),
    );
  }

  if (shift.status === "closed" && satisfies(me.role, "admin")) {
    actions.push(
      el("button", {
        className: "btn btn-primary btn-block",
        text: "Lock shift",
        attrs: { type: "button" },
        on: { click: () => lockShift(shift, { session, container, navigate }) },
      }),
      el("button", {
        className: "btn btn-block",
        text: "Reopen shift",
        attrs: { type: "button" },
        on: { click: () => reopenShift(shift, { session, container, navigate }) },
      }),
    );
  }

  if (!actions.length) {
    // Never a bare empty region (§16: every screen answers "what's here?"). Saying why there
    // is nothing to do is more useful than showing nothing.
    const reason =
      shift.status === "locked"
        ? "This shift is locked. Locked is terminal — corrections happen as reversals."
        : shift.status === "closed"
          ? "This shift is closed. An admin can lock or reopen it."
          : "Only a manager or admin can close a shift.";
    return el("div", { className: "card" }, [el("p", { className: "t-caption", text: reason })]);
  }

  return el("div", { className: "card stack" }, actions);
}

/* --- lifecycle --------------------------------------------------------------- */

async function closeShift(shift, context) {
  // No client-side pre-check of collections against sales, deliberately. §6.8 and §14: that
  // gap is the variance, and refusing to close on it leaves the salesman in front of a form
  // with one freely adjustable field. The server decides; we report.
  try {
    const updated = await api.patch(`/shifts/${shift.id}/close`, {});
    notify.success("Shift closed.");
    // Not renderToday(): that re-fetches /shifts/current, which 404s the instant this shift
    // stops being open and would drop the admin straight onto "no open shift" -- with the
    // Lock button nowhere reachable. The PATCH response already is the closed shift.
    renderShiftDetail(context.container, updated, context);
  } catch (error) {
    notify.error(explain(error), { requestId: error.requestId });
  }
}

async function lockShift(shift, context) {
  try {
    const updated = await api.patch(`/shifts/${shift.id}/lock`, {});
    notify.success("Shift locked.");
    renderShiftDetail(context.container, updated, context);
  } catch (error) {
    notify.error(explain(error), { requestId: error.requestId });
  }
}

function reopenShift(shift, context) {
  // §6.8: reopening takes a MANDATORY reason and is audit-logged. A sheet rather than a
  // confirm dialog, because the reason is the point -- and §5.2 requires it to be a real
  // sentence, not an acknowledgement.
  const reason = field({
    name: "reason",
    label: "Why is this being reopened?",
    required: true,
    hint: "Recorded in the audit trail against your name. 3–500 characters.",
  });
  const form = new Form({ reason });

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: "Reopen shift",
    attrs: { type: "button" },
  });

  submit.addEventListener("click", async () => {
    form.clearErrors();
    submit.disabled = true;
    try {
      const updated = await api.patch(`/shifts/${shift.id}/reopen`, {
        reason: form.values().reason,
      });
      sheet.close();
      notify.success("Shift reopened.");
      // §13.10: a mid-chain reopen FLAGS the next shift's reading for review rather than
      // recomputing it, and flags a finalised day's summary (§13.16). Said out loud, because
      // the consequence is invisible on this screen and somebody has to act on it.
      notify.info(
        "Any following shift's opening reading is now flagged for review. Nothing was recomputed.",
      );
      renderShiftDetail(context.container, updated, context);
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    }
  });

  const sheet = openSheet({
    title: "Reopen shift",
    body: el("div", { className: "stack" }, [
      el("p", {
        className: "t-caption",
        text: "Reopening moves this shift back to open. The reason is stored in the audit trail.",
      }),
      reason,
    ]),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}

/* --- no open shift ----------------------------------------------------------- */

async function renderNoShift(container, context) {
  const { session, navigate } = context;
  session.shell.setTitle("Today", "No open shift");

  // A closed-but-not-yet-locked shift has no other page pointing at it -- `/shifts/current`
  // only ever answers with the open one. Manager+ gets a way back in, the same floor
  // `GET /shifts` already reads at server-side (§8: "Read all shifts" is manager+, an
  // attendant reads only their own and has nothing here to act on anyway).
  let recent = [];
  if (satisfies(session.me.role, "manager")) {
    try {
      recent = (await api.get("/shifts", { limit: 8 })).items;
    } catch {
      // Not fatal -- the "open a shift" action below still works without this list.
      recent = [];
    }
  }

  render(
    container,
    el("div", { className: "stack" }, [
      el("div", { className: "card stack" }, [
        el("p", { className: "t-body", text: "There is no open shift at this outlet." }),
        el("p", {
          className: "t-caption",
          text: "Only one shift may be open at a time — that is what makes the carried-forward meter reading unambiguous.",
        }),
        el("button", {
          className: "btn btn-primary btn-block",
          text: "Open a shift",
          attrs: { type: "button" },
          // `container` is passed explicitly rather than read off `context`, and that is a
          // bug fix rather than a style preference. This function used to call
          // `openShiftSheet(context)`, and `renderNoShift` is invoked with
          // `{ session, navigate }` -- no `container` key -- so the refresh after a
          // successful POST reached `render(undefined, ...)` and threw. The shift was
          // created and the screen never showed it.
          //
          // Found in Phase 14 while adding the attendant picker below. §13.18 names this
          // exact class: everything below the browser is tested to 100% and a broken form
          // fails no suite. A positional argument is the version that cannot be forgotten.
          on: { click: () => openShiftSheet(container, context) },
        }),
      ]),
      recent.length
        ? el("div", { className: "stack" }, [
            el("div", { className: "section-label t-micro", text: "Recent shifts" }),
            el(
              "div",
              { className: "list" },
              recent.map((shift) =>
                linkRow(
                  `${businessDate(shift.business_date)} · shift ${shift.sequence}`,
                  shift.status === "closed"
                    ? "Closed — needs locking or review"
                    : shift.status[0].toUpperCase() + shift.status.slice(1),
                  () => navigate(`#/shifts/${shift.id}`),
                ),
              ),
            ),
          ])
        : null,
    ]),
  );
}

async function openShiftSheet(container, context) {
  const { me } = context.session;

  // Defaults come from the outlet's shift template (§5.1), which is exactly what it is for:
  // "nobody should retype 06:00 and 22:00 every morning". The values are materialised onto
  // the shift row by the server -- the template is never read back afterwards (§14).
  let templates = [];
  try {
    templates = await api.get("/shift-templates");
  } catch {
    // Not fatal: started_at is optional and the server falls back to the template itself.
    templates = [];
  }

  // Phase 14. A manager may open a shift in a salesman's name -- `POST /shifts` has
  // accepted `attendant_id` since Phase 4 -- but until the roster endpoint existed there
  // was no way to offer the choice, so through the app it was impossible.
  //
  // Gated on the manager floor for two reasons that agree: `GET /users` refuses an
  // attendant (§8), and `POST /shifts` refuses an attendant a foreign `attendant_id` with
  // 403 NOT_YOUR_SHIFT anyway -- so a picker shown to one would offer choices the server
  // would reject. Not hidden as a permission control (§8: hiding a button is UX); the
  // server enforces both halves regardless.
  let roster = [];
  if (satisfies(me.role, "manager")) {
    try {
      roster = await api.get("/users");
    } catch {
      // Not fatal either: with no roster the field is simply absent and the server
      // defaults `attendant_id` to the caller, which is the pre-Phase-14 behaviour.
      roster = [];
    }
  }

  const businessDateField = field({
    name: "business_date",
    label: "Business date",
    type: "date",
    // §6.1: a future business date is always a data-entry error, and "future" is evaluated
    // in the outlet's zone rather than the browser's.
    value: todayAtOutlet(),
    max: todayAtOutlet(),
    required: true,
    hint: "The trading day this shift belongs to — not necessarily the day it is typed in.",
  });

  const fields = { business_date: businessDateField };

  if (templates.length > 1) {
    fields.sequence_hint = select({
      name: "sequence_hint",
      label: "Shift",
      options: templates.map((template) => ({
        value: String(template.sequence),
        label: `${template.label} (${template.starts_at_local}–${template.ends_at_local})`,
      })),
      hint: "The sequence is assigned by the server; this only picks the default times.",
    });
  }

  if (roster.length) {
    fields.attendant_id = select({
      name: "attendant_id",
      label: "Attendant",
      value: me.id,
      options: roster.map((person) => ({
        value: person.id,
        label: person.id === me.id ? `${person.full_name} (you)` : person.full_name,
      })),
      hint: "The one person accountable for this shift's cash. A shortfall is booked against this name.",
    });
  }

  const form = new Form(fields);

  const submit = el("button", {
    className: "btn btn-primary btn-block",
    text: "Open shift",
    attrs: { type: "button" },
  });

  submit.addEventListener("click", async () => {
    form.clearErrors();
    submit.disabled = true;
    try {
      // `sequence` is deliberately never sent: it is server-assigned, and ShiftCreate's
      // extra="forbid" makes sending it a 422 (§14).
      const values = form.values();
      const body = { business_date: values.business_date };
      // Omitted entirely rather than sent as the caller's own id, so an attendant's request
      // is byte-identical to what it was before Phase 14 and the server's own default
      // (`payload.attendant_id or actor.user.id`) stays the single place that decision is
      // made.
      if (values.attendant_id) body.attendant_id = values.attendant_id;
      await api.post("/shifts", body);
      sheet.close();
      notify.success("Shift opened.");
      renderToday(container, context);
    } catch (error) {
      submit.disabled = false;
      const unmatched = error.isValidation ? form.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    }
  });

  const sheet = openSheet({
    title: "Open a shift",
    body: el("div", { className: "stack" }, form.nodes()),
    footer: el("div", { style: { padding: "0 1rem 1rem" } }, [submit]),
  });
}

/* --- shared ------------------------------------------------------------------ */

export function errorCard(error, onRetry) {
  return el("div", { className: "card stack" }, [
    el("p", { className: "t-body", text: explain(error) }),
    error?.requestId
      ? el("p", { className: "t-micro toast-request-id", text: `Reference ${error.requestId}` })
      : null,
    onRetry
      ? el("button", {
          className: "btn",
          text: "Try again",
          attrs: { type: "button" },
          on: { click: onRetry },
        })
      : null,
  ]);
}
