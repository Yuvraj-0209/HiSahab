/* Opening balances: what a customer already owed when this system started (§5.2, §6.6, §8).
 * Rebuilt in Phase 23 from credit_opening_balances.js.
 *
 * Admin only, for the reason §6.5 puts the cash locker's seeded opening there: it is a figure
 * nothing else in the system can check. Every other money row is corroborated by a meter, a
 * machine total or a receipt; this one is a person's word about the past. (The route gate is a
 * courtesy; the server refuses a manager with 403.)
 *
 * ## Zero is a button, not an omission
 *
 * §6.8: an absent row means nobody has looked at this customer; an entered ₹0.00 means somebody
 * checked and they were square. So "They owed nothing" is its own deliberate control, with the
 * date on its face -- it is the button pressed without reading the form, which is exactly how a
 * wrong date gets in.
 *
 * ## The date defaults to the ledger's start, not to today
 *
 * These are entered in a batch and share a date. Defaulting to today let the first customer set
 * the real date and every later one silently offer the wrong one -- a ₹0.00 once landed on 29
 * August instead of 1 July, and then blocked every back-entry before it.
 */

import { useState } from "react";
import { api } from "../api/client";
import { useApiQuery, useRefreshApi } from "../api/queries";
import type { Schemas } from "../api/types";
import { ScreenTitle } from "../app/chrome";
import { businessDate, todayAtOutlet } from "../lib/time";
import { Amount } from "../ui/Amount";
import { reportFailure } from "../ui/feedback";
import { TextField, useForm } from "../ui/form";
import { Button, Card, Empty, ErrorCard, ListRow, Pill, SectionLabel, Skeleton } from "../ui/primitives";
import { ReversalForm } from "../ui/reversal";
import { Sheet } from "../ui/Sheet";
import { notify } from "../ui/toast";

type Entry = Schemas["OpeningBalanceListItem"];
type Action = { kind: "enter"; entry: Entry } | { kind: "correct"; entry: Entry };

/** The earliest as_of_date already entered -- the ledger's start -- or today for an outlet that
 * has entered none. ISO date strings sort correctly; no Date is built. */
function ledgerStart(items: Entry[]): string {
  const dates = items.map((entry) => entry.as_of_date).filter((value): value is string => value !== null).sort();
  return dates[0] ?? todayAtOutlet();
}

