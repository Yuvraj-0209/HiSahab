/* The Summary tab: one window, the whole business, charted (Phase 19). Rebuilt in Phase 23
 * from summary.js, and rebuilt for reading in Phase 26.
 *
 * Every other screen answers a question about one business date or one shift. This one answers
 * questions about a PERIOD -- did petrol outsell diesel this quarter, is udhaar growing, where did
 * the bills go -- which are the questions the owner makes decisions on.
 *
 * ## Phase 26: each box answers one question, and the next one by itself
 *
 * The owner's first look found boxes that made him work: a whole card for two quantities, a
 * money-arrived split he did not use, expense bars that ended in a number, and an udhaar card of
 * three loose figures that left out every bank-transfer repayment. Now:
 *
 *   headline  every window total, quantities included (never summed across units, §4.5)
 *   fuel      a larger donut beside each fuel's figures; pointing at a fuel lifts its slice
 *   expenses  choose a category and its rows are listed by date, like a customer's ledger
 *   udhaar    a bridge -- owed at start, + given, − collected, = owed at end -- read from the
 *             billing statement for the same dates (§6.6), and who owes the most
 *
 * ## One request for the page, and why that is a rule rather than a convenience
 *
 * Every cross-panel figure here is a share: this fuel's part of sales, this category's part of
 * expenses. A share is `value / total`, arithmetic on money, forbidden in this client (§14). So
 * `GET /reports/summary` computes every one in Decimal and sends percentage strings this file
 * only assigns. The expense drill-down is a second request, but it adds nothing to the page's
 * figures: it lists the rows behind one bar, with every subtotal computed on the server, and
 * its total is the bar's because one query feeds both (§13.44).
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
 * unknowable and draws no slice rather than a zero-width one. A bridge with no geometry (a
 * negative balance, §6.6) still prints every figure.
 */

