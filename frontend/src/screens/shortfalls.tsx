/* One salesman's shortfall ledger, and settling it (CLAUDE.md §5.2, §13.14, §13.15). Rebuilt in
 * Phase 23 from shortfalls.js.
 *
 * A shortfall is its own record type and NEVER a credit sale (§13.14): putting staff debt into a
 * customer's balance would mean nobody could answer "what does this customer owe me" again.
 *
 * `outstanding = SUM(shortfalls) − SUM(settlements)` over every row, reversals included,
 * computed on every read and never stored (§14).
 *
 * A settlement names the salesman; booking a shortfall does not. Booking reads
 * `shifts.attendant_id` so a typo cannot put a debt on the wrong person; the cash arriving during
 * this shift may pay down anybody's balance, so the settlement says whose.
 *
 * §13.15, said on the screen: there is no write-off, every settlement is cash, and a small figure
 * nobody will chase stays on the balance permanently.
 */

import { useState } from "react";
import { useParams } from "react-router";
import { useApiQuery, useRefreshApi } from "../api/queries";
import { useSubmission } from "../api/submission";
import type { Schemas } from "../api/types";
import { ScreenTitle } from "../app/chrome";
import { isNegative } from "../lib/money";
import { dateTime } from "../lib/time";
import { Arrive } from "../ui/Arrive";
import { Amount } from "../ui/Amount";
import { reportFailure } from "../ui/feedback";
import { TextField, useForm } from "../ui/form";
import { Button, Card, Empty, ErrorCard, HeroFigure, ListCard, ListRow, Pill, Skeleton } from "../ui/primitives";
import { Sheet } from "../ui/Sheet";
import { notify } from "../ui/toast";

type Ledger = Schemas["app__api__v1__shortfalls__LedgerPage"];

export function ShortfallLedgerScreen() {
  const { salesmanId = "" } = useParams();
  const ledger = useApiQuery<Ledger>(`/salesman-shortfalls/${salesmanId}/ledger`, { limit: 100 });
  const outstanding = useApiQuery<Schemas["OutstandingReport"]>("/salesman-shortfalls/outstanding");
  // A settlement is filed against the shift the money arrived in, so one has to be open.
  const shift = useApiQuery<Schemas["ShiftResponse"]>("/shifts/current", undefined, { absentOn: ["NO_OPEN_SHIFT"] });
  const [settling, setSettling] = useState(false);

  if (ledger.isPending || outstanding.isPending || shift.isPending) {
    return (
      <>
        <ScreenTitle title="Shortfall ledger" />
        <Skeleton rows={3} />
      </>
    );
  }
  const failed = ledger.error ?? outstanding.error ?? shift.error;
  if (failed || !ledger.data || !outstanding.data) {
    return (
      <>
        <ScreenTitle title="Shortfall ledger" />
        <ErrorCard error={failed} onRetry={() => void Promise.all([ledger.refetch(), outstanding.refetch(), shift.refetch()])} />
      </>
    );
  }

  const row = outstanding.data.items.find((entry) => entry.salesman_id === salesmanId);
  const open = shift.data;

  return (
    <>
      <ScreenTitle title="Shortfall ledger" subtitle={row?.full_name} />
      <Arrive items="children" className="flex flex-col gap-5">
        <Card>
          <HeroFigure label="Outstanding">
            {/* No row in the outstanding report means nothing has ever been booked: a true zero. */}
            <Amount value={row?.outstanding ?? "0.00"} />
          </HeroFigure>
          <p className="mt-3 text-callout text-ink-muted">Booked shortfalls less settlements, reversals included. Computed on every read, never stored.</p>
          <div className="mt-4">
            {open ? (
              <Button variant="primary" block onClick={() => setSettling(true)}>
                Record a settlement
              </Button>
            ) : (
              <p className="text-footnote text-ink-muted">A settlement is filed against the shift the cash arrived in, so one has to be open.</p>
            )}
          </div>
        </Card>

        <p className="text-footnote text-ink-muted">
          A shortfall can only be repaid in cash. V1 has no way to write one off, so a small figure nobody will chase stays on this balance and it only ever grows.
        </p>

        {ledger.data.items.length ? (
          <ListCard>
            {ledger.data.items.map((entry) => (
              <ListRow
                key={entry.id}
                label={entry.kind === "shortfall" ? "Shortfall booked" : "Settlement"}
                detail={dateTime(entry.created_at)}
                value={
                  <span className="flex items-center justify-end gap-2">
                    {entry.is_reversal ? <Pill kind="neutral">reversal</Pill> : null}
                    <span className={isNegative(entry.balance_delta) ? "text-surplus" : "text-short"}>
                      <Amount value={entry.balance_delta} sign />
                    </span>
                  </span>
                }
              />
            ))}
          </ListCard>
        ) : (
          <Empty>Nothing on this ledger.</Empty>
        )}
      </Arrive>

      <Sheet open={settling} onClose={() => setSettling(false)} title="Record a settlement" subtitle={row?.full_name}>
        {open ? <SettlementForm shiftId={open.id} salesmanId={salesmanId} onDone={() => setSettling(false)} /> : null}
      </Sheet>
    </>
  );
}

function SettlementForm({ shiftId, salesmanId, onDone }: { shiftId: string; salesmanId: string; onDone: () => void }) {
  const submission = useSubmission("POST", `/shifts/${shiftId}/shortfall-settlements`);
  const refresh = useRefreshApi();
  const form = useForm({ amount: "" });
  const [busy, setBusy] = useState(false);

  async function submit() {
    form.clearErrors();
    setBusy(true);
    try {
      // salesman_id IS sent here, unlike when booking: see the module header.
      const body: Schemas["SettlementCreate"] = { salesman_id: salesmanId, amount: form.values.amount };
      await submission.run(body);
      onDone();
      notify.success("Settlement recorded.");
      await refresh();
    } catch (error) {
      reportFailure(error, form, () => void submit());
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <p className="text-callout text-ink-muted">Cash handed back by the salesman. It increases the expected cash for the shift it arrives in.</p>
      <TextField
        form={form}
        name="amount"
        label="Amount repaid"
        inputMode="decimal"
        required
        hint="More than they owe is accepted. The balance simply goes negative."
      />
      <Button variant="primary" block disabled={busy} onClick={() => void submit()}>
        {busy ? "Saving…" : "Record settlement"}
      </Button>
    </div>
  );
}
