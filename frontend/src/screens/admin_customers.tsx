/* Credit customers: who may take udhaar, and for how much (CLAUDE.md §5.1, §6.6). Rebuilt in
 * Phase 23 from admin_customers.js.
 *
 * ## credit_limit: null means NO limit, never zero
 *
 * §14: "coercing it refuses every sale to the customers who are trusted most." The field is
 * left genuinely empty rather than defaulted, an empty limit renders as the words "no limit",
 * and on this router an explicit null in a PATCH CLEARS the column -- which is exactly what an
 * emptied field means, so it is sent on purpose and only when the field was edited.
 *
 * ## Deactivation is asymmetric, deliberately
 *
 * §5.1: a deactivated customer refuses a new credit sale but still accepts a repayment. Said on
 * the toggle, because it reads as a bug otherwise.
 *
 * ## The phone is the natural key, not the name
 *
 * Names collide ("three customers called Ramesh"), and two rows for one person split one
 * balance so §6.6's limit never fires. Hence 409 CREDIT_CUSTOMER_PHONE_EXISTS, and phone
 * required.
 */

import { useState } from "react";
import { api } from "../api/client";
import { useApiQuery } from "../api/queries";
import type { Schemas } from "../api/types";
import { isNegative, isZero } from "../lib/money";
import { Amount } from "../ui/Amount";
import { CheckboxField, TextField, useForm } from "../ui/form";
import { Button, Empty, ListRow, Pill } from "../ui/primitives";
import { Sheet } from "../ui/Sheet";
import { ActivePill, AddAction, AdminCard, AdminGrid, AdminList, type Editing, Fields, SaveButton, useSave } from "./admin";
import { useGo } from "../app/navigation";

type Customer = Schemas["CreditCustomerResponse"];

export function CustomersScreen() {
  const navigate = useGo();
  // The manager-floor report, which carries phone, limit and balance -- unlike the lean list
  // every role can read (§8).
  const customers = useApiQuery<Customer[]>("/credit-customers/outstanding", { include_settled: true });
  const [editing, setEditing] = useState<Editing<Customer>>(null);

  return (
    <>
      <AddAction onClick={() => setEditing({ row: null })} />
      <AdminList title="Credit customers" query={customers}>
        {(rows) =>
          rows.length ? (
            <AdminGrid>
              {rows.map((customer) => {
                // §6.6: outstanding may be negative -- paid in advance -- and says so in words.
                const inCredit = isNegative(customer.outstanding) && !isZero(customer.outstanding);
                return (
                  <AdminCard
                    key={customer.id}
                    title={customer.name}
                    caption={customer.phone}
                    status={<ActivePill active={customer.is_active} inactiveLabel="deactivated" />}
                    onEdit={() => setEditing({ row: customer })}
                    extra={
                      <Button size="sm" variant="plain" onClick={() => navigate(`/credit/customers/${customer.id}`)}>
                        Ledger
                      </Button>
                    }
                  >
                    {customer.vehicle_numbers?.length ? (
                      <div className="mb-1 flex flex-wrap gap-1.5">
                        {customer.vehicle_numbers.map((vehicle) => (
                          <Pill key={vehicle}>{vehicle}</Pill>
                        ))}
                      </div>
                    ) : null}
                    <ListRow
                      label="Outstanding"
                      value={
                        <>
                          <Amount value={customer.outstanding} />
                          {inCredit ? <span className="text-ink-muted"> · in credit</span> : null}
                        </>
                      }
                    />
                    <ListRow label="Credit limit" value={<Amount value={customer.credit_limit} absent="no limit" />} />
                  </AdminCard>
                );
              })}
            </AdminGrid>
          ) : (
            <Empty>No credit customers yet. Add the people who may take udhaar.</Empty>
          )
        }
      </AdminList>
      <Sheet open={editing !== null} onClose={() => setEditing(null)} title={editing?.row ? editing.row.name : "New credit customer"}>
        {editing ? <CustomerForm key={editing.row?.id ?? "new"} existing={editing.row} onDone={() => setEditing(null)} /> : null}
      </Sheet>
    </>
  );
}

/** Comma-separated vehicles as a list; an emptied field is null, which clears on this router. */
function vehicles(text: string): string[] | null {
  const parsed = text
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
  return parsed.length ? parsed : null;
}

function CustomerForm({ existing, onDone }: { existing: Customer | null; onDone: () => void }) {
  const { busy, save } = useSave();
  const form = useForm({
    name: existing?.name ?? "",
    phone: existing?.phone ?? "",
    vehicle_numbers: existing?.vehicle_numbers?.join(", ") ?? "",
    credit_limit: existing?.credit_limit ?? "",
    is_active: existing?.is_active ?? true,
  });

  function submit() {
    if (existing) {
      const changes = form.changes();
      const body: Schemas["CreditCustomerUpdate"] = {};
      if (changes.name !== undefined) body.name = changes.name;
      if (changes.phone !== undefined) body.phone = changes.phone;
      if (changes.is_active !== undefined) body.is_active = changes.is_active;
      if (changes.vehicle_numbers !== undefined) body.vehicle_numbers = vehicles(changes.vehicle_numbers);
      // Blank is NO limit, sent as null on purpose; never "" and never 0 (§6.6, §14).
      if (changes.credit_limit !== undefined) body.credit_limit = changes.credit_limit.trim() === "" ? null : changes.credit_limit;
      if (!Object.keys(body).length) return onDone();
      return void save(form, () => api.patch(`/credit-customers/${existing.id}`, body), onDone);
    }
    const body: Schemas["CreditCustomerCreate"] = {
      name: form.values.name,
      phone: form.values.phone,
      vehicle_numbers: vehicles(form.values.vehicle_numbers),
      credit_limit: form.values.credit_limit.trim() === "" ? null : form.values.credit_limit,
    };
    void save(form, () => api.post("/credit-customers", body), onDone, "Customer added.");
  }

  return (
    <Fields>
      <TextField form={form} name="name" label="Name" required />
      <TextField form={form} name="phone" label="Phone" type="tel" inputMode="tel" required hint="The unique key for a customer here: names collide, phone numbers do not." />
      <TextField form={form} name="vehicle_numbers" label="Vehicle numbers (optional)" hint="Comma-separated. Upper-cased and de-duplicated automatically." />
      <TextField
        form={form}
        name="credit_limit"
        label="Credit limit (optional)"
        inputMode="decimal"
        hint="Leave blank for no limit. Blank is not zero: a zero limit would refuse every sale."
      />
      {existing ? (
        <CheckboxField form={form} name="is_active" label="Active" hint="A deactivated customer cannot take new udhaar but can still repay what they owe." />
      ) : null}
      <SaveButton busy={busy} creating={!existing} onClick={submit} />
    </Fields>
  );
}
