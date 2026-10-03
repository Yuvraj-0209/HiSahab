/* Non-fuel sales: the money no meter counts (CLAUDE.md §5.2, §6.4). Rebuilt in Phase 23.
 *
 * A ₹500 bottle of oil is in the salesman's hand and not in the meter-derived figure, but it IS
 * in the cash he counts into the locker. Without this row he shows a ₹500 surplus every day he
 * sells one -- a phantom in his name. It is added to SALES, never to the cash side: a card-paid
 * oil sale is already inside the card collections total, so on the sales side the arithmetic is
 * right however it was paid. That is why this form never asks how it was paid.
 */

import { useState } from "react";
import { useParams } from "react-router";
import { api } from "../api/client";
import { useApiQuery, useRefreshApi } from "../api/queries";
import { useSubmission } from "../api/submission";
import type { Schemas } from "../api/types";
import { ScreenTitle } from "../app/chrome";
import { useSession } from "../app/session";
import { satisfies } from "../lib/roles";
import { reportFailure } from "../ui/feedback";
import { TextField, useForm } from "../ui/form";
import { Button, ErrorCard, Skeleton } from "../ui/primitives";
import { isLive, ReversalBadge, ReversalForm } from "../ui/reversal";
import { Sheet } from "../ui/Sheet";
import { notify } from "../ui/toast";
import { MoneyRowCard, ShiftRowsFrame } from "./shiftRows";

type Sale = Schemas["NonFuelSaleResponse"];
type Action = { kind: "edit"; sale: Sale | null } | { kind: "reverse"; sale: Sale };

export function NonFuelSalesScreen() {
  const { shiftId = "" } = useParams();
  const { me } = useSession();
  const page = useApiQuery<Schemas["NonFuelSalePage"]>(`/shifts/${shiftId}/non-fuel-sales`);
  const shift = useApiQuery<Schemas["ShiftResponse"]>(`/shifts/${shiftId}`);
  const [action, setAction] = useState<Action | null>(null);

  if (page.isPending || shift.isPending) {
    return (
      <>
        <ScreenTitle title="Non-fuel sales" />
        <Skeleton rows={3} />
      </>
    );
  }
  if (!page.data || !shift.data) {
    return (
      <>
        <ScreenTitle title="Non-fuel sales" />
        <ErrorCard error={page.error ?? shift.error} onRetry={() => void Promise.all([page.refetch(), shift.refetch()])} />
      </>
    );
  }
  const editable = shift.data.status === "open";

  return (
    <>
      <ShiftRowsFrame
        title="Non-fuel sales"
        shift={shift.data}
        totalLabel="Total this shift"
        total={page.data.total}
        explanation="Added to sales, not to cash. However it was paid, the collections rows already record that."
        onAdd={() => setAction({ kind: "edit", sale: null })}
        emptyText="Nothing recorded. If a bottle of oil was sold, it belongs here. Otherwise the salesman shows a surplus that is not real."
        count={page.data.items.length}
        truncated={page.data.truncated}
      >
        {page.data.items.map((sale) => (
          <MoneyRowCard
            key={sale.id}
            title={sale.description ?? "Non-fuel sale"}
            amount={sale.amount}
            badges={<ReversalBadge row={sale} />}
            notes={sale.reversal_reason ? <p className="mt-2 text-[0.8125rem] text-ink-muted">Reason: {sale.reversal_reason}</p> : null}
            actions={
              isLive(sale) ? (
                <>
                  {editable ? (
                    <Button size="sm" onClick={() => setAction({ kind: "edit", sale })}>
                      Edit
                    </Button>
                  ) : null}
                  {satisfies(me.role, "manager") ? (
                    <Button size="sm" variant="danger" onClick={() => setAction({ kind: "reverse", sale })}>
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
        title={action?.kind === "reverse" ? "Reverse non-fuel sale" : action?.sale ? "Edit non-fuel sale" : "Record a non-fuel sale"}
      >
        {action?.kind === "edit" ? <SaleForm shiftId={shiftId} existing={action.sale} onDone={() => setAction(null)} /> : null}
        {action?.kind === "reverse" ? (
          <ReversalForm
            path={`/shifts/${shiftId}/non-fuel-sales/${action.sale.id}/reversals`}
            amount={action.sale.amount}
            description={action.sale.description ?? "Non-fuel sale"}
            replacementText={{ name: "replacement_description", label: "Corrected description (optional)", value: action.sale.description }}
            onDone={() => setAction(null)}
          />
        ) : null}
      </Sheet>
    </>
  );
}

function SaleForm({ shiftId, existing, onDone }: { shiftId: string; existing: Sale | null; onDone: () => void }) {
  const submission = useSubmission("POST", `/shifts/${shiftId}/non-fuel-sales`);
  const refresh = useRefreshApi();
  const form = useForm({ amount: existing?.amount ?? "", description: existing?.description ?? "" });
  const [busy, setBusy] = useState(false);

  async function submit() {
    form.clearErrors();
    setBusy(true);
    try {
      if (existing) {
        const changes = form.changes();
        if (Object.keys(changes).length === 0) return onDone();
        await api.patch(`/shifts/${shiftId}/non-fuel-sales/${existing.id}`, changes);
      } else {
        const body: Schemas["NonFuelSaleCreate"] = { amount: form.values.amount };
        if (form.values.description) body.description = form.values.description;
        await submission.run(body);
      }
      onDone();
      notify.success(existing ? "Updated." : "Recorded.");
      await refresh();
    } catch (error) {
      reportFailure(error, form, () => void submit());
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <TextField form={form} name="amount" label="Amount" inputMode="decimal" required />
      <TextField
        form={form}
        name="description"
        label="What was sold (optional)"
        hint="An amount and a note. There is no product catalogue and no stock in V1."
      />
      <Button variant="primary" block disabled={busy} onClick={() => void submit()}>
        {busy ? "Saving…" : existing ? "Save" : "Record sale"}
      </Button>
    </div>
  );
}