import { type ReactNode, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { ArrowRightIcon, CalendarDotsIcon, CaretRightIcon } from "@phosphor-icons/react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { apiKey, useApiQuery } from "../api/queries";
import type { Schemas } from "../api/types";
import { ScreenActions, ScreenTitle } from "../app/chrome";
import { useGo } from "../app/navigation";
import { lastMonth, lastThreeMonths, type Range, thisMonth, thisYear } from "../lib/calendar";
import { format, quantity } from "../lib/money";
import { businessDate, businessDateRange, businessDateWeekday, todayAtOutlet } from "../lib/time";
import { DURATION, EASE, gsap, useMotion } from "../motion/gsap";
import { Amount } from "../ui/Amount";
import { Bridge, CategoryBars, categoryColour, Donut, SalesBars, Swatch } from "../ui/chart";
import { TextField, useForm } from "../ui/form";
import { Button, Card, Empty, ErrorCard, Pill, SectionLabel, Skeleton, TruncationNotice } from "../ui/primitives";
import { Sheet } from "../ui/Sheet";
import { notify } from "../ui/toast";
import { SOURCE_PILL } from "./days";

type Report = Schemas["app__api__v1__reports__SummaryResponse"];
type FuelLine = Schemas["SummaryFuelLineResponse"];
type Drill = Schemas["ExpenseDrillResponse"];

const DRILL_PATH = "/reports/summary/expenses";

const MODE_LABEL: Record<string, string> = {
  cash: "Cash",
  card: "Card",
  upi: "UPI",
  bank_transfer: "Bank transfer",
};

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
        <SummaryBody report={report.data} />
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

function SummaryBody({ report }: { report: Report }) {
  const navigate = useGo();
  return (
    <div className="flex flex-col gap-4">
      <Headline report={report} />
      <Card>
        <SectionLabel>Sales by day</SectionLabel>
        {report.trend.length ? (
          <SalesBars days={report.trend} onSelect={(day) => navigate(`/days/${day.business_date}`)} />
        ) : (
          <Empty>No days in this window.</Empty>
        )}
        <p className="mt-2 text-[0.8125rem] text-ink-muted">Bar height is relative to the tallest day in the window.</p>
      </Card>
      <FuelCard report={report} />
      <ExpensesCard report={report} />
      <CreditCard report={report} />
      <Provenance report={report} />
    </div>
  );
}

/* --- the headline --------------------------------------------------------------------------- */

function Stat({ label, children, note }: { label: string; children: ReactNode; note?: ReactNode }) {
  return (
    <div className="min-w-0">
      <p className="text-[0.8125rem] font-medium text-ink-muted">{label}</p>
      <p className="tabular mt-0.5 truncate text-[1.25rem] font-semibold tracking-[-0.02em] text-ink sm:text-[1.375rem]">{children}</p>
      {note ? <p className="mt-0.5 text-[0.75rem] text-ink-faint">{note}</p> : null}
    </div>
  );
}

const UNIT_LABEL: Record<string, string> = {
  litre: "Litres sold",
  kilogram: "Kilograms sold",
};

function Headline({ report }: { report: Report }) {
  const missing = report.fuels_missing_margin;
  // §4.5: one stat per unit, never one summed figure. Litres first: the order is fixed, so a
  // window that sold no gas does not shuffle the grid.
  const units = (["litre", "kilogram"] as const).filter((unit) => report.quantity_by_unit[unit] !== undefined);
  return (
    <Card>
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)] lg:gap-10">
        <div className="flex flex-col justify-center">
          <p className="text-[0.8125rem] font-medium text-ink-muted">Total sales</p>
          <p className="tabular mt-1 text-[2.25rem] leading-none font-semibold tracking-[-0.03em] text-ink sm:text-[2.75rem]">
            <Amount value={report.total_sales} />
          </p>
          <p className="mt-2 text-[0.8125rem] text-ink-muted">
            Fuel and non-fuel, across {report.trading_days} trading day
            {report.trading_days === 1 ? "" : "s"}.
          </p>
        </div>
        <div className="grid grid-cols-2 gap-x-6 gap-y-5 sm:grid-cols-3 lg:border-l lg:border-hairline lg:pl-10">
          <Stat label="Fuel sales">
            <Amount value={report.fuel_sales_total} absent="not known" />
          </Stat>
          <Stat label="Non-fuel sales" note="Oil, coolant and the like">
            <Amount value={report.non_fuel_sales_total} />
          </Stat>
          <Stat label="Gross margin">
            <Amount value={report.gross_fuel_margin_total} absent="not knowable" />
          </Stat>
          <Stat label="Expenses">
            <Amount value={report.expenses_total} />
          </Stat>
          {units.map((unit) => (
            <Stat key={unit} label={UNIT_LABEL[unit] ?? unit}>
              {quantity(report.quantity_by_unit[unit], unit)}
            </Stat>
          ))}
        </div>
      </div>
      <p className="mt-5 border-t border-hairline pt-4 text-[0.8125rem] text-ink-muted">
        {missing.length
          ? `Gross margin is withheld because no dealer commission has been entered for ${missing.join(", ")}. A partial total presented as a total would be worse than none. Enter it under Admin, Margins.`
          : // §13.7 requires this label wherever the figure is shown.
            "Gross margin is quantity sold × dealer commission. It is not business profit: it excludes stock revaluation, non-fuel income and the IOCL ledger."}{" "}
        {/* §4.5 and §14: saying why stops somebody "fixing" it. */}
        Litres and kilograms are different measures, so they are never added together.
      </p>
      {report.partial ? (
        <p className="mt-3 rounded-[var(--radius-control)] bg-warning-tint px-3.5 py-2.5 text-[0.8125rem] text-warning">
          At least one day in this window could not be fully calculated, from a missing reading or price. These totals are a floor, not a complete
          figure.
        </p>
      ) : null}
    </Card>
  );
}

/* --- fuel --------------------------------------------------------------------------------- */

