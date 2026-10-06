/* Bank deposits: cash leaving the locker (CLAUDE.md §5.2, §6.4). Rebuilt in Phase 23.
 *
 * Subtracted from expected closing, because the money is genuinely gone from the drawer.
 * `business_date` is not on this form: the server takes it from the shift and refuses a client
 * value (§3 rule 7), so the two cannot drift. Manager floor even for reads (§8).
 */

import { useState } from "react";
import { useParams } from "react-router";
import { api } from "../api/client";
import { useApiQuery, useRefreshApi } from "../api/queries";
import { useSubmission } from "../api/submission";
import type { Schemas } from "../api/types";
import { ScreenTitle } from "../app/chrome";
import { businessDate } from "../lib/time";
import { reportFailure } from "../ui/feedback";
import { TextField, useForm } from "../ui/form";
import { Button, ErrorCard, Skeleton } from "../ui/primitives";
import { ReceiptButton } from "../ui/receipt";
import { ReceiptUpload } from "../ui/ReceiptUpload";
import { isLive, ReversalBadge, ReversalForm } from "../ui/reversal";
import { Sheet } from "../ui/Sheet";
import { notify } from "../ui/toast";
import { MoneyRowCard, ShiftRowsFrame } from "./shiftRows";

type Deposit = Schemas["BankDepositResponse"];
type Action = { kind: "edit"; deposit: Deposit | null } | { kind: "reverse"; deposit: Deposit };

export function BankDepositsScreen() {
  const { shiftId = "" } = useParams();
  const page = useApiQuery<Schemas["BankDepositPage"]>(`/shifts/${shiftId}/bank-deposits`);
  const shift = useApiQuery<Schemas["ShiftResponse"]>(`/shifts/${shiftId}`);
  const [action, setAction] = useState<Action | null>(null);

  if (page.isPending || shift.isPending) {
    return (
      <>
        <ScreenTitle title="Bank deposits" />
        <Skeleton rows={3} />
      </>
    );
  }
  if (!page.data || !shift.data) {
    return (
      <>
        <ScreenTitle title="Bank deposits" />
        <ErrorCard error={page.error ?? shift.error} onRetry={() => void Promise.all([page.refetch(), shift.refetch()])} />
      </>
    );
  }
  const editable = shift.data.status === "open";

  return (
    <>
      <ShiftRowsFrame
        title="Bank deposits"
        shift={shift.data}
        totalLabel="Deposited this shift"
        total={page.data.total}
        explanation="Subtracted from what should be in the locker at the end of the day."
        onAdd={() => setAction({ kind: "edit", deposit: null })}
        emptyText="No deposits recorded for this shift."
        count={page.data.items.length}
        truncated={page.data.truncated}
      >
        {page.data.items.map((deposit) => (
          <MoneyRowCard
            key={deposit.id}
            title={businessDate(deposit.business_date)}
            caption={deposit.bank_reference ? `Reference ${deposit.bank_reference}` : undefined}
            amount={deposit.amount}
            badges={<ReversalBadge row={deposit} />}
            notes={deposit.reversal_reason ? <p className="mt-2 text-footnote text-ink-muted">Reason: {deposit.reversal_reason}</p> : null}
            actions={
              <>
                <ReceiptButton attachmentId={deposit.attachment_id} label="View slip" />
                {isLive(deposit) && editable ? (
                  <Button size="sm" onClick={() => setAction({ kind: "edit", deposit })}>
                    Edit
                  </Button>
                ) : null}
                {isLive(deposit) ? (
                  <Button size="sm" variant="danger" onClick={() => setAction({ kind: "reverse", deposit })}>
                    Reverse
                  </Button>
                ) : null}
              </>
            }
          />
        ))}
      </ShiftRowsFrame>

      <Sheet
        open={action !== null}
        onClose={() => setAction(null)}
        title={action?.kind === "reverse" ? "Reverse deposit" : action?.deposit ? "Edit deposit" : "Record a deposit"}
      >
        {action?.kind === "edit" ? <DepositForm shiftId={shiftId} existing={action.deposit} onDone={() => setAction(null)} /> : null}
        {action?.kind === "reverse" ? (
          <ReversalForm
            path={`/shifts/${shiftId}/bank-deposits/${action.deposit.id}/reversals`}
            amount={action.deposit.amount}
            description="Bank deposit"
            replacementText={{ name: "replacement_reference", label: "Corrected reference (optional)", value: action.deposit.bank_reference }}
            onDone={() => setAction(null)}
          />
        ) : null}
      </Sheet>
    </>
  );
}

function DepositForm({ shiftId, existing, onDone }: { shiftId: string; existing: Deposit | null; onDone: () => void }) {
  const submission = useSubmission("POST", `/shifts/${shiftId}/bank-deposits`);
  const refresh = useRefreshApi();
  const form = useForm({ amount: existing?.amount ?? "", bank_reference: existing?.bank_reference ?? "" });
  const [attachmentId, setAttachmentId] = useState<string | null>(existing?.attachment_id ?? null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    form.clearErrors();
    setBusy(true);
    try {
      if (existing) {
        const changes = form.changes();
        if (Object.keys(changes).length === 0) return onDone();
        await api.patch(`/shifts/${shiftId}/bank-deposits/${existing.id}`, changes);
      } else {
        // No business_date: the server takes it from the shift and refuses a client value.
        const body: Schemas["BankDepositCreate"] = { amount: form.values.amount };
        if (form.values.bank_reference) body.bank_reference = form.values.bank_reference;
        if (attachmentId) body.attachment_id = attachmentId;
        await submission.run(body);
      }
      onDone();
      notify.success(existing ? "Updated." : "Deposit recorded.");
      await refresh();
    } catch (error) {
      reportFailure(error, form, () => void submit());
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <TextField form={form} name="amount" label="Amount deposited" inputMode="decimal" required />
      <TextField form={form} name="bank_reference" label="Bank reference (optional)" />
      {existing ? (
        <p className="text-footnote text-ink-muted">The deposit slip cannot be changed once a deposit is recorded.</p>
      ) : (
        <ReceiptUpload shiftId={shiftId} attachmentId={attachmentId} label="Deposit slip (optional)" onUploaded={setAttachmentId} />
      )}
      <p className="text-footnote text-ink-muted">The business date comes from the shift. It is never typed, so the two cannot drift.</p>
      <Button variant="primary" block disabled={busy} onClick={() => void submit()}>
        {busy ? "Saving…" : existing ? "Save" : "Record deposit"}
      </Button>
    </div>
  );
}
