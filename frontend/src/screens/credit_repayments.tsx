/* Udhaar repaid on a shift (CLAUDE.md §4.4, §5.1, §6.4). Rebuilt in Phase 23 from
 * credit_repayments.js. (The dated, shift-less form on the Credit tab lands with that tab.)
 *
 * §4.4: a customer settling an old bill brings money with no sale on the day it arrives.
 *
 * A deactivated customer can still repay, deliberately (§5.1): "refusing their money would be
 * backwards, and would leave a balance nothing can ever clear." So this list includes inactive
 * customers, labelled, while the credit SALE screen does not. The asymmetry is the rule.
 *
 * Only cash reaches the drawer. A card or UPI repayment taken on this shift is inside the
 * machine's collections total and enters §6.4 on the sales side; a bank transfer enters nowhere.
 */

import { useState } from "react";
import { useParams } from "react-router";
import { api } from "../api/client";
import { useApiQuery, useRefreshApi } from "../api/queries";
import { useSubmission } from "../api/submission";
import type { Schemas } from "../api/types";
import { ScreenActions, ScreenTitle } from "../app/chrome";
import { useSession } from "../app/session";
import { satisfies } from "../lib/roles";
import { Amount } from "../ui/Amount";
import { reportFailure } from "../ui/feedback";
import { SelectField, TextField, useForm } from "../ui/form";
import { Button, Card, Empty, ErrorCard, ListRow, Skeleton } from "../ui/primitives";
import { PlusIcon } from "@phosphor-icons/react";
import { businessDate, todayAtOutlet } from "../lib/time";
import { ReceiptUpload } from "../ui/ReceiptUpload";
import { isLive, ReversalBadge, ReversalForm } from "../ui/reversal";
import { Sheet } from "../ui/Sheet";
import { notify } from "../ui/toast";
import { MoneyRowCard, ShiftRowsFrame } from "./shiftRows";

type Repayment = Schemas["CreditRepaymentResponse"];
type Customer = Schemas["CreditCustomerListItem"];
type Action = { kind: "edit"; repayment: Repayment | null } | { kind: "reverse"; repayment: Repayment };

export const REPAYMENT_MODES = [
  { value: "cash", label: "Cash, into the locker" },
  { value: "card", label: "Card" },
  { value: "upi", label: "UPI" },
  { value: "bank_transfer", label: "Bank transfer" },
];