function FuelCard({ report }: { report: Report }) {
  const sold = report.fuel.filter((line) => line.sale_value !== null);
  // Pointing previews a fuel; a tap or click pins it, so a phone (no hover) can do the same.
  const [pointed, setPointed] = useState<string | null>(null);
  const [pinned, setPinned] = useState<string | null>(null);
  const active = pointed ?? pinned;
  const focus = sold.find((line) => line.fuel_type_id === active) ?? null;

  return (
    <Card>
      <SectionLabel>Fuel sales</SectionLabel>
      {sold.length ? (
        <div className="grid items-center gap-6 md:grid-cols-[auto_minmax(0,1fr)] md:gap-10">
          <Donut
            size="lg"
            active={active}
            slices={sold.map((line, index) => ({
              key: line.fuel_type_id,
              share_pct: line.share_pct,
              colour: categoryColour(index),
            }))}
          >
            {/* The centre swaps between the server's strings; nothing is counted or tweened. */}
            {focus ? (
              <>
                <span className="text-[0.8125rem] font-medium text-ink-muted">{focus.display_name}</span>
                <span className="tabular text-[1.125rem] font-semibold tracking-[-0.02em] text-ink sm:text-[1.25rem]">
                  {format(focus.sale_value)}
                </span>
                {focus.share_pct ? <span className="tabular text-[0.8125rem] text-ink-muted">{focus.share_pct} of fuel</span> : null}
              </>
            ) : (
              <>
                <span className="tabular text-[1.125rem] font-semibold tracking-[-0.02em] text-ink sm:text-[1.25rem]">
                  {format(report.fuel_sales_total, { absent: "not known" })}
                </span>
                <span className="text-[0.8125rem] text-ink-muted">all fuel</span>
              </>
            )}
          </Donut>
          <div className="flex flex-col gap-1" onPointerLeave={() => setPointed(null)}>
            {sold.map((line, index) => (
              <FuelRow
                key={line.fuel_type_id}
                line={line}
                colour={categoryColour(index)}
                pinned={pinned === line.fuel_type_id}
                receded={active !== null && active !== line.fuel_type_id}
                onPoint={() => setPointed(line.fuel_type_id)}
                onPin={() => setPinned((current) => (current === line.fuel_type_id ? null : line.fuel_type_id))}
              />
            ))}
          </div>
        </div>
      ) : (
        <Empty>No fuel moved in this window.</Empty>
      )}
    </Card>
  );
}

function FuelRow({
  line,
  colour,
  pinned,
  receded,
  onPoint,
  onPin,
}: {
  line: FuelLine;
  colour: number;
  pinned: boolean;
  receded: boolean;
  onPoint: () => void;
  onPin: () => void;
}) {
  const unit = line.unit_of_measure === "kilogram" ? "kg" : "L";
  return (
    <button
      type="button"
      aria-pressed={pinned}
      onClick={onPin}
      onPointerEnter={onPoint}
      onFocus={onPoint}
      className={`rounded-[var(--radius-control)] px-3.5 py-3 text-left transition-[background-color,opacity] duration-200 ${
        pinned ? "bg-accent-tint" : "hover:bg-surface-sunken"
      } ${receded ? "opacity-55" : "opacity-100"}`}
    >
      <span className="flex items-baseline justify-between gap-3">
        <span className="flex min-w-0 items-center gap-2.5">
          <Swatch colour={colour} />
          <span className="truncate text-[1.0625rem] font-semibold text-ink">{line.display_name}</span>
          {line.share_pct ? <span className="tabular text-[0.8125rem] text-ink-muted">{line.share_pct}</span> : null}
        </span>
        <span className="tabular shrink-0 text-[1.125rem] font-semibold text-ink">{format(line.sale_value)}</span>
      </span>
      <span className="mt-2 grid max-w-xl grid-cols-2 gap-x-4 gap-y-1 pl-5 sm:grid-cols-3">
        <span className="flex flex-col">
          <span className="text-[0.75rem] text-ink-faint">Sold</span>
          {/* §4.5: the unit is read from the fuel, never assumed to be litres. */}
          <span className="tabular text-[1rem] text-ink">{quantity(line.quantity, line.unit_of_measure)}</span>
        </span>
        <span className="flex flex-col">
          <span className="text-[0.75rem] text-ink-faint">Gross margin</span>
          {line.gross_fuel_margin === null ? (
            <span className="t-absent text-[1rem]">margin not entered</span>
          ) : (
            <span className="tabular text-[1rem] text-ink">{format(line.gross_fuel_margin)}</span>
          )}
        </span>
        {line.margin_per_unit !== null ? (
          <span className="flex flex-col">
            <span className="text-[0.75rem] text-ink-faint">Commission</span>
            <span className="tabular text-[1rem] text-ink">
              {format(line.margin_per_unit)} / {unit}
            </span>
          </span>
        ) : null}
      </span>
    </button>
  );
}

