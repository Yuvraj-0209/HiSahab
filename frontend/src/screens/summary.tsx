/* The Summary tab: one window, the whole business, charted (Phase 19). Rebuilt in Phase 23
 * from summary.js.
 *
 * Every other screen answers a question about one business date or one shift. This one answers
 * questions about a PERIOD -- did petrol outsell diesel this quarter, is udhaar growing, are the
 * bills creeping up -- which are the questions the owner makes decisions on.
 *
 * ## One request, and why that is a rule rather than a convenience
 *
 * Every cross-panel figure here is a share: this fuel's part of sales, this category's part of
 * expenses. A share is `value / total`, arithmetic on money, forbidden in this client (§14). So
 * `GET /reports/summary` computes every one in Decimal and sends percentage strings this file
 * only assigns. Four endpoints stitched together here would be four passes free to disagree,
 * surfacing as a donut that does not close.
 *
 * ## The distinction this screen must not soften
 *
 * §13.35: a window mixing reconciled and unreconciled days is part record and part live
 * estimate, and one total cannot say which. `days_by_source` can, so it is rendered in words.
 * `partial` means at least one day could not be computed, so the totals are a floor.
 *
 * ## Nulls
 *
 * `gross_fuel_margin: null` means no commission was entered for that fuel -- a prompt, never ₹0
 * (§13.7, §13.21). One such fuel withholds the combined total too. `share_pct: null` means
 * unknowable and draws no slice rather than a zero-width one.
 */

import { type ReactNode, useState } from "react";
import { useSearchParams } from "react-router";
import { CalendarDotsIcon } from "@phosphor-icons/react";
import { useApiQuery } from "../api/queries";
import type { Schemas } from "../api/types";
import { ScreenActions, ScreenTitle } from "../app/chrome";
import { useGo } from "../app/navigation";
import { lastMonth, lastThreeMonths, type Range, thisMonth, thisYear } from "../lib/calendar";
import { format, quantity } from "../lib/money";
import { businessDateRange, todayAtOutlet } from "../lib/time";
import { Amount } from "../ui/Amount";
import { categoryColour, Donut, SalesBars, ShareBars, Swatch } from "../ui/chart";
import { TextField, useForm } from "../ui/form";
import { Button, Card, Empty, ErrorCard, ListRow, Pill, SectionLabel, Skeleton } from "../ui/primitives";
import { Sheet } from "../ui/Sheet";
import { notify } from "../ui/toast";
import { SOURCE_PILL } from "./days";

type Report = Schemas["app__api__v1__reports__SummaryResponse"];

const MIX_LABEL: Record<string, string> = { cash: "Cash", card: "Card", upi: "UPI", wallet: "Wallet", credit: "Udhaar" };

export function SummaryScreen() {
  const navigate = useGo();
  const [params] = useSearchParams();
  const from = params.get("from") ?? undefined;
  const to = params.get("to") ?? undefined;
  const [choosing, setChoosing] = useState(false);
  // `from`/`to` are omitted unless the URL carries them, so the server's default applies:
  // §13.30's window, anchored on the most recent TRADING day rather than on today. Recomputing
  // that here would put a second copy of a timezone rule in the client.
  const report = useApiQuery<Report>("/reports/summary", { from, to });

  const subtitle = report.data ? businessDateRange(report.data.from, report.data.to) : undefined;

  return (
    <>
      <ScreenTitle large title="Summary" subtitle={subtitle} />
      <ScreenActions>
        <Button size="sm" icon={<CalendarDotsIcon size={16} aria-hidden />} onClick={() => setChoosing(true)}>
          Change range
        </Button>
      </ScreenActions>

      {report.isPending ? (
        <Skeleton shape="figure" rows={5} />
      ) : report.isError || !report.data ? (
        <ErrorCard error={report.error} onRetry={() => void report.refetch()} />
      ) : (
        <SummaryBody report={report.data} onOpenDay={(date) => navigate(`/days/${date}`)} />
      )}

      <RangeSheet
        open={choosing}
        onClose={() => setChoosing(false)}
        current={report.data ? { from: report.data.from, to: report.data.to } : null}
        onApply={(range) => {
          setChoosing(false);
          navigate(`/summary?from=${range.from}&to=${range.to}`);
        }}
      />
    </>
  );
}

