/* Today: the shift spine (CLAUDE.md §4.7, §5.2, §6.8). Rebuilt in Phase 23 from today.js.
 *
 * §5.2 calls `shifts` "the spine -- everything hangs off this", and this screen is that spine
 * made visible: which shift is open, what it is worth so far, and the lifecycle actions on it.
 *
 * ## Closing can legitimately fail, and those failures are the feature
 *
 * §6.8's preconditions -- MISSING_NOZZLE_READINGS, MISSING_COLLECTIONS,
 * CREDIT_SALE_MISSING_RECEIPT -- fire on *absence*, never on a mismatch. A shift whose
 * collections do not equal its sales closes normally, because that gap is §6.4's variance and
 * §6.6's udhaar. §14: blocking on it "teaches staff to type figures that balance". So this
 * screen never pre-checks the numbers and never disables Close on their strength. It sends the
 * request and reports what the server said.
 *
 * ## Profit is labelled, every time it appears
 *
 * §13.7: what this reports is *gross fuel margin on quantity sold*, not business profit. The
 * label travels with the figure.
 *
 * ## After Close, the screen follows the shift
 *
 * `/shifts/current` answers only for an *open* shift, so the instant a shift closes it would
 * vanish from Today and strand an admin with nothing to lock. A close therefore moves to
 * `#/shifts/{id}`, which reads the shift by id in any status.
 */

import { type ReactNode, useRef, useState } from "react";
import { useParams } from "react-router";
import { useQueryClient } from "@tanstack/react-query";
import { CaretRightIcon, LockIcon, NotePencilIcon } from "@phosphor-icons/react";
import { api } from "../api/client";
import { apiKey, useApiQuery, useRefreshApi } from "../api/queries";
import type { Schemas } from "../api/types";
import { ScreenTitle } from "../app/chrome";
import { useGo } from "../app/navigation";
import { useSession } from "../app/session";
import { format, quantity, reading, varianceLabel } from "../lib/money";
import { satisfies } from "../lib/roles";
import { businessDate, businessDateWeekday, timeOnly, todayAtOutlet } from "../lib/time";
import { Amount } from "../ui/Amount";
import { reportFailure } from "../ui/feedback";
import { SelectField, TextField, useForm } from "../ui/form";
import { useArrival } from "../ui/motion";
import { Button, Card, Empty, ErrorCard, ListRow, Pill, type PillKind, SectionLabel, Skeleton } from "../ui/primitives";
import { Sheet } from "../ui/Sheet";
import { notify } from "../ui/toast";

type Shift = Schemas["ShiftResponse"];

export const STATUS_PILL: Record<string, PillKind> = { open: "open", closed: "closed", locked: "locked" };

function shiftSubtitle(shift: Shift): string {
  return `${businessDateWeekday(shift.business_date)} ${businessDate(shift.business_date)} · shift ${shift.sequence}`;
}

/* --- routes ---------------------------------------------------------------------------- */

/** #/today -- the open shift, or the way to open one. */
export function TodayScreen() {
  const current = useApiQuery<Shift>("/shifts/current", undefined, { absentOn: ["NO_OPEN_SHIFT"] });

  if (current.isPending) {
    return (
      <>
        <ScreenTitle large title="Today" />
        <Skeleton shape="cards" rows={4} />
      </>
    );
  }
  if (current.isError) {
    // NOT_YOUR_SHIFT reaches an attendant when somebody else's shift is the open one: a real
    // state, explained in words rather than hidden.
    return (
      <>
        <ScreenTitle large title="Today" />
        <ErrorCard error={current.error} onRetry={() => void current.refetch()} />
      </>
    );
  }
  if (!current.data) return <NoShift />;
  return <ShiftDetail shift={current.data} />;
}

/** #/shifts/:shiftId -- any shift, in any status. The only page a closed shift has. */
export function ShiftByIdScreen() {
  const { shiftId = "" } = useParams();
  const shift = useApiQuery<Shift>(`/shifts/${shiftId}`);
  if (shift.isPending) {
    return (
      <>
        <ScreenTitle large title="Today" subtitle="Loading…" />
        <Skeleton shape="cards" rows={4} />
      </>
    );
  }
  if (shift.isError || !shift.data) {
    return (
      <>
        <ScreenTitle large title="Today" />
        <ErrorCard error={shift.error} onRetry={() => void shift.refetch()} />
      </>
    );
  }
  return <ShiftDetail shift={shift.data} />;
}

