/* The Summary tab: one window, the whole business, charted (Phase 19).
 *
 * Eighteen phases built screens that answer questions about **one business date or one
 * shift**. This is the first that answers a question about a *period* -- did petrol outsell
 * diesel this quarter, is udhaar growing, are the bills creeping up -- which are the
 * questions the owner actually makes decisions on.
 *
 * ## One request, and why that is a rule rather than a convenience
 *
 * Every cross-panel figure here is a *share*: this fuel's part of sales, this category's
 * part of expenses. A share is `value / total`, which is **arithmetic on money**, forbidden
 * in this client by §14 -- JavaScript has no decimal type and §3 rule 1 does not stop at the
 * API boundary. So `GET /reports/summary` computes every one of them in `Decimal` and sends
 * ready-made percentage strings that this file only ever *assigns*.
 *
 * That is also why it is one endpoint rather than four. Four responses stitched together
 * here would be four server passes free to disagree with each other, and the disagreement
 * would surface as a pie that does not close, with no obvious cause.
 *
 * ## The distinction this screen must not soften
 *
 * §13.35: a window mixing reconciled and unreconciled days is **part record and part live
 * estimate**, and a single total cannot say which. `days_by_source` is the only thing that
 * can, so it is rendered in words rather than left for a reader to assume. `partial` means
 * at least one day could not be computed, and the totals are therefore a floor.
 *
 * ## Nulls
 *
 * `gross_fuel_margin: null` means no commission has been entered for that fuel -- rendered
 * as a prompt, never as ₹0 (§13.7, §13.21). One such fuel withholds the *combined* total
 * too, because a partial profit presented as a total is exactly the plausible-but-wrong
 * number this project exists to prevent. `share_pct: null` means unknowable, and draws no
 * slice rather than a zero-width one.
 */

import { el, empty, pill, render } from "../dom.js";
import { api } from "../api.js";
import { format, quantity as formatQuantity } from "../money.js";
import { businessDate, todayAtOutlet } from "../time.js";
import { donut, salesBars, shareBars } from "../ui/chart.js";
import { field, Form } from "../ui/field.js";
import { openSheet } from "../ui/sheet.js";
import { notify } from "../ui/toast.js";
import { errorCard } from "./today.js";
import { SOURCE_PILL } from "./reports.js";

/* Colour is assigned by position in a stable list, not by hashing the code and not by size.
 *
 * Stability is the whole point: petrol must be the same colour in the donut, in the legend
 * and in the payment mix, and it must still be that colour tomorrow. A palette keyed on rank
 * would repaint the chart every time diesel overtook petrol, and the eye trusts colour more
 * than it trusts a label. Six is the palette's width; a seventh category wraps, which is
 * acceptable because it only happens with categories, never with this outlet's four fuels. */
const CATEGORY_COLOURS = 6;

function colourOf(index) {
  return (index % CATEGORY_COLOURS) + 1;
}

const MIX_LABEL = {
  cash: "Cash",
  card: "Card",
  upi: "UPI",
  wallet: "Wallet",
  credit: "Udhaar",
};

export async function renderSummary(container, { session, navigate }, query = {}) {
  const { shell } = session;
  shell.setTab("summary");
  shell.setTitle("Summary");
  shell.setActions(
    el("button", {
      className: "btn",
      text: "Change range",
      attrs: { type: "button" },
      on: { click: () => openRangeSheet({ navigate, query }) },
    }),
  );

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  let report;
  try {
    // `from`/`to` omitted unless the caller supplied them, so the server's default applies:
    // §13.30's window, anchored on the outlet's most recent *trading* day rather than on
    // today. Recomputing that here would put a second copy of a timezone rule in the client.
    report = await api.get("/reports/summary", { from: query.from, to: query.to });
  } catch (error) {
    render(
      container,
      errorCard(error, () => renderSummary(container, { session, navigate }, query)),
    );
    return;
  }

  shell.setTitle("Summary", `${businessDate(report.from)} – ${businessDate(report.to)}`);

  render(
    container,
    el("div", { className: "grid" }, [
      headlineCard(report),
      trendCard(report, navigate),
      fuelCard(report),
      quantityCard(report),
      paymentMixCard(report),
      expensesCard(report),
      creditCard(report),
      provenanceCard(report),
    ]),
  );
}

