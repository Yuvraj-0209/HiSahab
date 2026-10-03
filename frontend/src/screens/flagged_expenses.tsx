/* The flagged-expense queue, and the month-end summary (CLAUDE.md §6.7, §8). Rebuilt in
 * Phase 23 from flagged_expenses.js.
 *
 * Flags are never cleared by anything but a person (§6.7), and a shift cannot be locked while it
 * holds one, so this is a queue rather than a report.
 *
 * Rows are grouped by (business date, category) because that grouping IS the signal: §6.7's
 * aggregate rule flags every row in a group that crosses the threshold together, and a flat list
 * of small amounts would hide the ₹1,300 pattern the rule exists to surface.
 *
 * Reviewing happens on the expense's own shift screen, where its receipt and history are.
 */

import { useState } from "react";
import { useNavigate } from "react-router";
import { CaretRightIcon } from "@phosphor-icons/react";
import { api } from "../api/client";
import { useApiQuery } from "../api/queries";
import type { Schemas } from "../api/types";
import { ScreenTitle } from "../app/chrome";
import { businessDate, todayAtOutlet } from "../lib/time";
import { Amount } from "../ui/Amount";
import { reportFailure } from "../ui/feedback";
import { TextField, useForm } from "../ui/form";
import { Button, Card, Empty, ErrorCard, ListRow, Pill, Skeleton } from "../ui/primitives";
import { Sheet } from "../ui/Sheet";

type Flagged = Schemas["FlaggedExpenseResponse"];

export function FlaggedExpensesScreen() {
  const navigate = useNavigate();
  const page = useApiQuery<Schemas["FlaggedExpensePage"]>("/expenses/flagged", { limit: 100 });
  const [summaryOpen, setSummaryOpen] = useState(false);

  if (page.isPending) {
    return (
      <>
        <ScreenTitle title="Flagged expenses" />
        <Skeleton rows={3} />
      </>
    );
  }
  if (page.isError || !page.data) {
    return (
      <>
        <ScreenTitle title="Flagged expenses" />
        <ErrorCard error={page.error} onRetry={() => void page.refetch()} />
      </>
    );
  }

  // Grouped by (business_date, category): the key §6.7's aggregate rule uses.
  const groups = new Map<string, Flagged[]>();
  for (const expense of page.data.items) {
    const key = `${expense.business_date}|${expense.category_code}`;
    groups.set(key, [...(groups.get(key) ?? []), expense]);
  }

  return (
    <>
      <ScreenTitle title="Flagged expenses" subtitle={`${page.data.items.length} waiting`} />
      <div className="flex flex-col gap-5">
        <Card>
          <p className="text-[0.875rem] text-ink-muted">
            A shift cannot be locked while it holds an unreviewed flagged expense. Reviewing one is a decision with your name on it: flags are never cleared automatically, not even when a reversal drops the day back under the threshold.
          </p>
          <div className="mt-4">
            <Button block onClick={() => setSummaryOpen(true)}>
              Month-end summary
            </Button>
          </div>
        </Card>

        {page.data.items.length ? (
          [...groups.entries()].map(([key, expenses]) => {
            const [date = "", category = ""] = key.split("|");
            return (
              <Card key={key}>
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <h2 className="text-[0.9375rem] font-semibold text-ink">{category}</h2>
                    <p className="text-[0.8125rem] text-ink-muted">{businessDate(date)}</p>
                  </div>
                  {expenses.length > 1 ? <Pill kind="review">{expenses.length} together</Pill> : null}
                </div>
                {expenses.length > 1 ? (
                  <p className="mt-2 text-[0.8125rem] text-ink-muted">These were flagged as a group: individually small, together over the threshold.</p>
                ) : null}
                <div className="mt-2">
                  {expenses.map((expense) => (
                    <button
                      key={expense.id}
                      type="button"
                      onClick={() => navigate(`/shifts/${expense.shift_id}/expenses`)}
                      className="pressable flex w-full items-center gap-3 border-b border-hairline py-3 text-left last:border-b-0"
                    >
                      <span className="min-w-0 grow">
                        <span className="block truncate text-[0.9375rem] text-ink">{expense.description}</span>
                        <span className="block text-[0.8125rem] text-accent">Review on its shift</span>
                      </span>
                      <Amount value={expense.amount} />
                      <CaretRightIcon size={16} className="text-ink-faint" aria-hidden />
                    </button>
                  ))}
                </div>
              </Card>
            );
          })
        ) : (
          <Empty>Nothing flagged. Every expense is either under the threshold or already reviewed.</Empty>
        )}

        {page.data.next_cursor ? <p className="text-[0.8125rem] text-ink-muted">There are more flagged expenses than this page lists.</p> : null}
      </div>

      <Sheet open={summaryOpen} onClose={() => setSummaryOpen(false)} title="Month-end expense summary">
        <MonthSummary />
      </Sheet>
    </>
  );
}

/* `GET /expenses/summary` takes literal `from` / `to` query names, aliased server-side around
 * the reserved word. Renaming them would 422. */
function MonthSummary() {
  const today = todayAtOutlet();
  const form = useForm({ from: `${today.slice(0, 8)}01`, to: today });
  const [result, setResult] = useState<Schemas["ExpenseSummaryResponse"] | null>(null);
  const [busy, setBusy] = useState(false);

  async function run() {
    form.clearErrors();
    setBusy(true);
    try {
      setResult(await api.get<Schemas["ExpenseSummaryResponse"]>("/expenses/summary", { from: form.values.from, to: form.values.to }));
    } catch (error) {
      reportFailure(error, form);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-3">
        <TextField form={form} name="from" label="From" type="date" required />
        <TextField form={form} name="to" label="To" type="date" required hint="Up to 366 days." />
      </div>
      <Button variant="primary" block disabled={busy} onClick={() => void run()}>
        {busy ? "Totalling…" : "Show totals"}
      </Button>
      {result ? (
        <div>
          {Object.entries(result.totals_by_category ?? {}).map(([code, amount]) => (
            <ListRow key={code} label={code} value={<Amount value={amount as string} />} />
          ))}
          <div className="border-t border-hairline-strong">
            <ListRow label="Total" value={<Amount value={result.total} />} strong />
          </div>
          <p className="mt-2 text-[0.8125rem] text-ink-muted">Every mode, not only cash. A bank-paid bill is a real expense even though it never touched the drawer.</p>
        </div>
      ) : null}
    </div>
  );
}