export function CreditRepaymentsScreen() {
  const { shiftId = "" } = useParams();
  const { me } = useSession();
  const page = useApiQuery<Schemas["CreditRepaymentPage"]>(`/shifts/${shiftId}/credit-repayments`);
  const shift = useApiQuery<Schemas["ShiftResponse"]>(`/shifts/${shiftId}`);
  const customers = useApiQuery<Customer[]>("/credit-customers", { include_inactive: true });
  const [action, setAction] = useState<Action | null>(null);

  const queries = [page, shift, customers];
  if (queries.some((query) => query.isPending)) {
    return (
      <>
        <ScreenTitle title="Repayments" />
        <Skeleton rows={3} />
      </>
    );
  }
  if (!page.data || !shift.data || !customers.data) {
    return (
      <>
        <ScreenTitle title="Repayments" />
        <ErrorCard
          error={queries.find((query) => query.error)?.error}
          onRetry={() => void Promise.all(queries.map((query) => query.refetch()))}
        />
      </>
    );
  }

  const editable = shift.data.status === "open";
  const customerName = (id: string) => customers.data?.find((customer) => customer.id === id)?.name ?? "Unknown customer";

  return (
    <>
      <ShiftRowsFrame
        title="Repayments"
        shift={shift.data}
        totalLabel="Repaid this shift"
        total={page.data.total}
        extra={
          <div className="mt-3">
            <ListRow label="Of which cash" value={<Amount value={page.data.cash_total} />} />
          </div>
        }
        explanation="Only the cash figure reaches the drawer. A bank transfer is recorded but never added to expected cash."
        onAdd={() => setAction({ kind: "edit", repayment: null })}
        emptyText="No repayments recorded for this shift."
        count={page.data.items.length}
        truncated={page.data.truncated}
      >
        {page.data.items.map((repayment) => (
          <MoneyRowCard
            key={repayment.id}
            title={customerName(repayment.credit_customer_id)}
            caption={repayment.mode.replace("_", " ")}
            amount={repayment.amount}
            badges={<ReversalBadge row={repayment} />}
            notes={repayment.reversal_reason ? <p className="mt-2 text-[0.8125rem] text-ink-muted">Reason: {repayment.reversal_reason}</p> : null}
            actions={
              isLive(repayment) ? (
                <>
                  {editable ? (
                    <Button size="sm" onClick={() => setAction({ kind: "edit", repayment })}>
                      Edit
                    </Button>
                  ) : null}
                  {satisfies(me.role, "manager") ? (
                    <Button size="sm" variant="danger" onClick={() => setAction({ kind: "reverse", repayment })}>
                      Reverse
                    </Button>
                  ) : null}
                </>
              ) : null
            }
          />
        ))}
      </ShiftRowsFrame>

      <Sheet
        open={action !== null}
        onClose={() => setAction(null)}
        title={action?.kind === "reverse" ? "Reverse repayment" : action?.repayment ? "Edit repayment" : "Record a repayment"}
      >
        {action?.kind === "edit" ? (
          <RepaymentForm shiftId={shiftId} existing={action.repayment} customers={customers.data} onDone={() => setAction(null)} />
        ) : null}
        {action?.kind === "reverse" ? (
          <ReversalForm
            path={`/shifts/${shiftId}/credit-repayments/${action.repayment.id}/reversals`}
            amount={action.repayment.amount}
            description={customerName(action.repayment.credit_customer_id)}
            onDone={() => setAction(null)}
          />
        ) : null}
      </Sheet>
    </>
  );
}

