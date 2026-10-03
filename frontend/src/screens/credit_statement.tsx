/* The billing-period statement (CLAUDE.md §6.6, §8, §13.40-42). Rebuilt in Phase 23 from
 * credit_statement.js.
 *
 * Bills go out on the 16th (for the 1st to the 15th) and the 1st (for the rest). For any window
 * and every customer: owed going in, udhaar and payments inside it, what the bill should say,
 * what has come in since, and what they owe today.
 *
 * Every figure arrives computed, including the column totals: a statement is money arithmetic
 * from top to bottom, and this file adds nothing to anything (§14).
 *
 * `paid_since` is not "this bill, paid" (§13.40): nothing allocates a payment to a bill, so the
 * screen puts `billed` beside `paid_since` and lets the owner read it.
 *
 * Printing: the print stylesheet hides the chrome and opens every customer's entries; "Print
 * this customer" marks one card for a single print and unmarks it on `afterprint`.
 */

import { type FormEvent, type ReactNode, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { CaretDownIcon, PrinterIcon } from "@phosphor-icons/react";
import { useApiQuery } from "../api/queries";
import type { Schemas } from "../api/types";
import { ScreenActions, ScreenTitle } from "../app/chrome";
import { useGo } from "../app/navigation";
import { lastCompletedHalf } from "../lib/billing";
import { format, isZero } from "../lib/money";
import { businessDate, businessDateRange, todayAtOutlet } from "../lib/time";
import { Amount } from "../ui/Amount";
import { TextField, useForm } from "../ui/form";
import { useArrival } from "../ui/motion";
import { Button, Card, Empty, ErrorCard, ListRow, Pill, type PillKind, SectionLabel, Skeleton } from "../ui/primitives";
import { notify } from "../ui/toast";

type Row = Schemas["StatementRowResponse"];
type Line = Schemas["StatementLineResponse"];

const MODE_LABELS: Record<string, string> = { cash: "Cash", card: "Card", upi: "UPI", bank_transfer: "Bank transfer" };
const UNIT_SHORT: Record<string, string> = { litre: "L", kilogram: "kg" };

/* The bank's verdict on one bank-transfer payment (§13.42). "No statement uploaded" is not a
 * problem, so it is neutral. */
const BANK_STATUS: Record<string, { text: string; kind: PillKind }> = {
  verified: { text: "on bank statement", kind: "open" },
  ambiguous: { text: "bank: ambiguous", kind: "closed" },
  not_on_statement: { text: "not on bank statement", kind: "review" },
  no_statement: { text: "no statement uploaded", kind: "neutral" },
};

function printStatement(card: HTMLElement | null) {
  if (card) {
    document.body.classList.add("print-one");
    card.classList.add("print-target");
    window.addEventListener(
      "afterprint",
      () => {
        document.body.classList.remove("print-one");
        card.classList.remove("print-target");
      },
      { once: true },
    );
  }
  window.print();
}

export function CreditStatementScreen() {
  const [params] = useSearchParams();
  const fallback = lastCompletedHalf(todayAtOutlet());
  const from = params.get("from") || fallback.from;
  const to = params.get("to") || fallback.to;
  const statement = useApiQuery<Schemas["StatementResponse"]>("/credit-customers/statement", { from, to });
  const list = useRef<HTMLDivElement>(null);
  useArrival(list, Boolean(statement.data));

  const picker = <DateForm key={`${from}|${to}`} from={from} to={to} />;

  if (statement.isPending) {
    return (
      <>
        <ScreenTitle title="Billing statement" />
        {picker}
        <div className="mt-4">
          <Skeleton shape="list" rows={3} />
        </div>
      </>
    );
  }
  if (statement.isError || !statement.data) {
    return (
      <>
        <ScreenTitle title="Billing statement" />
        {picker}
        <div className="mt-4">
          <ErrorCard error={statement.error} onRetry={() => void statement.refetch()} />
        </div>
      </>
    );
  }

  const s = statement.data;
  const period = businessDateRange(s.from, s.to);
  const unanchored = s.rows.filter((entry) => !entry.opening_balance_entered).length;

  return (
    <>
      <ScreenTitle title="Billing statement" subtitle={period} />
      <ScreenActions>
        <Button size="sm" icon={<PrinterIcon size={16} aria-hidden />} onClick={() => printStatement(null)}>
          Print
        </Button>
      </ScreenActions>
      <div className="flex flex-col gap-4">
        {picker}

        {/* Only on paper: the screen's own title bar is hidden when printing. */}
        <div className="print-only">
          <p className="text-[1.375rem] font-semibold">Udhaar statement</p>
          <p className="text-[0.9375rem]">
            {period} · printed {businessDate(s.today)}
          </p>
        </div>

        {s.open_shift_count ? (
          <Notice>
            {s.open_shift_count} shift{s.open_shift_count === 1 ? " is" : "s are"} in this period still open. Its udhaar is counted, but the figures may still change until it is closed.
          </Notice>
        ) : null}
        {unanchored ? (
          <Notice className="no-print">
            {unanchored} customer{unanchored === 1 ? " has" : "s have"} no opening balance entered, so “owed before” only counts what has been typed into the app.
          </Notice>
        ) : null}
        {s.lines_truncated ? (
          <Notice>This period has more entries than the lists can show. Every figure is still exact; choose a shorter period to see every line.</Notice>
        ) : null}

        <p className="no-print text-[0.8125rem] text-ink-muted">
          Bill = owed before + udhaar − repaid, inside the dates. Paid since is every payment after the end date; it is not matched to any particular bill.
        </p>

        {s.rows.length ? (
          <div ref={list} className="flex flex-col gap-3">
            {s.rows.map((entry) => (
              <CustomerCard key={entry.customer_id} entry={entry} to={s.to} />
            ))}
          </div>
        ) : (
          <Empty>Nobody owed anything or had any udhaar or payments in this period.</Empty>
        )}

        {s.rows.length ? (
          <Card className="statement-totals">
            <SectionLabel>Totals · {s.rows.length} customers</SectionLabel>
            {/* Summed by the server, in one pass with the rows (§14). */}
            <ListRow label="Owed before" value={<Amount value={s.totals.owed_before} />} />
            <ListRow label="Udhaar in the period" value={<Amount value={s.totals.udhaar_in} />} />
            <ListRow label="Repaid in the period" value={<Amount value={s.totals.repaid_in} />} />
            <ListRow label="Bills" value={<Amount value={s.totals.billed} />} strong />
            <ListRow label="Udhaar since" value={<Amount value={s.totals.udhaar_since} />} />
            <ListRow label="Repaid since" value={<Amount value={s.totals.paid_since} />} />
            <ListRow label="Owed today" value={<Amount value={s.totals.owes_today} />} strong />
          </Card>
        ) : null}
      </div>
    </>
  );
}

function Notice({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <p className={`rounded-[var(--radius-control)] bg-warning-tint px-3.5 py-2.5 text-[0.8125rem] text-warning ${className}`}>{children}</p>;
}

function DateForm({ from, to }: { from: string; to: string }) {
  const navigate = useGo();
  const form = useForm({ from, to });

  function submit(event: FormEvent) {
    // Submitted by script; the CSP's form-action 'none' would block a real form post.
    event.preventDefault();
    const values = form.values;
    if (!values.from || !values.to) return void notify.warning("Pick both a start and an end date.");
    // ISO dates compare correctly as strings. The server refuses this too; this is the courtesy.
    if (values.from > values.to) return void notify.warning("The start date must not be after the end date.");
    navigate(`/credit/statement?from=${values.from}&to=${values.to}`);
  }

  return (
    <form onSubmit={submit} noValidate className="no-print">
      <Card>
        <div className="grid grid-cols-2 gap-3">
          <TextField form={form} name="from" label="From" type="date" required />
          <TextField form={form} name="to" label="To" type="date" max={todayAtOutlet()} required />
        </div>
        <p className="mt-2 text-[0.8125rem] text-ink-muted">Both dates are included. Up to 366 days.</p>
        <div className="mt-3">
          <Button type="submit" variant="primary">
            Show statement
          </Button>
        </div>
      </Card>
    </form>
  );
}

function CustomerCard({ entry, to }: { entry: Row; to: string }) {
  const navigate = useGo();
  const [open, setOpen] = useState(false);
  const card = useRef<HTMLDivElement>(null);
  const inRange = entry.lines.filter((line) => line.period === "in_range");
  const since = entry.lines.filter((line) => line.period === "since");

  // §6.8, §14: no opening balance and nothing before the window sums to ₹0.00, which is not the
  // same fact as "owed nothing".
  const owedBefore = !entry.opening_balance_entered && isZero(entry.owed_before) ? null : entry.owed_before;

  return (
    <div ref={card} data-arrive className="statement-customer">
      <Card>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="truncate text-[0.9375rem] font-semibold text-ink">{entry.name}</h2>
              {entry.is_active ? null : <Pill kind="neutral">inactive</Pill>}
            </div>
            <p className="text-[0.8125rem] text-ink-muted">{entry.phone}</p>
          </div>
          <div className="text-right">
            <p className="text-[0.75rem] text-ink-muted">Bill</p>
            <p className="text-[1.375rem] leading-none font-semibold tracking-[-0.02em] text-ink">
              <Amount value={entry.billed} />
            </p>
          </div>
        </div>

        {/* The six figures, in the order a bill reads. */}
        <div className="mt-4 grid grid-cols-3 gap-x-3 gap-y-3 sm:grid-cols-6">
          <Figure label="Owed before" value={<Amount value={owedBefore} absent="not entered" />} />
          <Figure label="Udhaar" value={<Amount value={entry.udhaar_in} />} />
          <Figure label="Repaid" value={<Amount value={entry.repaid_in} />} />
          <Figure label="Bill" value={<Amount value={entry.billed} />} strong />
          <Figure label="Paid since" value={<Amount value={entry.paid_since} />} />
          <Figure label="Owes today" value={<Amount value={entry.owes_today} />} strong />
        </div>

        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
          className="no-print pressable mt-4 flex items-center gap-1.5 text-[0.875rem] font-medium text-accent"
        >
          {open ? "Hide entries" : `Show entries (${entry.lines.length})`}
          <CaretDownIcon size={14} className={`transition-transform ${open ? "rotate-180" : ""}`} aria-hidden />
        </button>

        <div className="statement-detail mt-3" data-collapsed={open ? "false" : "true"}>
          <SectionLabel>In the period</SectionLabel>
          {inRange.length ? inRange.map((line) => <LineRow key={line.id} line={line} />) : <p className="text-[0.8125rem] text-ink-muted">No udhaar or payments inside these dates.</p>}
          <SectionLabel className="mt-4">Since {businessDate(to)}</SectionLabel>
          <ListRow label="Udhaar since" value={<Amount value={entry.udhaar_since} />} />
          {isZero(entry.opening_since) ? null : <ListRow label="Opening balance dated after the period" value={<Amount value={entry.opening_since} />} />}
          {since.map((line) => (
            <LineRow key={line.id} line={line} />
          ))}
          <p className="mt-2 text-[0.8125rem] text-ink-muted">Owes today = bill + udhaar since − paid since.</p>
          <div className="no-print mt-3 flex flex-wrap gap-2">
            <Button size="sm" icon={<PrinterIcon size={16} aria-hidden />} onClick={() => printStatement(card.current)}>
              Print this customer
            </Button>
            <Button size="sm" variant="plain" onClick={() => navigate(`/credit/customers/${entry.customer_id}`)}>
              Open the ledger
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}

function Figure({ label, value, strong = false }: { label: string; value: ReactNode; strong?: boolean }) {
  return (
    <div className="min-w-0">
      <p className="text-[0.75rem] text-ink-muted">{label}</p>
      <p className={`truncate ${strong ? "text-[0.9375rem] font-semibold text-ink" : "text-[0.875rem] text-ink"}`}>{value}</p>
    </div>
  );
}

function lineTitle(line: Line): string {
  if (line.kind === "opening") return "Opening balance (in owed before)";
  if (line.kind === "repayment") return `Payment · ${MODE_LABELS[line.mode ?? ""] ?? line.mode ?? ""}`;
  if (line.fuel_display_name === null) return "Udhaar · non-fuel";
  // §4.5: a quantity carries its own unit, never assumed litres.
  const qty = line.quantity === null ? "" : ` ${line.quantity} ${UNIT_SHORT[line.unit_of_measure ?? ""] ?? line.unit_of_measure ?? ""}`;
  return `Udhaar · ${line.fuel_display_name}${qty}`;
}

function lineCaption(line: Line): string {
  const parts = [businessDate(line.business_date)];
  if (line.vehicle_number) parts.push(line.vehicle_number);
  if (line.bank_reference) parts.push(line.bank_reference);
  if (line.kind === "repayment" && line.shift_id === null) parts.push("to the bank");
  if (line.reversal_reason) parts.push(line.reversal_reason);
  return parts.join(" · ");
}

function LineRow({ line }: { line: Line }) {
  const status = line.bank_status ? BANK_STATUS[line.bank_status] : null;
  return (
    <ListRow
      label={lineTitle(line)}
      detail={lineCaption(line)}
      value={
        <span className="flex flex-wrap items-center justify-end gap-1.5">
          {line.is_reversed ? <Pill kind="neutral">cancelled</Pill> : null}
          {line.is_reversal ? <Pill kind="neutral">reversal</Pill> : null}
          {status ? <Pill kind={status.kind}>{status.text}</Pill> : null}
          <span>{format(line.amount)}</span>
        </span>
      }
    />
  );
}