/* --- the headline ------------------------------------------------------------- */

function stat(label, value, { className = "" } = {}) {
  return el("div", { className: "stat" }, [
    el("div", { className: "t-micro", text: label }),
    el("div", { className: `stat-value ${className}`, text: value }),
  ]);
}

function headlineCard(report) {
  // §13.7 and §13.21: a null margin total is "not knowable", never zero, and the reason it
  // is null gets named right underneath rather than left as a dash.
  const missing = report.fuels_missing_margin;

  return el("div", { className: "card stack grid-wide" }, [
    el("div", { className: "stat-grid" }, [
      stat("Total sales", format(report.total_sales)),
      stat("Fuel sales", format(report.fuel_sales_total, { absent: "—" })),
      stat("Gross margin", format(report.gross_fuel_margin_total, { absent: "not knowable" })),
      stat("Expenses", format(report.expenses_total)),
    ]),
    missing.length
      ? el("p", {
          className: "t-caption",
          text:
            `Gross margin is withheld because no dealer commission has been entered for ` +
            `${missing.join(", ")}. A partial total presented as a total would be worse ` +
            `than none. Enter the margin under Admin → Margins.`,
        })
      : el("p", {
          className: "t-caption",
          // §13.7 requires this label wherever the figure is shown.
          text:
            "Gross margin is quantity sold × dealer commission. It is not business " +
            "profit: it excludes stock revaluation, non-fuel income and the IOCL ledger.",
        }),
    report.partial
      ? el("p", {
          className: "t-caption text-short",
          text:
            "At least one day in this window could not be fully computed — a missing " +
            "reading or price. These totals are a floor, not a complete figure.",
        })
      : null,
  ]);
}

/* --- the trend ---------------------------------------------------------------- */

function trendCard(report, navigate) {
  return el("div", { className: "card stack grid-wide" }, [
    el("div", { className: "t-micro", text: "Sales by day" }),
    salesBars(report.trend, {
      onSelect: (day) => navigate(`#/days/${day.business_date}`),
    }) ?? empty("No days in this window."),
    el("div", {
      className: "t-caption",
      text: "Bar height is relative to the tallest day in the window. Tap a day to open it.",
    }),
  ]);
}

/* --- fuel --------------------------------------------------------------------- */

function fuelCard(report) {
  const sold = report.fuel.filter((line) => line.sale_value !== null);

  if (!sold.length) {
    return el("div", { className: "card stack" }, [
      el("div", { className: "t-micro", text: "Fuel" }),
      empty("No fuel moved in this window."),
    ]);
  }

  const slices = sold.map((line, index) => ({
    label: line.display_name,
    share_pct: line.share_pct,
    colorIndex: colourOf(index),
  }));

  return el("div", { className: "card stack" }, [
    el("div", { className: "t-micro", text: "Fuel sales" }),
    donut(slices, {
      centerValue: format(report.fuel_sales_total, { absent: "—" }),
      centerLabel: "fuel",
    }),
    el(
      "div",
      { className: "stack" },
      sold.map((line, index) => fuelRow(line, colourOf(index))),
    ),
  ]);
}

function fuelRow(line, colourIndex) {
  return el("div", { className: "col", style: { gap: "2px" } }, [
    el("div", { className: "row-between", style: { gap: "0.75rem", alignItems: "baseline" } }, [
      el("span", { className: "row", style: { gap: "0.5rem", alignItems: "center", minWidth: 0 } }, [
        el("span", { className: `legend-swatch cat-${colourIndex}` }),
        el("span", { className: "t-body truncate", text: line.display_name }),
      ]),
      el("span", { className: "t-body t-numeric", text: format(line.sale_value) }),
    ]),
    el("div", { className: "row-between", style: { gap: "0.75rem" } }, [
      el("span", {
        className: "t-caption",
        // §4.5: the unit is read from the fuel, never assumed to be litres.
        text: formatQuantity(line.quantity, line.unit_of_measure),
      }),
      el("span", {
        className: "t-caption t-numeric",
        text:
          line.gross_fuel_margin === null
            ? "margin not entered"
            : `${format(line.gross_fuel_margin)} margin`,
      }),
    ]),
  ]);
}

