/* The flagged-expense queue, one trading day at a time, and the month-end summary (CLAUDE.md
 * §6.7, §8). Rebuilt in Phase 23 from flagged_expenses.js; made a day-at-a-time screen in
 * Phase 30.
 *
 * Flags are never cleared by anything but a person (§6.7), and a shift cannot be locked while it
 * holds one, so this is a queue rather than a report.
 *
 * ## Why one day, and which
 *
 * The whole backlog on one page was long and mixed days that have nothing to do with each other.
 * §4.7 says days are typed in after the fact, so a manager reviews them a day at a time anyway.
 * The page opens on the day named in `?date=` -- the lock error sends the shift's own date -- or,
 * failing that, the oldest day still waiting, which is where the oldest-first rule of §6.5 says
 * the work is.
 *
 * ## Grouped by category within the day
 *
 * That grouping IS the signal: §6.7's aggregate rule flags every row in a (date, category) group
 * together, and a flat list of small amounts would hide the pattern the rule exists to surface.
 *
 * Reviewing happens here, in a sheet (the same form the shift's expenses screen uses); the row's
 * body still opens the shift, where its receipt and history are.
 */

import { useState } from "react";
import { useSearchParams } from "react-router";
import { api } from "../api/client";
import { useApiQuery } from "../api/queries";
import type { Schemas } from "../api/types";
import { ScreenTitle } from "../app/chrome";
import { useGo } from "../app/navigation";
import { addDays, businessDate, businessDateWeekday, todayAtOutlet } from "../lib/time";
import { Amount } from "../ui/Amount";
import { DateStrip } from "../ui/DateStrip";
import { reportFailure } from "../ui/feedback";
import { TextField, useForm } from "../ui/form";
import { Button, Card, Empty, ErrorCard, ListRow, Notice, Pill, Skeleton } from "../ui/primitives";
import { ReviewForm } from "../ui/ReviewExpense";
import { Sheet } from "../ui/Sheet";

type Flagged = Schemas["FlaggedExpenseResponse"];

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
/** The counts window either side of the chosen day. 15 + 15 fits the endpoint's 31-day cap and
 * covers every chip the strip renders, parked ones included. */
const COUNT_WINDOW = 15;