/* --- one shift ------------------------------------------------------------------------- */

function ShiftDetail({ shift }: { shift: Shift }) {
  const { me } = useSession();
  const isManager = satisfies(me.role, "manager");

  return (
    <>
      <ScreenTitle large title="Today" subtitle={shiftSubtitle(shift)} />
      <div className="flex flex-col gap-4">
        <ShiftHeader shift={shift} />
        {isManager ? <ShiftFigures shift={shift} /> : null}
      </div>
    </>
  );
}

function ShiftHeader({ shift }: { shift: Shift }) {
  const { me } = useSession();
  const navigate = useGo();
  const queryClient = useQueryClient();
  const refresh = useRefreshApi();
  const [busy, setBusy] = useState<"close" | "lock" | null>(null);
  const [reopening, setReopening] = useState(false);

  async function transition(action: "close" | "lock") {
    // No client-side pre-check of collections against sales (§6.8, §14). The server decides.
    setBusy(action);
    try {
      const updated = await api.patch<Shift>(`/shifts/${shift.id}/${action}`, {});
      queryClient.setQueryData(apiKey(`/shifts/${shift.id}`), updated);
      notify.success(action === "close" ? "Shift closed." : "Shift locked.");
      await refresh();
      navigate(`/shifts/${shift.id}`, { replace: true });
    } catch (error) {
      reportFailure(error);
    } finally {
      setBusy(null);
    }
  }

  const canClose = shift.status === "open" && satisfies(me.role, "manager");
  const canLockOrReopen = shift.status === "closed" && satisfies(me.role, "admin");
  const isOwnAttendantShift = !satisfies(me.role, "manager") && shift.status === "open";

  // Never a bare region (§16): when there is nothing to do, say why.
  const reason =
    shift.status === "locked"
      ? "This shift is locked, and locked is final. Corrections happen as reversals."
      : shift.status === "closed" && !canLockOrReopen
        ? "This shift is closed. An admin can lock or reopen it."
        : !canClose && shift.status === "open"
          ? "Only a manager or admin can close a shift."
          : null;

  return (
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Pill kind={STATUS_PILL[shift.status] ?? "neutral"}>{shift.status}</Pill>
            <span className="text-[0.8125rem] text-ink-muted">Shift {shift.sequence}</span>
          </div>
          <p className="mt-2 text-[1.375rem] leading-tight font-semibold tracking-[-0.02em] text-ink">
            {businessDateWeekday(shift.business_date)} {businessDate(shift.business_date)}
          </p>
          <p className="mt-1 text-[0.875rem] text-ink-muted">
            {timeOnly(shift.started_at)}
            {shift.ended_at ? ` to ${timeOnly(shift.ended_at)}` : " onwards"}
          </p>
        </div>
        {shift.status === "locked" ? <LockIcon size={22} className="text-ink-faint" aria-hidden /> : null}
      </div>

      {canClose || canLockOrReopen || isOwnAttendantShift ? (
        <div className="mt-5 flex flex-col gap-2 sm:flex-row">
          {isOwnAttendantShift ? (
            <Button variant="primary" block icon={<NotePencilIcon size={18} aria-hidden />} onClick={() => navigate("/entry")}>
              Enter this shift
            </Button>
          ) : null}
          {canClose ? (
            <Button variant="primary" block disabled={busy !== null} onClick={() => void transition("close")}>
              {busy === "close" ? "Closing…" : "Close shift"}
            </Button>
          ) : null}
          {canLockOrReopen ? (
            <>
              <Button variant="primary" block disabled={busy !== null} onClick={() => void transition("lock")}>
                {busy === "lock" ? "Locking…" : "Lock shift"}
              </Button>
              <Button block disabled={busy !== null} onClick={() => setReopening(true)}>
                Reopen shift
              </Button>
            </>
          ) : null}
        </div>
      ) : null}
      {reason ? <p className="mt-4 text-[0.8125rem] text-ink-muted">{reason}</p> : null}

      <Sheet open={reopening} onClose={() => setReopening(false)} title="Reopen shift" subtitle={shiftSubtitle(shift)}>
        <ReopenForm shift={shift} onDone={() => setReopening(false)} />
      </Sheet>
    </Card>
  );
}

/** §6.8: reopening takes a MANDATORY reason and is audit-logged. The reason is the point, so it
 * is a sheet with a sentence in it, not a confirm dialog. */