function SummaryBody({ report, onOpenDay }: { report: Report; onOpenDay: (date: string) => void }) {
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Headline report={report} />
      <Card className="lg:col-span-2">
        <SectionLabel>Sales by day</SectionLabel>
        {report.trend.length ? (
          <SalesBars days={report.trend} onSelect={(day) => onOpenDay(day.business_date)} />
        ) : (
          <Empty>No days in this window.</Empty>
        )}
        <p className="mt-2 text-[0.8125rem] text-ink-muted">Bar height is relative to the tallest day in the window.</p>
      </Card>
      <FuelCard report={report} />
      <QuantityCard report={report} />
      <PaymentMixCard report={report} />
      <ExpensesCard report={report} />
      <CreditCard report={report} />
      <Provenance report={report} />
    </div>
  );
}

function Stat({ label, children, lead = false }: { label: string; children: ReactNode; lead?: boolean }) {
  return (
    <div className="min-w-0">
      <p className="text-[0.75rem] font-medium text-ink-muted">{label}</p>
      <p className={`tabular truncate font-semibold tracking-[-0.02em] text-ink ${lead ? "text-[1.75rem] leading-tight" : "text-[1.125rem]"}`}>{children}</p>
    </div>
  );
}

function Headline({ report }: { report: Report }) {
  const missing = report.fuels_missing_margin;
  return (
    <Card className="lg:col-span-2">
      <div className="grid grid-cols-2 gap-x-4 gap-y-4 sm:grid-cols-4">
        <div className="col-span-2 sm:col-span-1">
          <Stat label="Total sales" lead>
            <Amount value={report.total_sales} />
          </Stat>
        </div>
        <Stat label="Fuel sales">
          <Amount value={report.fuel_sales_total} absent="not known" />
        </Stat>
        <Stat label="Gross margin">
          <Amount value={report.gross_fuel_margin_total} absent="not knowable" />
        </Stat>
        <Stat label="Expenses">
          <Amount value={report.expenses_total} />
        </Stat>
      </div>
      <p className="mt-4 text-[0.8125rem] text-ink-muted">
        {missing.length
          ? `Gross margin is withheld because no dealer commission has been entered for ${missing.join(", ")}. A partial total presented as a total would be worse than none. Enter it under Admin, Margins.`
          : // §13.7 requires this label wherever the figure is shown.
            "Gross margin is quantity sold × dealer commission. It is not business profit: it excludes stock revaluation, non-fuel income and the IOCL ledger."}
      </p>
      {report.partial ? (
        <p className="mt-3 rounded-[var(--radius-control)] bg-warning-tint px-3.5 py-2.5 text-[0.8125rem] text-warning">
          At least one day in this window could not be fully calculated, from a missing reading or price. These totals are a floor, not a complete figure.
        </p>
      ) : null}
    </Card>
  );
}

