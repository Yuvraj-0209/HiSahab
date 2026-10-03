/* Udhaar issued (CLAUDE.md §5.2, §6.6). Rebuilt in Phase 23 from credit_sales.js.
 *
 * ## The receipt is not optional here
 *
 * `credit_sales.attachment_id` is NOT NULL at the database ("a client can bypass JavaScript, but
 * not a database constraint"). Save stays disabled until a receipt has actually uploaded -- not
 * as enforcement, which is the server's, but because filling in a whole form and then meeting a
 * 422 is a bad way to learn a rule that was never going to bend.
 *
 * ## The credit limit
 *
 * `outstanding + amount > credit_limit` is refused with 409 by the SERVER; this screen never
 * computes it, and never reads a null limit as zero (§6.6). An admin may override with a reason
 * that lands on the row as well as the audit log; anyone else supplying one is 403.
 *
 * ## The customer list is the lean one, for every role
 *
 * `GET /credit-customers` returns names and vehicles only -- no phone, no limit, no balance --
 * and a deactivated customer refuses a NEW sale (409 CREDIT_CUSTOMER_INACTIVE) but still accepts
 * a repayment (§5.1). So inactive customers are absent here and present on the repayment screen.
 */

import { useState } from "react";
import { useParams } from "react-router";
import { api } from "../api/client";
import { useApiQuery, useRefreshApi } from "../api/queries";
import { useSubmission } from "../api/submission";
import type { Schemas } from "../api/types";
import { ScreenTitle } from "../app/chrome";
import { useSession } from "../app/session";
import { quantity } from "../lib/money";
import { satisfies } from "../lib/roles";
import { reportFailure } from "../ui/feedback";
import { SelectField, TextField, useForm } from "../ui/form";
import { Button, ErrorCard, Pill, Skeleton } from "../ui/primitives";
import { ReceiptButton } from "../ui/receipt";
import { ReceiptUpload } from "../ui/ReceiptUpload";
import { isLive, ReversalBadge, ReversalForm } from "../ui/reversal";
import { Sheet } from "../ui/Sheet";
import { notify } from "../ui/toast";
import { ApiError, explain } from "../api/client";
import { MoneyRowCard, ShiftRowsFrame } from "./shiftRows";

type Sale = Schemas["CreditSaleResponse"];
type Customer = Schemas["CreditCustomerListItem"];
type FuelType = Schemas["FuelTypeResponse"];
type Action = { kind: "edit"; sale: Sale | null } | { kind: "reverse"; sale: Sale };