/* --- expenses ------------------------------------------------------------------------------ */

/* How long a pointer must rest on a bar before it is chosen. Long enough that sweeping the
 * pointer across five bars on its way somewhere else does not flick the list five times; short
 * enough that resting on one feels immediate. The fetch starts at once, on arrival. */
const POINT_INTENT_MS = 120;

function ExpensesCard({ report }: { report: Report }) {
  const rows = report.expenses_by_category;
  // The largest category is chosen to begin with, so the list beside the bars is never empty.
  const [selected, setSelected] = useState<string | null>(rows[0]?.code ?? null);
  const timer = useRef<number | null>(null);
  const client = useQueryClient();
  const canHover = typeof matchMedia === "function" && matchMedia("(hover: hover)").matches;
  const span = { from: report.from, to: report.to };

  // A new window may not contain the chosen category; fall back to its largest.
  useEffect(() => {
    if (!rows.some((row) => row.code === selected)) setSelected(rows[0]?.code ?? null);
  }, [rows, selected]);

  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
    },
    [],
  );

  function prefetch(code: string) {
    const query = { ...span, category: code };
    void client.prefetchQuery({
      queryKey: apiKey(DRILL_PATH, query),
      queryFn: () => api.get<Drill>(DRILL_PATH, query),
    });
  }

  function point(code: string) {
    prefetch(code);
    if (!canHover) return;
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = setTimeout(() => setSelected(code), POINT_INTENT_MS) as unknown as number;
  }

  function unpoint() {
    // Leaving the bars keeps the choice, so the pointer can travel into the list and scroll it.
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
  }

  return (
    <Card>
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <SectionLabel className="mb-0!">Expenses by category</SectionLabel>
        <span className="tabular text-[0.9375rem] font-semibold text-ink">{format(report.expenses_total)}</span>
      </div>
      {rows.length && selected ? (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)] lg:gap-6">
          <div>
            <CategoryBars
              label="Expense categories"
              rows={rows.map((row) => ({
                key: row.code,
                label: row.display_name,
                value: format(row.amount),
                share_pct: row.share_pct,
                bar_pct: row.bar_pct,
              }))}
              selected={selected}
              onChoose={(code) => {
                unpoint();
                setSelected(code);
              }}
              onPoint={point}
              onUnpoint={unpoint}
            />
            <p className="mt-2 px-3 text-[0.8125rem] text-ink-muted">
              {canHover ? "Point at a category, or choose it, to see where the money went." : "Tap a category to see where the money went."}
            </p>
          </div>
          <ExpenseLedger from={report.from} to={report.to} code={selected} share={rows.find((row) => row.code === selected)?.share_pct ?? null} />
        </div>
      ) : (
        <Empty>No expenses in this window.</Empty>
      )}
    </Card>
  );
}