export function OpeningBalancesScreen() {
  const page = useApiQuery<Schemas["OpeningBalancePage"]>("/credit-opening-balances");
  const [action, setAction] = useState<Action | null>(null);

  if (page.isPending) {
    return (
      <>
        <ScreenTitle title="Opening balances" />
        <Skeleton rows={3} />
      </>
    );
  }
  if (page.isError || !page.data) {
    return (
      <>
        <ScreenTitle title="Opening balances" />
        <ErrorCard error={page.error} onRetry={() => void page.refetch()} />
      </>
    );
  }

  const items = page.data.items;
  const defaultDate = ledgerStart(items);
  const pending = items.filter((entry) => entry.opening_balance === null);
  const anchored = items.filter((entry) => entry.opening_balance !== null);

  return (
    <>
      <ScreenTitle title="Opening balances" subtitle={`${anchored.length} of ${items.length} entered`} />
      <div className="flex flex-col gap-5">
        <Card>
          <SectionLabel>What this is</SectionLabel>
          <p className="text-[0.9375rem] text-ink">
            What each customer owed before this app started counting. Without it their ledger begins at zero, and the first payment they make drives their balance negative, as though the pump owed them money.
          </p>
          <p className="mt-2 text-[0.8125rem] text-ink-muted">
            It can only be set once per customer. Correcting one reverses the old figure with a reason and records the new one, so the change stays on the record.
          </p>
        </Card>

        {pending.length ? (
          <section>
            <SectionLabel>Not entered yet · {pending.length}</SectionLabel>
            <div className="flex flex-col gap-2.5">
              {pending.map((entry) => (
                <Card key={entry.credit_customer_id}>
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <h2 className="text-[0.9375rem] font-semibold text-ink">{entry.name}</h2>
                      <p className="t-absent text-[0.8125rem]">No opening balance entered</p>
                    </div>
                    {entry.is_active ? null : <Pill kind="neutral">inactive</Pill>}
                  </div>
                  <div className="mt-3">
                    <Button variant="primary" block onClick={() => setAction({ kind: "enter", entry })}>
                      Enter what they owed
                    </Button>
                  </div>
                </Card>
              ))}
            </div>
          </section>
        ) : null}

        {anchored.length ? (
          <section>
            <SectionLabel>Entered · {anchored.length}</SectionLabel>
            <div className="flex flex-col gap-2.5">
              {anchored.map((entry) => (
                <Card key={entry.credit_customer_id}>
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <h2 className="text-[0.9375rem] font-semibold text-ink">{entry.name}</h2>
                      <p className="text-[0.8125rem] text-ink-muted">As of {businessDate(entry.as_of_date)}</p>
                    </div>
                    <span className="text-[1.125rem] font-semibold text-ink">
                      <Amount value={entry.opening_balance} />
                    </span>
                  </div>
                  <ListRow label="Outstanding now" value={<Amount value={entry.outstanding} />} />
                  <div className="mt-3">
                    <Button size="sm" variant="danger" onClick={() => setAction({ kind: "correct", entry })}>
                      Correct this figure
                    </Button>
                  </div>
                </Card>
              ))}
            </div>
          </section>
        ) : null}

        {items.length ? null : <Empty>No credit customers yet. Add them under Admin, Credit customers, first.</Empty>}
      </div>

      <Sheet
        open={action !== null}
        onClose={() => setAction(null)}
        title={action?.kind === "correct" ? "Correct the opening balance" : (action?.entry.name ?? "")}
        subtitle={action?.kind === "correct" ? action.entry.name : "Opening balance"}
      >
        {action?.kind === "enter" ? <OpeningForm entry={action.entry} defaultDate={defaultDate} onDone={() => setAction(null)} /> : null}
        {action?.kind === "correct" && action.entry.opening_balance_id && action.entry.opening_balance !== null ? (
          <ReversalForm
            path={`/credit-opening-balances/${action.entry.opening_balance_id}/reversals`}
            amount={action.entry.opening_balance}
            description={action.entry.name}
            // A replacement inherits the original's as_of_date (a correction restates what was
            // owed, never when), so a wrong DATE is repaired by removing and re-entering.
            replacementHint="Leave blank to remove it entirely, which is what you want if the DATE was wrong. Then enter it again on the right date."
            onDone={() => setAction(null)}
          />
        ) : null}
      </Sheet>
    </>
  );
}

function OpeningForm({ entry, defaultDate, onDone }: { entry: Entry; defaultDate: string; onDone: () => void }) {
  const refresh = useRefreshApi();
  const form = useForm({ amount: "", as_of_date: defaultDate });
  const [busy, setBusy] = useState(false);

  async function send(amount: string) {
    form.clearErrors();
    setBusy(true);
    try {
      // No Idempotency-Key, and none is accepted: a second live opening balance is refused
      // with 409, so a retry cannot duplicate one.
      const body: Schemas["OpeningBalanceCreate"] = { credit_customer_id: entry.credit_customer_id, amount, as_of_date: form.values.as_of_date };
      await api.post("/credit-opening-balances", body);
      onDone();
      notify.success(`Opening balance recorded for ${entry.name}.`);
      await refresh();
    } catch (error) {
      reportFailure(error, form);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <TextField
        form={form}
        name="amount"
        label="What they owed"
        inputMode="decimal"
        required
        // No minimum: §6.6 permits a negative outstanding (paid in advance), and the server
        // carries no sign CHECK for the same reason.
        hint="Negative if the pump owed them. Zero if you checked and they were square."
      />
      <TextField
        form={form}
        name="as_of_date"
        label="As of"
        type="date"
        required
        hint="Their ledger starts here. Nothing dated before this can be entered afterwards, so if you are still back-entering July this must be the start of July, not today."
      />
      <p className="text-[0.8125rem] text-ink-muted">
        This can only be set once. Getting it wrong is recoverable, by reversing it with a reason, but the correction stays visible, so it is worth checking the register.
      </p>
      <Button
        variant="primary"
        block
        disabled={busy}
        onClick={() => {
          if (!form.values.amount.trim()) {
            form.setError("amount", "Enter a figure, or use “They owed nothing”.");
            return;
          }
          void send(form.values.amount);
        }}
      >
        {busy ? "Saving…" : "Record it"}
      </Button>
      {/* §6.8: zero as an answer, a deliberate act. The date is on the button. */}
      <Button block disabled={busy} onClick={() => void send("0.00")}>
        {form.values.as_of_date ? `They owed nothing on ${businessDate(form.values.as_of_date)}` : "They owed nothing"}
      </Button>
    </div>
  );
}