/* --- quantity ----------------------------------------------------------------- */

function quantityCard(report) {
  const units = Object.entries(report.quantity_by_unit);
  if (!units.length) return null;

  return el("div", { className: "card stack" }, [
    el("div", { className: "t-micro", text: "Quantity sold" }),
    el(
      "div",
      { className: "stat-grid" },
      units.map(([unit, value]) =>
        stat(unit === "kilogram" ? "Kilograms" : "Litres", formatQuantity(value, unit)),
      ),
    ),
    el("p", {
      className: "t-caption",
      // §4.5 and §14: the two are never added, and saying why stops somebody "fixing" it.
      text:
        "Litres and kilograms are reported separately and never added — they are different " +
        "measures. Volume is the trend to watch when a rate revision moves the rupee figure.",
    }),
  ]);
}

/* --- payment mix -------------------------------------------------------------- */

function paymentMixCard(report) {
  const rows = report.payment_mix.map((row, index) => ({
    label: MIX_LABEL[row.code] ?? row.code,
    share_pct: row.share_pct,
    colorIndex: colourOf(index),
    value: format(row.amount),
  }));

  return el("div", { className: "card stack" }, [
    el("div", { className: "t-micro", text: "How the money arrived" }),
    shareBars(rows) ?? empty("Nothing was collected in this window."),
    el("p", {
      className: "t-caption",
      // §5.2's rule, worth restating where somebody might otherwise read "Cash" as a count.
      text:
        "Cash is derived — total sales less card, UPI, wallet and udhaar — not the figure " +
        "a salesman declared. The two are compared per shift on the Cash tab.",
    }),
  ]);
}

/* --- expenses ----------------------------------------------------------------- */

function expensesCard(report) {
  const rows = report.expenses_by_category;
  if (!rows.length) {
    return el("div", { className: "card stack" }, [
      el("div", { className: "t-micro", text: "Expenses" }),
      empty("No expenses in this window."),
    ]);
  }

  return el("div", { className: "card stack" }, [
    el("div", { className: "t-micro", text: "Expenses by category" }),
    shareBars(
      rows.map((row, index) => ({
        label: row.code,
        share_pct: row.share_pct,
        colorIndex: colourOf(index),
        value: format(row.amount),
      })),
    ),
    el("div", { className: "row-between", style: { gap: "0.75rem", alignItems: "baseline" } }, [
      el("span", { className: "t-caption", text: "Total" }),
      el("span", { className: "t-body t-numeric", text: format(report.expenses_total) }),
    ]),
  ]);
}

/* --- credit ------------------------------------------------------------------- */

function creditCard(report) {
  return el("div", { className: "card stack" }, [
    el("div", { className: "t-micro", text: "Udhaar in this window" }),
    el("div", { className: "stat-grid" }, [
      stat("Issued", format(report.credit_sales_total)),
      stat("Repaid in cash", format(report.cash_credit_repayments)),
      stat("Repaid on machine", format(report.card_upi_credit_repayments)),
    ]),
    el("p", {
      className: "t-caption",
      // The figures are windowed; the balance is not. Saying so prevents the obvious misread.
      text:
        "These are movements in this window, not what customers currently owe. " +
        "Outstanding balances live on the Credit tab.",
    }),
  ]);
}

/* --- provenance --------------------------------------------------------------- */

function provenanceCard(report) {
  const counts = report.days_by_source;
  const parts = [];
  if (counts.snapshot) parts.push(`${counts.snapshot} reconciled`);
  if (counts.computed) parts.push(`${counts.computed} not yet reconciled`);
  if (counts.no_trading) parts.push(`${counts.no_trading} with no trading`);
  if (counts.unavailable) parts.push(`${counts.unavailable} that could not be computed`);

  return el("div", { className: "card stack grid-wide" }, [
    el("div", { className: "t-micro", text: "What this is made of" }),
    el("div", { className: "row", style: { gap: "0.375rem", flexWrap: "wrap" } }, [
      counts.snapshot ? pill(`${counts.snapshot} reconciled`, SOURCE_PILL.snapshot) : null,
      counts.computed ? pill(`${counts.computed} computed`, SOURCE_PILL.computed) : null,
      counts.no_trading ? pill(`${counts.no_trading} no trading`, SOURCE_PILL.no_trading) : null,
      counts.unavailable
        ? pill(`${counts.unavailable} unavailable`, SOURCE_PILL.unavailable)
        : null,
    ]),
    el("p", {
      className: "t-caption",
      text: `${report.trading_days} trading day${report.trading_days === 1 ? "" : "s"}: ${parts.join(", ")}.`,
    }),
    el("p", { className: "t-caption", text: report.window_basis }),
  ]);
}