export function FlaggedExpensesScreen() {
  const navigate = useGo();
  const today = todayAtOutlet();
  const [params, setParams] = useSearchParams();
  const requested = params.get("date");
  const fromShift = params.get("shift");
  const requestedDate = requested && ISO_DATE.test(requested) && requested <= today ? requested : null;

  // Which way the last change of day went, so the list can arrive from that side. Null until a
  // person has chosen a day: the screen's first paint is not animated by this.
  const [direction, setDirection] = useState<"next" | "prev" | null>(null);
  const [reviewing, setReviewing] = useState<Flagged | null>(null);
  const [summaryOpen, setSummaryOpen] = useState(false);

  // The counts come first when no date was asked for: the day to open on is the oldest one
  // waiting, and only they know it. Windowed around today until then.
  const [anchor, setAnchor] = useState<string | null>(null);
  const counts = useApiQuery<Schemas["FlaggedByDateResponse"]>(
    "/expenses/flagged/by-date",
    {
      from: addDays(requestedDate ?? anchor ?? today, -COUNT_WINDOW),
      to: addDays(requestedDate ?? anchor ?? today, COUNT_WINDOW),
    },
    { keepPrevious: true },
  );

  const oldest = counts.data?.oldest_business_date ?? null;
  const selected = requestedDate ?? anchor ?? (oldest && oldest <= today ? oldest : today);
  const ready = requestedDate !== null || counts.isSuccess;

  const list = useApiQuery<Schemas["FlaggedExpensePage"]>(
    "/expenses/flagged",
    { business_date: selected, limit: 100 },
    { enabled: ready, keepPrevious: true },
  );

  function choose(date: string) {
    setDirection(date > selected ? "next" : "prev");
    setAnchor(date);
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        next.set("date", date);
        return next;
      },
      // Replace, not push: stepping through ten days must not take ten Back presses to leave.
      { replace: true },
    );
  }

  const badges: Record<string, number> = {};
  for (const day of counts.data?.items ?? []) badges[day.business_date] = day.count;
  const dayTotal = counts.data?.items.find((day) => day.business_date === selected)?.total_amount ?? null;

  if (counts.isError && !counts.data) {
    return (
      <>
        <ScreenTitle title="Flagged expenses" />
        <ErrorCard error={counts.error} onRetry={() => void counts.refetch()} />
      </>
    );
  }
  if (!ready || list.isPending) {
    return (
      <>
        <ScreenTitle title="Flagged expenses" />
        <Skeleton rows={3} />
      </>
    );
  }

  const items = list.data?.items ?? [];
  // Grouped by category within the day: the key §6.7's aggregate rule uses.
  const groups = new Map<string, Flagged[]>();
  for (const expense of items) {
    groups.set(expense.category_code, [...(groups.get(expense.category_code) ?? []), expense]);
  }
  const settled = !list.isPlaceholderData && !list.isError;
  const weekday = businessDateWeekday(selected);

  return (
    <>
      <ScreenTitle title="Flagged expenses" />
      <div className="flex flex-col gap-5">
        <div className="section-sticky pt-1 pb-2">
          <DateStrip value={selected} onChange={choose} max={today} badges={badges} label="Day to review" />
        </div>

        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-headline font-semibold text-ink">
              {weekday} {businessDate(selected)}
            </h2>
            <p className="text-footnote text-ink-muted">
              {items.length === 0 ? "Nothing waiting" : `${items.length} waiting`}
              {dayTotal !== null && items.length > 0 ? " · " : ""}
              {dayTotal !== null && items.length > 0 ? <Amount value={dayTotal} /> : null}
            </p>
          </div>
          <Button size="sm" variant="plain" onClick={() => setSummaryOpen(true)}>
            Month-end summary
          </Button>
        </div>

        <div key={selected} className="day-swap flex flex-col gap-4" data-dir={direction ?? undefined}>
          {list.isError ? (
            <ErrorCard error={list.error} onRetry={() => void list.refetch()} />
          ) : items.length ? (
            [...groups.entries()].map(([category, expenses]) => (
              <Card key={category}>
                <div className="flex items-center justify-between gap-3">
                  <h3 className="text-body font-semibold text-ink">{category}</h3>
                  {expenses.length > 1 ? <Pill kind="review">{expenses.length} together</Pill> : null}
                </div>
                {expenses.length > 1 ? (
                  <p className="mt-1 text-footnote text-ink-muted">
                    Flagged as a group: individually small, together over the threshold.
                  </p>
                ) : null}
                <div className="mt-2">
                  {expenses.map((expense) => (
                    <div key={expense.id} className="flex items-center gap-3 border-b border-hairline py-3 last:border-b-0">
                      <button
                        type="button"
                        className="pressable min-w-0 grow text-left"
                        onClick={() => navigate(`/shifts/${expense.shift_id}/expenses`)}
                      >
                        <span className="block truncate text-body text-ink">{expense.description}</span>
                        <span className="block text-footnote text-ink-muted">On its shift</span>
                      </button>
                      <Amount value={expense.amount} />
                      <Button size="sm" variant="primary" onClick={() => setReviewing(expense)}>
                        Review
                      </Button>
                    </div>
                  ))}
                </div>
              </Card>
            ))
          ) : (
            <Empty>
              Nothing to review on {businessDate(selected)}.
              {oldest && oldest < selected ? (
                <span className="mt-3 block">
                  <Button size="sm" onClick={() => choose(oldest)}>
                    Go to the oldest waiting: {businessDate(oldest)}
                  </Button>
                </span>
              ) : null}
            </Empty>
          )}

          {settled && items.length === 0 && fromShift ? (
            <Notice role="status">
              Everything on this day is reviewed.{" "}
              <button type="button" className="font-semibold underline" onClick={() => navigate(`/shifts/${fromShift}`)}>
                Back to the shift
              </button>{" "}
              to lock it.
            </Notice>
          ) : null}
          {list.data?.next_cursor ? (
            <p className="text-footnote text-ink-muted">There are more flagged expenses on this day than this page lists.</p>
          ) : null}
        </div>
      </div>

      <Sheet open={reviewing !== null} onClose={() => setReviewing(null)} title="Review expense" subtitle={reviewing?.category_code}>
        {reviewing ? <ReviewForm shiftId={reviewing.shift_id} expense={reviewing} onDone={() => setReviewing(null)} /> : null}
      </Sheet>

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
          <p className="mt-2 text-footnote text-ink-muted">Every mode, not only cash. A bank-paid bill is a real expense even though it never touched the drawer.</p>
        </div>
      ) : null}
    </div>
  );
}