export function CreditSalesScreen() {
  const { shiftId = "" } = useParams();
  const { me } = useSession();
  const page = useApiQuery<Schemas["CreditSalePage"]>(`/shifts/${shiftId}/credit-sales`);
  const shift = useApiQuery<Schemas["ShiftResponse"]>(`/shifts/${shiftId}`);
  const customers = useApiQuery<Customer[]>("/credit-customers");
  const fuelTypes = useApiQuery<FuelType[]>("/fuel-types");
  const [action, setAction] = useState<Action | null>(null);

  const queries = [page, shift, customers, fuelTypes];
  if (queries.some((query) => query.isPending)) {
    return (
      <>
        <ScreenTitle title="Credit sales" />
        <Skeleton rows={3} />
      </>
    );
  }
  if (!page.data || !shift.data || !customers.data || !fuelTypes.data) {
    return (
      <>
        <ScreenTitle title="Credit sales" />
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
        title="Credit sales"
        shift={shift.data}
        totalLabel="Udhaar issued this shift"
        total={page.data.total}
        explanation="Subtracted from what the salesman should be holding: the fuel left, the cash did not."
        onAdd={() => setAction({ kind: "edit", sale: null })}
        emptyText="No credit sales recorded for this shift."
        count={page.data.items.length}
        truncated={page.data.truncated}
      >
        {page.data.items.map((sale) => {
          const fuel = fuelTypes.data?.find((type) => type.id === sale.fuel_type_id);
          return (
            <MoneyRowCard
              key={sale.id}
              title={customerName(sale.credit_customer_id)}
              caption={[
                fuel?.code,
                // §4.5: the unit comes from the fuel type; a CBG udhaar is in kilograms.
                sale.quantity !== null && fuel ? quantity(sale.quantity, fuel.unit_of_measure) : null,
                sale.vehicle_number,
              ]
                .filter(Boolean)
                .join(" · ")}
              amount={sale.amount}
              badges={<ReversalBadge row={sale} />}
              notes={
                <>
                  {sale.limit_override_reason ? (
                    <div className="mt-2 flex flex-col items-start gap-1">
                      <Pill kind="review">limit overridden</Pill>
                      <p className="text-[0.8125rem] text-ink-muted">{sale.limit_override_reason}</p>
                    </div>
                  ) : null}
                  {sale.reversal_reason ? <p className="mt-2 text-[0.8125rem] text-ink-muted">Reason: {sale.reversal_reason}</p> : null}
                </>
              }
              actions={
                <>
                  <ReceiptButton attachmentId={sale.attachment_id} />
                  {editable && isLive(sale) ? (
                    <Button size="sm" onClick={() => setAction({ kind: "edit", sale })}>
                      Edit
                    </Button>
                  ) : null}
                  {isLive(sale) && satisfies(me.role, "manager") ? (
                    <Button size="sm" variant="danger" onClick={() => setAction({ kind: "reverse", sale })}>
                      Reverse
                    </Button>
                  ) : null}
                </>
              }
            />
          );
        })}
      </ShiftRowsFrame>

      <Sheet
        open={action !== null}
        onClose={() => setAction(null)}
        title={action?.kind === "reverse" ? "Reverse credit sale" : action?.sale ? "Edit credit sale" : "Record udhaar"}
      >
        {action?.kind === "edit" ? (
          <SaleForm
            shiftId={shiftId}
            existing={action.sale}
            customers={customers.data}
            fuelTypes={fuelTypes.data}
            onDone={() => setAction(null)}
          />
        ) : null}
        {action?.kind === "reverse" ? (
          <ReversalForm
            path={`/shifts/${shiftId}/credit-sales/${action.sale.id}/reversals`}
            amount={action.sale.amount}
            // §6.11 / §5.3: no receipt is asked for; the reversal inherits the original's.
            description={`${customerName(action.sale.credit_customer_id)}. The receipt carries over, no re-upload needed.`}
            onDone={() => setAction(null)}
          />
        ) : null}
      </Sheet>
    </>
  );
}

function SaleForm({
  shiftId,
  existing,
  customers,
  fuelTypes,
  onDone,
}: {
  shiftId: string;
  existing: Sale | null;
  customers: Customer[];
  fuelTypes: FuelType[];
  onDone: () => void;
}) {
  const { me } = useSession();
  const isAdmin = satisfies(me.role, "admin");
  const submission = useSubmission("POST", `/shifts/${shiftId}/credit-sales`);
  const refresh = useRefreshApi();
  const form = useForm({
    credit_customer_id: existing?.credit_customer_id ?? "",
    amount: existing?.amount ?? "",
    fuel_type_id: existing?.fuel_type_id ?? "",
    quantity: existing?.quantity ?? "",
    vehicle_number: existing?.vehicle_number ?? "",
    limit_override_reason: "",
  });
  const [attachmentId, setAttachmentId] = useState<string | null>(existing?.attachment_id ?? null);
  const [busy, setBusy] = useState(false);
  const active = customers.filter((customer) => customer.is_active);

  async function submit() {
    form.clearErrors();
    const values = form.values;
    if (!existing && !values.credit_customer_id) return form.setError("credit_customer_id", "Choose a customer.");
    if (values.quantity && !values.fuel_type_id) {
      return form.setError("quantity", "A quantity needs a fuel type: a measure with no unit is meaningless.");
    }

    setBusy(true);
    try {
      if (existing) {
        // CreditSaleUpdate takes amount, quantity and vehicle number only. On this router an
        // explicit null CLEARS quantity and vehicle_number, so only edited fields are sent.
        const all = form.changes();
        const changes: Record<string, unknown> = {};
        for (const key of ["amount", "quantity", "vehicle_number"] as const) {
          if (key in all) changes[key] = all[key];
        }
        if (Object.keys(changes).length === 0) return onDone();
        await api.patch(`/shifts/${shiftId}/credit-sales/${existing.id}`, changes);
      } else {
        const body: Schemas["CreditSaleCreate"] = {
          credit_customer_id: values.credit_customer_id,
          amount: values.amount,
          attachment_id: attachmentId ?? "",
        };
        if (values.fuel_type_id) body.fuel_type_id = values.fuel_type_id;
        if (values.quantity) body.quantity = values.quantity;
        if (values.vehicle_number) body.vehicle_number = values.vehicle_number;
        if (values.limit_override_reason) body.limit_override_reason = values.limit_override_reason;
        await submission.run(body);
      }
      onDone();
      notify.success(existing ? "Updated." : "Credit sale recorded.");
      await refresh();
    } catch (error) {
      if (error instanceof ApiError && error.code === "CREDIT_LIMIT_EXCEEDED" && isAdmin) {
        notify.error(explain(error), {
          requestId: error.requestId,
          detail: "As an admin you can override this by entering a reason.",
        });
      } else {
        reportFailure(error, form, () => void submit());
      }
    } finally {
      setBusy(false);
    }
  }

  const needsReceipt = !existing && !attachmentId;

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
            ...active.map((customer) => ({
              value: customer.id,
              label: customer.vehicle_numbers?.length ? `${customer.name} (${customer.vehicle_numbers.join(", ")})` : customer.name,
            })),
          ]}
          hint="Only active customers can take new udhaar. A deactivated one can still repay."
        />
      )}
      <TextField form={form} name="amount" label="Amount" inputMode="decimal" required />
      {existing ? null : (
        <SelectField
          form={form}
          name="fuel_type_id"
          label="Fuel (optional)"
          options={[{ value: "", label: "Not a fuel sale" }, ...fuelTypes.map((type) => ({ value: type.id, label: type.display_name }))]}
          hint="Leave empty for a non-fuel credit sale."
        />
      )}
      <TextField
        form={form}
        name="quantity"
        label="Quantity (optional)"
        inputMode="decimal"
        hint="Leave blank to record only the amount. A quantity needs a fuel type, because it needs a unit."
      />
      <TextField form={form} name="vehicle_number" label="Vehicle number (optional)" />
      {isAdmin && !existing ? (
        <TextField
          form={form}
          name="limit_override_reason"
          label="Credit limit override reason (admin)"
          hint="Only if the sale needs to go past the customer's limit. Stored on the row, not just in the audit log."
        />
      ) : null}
      {existing ? (
        <p className="text-[0.8125rem] text-ink-muted">The receipt cannot be changed. Correct this sale with a reversal instead.</p>
      ) : (
        <ReceiptUpload shiftId={shiftId} attachmentId={attachmentId} label="Receipt (required)" onUploaded={setAttachmentId} />
      )}
      <Button
        variant="primary"
        block
        disabled={busy || needsReceipt}
        reason={needsReceipt ? "A receipt is required before this can be saved." : undefined}
        onClick={() => void submit()}
      >
        {busy ? "Saving…" : existing ? "Save" : "Record credit sale"}
      </Button>
    </div>
  );
}