function RepaymentForm({
  shiftId,
  existing,
  customers,
  onDone,
}: {
  shiftId: string;
  existing: Repayment | null;
  customers: Customer[];
  onDone: () => void;
}) {
  const submission = useSubmission("POST", `/shifts/${shiftId}/credit-repayments`);
  const refresh = useRefreshApi();
  const form = useForm({
    credit_customer_id: existing?.credit_customer_id ?? "",
    amount: existing?.amount ?? "",
    mode: existing?.mode ?? "",
  });
  const [attachmentId, setAttachmentId] = useState<string | null>(existing?.attachment_id ?? null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    form.clearErrors();
    const values = form.values;
    if (!values.credit_customer_id) return form.setError("credit_customer_id", "Choose a customer.");
    if (!values.mode) return form.setError("mode", "Choose how the money arrived.");

    setBusy(true);
    try {
      if (existing) {
        // CreditRepaymentUpdate accepts amount and mode only.
        const { credit_customer_id: _ignored, ...changes } = form.changes();
        if (Object.keys(changes).length === 0) return onDone();
        await api.patch(`/shifts/${shiftId}/credit-repayments/${existing.id}`, changes);
      } else {
        const body: Schemas["CreditRepaymentCreate"] = {
          credit_customer_id: values.credit_customer_id,
          amount: values.amount,
          mode: values.mode as Schemas["CreditRepaymentCreate"]["mode"],
        };
        if (attachmentId) body.attachment_id = attachmentId;
        await submission.run(body);
      }
      onDone();
      notify.success(existing ? "Updated." : "Repayment recorded.");
      await refresh();
    } catch (error) {
      reportFailure(error, form, () => void submit());
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {existing ? (
        <p className="text-[0.9375rem] text-ink">
          {customers.find((customer) => customer.id === existing.credit_customer_id)?.name ?? "Unknown customer"}
        </p>
      ) : (
        <SelectField
          form={form}
          name="credit_customer_id"
          label="Customer"
          options={[
            { value: "", label: "Choose…" },
            ...customers.map((customer) => ({
              value: customer.id,
              label: customer.is_active ? customer.name : `${customer.name} (deactivated)`,
            })),
          ]}
          hint="A deactivated customer can still pay off what they owe."
        />
      )}
      <TextField
        form={form}
        name="amount"
        label="Amount"
        inputMode="decimal"
        required
        // §6.6: more than outstanding is accepted; the balance goes negative.
        hint="More than they owe is accepted. The balance simply goes negative."
      />
      <SelectField
        form={form}
        name="mode"
        label="How did it arrive?"
        options={[{ value: "", label: "Choose…" }, ...REPAYMENT_MODES]}
        hint="Only cash reaches the locker and the shift's cash position."
      />
      {existing ? (
        <p className="text-[0.8125rem] text-ink-muted">The receipt cannot be changed.</p>
      ) : (
        <ReceiptUpload shiftId={shiftId} attachmentId={attachmentId} label="Receipt (optional)" onUploaded={setAttachmentId} />
      )}
      <Button variant="primary" block disabled={busy} onClick={() => void submit()}>
        {busy ? "Saving…" : existing ? "Save" : "Record repayment"}
      </Button>
    </div>
  );
}

/* --- Repayments that arrived at the bank (§5.2, Phase 16) ------------------------------
 *
 * The Credit tab's write screen, and the one that makes a ledger reconstructable: a transfer
 * against a day already locked, or on a day the outlet was shut, has no shift to hang off.
 *
 * Cash is deliberately absent from this form. Cash lands in a drawer, so it belongs to the
 * shift it arrived on; the server refuses `mode = cash` here with 422 and the database refuses
 * it too, so omitting it is a courtesy rather than the control (§8).
 */

const BANK_MODES = [
  { value: "bank_transfer", label: "Bank transfer" },
  { value: "upi", label: "UPI, to a bank account, not the pump's QR" },
  { value: "card", label: "Card, not on the pump's machine" },
];

type BankAction = { kind: "add" } | { kind: "reverse"; repayment: Repayment };

export function LedgerRepaymentsScreen() {
  const page = useApiQuery<Schemas["DatedCreditRepaymentPage"]>("/credit-repayments", { limit: 25 });
  const customers = useApiQuery<Customer[]>("/credit-customers", { include_inactive: true });
  const [action, setAction] = useState<BankAction | null>(null);

  if (page.isPending || customers.isPending) {
    return (
      <>
        <ScreenTitle title="Payments received" />
        <Skeleton rows={3} />
      </>
    );
  }
  if (!page.data || !customers.data) {
    return (
      <>
        <ScreenTitle title="Payments received" />
        <ErrorCard error={page.error ?? customers.error} onRetry={() => void Promise.all([page.refetch(), customers.refetch()])} />
      </>
    );
  }
  const customerName = (id: string) => customers.data?.find((customer) => customer.id === id)?.name ?? "Unknown customer";

  return (
    <>
      <ScreenTitle title="Payments received" />
      <ScreenActions>
        <Button variant="primary" size="sm" icon={<PlusIcon size={16} weight="bold" aria-hidden />} onClick={() => setAction({ kind: "add" })}>
          Add
        </Button>
      </ScreenActions>
      <div className="flex flex-col gap-5">
        <Card>
          <p className="text-[0.8125rem] font-medium text-ink-muted">Money that reached the bank</p>
          <p className="mt-1 text-[0.9375rem] text-ink">
            A settlement that arrived by transfer rather than at the pump. It reduces what the customer owes and changes no day's cash: the locker never saw it.
          </p>
          <p className="mt-2 text-[0.8125rem] text-ink-muted">Cash belongs on the shift it arrived on. Enter that under Entry, Credit repayments.</p>
        </Card>
        {page.data.items.length ? (
          <div className="flex flex-col gap-3">
            {page.data.items.map((repayment) => (
              <MoneyRowCard
                key={repayment.id}
                title={customerName(repayment.credit_customer_id)}
                caption={`${businessDate(repayment.business_date)} · ${repayment.mode.replace("_", " ")}${repayment.shift_id === null ? "" : " · on a shift"}`}
                amount={repayment.amount}
                badges={<ReversalBadge row={repayment} />}
                notes={repayment.reversal_reason ? <p className="mt-2 text-[0.8125rem] text-ink-muted">Reason: {repayment.reversal_reason}</p> : null}
                actions={
                  // §6.9: a shift-less row cannot reach the shift-scoped reversal route, so it has
                  // its own. Without it a mistyped transfer would sit in a ledger permanently.
                  isLive(repayment) && repayment.shift_id === null ? (
                    <Button size="sm" variant="danger" onClick={() => setAction({ kind: "reverse", repayment })}>
                      Reverse
                    </Button>
                  ) : null
                }
              />
            ))}
          </div>
        ) : (
          <Empty>Nothing recorded yet.</Empty>
        )}
      </div>

      <Sheet open={action !== null} onClose={() => setAction(null)} title={action?.kind === "reverse" ? "Reverse payment" : "Record a bank payment"}>
        {action?.kind === "add" ? <BankRepaymentForm customers={customers.data} onDone={() => setAction(null)} /> : null}
        {action?.kind === "reverse" ? (
          <ReversalForm
            path={`/credit-repayments/${action.repayment.id}/reversals`}
            amount={action.repayment.amount}
            description={customerName(action.repayment.credit_customer_id)}
            onDone={() => setAction(null)}
          />
        ) : null}
      </Sheet>
    </>
  );
}

function BankRepaymentForm({ customers, onDone }: { customers: Customer[]; onDone: () => void }) {
  const submission = useSubmission("POST", "/credit-repayments");
  const refresh = useRefreshApi();
  const form = useForm({ credit_customer_id: "", amount: "", mode: "", business_date: todayAtOutlet() });
  const [busy, setBusy] = useState(false);

  async function submit() {
    form.clearErrors();
    const values = form.values;
    if (!values.credit_customer_id) return form.setError("credit_customer_id", "Choose a customer.");
    if (!values.mode) return form.setError("mode", "Choose how the money arrived.");
    setBusy(true);
    try {
      const body: Schemas["DatedCreditRepaymentCreate"] = {
        credit_customer_id: values.credit_customer_id,
        amount: values.amount,
        mode: values.mode as Schemas["DatedCreditRepaymentCreate"]["mode"],
        business_date: values.business_date,
      };
      await submission.run(body);
      onDone();
      notify.success("Payment recorded.");
      await refresh();
    } catch (error) {
      reportFailure(error, form, () => void submit());
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <SelectField
        form={form}
        name="credit_customer_id"
        label="Customer"
        options={[
          { value: "", label: "Choose…" },
          ...customers.map((customer) => ({ value: customer.id, label: customer.is_active ? customer.name : `${customer.name} (deactivated)` })),
        ]}
        hint="A deactivated customer can still pay off what they owe."
      />
      <TextField form={form} name="amount" label="Amount" inputMode="decimal" required hint="More than they owe is accepted. The balance simply goes negative." />
      <SelectField
        form={form}
        name="mode"
        label="How did it arrive?"
        options={[{ value: "", label: "Choose…" }, ...BANK_MODES]}
        hint="None of these touch the locker, so no day's cash position changes."
      />
      <TextField
        form={form}
        name="business_date"
        label="Date it arrived"
        type="date"
        required
        hint="The day the money reached the account, not the day you are typing this in."
      />
      <Button variant="primary" block disabled={busy} onClick={() => void submit()}>
        {busy ? "Saving…" : "Record payment"}
      </Button>
    </div>
  );
}