/* --- the date range sheet ------------------------------------------------------
 *
 * The window lives in the URL (`#/summary?from=…&to=…`) rather than in a module variable, so
 * it survives a reload, a back button and a shared link. `router.js` already parses the query
 * string, so this costs nothing but the decision to use it.
 */

function shiftMonths(isoDate, months) {
  // Date parts, never a `Date` object -- time.js explains at length why parsing a business
  // date as an instant moves it a day west of UTC. Months are calendar arithmetic on
  // integers, not money, so there is nothing here §14 objects to.
  const [year, month, day] = isoDate.split("-").map(Number);
  const total = year * 12 + (month - 1) + months;
  const newYear = Math.floor(total / 12);
  const newMonth = (total % 12) + 1;
  // Clamp the day: 31 January minus one month is 28/29 February, not 31 February.
  const lastDay = new Date(Date.UTC(newYear, newMonth, 0)).getUTCDate();
  const safeDay = Math.min(day, lastDay);
  return `${String(newYear).padStart(4, "0")}-${String(newMonth).padStart(2, "0")}-${String(safeDay).padStart(2, "0")}`;
}

function monthStart(isoDate) {
  return `${isoDate.slice(0, 8)}01`;
}

function openRangeSheet({ navigate, query }) {
  const today = todayAtOutlet();
  const from = field({
    name: "from",
    label: "From",
    type: "date",
    value: query.from ?? monthStart(today),
    required: true,
  });
  const to = field({
    name: "to",
    label: "To",
    type: "date",
    value: query.to ?? today,
    required: true,
    hint: "Up to 366 days.",
  });
  const form = new Form({ from, to });

  const setRange = (start, end) => {
    from._input.value = start;
    to._input.value = end;
  };

  // The four windows somebody actually asks for. Each is one tap instead of two date
  // pickers, which on a phone is the difference between using this screen and not.
  const presets = el("div", { className: "row", style: { gap: "0.5rem", flexWrap: "wrap" } }, [
    presetButton("This month", () => setRange(monthStart(today), today)),
    presetButton("Last month", () => {
      const start = monthStart(shiftMonths(today, -1));
      // The last day of the previous month is the day before this month starts.
      const end = shiftMonths(monthStart(today), 0);
      const [y, m] = end.split("-").map(Number);
      const lastDay = new Date(Date.UTC(y, m - 1, 0)).getUTCDate();
      setRange(start, `${end.slice(0, 8)}${String(lastDay).padStart(2, "0")}`);
    }),
    presetButton("Last 3 months", () => setRange(monthStart(shiftMonths(today, -2)), today)),
    presetButton("This year", () => setRange(`${today.slice(0, 4)}-01-01`, today)),
  ]);

  const apply = el("button", {
    className: "btn btn-primary btn-block",
    text: "Show summary",
    attrs: { type: "button" },
  });

  const sheet = openSheet({
    title: "Date range",
    body: el("div", { className: "stack" }, [presets, ...form.nodes()]),
    footer: apply,
  });

  apply.addEventListener("click", () => {
    const values = form.values();
    if (!values.from || !values.to) {
      notify.warning("Pick both a start and an end date.");
      return;
    }
    // A string comparison, which is exactly right for ISO dates and avoids constructing a
    // `Date`. The server refuses this too -- this is the courtesy, not the control (§8).
    if (values.from > values.to) {
      notify.warning("The start date must not be after the end date.");
      return;
    }
    sheet.close();
    navigate(`#/summary?from=${values.from}&to=${values.to}`);
  });
}

function presetButton(label, onClick) {
  return el("button", {
    className: "btn",
    text: label,
    attrs: { type: "button" },
    on: { click: onClick },
  });
}