function FuelCard({ report }: { report: Report }) {
  const sold = report.fuel.filter((line) => line.sale_value !== null);
  return (
    <Card>
      <SectionLabel>Fuel sales</SectionLabel>
      {sold.length ? (
        <>
          <Donut slices={sold.map((line, index) => ({ key: line.fuel_type_id, share_pct: line.share_pct, colour: categoryColour(index) }))}>
            <span className="tabular text-[0.9375rem] font-semibold tracking-[-0.02em] text-ink">{format(report.fuel_sales_total, { absent: "not known" })}</span>
            <span className="text-[0.75rem] text-ink-muted">fuel</span>
          </Donut>
          <div className="mt-2 flex flex-col divide-y divide-hairline">
            {sold.map((line, index) => (
              <div key={line.fuel_type_id} className="py-2.5">
                <div className="flex items-baseline justify-between gap-3">
                  <span className="flex min-w-0 items-center gap-2">
                    <Swatch colour={categoryColour(index)} />
                    <span className="truncate text-[0.9375rem] text-ink">{line.display_name}</span>
                    {line.share_pct ? <span className="tabular text-[0.75rem] text-ink-muted">{line.share_pct}</span> : null}
                  </span>
                  <span className="tabular shrink-0 text-[0.9375rem] text-ink">{format(line.sale_value)}</span>
                </div>
                <div className="mt-0.5 flex justify-between gap-3 text-[0.8125rem]">
                  {/* §4.5: the unit is read from the fuel, never assumed to be litres. */}
                  <span className="tabular text-ink-muted">{quantity(line.quantity, line.unit_of_measure)}</span>
                  {line.gross_fuel_margin === null ? (
                    <span className="t-absent">margin not entered</span>
                  ) : (
                    <span className="tabular text-ink-muted">{format(line.gross_fuel_margin)} margin</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </>
      ) : (
        <Empty>No fuel moved in this window.</Empty>
      )}
    </Card>
  );
}

function QuantityCard({ report }: { report: Report }) {
  const units = Object.entries(report.quantity_by_unit);
  if (!units.length) return null;
  return (
    <Card>
      <SectionLabel>Quantity sold</SectionLabel>
      <div className="grid grid-cols-2 gap-4">
        {units.map(([unit, value]) => (
          <Stat key={unit} label={unit === "kilogram" ? "Kilograms" : "Litres"}>
            {quantity(value, unit)}
          </Stat>
        ))}
      </div>
      {/* §4.5 and §14: never added together, and saying why stops somebody "fixing" it. */}
      <p className="mt-3 text-[0.8125rem] text-ink-muted">
        Litres and kilograms are reported separately and never added: they are different measures. Volume is the trend to watch when a rate revision moves the rupee figure.
      </p>
    </Card>
  );
}

function PaymentMixCard({ report }: { report: Report }) {
  const rows = report.payment_mix.map((row, index) => ({
    key: row.code,
    label: MIX_LABEL[row.code] ?? row.code,
    value: format(row.amount),
    share_pct: row.share_pct,
    colour: categoryColour(index),
  }));
  return (
    <Card>
      <SectionLabel>How the money arrived</SectionLabel>
      {rows.length ? <ShareBars rows={rows} /> : <Empty>Nothing was collected in this window.</Empty>}
      {/* §5.2's rule, where somebody might otherwise read "Cash" as a count. */}
      <p className="mt-3 text-[0.8125rem] text-ink-muted">
        Cash is derived: total sales less card, UPI, wallet and udhaar. It is not the figure a salesman declared; the two are compared per shift on the Cash tab.
      </p>
    </Card>
  );
}

function ExpensesCard({ report }: { report: Report }) {
  const rows = report.expenses_by_category.map((row, index) => ({
    key: row.code,
    label: row.code,
    value: format(row.amount),
    share_pct: row.share_pct,
    colour: categoryColour(index),
  }));
  return (
    <Card>
      <SectionLabel>Expenses by category</SectionLabel>
      {rows.length ? (
        <>
          <ShareBars rows={rows} />
          <div className="mt-3 flex items-baseline justify-between border-t border-hairline pt-3">
            <span className="text-[0.875rem] text-ink-muted">Total</span>
            <span className="tabular text-[0.9375rem] font-semibold text-ink">{format(report.expenses_total)}</span>
          </div>
        </>
      ) : (
        <Empty>No expenses in this window.</Empty>
      )}
    </Card>
  );
}

function CreditCard({ report }: { report: Report }) {
  return (
    <Card>
      <SectionLabel>Udhaar in this window</SectionLabel>
      <ListRow label="Issued" value={<Amount value={report.credit_sales_total} />} strong />
      <ListRow label="Repaid in cash" value={<Amount value={report.cash_credit_repayments} />} />
      <ListRow label="Repaid on the card machine or UPI" value={<Amount value={report.card_upi_credit_repayments} />} />
      {/* The figures are windowed; a balance is not. Saying so prevents the obvious misread. */}
      <p className="mt-3 text-[0.8125rem] text-ink-muted">These are movements in this window, not what customers owe now. Outstanding balances are on the Credit tab.</p>
    </Card>
  );
}

function Provenance({ report }: { report: Report }) {
  const counts = report.days_by_source;
  // A count that is absent is simply not shown; nothing defaults it to a number.
  const n = (key: string) => counts[key];
  const parts = [
    n("snapshot") ? `${n("snapshot")} reconciled` : null,
    n("computed") ? `${n("computed")} not yet reconciled` : null,
    n("no_trading") ? `${n("no_trading")} with no trading` : null,
    n("unavailable") ? `${n("unavailable")} that could not be calculated` : null,
  ].filter((part): part is string => part !== null);

  return (
    <Card className="lg:col-span-2">
      <SectionLabel>What these figures are made of</SectionLabel>
      <div className="flex flex-wrap gap-1.5">
        {n("snapshot") ? <Pill kind={SOURCE_PILL.snapshot}>{n("snapshot")} reconciled</Pill> : null}
        {n("computed") ? <Pill kind={SOURCE_PILL.computed}>{n("computed")} calculated live</Pill> : null}
        {n("no_trading") ? <Pill kind={SOURCE_PILL.no_trading}>{n("no_trading")} no trading</Pill> : null}
        {n("unavailable") ? <Pill kind={SOURCE_PILL.unavailable}>{n("unavailable")} unavailable</Pill> : null}
      </div>
      <p className="mt-2.5 text-[0.875rem] text-ink">
        {report.trading_days} trading day{report.trading_days === 1 ? "" : "s"}
        {parts.length ? `: ${parts.join(", ")}.` : "."}
      </p>
      <p className="mt-1 text-[0.8125rem] text-ink-muted">{report.window_basis}</p>
    </Card>
  );
}

/* --- the date range ------------------------------------------------------------------------
 *
 * The window lives in the URL (`#/summary?from=…&to=…`), so it survives a reload, the back
 * button and a shared link. */

const PRESETS: { label: string; range: (today: string) => Range }[] = [
  { label: "This month", range: thisMonth },
  { label: "Last month", range: lastMonth },
  { label: "Last 3 months", range: lastThreeMonths },
  { label: "This year", range: thisYear },
];

function RangeSheet({ open, onClose, current, onApply }: { open: boolean; onClose: () => void; current: Range | null; onApply: (range: Range) => void }) {
  return (
    <Sheet open={open} onClose={onClose} title="Date range" subtitle="Up to 366 days">
      {/* Remounted per opening, so it starts from the window on screen. */}
      {open ? <RangeForm current={current} onApply={onApply} /> : null}
    </Sheet>
  );
}

function RangeForm({ current, onApply }: { current: Range | null; onApply: (range: Range) => void }) {
  const today = todayAtOutlet();
  const form = useForm({ from: current?.from ?? thisMonth(today).from, to: current?.to ?? today });

  function apply() {
    const { from, to } = form.values;
    if (!from || !to) return void notify.warning("Pick both a start and an end date.");
    // ISO dates compare correctly as strings. The server refuses this too; this is the courtesy,
    // not the control (§8).
    if (from > to) return void notify.warning("The start date must not be after the end date.");
    onApply({ from, to });
  }

  return (
    <div className="flex flex-col gap-4">
      {/* The four windows somebody actually asks for: one tap instead of two date pickers. */}
      <div className="grid grid-cols-2 gap-2">
        {PRESETS.map((preset) => {
          const range = preset.range(today);
          const active = form.values.from === range.from && form.values.to === range.to;
          return (
            <button
              key={preset.label}
              type="button"
              aria-pressed={active}
              onClick={() => {
                form.set("from", range.from);
                form.set("to", range.to);
              }}
              className="pressable h-11 rounded-[var(--radius-control)] border border-hairline bg-surface text-[0.875rem] font-medium text-ink aria-pressed:border-accent aria-pressed:bg-accent-tint aria-pressed:text-accent"
            >
              {preset.label}
            </button>
          );
        })}
      </div>
      <div className="grid grid-cols-2 gap-3">
        <TextField form={form} name="from" label="From" type="date" max={today} required />
        <TextField form={form} name="to" label="To" type="date" max={today} required />
      </div>
      <Button variant="primary" block onClick={apply}>
        Show summary
      </Button>
    </div>
  );
}