function ExpenseLedger({ from, to, code, share }: { from: string; to: string; code: string; share: string | null }) {
  const navigate = useGo();
  // The previous category's list stays up, dimmed, until the chosen one arrives: a blank frame
  // between two lists reads as a flicker. Dimmed and busy, so it is never mistaken for the
  // answer -- and usually never seen at all, because pointing at a bar fetched it already.
  const drill = useApiQuery<Drill>(DRILL_PATH, { from, to, category: code }, { keepPrevious: true });
  const scope = useRef<HTMLDivElement>(null);
  const data = drill.data;
  const stale = drill.isPlaceholderData;

  // A short cross-fade when the category changes, so the eye registers that the list is new
  // rather than reading two categories' rows as one. Opacity and a few pixels: no figure moves.
  useMotion(
    (play) => {
      if (!data) return;
      play(() => {
        gsap.from("[data-ledger-body]", {
          opacity: 0,
          y: 6,
          duration: DURATION.small,
          ease: EASE.enter.gsap,
        });
      });
    },
    { scope, dependencies: [data?.code] },
  );

  return (
    <div
      ref={scope}
      className="flex min-h-64 flex-col rounded-[var(--radius-card)] bg-surface-sunken p-3 sm:p-4"
      aria-live="polite"
      aria-busy={stale || drill.isPending}
    >
      {drill.isPending ? (
        <Skeleton shape="list" rows={4} />
      ) : drill.isError || !data ? (
        <ErrorCard error={drill.error} onRetry={() => void drill.refetch()} />
      ) : (
        // Two elements on purpose: CSS dims the outer one while the next list loads, GSAP fades
        // the inner one in when it lands -- one engine per element (§14).
        <div className={`transition-opacity duration-150 ${stale ? "opacity-40" : ""}`}>
          <div data-ledger-body className="flex flex-col">
            <div className="flex items-start justify-between gap-3 px-1 pb-3">
              <div className="min-w-0">
                <p className="truncate text-[1.0625rem] font-semibold text-ink">{data.display_name}</p>
                <p className="text-[0.8125rem] text-ink-muted">
                  {data.row_count} {data.row_count === 1 ? "entry" : "entries"}
                  {share ? ` · ${share} of all expenses` : ""}
                </p>
              </div>
              <span className="tabular shrink-0 text-[1.25rem] font-semibold tracking-[-0.02em] text-ink">{format(data.total)}</span>
            </div>
            {data.truncated ? (
              <div className="mb-2">
                <TruncationNotice count={data.days.reduce((count, day) => count + day.items.length, 0)} />
              </div>
            ) : null}
            {data.days.length ? (
              <div className="flex max-h-[30rem] flex-col gap-2 overflow-y-auto overscroll-contain">
                {data.days.map((day) => (
                  <section key={day.business_date} className="overflow-hidden rounded-[var(--radius-control)] border border-hairline bg-surface">
                    <button
                      type="button"
                      onClick={() => navigate(`/days/${day.business_date}`)}
                      className="flex w-full items-center justify-between gap-3 border-b border-hairline px-3.5 py-2 text-left hover:bg-surface-raised"
                    >
                      <span className="flex items-center gap-1.5 text-[0.8125rem] font-medium text-ink-muted">
                        {businessDateWeekday(day.business_date)}, {businessDate(day.business_date)}
                        <CaretRightIcon size={12} weight="bold" aria-hidden />
                      </span>
                      <span className="tabular text-[0.875rem] font-semibold text-ink">{format(day.total)}</span>
                    </button>
                    <ul>
                      {day.items.map((item) => (
                        <li
                          key={item.id}
                          className="flex items-baseline justify-between gap-3 border-b border-hairline px-3.5 py-2.5 last:border-b-0"
                        >
                          <div className="min-w-0">
                            <p className={`text-[0.9375rem] ${item.is_reversed ? "text-ink-faint line-through" : "text-ink"}`}>
                              {item.is_reversal ? `Cancelled: ${item.reversal_reason ?? "no reason given"}` : item.description}
                            </p>
                            <p className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[0.75rem] text-ink-muted">
                              <span>{[MODE_LABEL[item.mode] ?? item.mode, item.paid_to].filter(Boolean).join(" · ")}</span>
                              {item.is_reversed ? <Pill kind="neutral">Reversed</Pill> : null}
                              {item.is_reversal ? <Pill kind="neutral">Reversal</Pill> : null}
                            </p>
                          </div>
                          <span
                            className={`tabular shrink-0 text-[0.9375rem] ${item.is_reversed || item.is_reversal ? "text-ink-muted" : "text-ink"}`}
                          >
                            {format(item.amount)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </section>
                ))}
              </div>
            ) : (
              <Empty>Nothing filed under this category in the window.</Empty>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/* --- udhaar ----------------------------------------------------------------------------------- */

function CreditCard({ report }: { report: Report }) {
  const navigate = useGo();
  const credit = report.credit;
  const steps = Object.fromEntries(credit.bridge.map((step) => [step.key, step]));
  const row = (key: string, label: string, emphasis = false) => {
    const step = steps[key];
    return {
      key,
      label,
      value: format(step?.amount),
      offset_pct: step?.offset_pct ?? null,
      width_pct: step?.width_pct ?? null,
      emphasis,
    };
  };

  return (
    <Card>
      <SectionLabel>Udhaar in this window</SectionLabel>
      <div className="grid gap-6 lg:grid-cols-[minmax(0,7fr)_minmax(0,5fr)] lg:gap-10">
        <div>
          <Bridge
            rows={[row("start", "Owed at start"), row("given", "+ Given"), row("collected", "− Collected"), row("end", "= Owed at end", true)]}
          />
          <p className="mt-3 text-[0.8125rem] text-ink-muted">
            {credit.customers_owing} customer
            {credit.customers_owing === 1 ? "" : "s"} owed money at the end of this window. Today they owe{" "}
            <span className="tabular font-medium text-ink">{format(credit.owes_today)}</span>.
          </p>
        </div>
        <div className="flex flex-col">
          <p className="mb-1 text-[0.8125rem] font-medium text-ink-muted">Owe the most at the window's end</p>
          {credit.top_owing.length ? (
            <ul className="flex flex-col">
              {credit.top_owing.map((customer) => (
                <li key={customer.customer_id}>
                  <button
                    type="button"
                    onClick={() => navigate(`/credit/customers/${customer.customer_id}`)}
                    className="flex w-full items-center justify-between gap-3 border-b border-hairline py-2.5 text-left last:border-b-0 hover:text-accent"
                  >
                    <span className="truncate text-[0.9375rem] text-ink">{customer.name}</span>
                    <span className="flex shrink-0 items-center gap-1.5">
                      <span className="tabular text-[0.9375rem] text-ink">{format(customer.owed_at_end)}</span>
                      <CaretRightIcon size={14} className="text-ink-faint" aria-hidden />
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="py-2 text-[0.9375rem] text-ink-muted">Nobody owed anything at the end of this window.</p>
          )}
          <div className="mt-4">
            <Button
              size="sm"
              icon={<ArrowRightIcon size={16} aria-hidden />}
              onClick={() => navigate(`/credit/statement?from=${report.from}&to=${report.to}`)}
            >
              Statement for these dates
            </Button>
          </div>
        </div>
      </div>
      {/* §6.6's Phase 26 note, where somebody comparing with the old card would look. */}
      <p className="mt-4 border-t border-hairline pt-3 text-[0.8125rem] text-ink-muted">
        The billing statement's figures for the same dates. Collected counts every repayment, in cash, on the card machine, by UPI or by bank
        transfer.
      </p>
    </Card>
  );
}

/* --- provenance ----------------------------------------------------------------------------- */

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
    <Card>
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

function RangeSheet({
  open,
  onClose,
  current,
  onApply,
}: {
  open: boolean;
  onClose: () => void;
  current: Range | null;
  onApply: (range: Range) => void;
}) {
  return (
    <Sheet open={open} onClose={onClose} title="Date range" subtitle="Up to 366 days">
      {/* Remounted per opening, so it starts from the window on screen. */}
      {open ? <RangeForm current={current} onApply={onApply} /> : null}
    </Sheet>
  );
}

function RangeForm({ current, onApply }: { current: Range | null; onApply: (range: Range) => void }) {
  const today = todayAtOutlet();
  const form = useForm({
    from: current?.from ?? thisMonth(today).from,
    to: current?.to ?? today,
  });

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