function ReopenForm({ shift, onDone }: { shift: Shift; onDone: () => void }) {
  const form = useForm({ reason: "" });
  const queryClient = useQueryClient();
  const refresh = useRefreshApi();
  const [busy, setBusy] = useState(false);

  async function submit() {
    form.clearErrors();
    setBusy(true);
    try {
      const updated = await api.patch<Shift>(`/shifts/${shift.id}/reopen`, { reason: form.values.reason });
      queryClient.setQueryData(apiKey(`/shifts/${shift.id}`), updated);
      onDone();
      notify.success("Shift reopened.");
      // §13.10: a mid-chain reopen FLAGS the next shift's reading and a finalised day's summary
      // rather than recomputing them. Said out loud, because the consequence is invisible here.
      notify.info("Any following shift's opening reading is now flagged for review. Nothing was recomputed.");
      await refresh();
    } catch (error) {
      reportFailure(error, form);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <p className="text-[0.875rem] text-ink-muted">
        Reopening moves this shift back to open. The reason is stored in the audit trail against your name.
      </p>
      <TextField form={form} name="reason" label="Why is this being reopened?" hint="3 to 500 characters." required />
      <Button variant="primary" block disabled={busy} onClick={() => void submit()}>
        {busy ? "Reopening…" : "Reopen shift"}
      </Button>
    </div>
  );
}

/* --- the figures (manager floor) ------------------------------------------------------- */

type Line = { label: ReactNode; value: ReactNode; valueClassName?: string; strong?: boolean } | { note: string };

const DAY_SOURCE_PILL: Record<string, string> = {
  snapshot: "finalised",
  computed: "live",
  no_trading: "no trading",
  unavailable: "unavailable",
};

/* §13.20's provenance: a snapshot is a record of what a manager was shown; a computed figure is
 * an estimate of a day still in motion. Neither register hides which it is. */
const DAY_SOURCE_LABEL: Record<string, string> = {
  snapshot: "Finalised. These are the figures as they were recorded on the day.",
  computed: "Live estimate. This day has not been reconciled, so these are derived now.",
  no_trading: "No trading recorded for this date.",
  unavailable: "Not available.",
};

function ShiftFigures({ shift }: { shift: Shift }) {
  // Each read is independent: one section failing to load must not blank the page.
  const id = shift.id;
  const sales = useApiQuery<Schemas["ShiftSales"]>(`/shifts/${id}/sales`);
  const collections = useApiQuery<Schemas["CollectionPage"]>(`/shifts/${id}/collections`);
  const expenses = useApiQuery<Schemas["ExpensePage"]>(`/shifts/${id}/expenses`);
  const creditSales = useApiQuery<Schemas["CreditSalePage"]>(`/shifts/${id}/credit-sales`);
  const repayments = useApiQuery<Schemas["CreditRepaymentPage"]>(`/shifts/${id}/credit-repayments`);
  const customers = useApiQuery<Schemas["CreditCustomerListItem"][]>("/credit-customers", { include_inactive: true });
  const deposits = useApiQuery<Schemas["BankDepositPage"]>(`/shifts/${id}/bank-deposits`);
  // Never /daily-summaries/{date}, which 404s until someone reconciles. /reports/daily always
  // answers, live-computing when nothing is stored (§13.20's `source` says which).
  const day = useApiQuery<Schemas["DailyReportResponse"]>(`/reports/daily/${shift.business_date}`);
  const worksheet = useApiQuery<Schemas["Worksheet"]>(`/shifts/${id}/readings`);

  const grid = useRef<HTMLDivElement>(null);
  useArrival(grid, !sales.isPending);

  const [open, setOpen] = useState<null | "sales" | "collections" | "expenses" | "credit" | "deposits" | "day">(null);

  const customerName = (customerId: string) =>
    customers.data?.find((customer) => customer.id === customerId)?.name ?? "Unknown customer";

  const unreviewed = expenses.data?.items.filter((e) => e.requires_review && !e.reviewed_at).length ?? 0;
  const dayCash = day.data?.cash ?? null;

  return (
    <div ref={grid} className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
      <DomainCard
        title="Metered sales"
        figure={
          sales.isError ? (
            <span className="t-absent">not valued</span>
          ) : (
            <Amount value={sales.data?.total_sale_value ?? null} absent={sales.isPending ? "…" : "not valued"} />
          )
        }
        lines={salesLines(sales.data ?? null, sales.error).slice(1)}
        loading={sales.isPending}
        onDetails={() => setOpen("sales")}
      />
      <DomainCard
        title="Collections"
        caption="Declared cash and what came in by machine"
        figure={<Amount value={collections.data?.declared_cash ?? null} absent={collections.isPending ? "…" : "not declared"} />}
        lines={collectionLines(collections.data ?? null).slice(1)}
        loading={collections.isPending}
        onDetails={() => setOpen("collections")}
      />
      <DomainCard
        title="Expenses"
        caption={countLabel(expenses.data?.items.length, "item")}
        badge={unreviewed ? <Pill kind="review">{unreviewed} to review</Pill> : null}
        figure={<Amount value={expenses.data?.total ?? null} absent="…" />}
        lines={expenseLines(expenses.data ?? null)}
        loading={expenses.isPending}
        onDetails={() => setOpen("expenses")}
      />
      <DomainCard
        title="Credit"
        caption={`${countLabel(creditSales.data?.items.length, "sale")}, ${countLabel(repayments.data?.items.length, "repayment")}`}
        figure={<Amount value={creditSales.data?.total ?? null} absent="…" />}
        figureLabel="issued"
        lines={[
          { label: "Repaid", value: <Amount value={repayments.data?.total ?? null} absent="-" /> },
          { label: "Of which cash", value: <Amount value={repayments.data?.cash_total ?? null} absent="-" /> },
        ]}
        loading={creditSales.isPending}
        onDetails={() => setOpen("credit")}
      />
      <DomainCard
        title="Bank deposits"
        caption={countLabel(deposits.data?.items.length, "deposit")}
        figure={<Amount value={deposits.data?.total ?? null} absent="…" />}
        lines={[]}
        loading={deposits.isPending}
        onDetails={() => setOpen("deposits")}
      />
      <DomainCard
        title="Day cash"
        caption={businessDate(shift.business_date)}
        badge={
          dayCash ? (
            <Pill kind={dayCash.source === "snapshot" ? "locked" : "neutral"}>
              {DAY_SOURCE_PILL[dayCash.source] ?? dayCash.source}
            </Pill>
          ) : null
        }
        figure={<Amount value={dayCash?.expected_closing ?? null} absent={day.isPending ? "…" : "-"} />}
        figureLabel="expected closing"
        lines={dayCashLines(dayCash).filter((line) => !("note" in line)).slice(2)}
        loading={day.isPending}
        onDetails={() => setOpen("day")}
      />

      <Sheet open={open === "sales"} onClose={() => setOpen(null)} title="Metered sales" subtitle={shiftSubtitle(shift)}>
        <Lines lines={salesLines(sales.data ?? null, sales.error)} />
        <SectionLabel className="mt-6">Nozzle readings</SectionLabel>
        {worksheet.data?.lines.length ? (
          <div className="flex flex-col gap-3">
            {worksheet.data.lines.map((line) => (
              <NozzleReadings key={line.nozzle_id} line={line} />
            ))}
          </div>
        ) : (
          <p className="text-[0.875rem] text-ink-muted">Nozzle readings unavailable.</p>
        )}
      </Sheet>
      <Sheet open={open === "collections"} onClose={() => setOpen(null)} title="Collections" subtitle={shiftSubtitle(shift)}>
        <Lines lines={collectionLines(collections.data ?? null)} />
      </Sheet>
      <Sheet open={open === "expenses"} onClose={() => setOpen(null)} title="Expenses" subtitle={shiftSubtitle(shift)}>
        <Lines lines={expenseLines(expenses.data ?? null, true)} />
      </Sheet>
      <Sheet open={open === "credit"} onClose={() => setOpen(null)} title="Credit" subtitle={shiftSubtitle(shift)}>
        <SectionLabel>Issued</SectionLabel>
        <Lines lines={creditLines(creditSales.data ?? null, customerName, "Issued")} />
        <SectionLabel className="mt-6">Repaid</SectionLabel>
        <Lines lines={creditLines(repayments.data ?? null, customerName, "Repaid")} />
      </Sheet>
      <Sheet open={open === "deposits"} onClose={() => setOpen(null)} title="Bank deposits" subtitle={shiftSubtitle(shift)}>
        <Lines lines={depositLines(deposits.data ?? null)} />
      </Sheet>
      <Sheet open={open === "day"} onClose={() => setOpen(null)} title="Day cash" subtitle={businessDate(shift.business_date)}>
        <Lines lines={dayCashLines(dayCash)} />
      </Sheet>
    </div>
  );
}

/** One domain: a headline figure the card is read for, a few supporting lines, and Details. */
function DomainCard({
  title,
  caption,
  badge,
  figure,
  figureLabel,
  lines,
  loading,
  onDetails,
}: {
  title: string;
  caption?: string | undefined;
  badge?: ReactNode;
  figure: ReactNode;
  figureLabel?: string;
  lines: Line[];
  loading: boolean;
  onDetails: () => void;
}) {
  const shown = lines.filter((line) => !("note" in line)).slice(0, 3);
  return (
    <div data-arrive>
      <Card className="flex h-full flex-col">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-[0.9375rem] font-semibold text-ink">{title}</h2>
            {caption ? <p className="text-[0.8125rem] text-ink-muted">{caption}</p> : null}
          </div>
          {badge}
        </div>
        <div className="mt-3 flex items-baseline gap-2">
          <span className="text-[1.75rem] leading-none font-semibold tracking-[-0.025em] text-ink">
            {loading ? <span className="skeleton inline-block h-7 w-32 rounded-lg align-middle" /> : figure}
          </span>
          {figureLabel && !loading ? <span className="text-[0.8125rem] text-ink-muted">{figureLabel}</span> : null}
        </div>
        {shown.length ? (
          <div className="mt-3">
            {shown.map((line, index) =>
              "note" in line ? null : (
                <ListRow key={index} label={line.label} value={line.value} valueClassName={line.valueClassName ?? ""} />
              ),
            )}
          </div>
        ) : null}
        <div className="mt-auto pt-4">
          <button
            type="button"
            onClick={onDetails}
            className="pressable flex w-full items-center justify-between rounded-[var(--radius-control)] bg-surface-sunken px-3.5 py-2.5 text-[0.875rem] font-medium text-ink"
          >
            Details
            <CaretRightIcon size={16} className="text-ink-faint" aria-hidden />
          </button>
        </div>
      </Card>
    </div>
  );
}

function Lines({ lines }: { lines: Line[] }) {
  if (lines.length === 0) return <Empty>Nothing recorded.</Empty>;
  return (
    <div>
      {lines.map((line, index) =>
        "note" in line ? (
          <p key={index} className="border-b border-hairline py-3 text-[0.8125rem] text-ink-muted last:border-b-0">
            {line.note}
          </p>
        ) : (
          <ListRow
            key={index}
            label={line.label}
            value={line.value}
            valueClassName={line.valueClassName ?? ""}
            strong={line.strong ?? false}
          />
        ),
      )}
    </div>
  );
}

function countLabel(count: number | undefined, noun: string): string {
  if (count === undefined) return "loading";
  if (count === 0) return `no ${noun}s`;
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

function salesLines(sales: Schemas["ShiftSales"] | null, error: unknown): Line[] {
  if (error) {
    // A missing price refuses valuation (§6.3), correctly: a day valued at zero would reconcile
    // to a surplus nobody can explain.
    return [{ note: "Sales cannot be valued until a price exists for every fuel sold. Nothing is wrong with the readings." }];
  }
  if (!sales) return [];
  return [
    { label: "Total", value: <Amount value={sales.total_sale_value} />, strong: true },
    ...Object.entries(sales.quantity_by_unit ?? {}).map(([unit, amount]) => ({
      label: unit === "kilogram" ? "Kilograms" : "Litres",
      value: quantity(amount as string, unit),
    })),
    {
      label: "Gross fuel margin",
      value: <Amount value={sales.total_gross_fuel_margin} absent="no margin entered" />,
    },
    // §13.7: not optional.
    { note: "Gross fuel margin on quantity sold, not business profit. It excludes stock revaluation." },
    ...(sales.incomplete ? [{ note: "Some nozzles have no closing reading yet, so this total is partial." }] : []),
  ];
}

function NozzleReadings({ line }: { line: Schemas["WorksheetLine"] }) {
  const saved = line.reading;
  return (
    <div className="rounded-[var(--radius-control)] border border-hairline px-3.5 py-1">
      <ListRow label={line.nozzle_label} detail={`${line.dispenser_label} · ${line.fuel_type_code}`} />
      {saved ? (
        <>
          <ListRow label="Opening" value={reading(saved.opening_reading)} />
          <ListRow label="Closing" value={reading(saved.closing_reading, { absent: "not entered" })} />
          <ListRow label="Testing" value={quantity(saved.testing_quantity, line.unit_of_measure)} />
          <ListRow
            label="Sold"
            value={quantity(saved.quantity_sold, line.unit_of_measure, { absent: "awaiting closing" })}
            strong
          />
        </>
      ) : (
        <ListRow label="Reading" value={<span className="t-absent">not recorded</span>} />
      )}
    </div>
  );
}

const MACHINE_MODES = [
  { value: "card", label: "Card" },
  { value: "upi", label: "UPI" },
  { value: "wallet", label: "Wallet" },
] as const;

function collectionLines(page: Schemas["CollectionPage"] | null): Line[] {
  if (!page) return [];
  // Live rows only: a reversed row and its reversal are history, not the current declaration.
  const live = page.items.filter((item) => !item.reverses_id && !item.is_reversed);
  return [
    // "not declared" and ₹0.00 are different facts (§6.8): never coalesced.
    { label: "Cash declared", value: <Amount value={page.declared_cash} absent="not declared" />, strong: true },
    ...MACHINE_MODES.map((mode) => ({
      label: mode.label,
      value: <Amount value={live.find((item) => item.mode === mode.value)?.amount ?? null} absent="not entered" />,
    })),
  ];
}

function expenseLines(page: Schemas["ExpensePage"] | null, detailed = false): Line[] {
  if (!page) return [];
  // `total` and the per-category totals are server-computed (§14 forbids summing in JS).
  const lines: Line[] = Object.entries(page.totals_by_category ?? {}).map(([code, amount]) => ({
    label: code,
    value: format(amount as string),
  }));
  if (!detailed) return lines;
  return [
    { label: "Total", value: format(page.total), strong: true },
    ...lines,
    ...(page.items.length
      ? page.items.map((expense) => ({
          label: (
            <span className="flex flex-wrap items-center gap-2">
              {expense.description}
              {expense.requires_review && !expense.reviewed_at ? <Pill kind="review">review</Pill> : null}
            </span>
          ),
          value: format(expense.amount),
        }))
      : [{ note: "No expenses recorded for this shift." }]),
    ...(page.truncated ? [{ note: "This shift has more expenses than shown here." }] : []),
  ];
}

function creditLines(
  page: Schemas["CreditSalePage"] | Schemas["CreditRepaymentPage"] | null,
  customerName: (id: string) => string,
  totalLabel: string,
): Line[] {
  if (!page) return [];
  const lines: Line[] = [{ label: totalLabel, value: format(page.total), strong: true }];
  // Only the cash half of repayments reaches the drawer (§6.4).
  if ("cash_total" in page) lines.push({ label: "Of which cash", value: format(page.cash_total) });
  if (!page.items.length) return [...lines, { note: "Nothing recorded this shift." }];
  return [
    ...lines,
    ...page.items.map((entry) => ({ label: customerName(entry.credit_customer_id), value: format(entry.amount) })),
  ];
}

function depositLines(page: Schemas["BankDepositPage"] | null): Line[] {
  if (!page) return [];
  const lines: Line[] = [{ label: "Deposited", value: format(page.total), strong: true }];
  if (!page.items.length) return [...lines, { note: "No deposits recorded for this shift." }];
  return [
    ...lines,
    ...page.items.map((deposit) => ({ label: deposit.bank_reference ?? "Deposit", value: format(deposit.amount) })),
    ...(page.truncated ? [{ note: "This shift has more deposits than shown here." }] : []),
  ];
}

function dayCashLines(cash: Schemas["DayCashResponse"] | null): Line[] {
  if (!cash) return [];
  // The variance sign runs opposite to a gap's: money.ts's varianceLabel, never gapLabel.
  const variance = varianceLabel(cash.variance);
  return [
    { label: "Opening balance", value: <Amount value={cash.opening_balance} /> },
    { label: "Expected closing", value: <Amount value={cash.expected_closing} />, strong: true },
    { label: "Actual counted", value: <Amount value={cash.actual_counted} absent="not counted" /> },
    { label: "Variance", value: variance.text, valueClassName: variance.className },
    { note: DAY_SOURCE_LABEL[cash.source] ?? cash.source },
    ...(cash.unavailable_reason ? [{ note: cash.unavailable_reason }] : []),
  ];
}

/* --- no open shift --------------------------------------------------------------------- */

function NoShift() {
  const { me } = useSession();
  const navigate = useGo();
  const isManager = satisfies(me.role, "manager");
  const [opening, setOpening] = useState(false);
  // A closed-but-unlocked shift has no other page pointing at it, so manager+ gets a way back.
  const recent = useApiQuery<Schemas["ShiftPage"]>("/shifts", { limit: 8 }, { enabled: isManager });

  return (
    <>
      <ScreenTitle large title="Today" subtitle="No open shift" />
      <div className="flex flex-col gap-5">
        <Card>
          <p className="text-[1.125rem] font-semibold tracking-[-0.015em] text-ink">No shift is open at this outlet.</p>
          <p className="mt-1.5 max-w-[60ch] text-[0.875rem] text-ink-muted">
            Only one shift may be open at a time. That is what makes the carried-forward meter reading unambiguous.
          </p>
          <div className="mt-5">
            <Button variant="primary" onClick={() => setOpening(true)}>
              Open a shift
            </Button>
          </div>
        </Card>

        {recent.data?.items.length ? (
          <section>
            <SectionLabel>Recent shifts</SectionLabel>
            <Card className="py-1 sm:py-1">
              {recent.data.items.map((shift) => (
                <button
                  key={shift.id}
                  type="button"
                  onClick={() => navigate(`/shifts/${shift.id}`)}
                  className="pressable flex w-full items-center gap-3 border-b border-hairline py-3 text-left last:border-b-0"
                >
                  <div className="min-w-0 grow">
                    <p className="text-[0.9375rem] text-ink">
                      {businessDate(shift.business_date)} · shift {shift.sequence}
                    </p>
                    <p className="text-[0.8125rem] text-ink-muted">
                      {shift.status === "closed" ? "Closed, needs locking or review" : shift.status}
                    </p>
                  </div>
                  <Pill kind={STATUS_PILL[shift.status] ?? "neutral"}>{shift.status}</Pill>
                  <CaretRightIcon size={16} className="text-ink-faint" aria-hidden />
                </button>
              ))}
            </Card>
          </section>
        ) : null}
      </div>

      <Sheet open={opening} onClose={() => setOpening(false)} title="Open a shift">
        <OpenShiftForm onDone={() => setOpening(false)} />
      </Sheet>
    </>
  );
}

function OpenShiftForm({ onDone }: { onDone: () => void }) {
  const { me } = useSession();
  const refresh = useRefreshApi();
  const isManager = satisfies(me.role, "manager");
  // Phase 14: a manager may open a shift in a salesman's name. Manager floor because GET /users
  // refuses an attendant and POST /shifts refuses an attendant a foreign attendant_id anyway.
  const roster = useApiQuery<Schemas["UserListItem"][]>("/users", undefined, { enabled: isManager });
  const today = todayAtOutlet();
  const form = useForm({ business_date: today, attendant_id: me.id });
  const [busy, setBusy] = useState(false);

  async function submit() {
    form.clearErrors();
    setBusy(true);
    try {
      // `sequence` is never sent: it is server-assigned, and ShiftCreate forbids it (§14). The
      // attendant is omitted when it is the caller, so the server's own default decides.
      const body: Schemas["ShiftCreate"] = { business_date: form.values.business_date };
      if (isManager && form.values.attendant_id && form.values.attendant_id !== me.id) {
        body.attendant_id = form.values.attendant_id;
      }
      await api.post("/shifts", body);
      onDone();
      notify.success("Shift opened.");
      await refresh();
    } catch (error) {
      reportFailure(error, form);
    } finally {
      setBusy(false);
    }
  }

  const people = (roster.data ?? []).filter((person) => person.is_active);

  return (
    <div className="flex flex-col gap-4">
      <TextField
        form={form}
        name="business_date"
        label="Business date"
        type="date"
        max={today}
        required
        hint="The trading day this shift belongs to, which is not necessarily the day it is typed in."
      />
      {isManager && people.length ? (
        <SelectField
          form={form}
          name="attendant_id"
          label="Attendant"
          options={people.map((person) => ({
            value: person.id,
            label: person.id === me.id ? `${person.full_name} (you)` : person.full_name,
          }))}
          hint="The one person accountable for this shift's cash. A shortfall is booked against this name."
        />
      ) : null}
      <Button variant="primary" block disabled={busy} onClick={() => void submit()}>
        {busy ? "Opening…" : "Open shift"}
      </Button>
    </div>
  );
}
