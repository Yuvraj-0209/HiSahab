/* Admin: the reference data every money rule reads from (CLAUDE.md §5.1, §8, §11). Rebuilt in
 * Phase 23 from admin.js.
 *
 * Three of these tables gate money rules directly:
 *
 *   expense_categories.requires_receipt     the knob §6.11 exists to give the admin
 *   credit_customers.credit_limit           decides what §6.6 refuses
 *   outlet_shift_templates.starts_at_local  supplies the instant §6.3 prices a whole shift from
 *
 * Every write is audit-logged server-side (Phase 11), so these screens can be plain about
 * consequences: an admin changing a limit is making a decision with their name on it.
 *
 * ## Two rules this whole section is built around
 *
 * **Nothing is deleted.** §3 rule 6. Every reference table retires a row with `is_active`, so
 * there is no delete button anywhere: only Active, which refuses new rows while history keeps
 * reading and reporting.
 *
 * **Codes are immutable.** `fuel_types.code`, `fuel_types.unit_of_measure` and
 * `expense_categories.code` cannot change (§5.1), and the API refuses them with 422. The edit
 * forms do not offer them at all rather than offering a field that would be rejected.
 *
 * ## A PATCH sends only what changed
 *
 * `form.changes()`. On some routers an explicit null CLEARS a column, so "I did not touch this"
 * must never be sent as a value (ui/form.tsx).
 */

import { type ReactNode, useState } from "react";
import {
  AddressBookIcon,
  BankIcon,
  ClockCounterClockwiseIcon,
  ClockIcon,
  CurrencyInrIcon,
  GasPumpIcon,
  GaugeIcon,
  PercentIcon,
  PlusIcon,
  TagIcon,
  UserGearIcon,
} from "@phosphor-icons/react";
import { api } from "../api/client";
import { useApiQuery, useRefreshApi } from "../api/queries";
import type { Schemas } from "../api/types";
import { ScreenActions, ScreenTitle } from "../app/chrome";
import { useGo } from "../app/navigation";
import { quantity, reading } from "../lib/money";
import { localTime, toOffsetISO } from "../lib/time";
import { reportFailure } from "../ui/feedback";
import { CheckboxField, type FormState, type FormValues, SelectField, TextField, useForm } from "../ui/form";
import { Button, Card, Empty, ErrorCard, LinkTile, ListRow, Notice, Pill, Skeleton } from "../ui/primitives";
import { Sheet } from "../ui/Sheet";
import { notify } from "../ui/toast";

/* --- the hub --------------------------------------------------------------------------------- */

const SECTIONS: { label: string; hint: string; route: string; Icon: typeof GasPumpIcon }[] = [
  { label: "Fuel types", hint: "Products sold, and the unit each is measured in", route: "/admin/fuel-types", Icon: GasPumpIcon },
  { label: "Nozzles", hint: "Meters, their fuel, and the rollover ceiling", route: "/admin/nozzles", Icon: GaugeIcon },
  { label: "Prices", hint: "Effective-dated rates, append-only", route: "/admin/prices", Icon: CurrencyInrIcon },
  { label: "Margins", hint: "Dealer commission per unit, append-only", route: "/admin/margins", Icon: PercentIcon },
  { label: "Expense categories", hint: "What an expense is filed under, and which need a receipt", route: "/admin/categories", Icon: TagIcon },
  { label: "Credit customers", hint: "Who may take udhaar, and their limit", route: "/admin/customers", Icon: AddressBookIcon },
  { label: "Shift templates", hint: "The hours this outlet usually trades", route: "/admin/shift-templates", Icon: ClockIcon },
  { label: "Bank accounts", hint: "What statements are imported against", route: "/admin/bank-accounts", Icon: BankIcon },
  { label: "Users", hint: "Who may sign in, and what they may do", route: "/admin/users", Icon: UserGearIcon },
  { label: "Audit log", hint: "Who changed what, and what it was before", route: "/admin/audit", Icon: ClockCounterClockwiseIcon },
];

export function AdminHubScreen() {
  const navigate = useGo();
  return (
    <>
      <ScreenTitle large title="Admin" />
      <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
        {SECTIONS.map(({ label, hint, route, Icon }) => (
          <LinkTile key={route} arrive Icon={Icon} label={label} hint={hint} onClick={() => navigate(route)} />
        ))}
      </div>
      <p className="mt-4 text-footnote text-ink-muted">
        Nothing here can be deleted. A row no longer used is deactivated: it refuses new entries while everything historical keeps reading and reporting.
      </p>
    </>
  );
}

/* --- shared pieces ------------------------------------------------------------------------- */

export function ActivePill({ active, inactiveLabel = "inactive" }: { active: boolean; inactiveLabel?: string }) {
  return active ? <Pill kind="open">active</Pill> : <Pill kind="neutral">{inactiveLabel}</Pill>;
}

/** The Add button every list screen carries in its header. */
export function AddAction({ onClick }: { onClick: () => void }) {
  return (
    <ScreenActions>
      <Button size="sm" variant="primary" icon={<PlusIcon size={16} weight="bold" aria-hidden />} onClick={onClick}>
        Add
      </Button>
    </ScreenActions>
  );
}

/** Loading, failure and the list itself, in the shape every admin screen shares. */
export function AdminList<T>({
  title,
  query,
  children,
}: {
  title: string;
  query: { data: T | null | undefined; isPending: boolean; isError: boolean; error: unknown; refetch: () => unknown };
  children: (data: T) => ReactNode;
}) {
  return (
    <>
      <ScreenTitle title={title} />
      {query.isPending ? (
        <Skeleton rows={3} />
      ) : query.isError || query.data === undefined || query.data === null ? (
        <ErrorCard error={query.error} onRetry={() => void query.refetch()} />
      ) : (
        children(query.data)
      )}
    </>
  );
}

/** One row of reference data: a name, a code, its state, a few facts and an Edit button. */
export function AdminCard({
  title,
  caption,
  status,
  children,
  onEdit,
  extra,
}: {
  title: string;
  caption?: string | undefined;
  status: ReactNode;
  children?: ReactNode;
  onEdit: () => void;
  extra?: ReactNode;
}) {
  return (
    <div data-arrive>
      <Card>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="truncate text-lead font-semibold text-ink">{title}</h2>
            {caption ? <p className="text-footnote text-ink-muted">{caption}</p> : null}
          </div>
          <div className="flex shrink-0 flex-wrap justify-end gap-1.5">{status}</div>
        </div>
        {children ? <div className="mt-2">{children}</div> : null}
        <div className="mt-3 flex gap-2">
          <Button size="sm" block onClick={onEdit}>
            Edit
          </Button>
          {extra}
        </div>
      </Card>
    </div>
  );
}

export function AdminGrid({ children }: { children: ReactNode }) {
  return <div className="grid grid-cols-1 gap-3 md:grid-cols-2">{children}</div>;
}

/** Save a sheet's form: clear old errors, write, close, refresh every cached read. A 422 lands
 * on the fields it names (ui/feedback.ts). */
export function useSave() {
  const refresh = useRefreshApi();
  const [busy, setBusy] = useState(false);
  async function save<T extends FormValues>(form: FormState<T>, write: () => Promise<unknown>, onDone: () => void, message = "Saved.") {
    form.clearErrors();
    setBusy(true);
    try {
      await write();
      onDone();
      notify.success(message);
      await refresh();
    } catch (error) {
      reportFailure(error, form);
    } finally {
      setBusy(false);
    }
  }
  return { busy, save };
}

/** An edit-or-create sheet's state: closed, a new row, or an existing one. */
export type Editing<T> = { row: T | null } | null;

export function SaveButton({ busy, creating, onClick, label }: { busy: boolean; creating: boolean; onClick: () => void; label?: string }) {
  return (
    <Button variant="primary" block disabled={busy} onClick={onClick}>
      {busy ? "Saving…" : (label ?? (creating ? "Create" : "Save"))}
    </Button>
  );
}

/** A sheet's fields, stacked. Never a native <form>: a native post under hash routing reaches
 * the static mount, and the CSP's form-action 'none' blocks it anyway. */
export function Fields({ children }: { children: ReactNode }) {
  return <div className="flex flex-col gap-4">{children}</div>;
}

/* --- fuel types ---------------------------------------------------------------------------- */

type FuelType = Schemas["FuelTypeResponse"];

export function FuelTypesScreen() {
  const types = useApiQuery<FuelType[]>("/fuel-types", { include_inactive: true });
  const [editing, setEditing] = useState<Editing<FuelType>>(null);

  return (
    <>
      <AddAction onClick={() => setEditing({ row: null })} />
      <AdminList title="Fuel types" query={types}>
        {(rows) => (
          <div className="flex flex-col gap-3">
            <p className="text-footnote text-ink-muted">A fuel's code and unit can never change: a fuel that is genuinely different is a new row. Everything else is editable.</p>
            {rows.length ? (
              <AdminGrid>
                {rows.map((type) => (
                  <AdminCard key={type.id} title={type.display_name} caption={type.code} status={<ActivePill active={type.is_active} />} onEdit={() => setEditing({ row: type })}>
                    <ListRow label="Measured in" value={type.unit_of_measure === "kilogram" ? "kilograms" : "litres"} />
                    <ListRow label="Most per minute" value={quantity(type.max_flow_rate_per_minute, type.unit_of_measure)} />
                  </AdminCard>
                ))}
              </AdminGrid>
            ) : (
              <Empty>No fuel types yet. Add each product the outlet sells.</Empty>
            )}
          </div>
        )}
      </AdminList>
      <Sheet open={editing !== null} onClose={() => setEditing(null)} title={editing?.row ? editing.row.code : "New fuel type"}>
        {editing ? <FuelTypeForm key={editing.row?.id ?? "new"} existing={editing.row} onDone={() => setEditing(null)} /> : null}
      </Sheet>
    </>
  );
}

function FuelTypeForm({ existing, onDone }: { existing: FuelType | null; onDone: () => void }) {
  const { busy, save } = useSave();
  const form = useForm({
    code: "",
    unit_of_measure: "",
    display_name: existing?.display_name ?? "",
    max_flow_rate_per_minute: existing?.max_flow_rate_per_minute ?? "",
    is_active: existing?.is_active ?? true,
  });

  function submit() {
    if (existing) {
      const { display_name, max_flow_rate_per_minute, is_active } = form.changes();
      const body: Schemas["FuelTypeUpdate"] = {};
      if (display_name !== undefined) body.display_name = display_name;
      if (max_flow_rate_per_minute !== undefined) body.max_flow_rate_per_minute = max_flow_rate_per_minute;
      if (is_active !== undefined) body.is_active = is_active;
      if (!Object.keys(body).length) return onDone();
      return void save(form, () => api.patch(`/fuel-types/${existing.id}`, body), onDone);
    }
    if (!form.values.unit_of_measure) return form.setError("unit_of_measure", "Choose how this fuel is measured.");
    const body: Schemas["FuelTypeCreate"] = {
      code: form.values.code,
      display_name: form.values.display_name,
      unit_of_measure: form.values.unit_of_measure as Schemas["UnitOfMeasure"],
      max_flow_rate_per_minute: form.values.max_flow_rate_per_minute,
    };
    void save(form, () => api.post("/fuel-types", body), onDone, "Fuel type created.");
  }

  return (
    <Fields>
      {existing ? null : (
        <>
          <TextField form={form} name="code" label="Code" required hint="Upper-cased automatically. Permanent: it cannot be changed later." />
          <SelectField
            form={form}
            name="unit_of_measure"
            label="Measured in"
            required
            options={[
              { value: "", label: "Choose" },
              { value: "litre", label: "Litres" },
              { value: "kilogram", label: "Kilograms (CBG)" },
            ]}
            hint="Permanent. Changing it later would reinterpret every quantity ever recorded against this fuel."
          />
        </>
      )}
      <TextField form={form} name="display_name" label="Display name" required />
      <TextField
        form={form}
        name="max_flow_rate_per_minute"
        label="Most a nozzle can dispense per minute"
        inputMode="decimal"
        required
        // §6.2 reads this as the ceiling that refuses a mistyped reading, and §14 records that
        // the seeded CBG figure is a guess. Worth saying where it is set.
        hint="A reading implying more than this per minute is refused as a typo. Too high and it never fires; too low and it refuses a busy day."
      />
      {existing ? <CheckboxField form={form} name="is_active" label="Active" hint="Deactivating refuses new nozzles and sales; history keeps reading." /> : null}
      <SaveButton busy={busy} creating={!existing} onClick={submit} />
    </Fields>
  );
}

/* --- nozzles --------------------------------------------------------------------------------- */

type Nozzle = Schemas["NozzleResponse"];

export function NozzlesScreen() {
  const nozzles = useApiQuery<Nozzle[]>("/nozzles", { include_inactive: true });
  const fuelTypes = useApiQuery<FuelType[]>("/fuel-types");
  const [editing, setEditing] = useState<Editing<Nozzle>>(null);

  return (
    <>
      <AddAction onClick={() => setEditing({ row: null })} />
      <AdminList title="Nozzles" query={nozzles}>
        {(rows) => (
          <div className="flex flex-col gap-3">
            {rows.length ? (
              <AdminGrid>
                {rows.map((nozzle) => (
                  <AdminCard
                    key={nozzle.id}
                    title={nozzle.label}
                    caption={`${nozzle.dispenser_label} · ${nozzle.fuel_type_code}`}
                    status={<ActivePill active={nozzle.is_active} />}
                    onEdit={() => setEditing({ row: nozzle })}
                  >
                    <ListRow label="Rollover ceiling" value={reading(nozzle.totalizer_max_value)} />
                  </AdminCard>
                ))}
              </AdminGrid>
            ) : (
              <Empty>No nozzles yet. Readings cannot be recorded until at least one exists.</Empty>
            )}
            <p className="text-footnote text-ink-muted">A nozzle's fuel and rollover ceiling are fixed at creation: both are baked into every reading already recorded on it.</p>
          </div>
        )}
      </AdminList>
      <Sheet open={editing !== null} onClose={() => setEditing(null)} title={editing?.row ? editing.row.label : "New nozzle"}>
        {editing ? <NozzleForm key={editing.row?.id ?? "new"} existing={editing.row} fuelTypes={fuelTypes.data ?? []} onDone={() => setEditing(null)} /> : null}
      </Sheet>
    </>
  );
}

function NozzleForm({ existing, fuelTypes, onDone }: { existing: Nozzle | null; fuelTypes: FuelType[]; onDone: () => void }) {
  const { busy, save } = useSave();
  const form = useForm({
    label: existing?.label ?? "",
    dispenser_label: existing?.dispenser_label ?? "",
    fuel_type_id: "",
    totalizer_max_value: "",
    meter_installed_at: "",
    is_active: existing?.is_active ?? true,
  });

  function submit() {
    if (existing) {
      const { label, dispenser_label, is_active } = form.changes();
      const body: Schemas["NozzleUpdate"] = {};
      if (label !== undefined) body.label = label;
      if (dispenser_label !== undefined) body.dispenser_label = dispenser_label;
      if (is_active !== undefined) body.is_active = is_active;
      if (!Object.keys(body).length) return onDone();
      return void save(form, () => api.patch(`/nozzles/${existing.id}`, body), onDone);
    }
    if (!form.values.fuel_type_id) return form.setError("fuel_type_id", "Choose the fuel this nozzle dispenses.");
    // A datetime-local value is naive, and every timestamp the API accepts needs an offset
    // (§3 rule 4): without this the create is refused with NAIVE_TIMESTAMP.
    const installed = toOffsetISO(form.values.meter_installed_at);
    if (!installed) return form.setError("meter_installed_at", "When was this meter installed?");
    const body: Schemas["NozzleCreate"] = {
      label: form.values.label,
      dispenser_label: form.values.dispenser_label,
      fuel_type_id: form.values.fuel_type_id,
      totalizer_max_value: form.values.totalizer_max_value,
      meter_installed_at: installed,
    };
    void save(form, () => api.post("/nozzles", body), onDone, "Nozzle created.");
  }

  return (
    <Fields>
      <TextField form={form} name="label" label="Nozzle label" required hint="e.g. DU-1/N-2. Unique at this outlet." />
      <TextField form={form} name="dispenser_label" label="Dispenser label" required hint="e.g. DU-1. Groups nozzles in reports." />
      {existing ? (
        <CheckboxField form={form} name="is_active" label="Active" hint="An inactive nozzle drops out of the reading worksheet and stops blocking a shift close." />
      ) : (
        <>
          <SelectField
            form={form}
            name="fuel_type_id"
            label="Fuel"
            required
            options={[{ value: "", label: "Choose" }, ...fuelTypes.map((type) => ({ value: type.id, label: type.display_name }))]}
            hint="Fixed once created: every reading on this nozzle is read in this fuel's unit."
          />
          <TextField
            form={form}
            name="totalizer_max_value"
            label="Rollover ceiling"
            inputMode="decimal"
            required
            // §6.2's rollover arithmetic reads this directly; a wrong value mis-computes every
            // rollover this meter ever has.
            hint="The meter's maximum before it wraps to zero, e.g. 999999.99."
          />
          <TextField form={form} name="meter_installed_at" label="Meter installed" type="datetime-local" required hint="A reading dated before this is refused." />
        </>
      )}
      <SaveButton busy={busy} creating={!existing} onClick={submit} />
    </Fields>
  );
}

/* --- expense categories -------------------------------------------------------------------- */

type Category = Schemas["ExpenseCategoryResponse"];

export function CategoriesScreen() {
  const categories = useApiQuery<Category[]>("/expense-categories", { include_inactive: true });
  const [editing, setEditing] = useState<Editing<Category>>(null);

  return (
    <>
      <AddAction onClick={() => setEditing({ row: null })} />
      <AdminList title="Expense categories" query={categories}>
        {(rows) => (
          <div className="flex flex-col gap-3">
            {/* §14, twice over. The enum that once made this impossible is gone, so the warning
             * lives where somebody would otherwise create the category. */}
            <Notice>
              Never create a category for a fuel restock, a tanker delivery, or an IOCL or PAD settlement. That money leaves the bank, not the drawer: filing it here makes the cash
              engine invent a daily shortage that never happened.
            </Notice>
            {rows.length ? (
              <AdminGrid>
                {rows.map((category) => (
                  <AdminCard
                    key={category.id}
                    title={category.display_name}
                    caption={category.code}
                    status={
                      <>
                        {category.requires_receipt ? <Pill kind="review">receipt required</Pill> : null}
                        <ActivePill active={category.is_active} />
                      </>
                    }
                    onEdit={() => setEditing({ row: category })}
                  />
                ))}
              </AdminGrid>
            ) : (
              <Empty>No categories yet.</Empty>
            )}
          </div>
        )}
      </AdminList>
      <Sheet open={editing !== null} onClose={() => setEditing(null)} title={editing?.row ? editing.row.code : "New category"}>
        {editing ? <CategoryForm key={editing.row?.id ?? "new"} existing={editing.row} onDone={() => setEditing(null)} /> : null}
      </Sheet>
    </>
  );
}

function CategoryForm({ existing, onDone }: { existing: Category | null; onDone: () => void }) {
  const { busy, save } = useSave();
  const form = useForm({
    code: "",
    display_name: existing?.display_name ?? "",
    requires_receipt: existing?.requires_receipt ?? false,
    is_active: existing?.is_active ?? true,
  });

  function submit() {
    if (existing) {
      const { display_name, requires_receipt, is_active } = form.changes();
      const body: Schemas["ExpenseCategoryUpdate"] = {};
      if (display_name !== undefined) body.display_name = display_name;
      if (requires_receipt !== undefined) body.requires_receipt = requires_receipt;
      if (is_active !== undefined) body.is_active = is_active;
      if (!Object.keys(body).length) return onDone();
      return void save(form, () => api.patch(`/expense-categories/${existing.id}`, body), onDone);
    }
    const body: Schemas["ExpenseCategoryCreate"] = {
      code: form.values.code,
      display_name: form.values.display_name,
      requires_receipt: form.values.requires_receipt,
    };
    void save(form, () => api.post("/expense-categories", body), onDone, "Category created.");
  }

  return (
    <Fields>
      {existing ? null : (
        <TextField
          form={form}
          name="code"
          label="Code"
          required
          hint="Letters, digits and underscores; upper-cased automatically. Permanent: changing it later would relabel every expense ever filed under it."
        />
      )}
      <TextField form={form} name="display_name" label="Display name" required />
      <CheckboxField
        form={form}
        name="requires_receipt"
        label="Always requires a receipt"
        // §6.11: snapshotted onto each expense at insert, so flipping it never makes an old
        // expense non-compliant. Saying so is the difference between flipping it confidently
        // and not touching it at all.
        hint="Applies to new expenses only. Existing ones keep the rule that was in force when they were filed."
      />
      {existing ? <CheckboxField form={form} name="is_active" label="Active" hint="An inactive category refuses new expenses; historical ones still read and report." /> : null}
      <SaveButton busy={busy} creating={!existing} onClick={submit} />
    </Fields>
  );
}

/* --- shift templates ----------------------------------------------------------------------- */

type Template = Schemas["ShiftTemplateResponse"];

export function ShiftTemplatesScreen() {
  const templates = useApiQuery<Template[]>("/shift-templates", { include_inactive: true });
  const [editing, setEditing] = useState<Editing<Template>>(null);

  return (
    <>
      <AddAction onClick={() => setEditing({ row: null })} />
      <AdminList title="Shift templates" query={templates}>
        {(rows) => (
          <div className="flex flex-col gap-3">
            {/* §5.1, §14: a default at creation, never consulted again. */}
            <p className="text-footnote text-ink-muted">
              These supply the default start and end when a shift is opened. Changing one never alters a shift that already exists: the times are copied onto the shift when it is created.
            </p>
            {rows.length ? (
              <AdminGrid>
                {rows.map((template) => (
                  <AdminCard
                    key={template.id}
                    title={template.label}
                    caption={`Shift ${template.sequence} of the day`}
                    status={<ActivePill active={template.is_active} />}
                    onEdit={() => setEditing({ row: template })}
                  >
                    <ListRow
                      label="Trades"
                      value={`${localTime(template.starts_at_local)} to ${localTime(template.ends_at_local)}${template.crosses_midnight ? ", next day" : ""}`}
                    />
                  </AdminCard>
                ))}
              </AdminGrid>
            ) : (
              <Empty>No templates. A shift can still be opened; its times just have no default.</Empty>
            )}
          </div>
        )}
      </AdminList>
      <Sheet open={editing !== null} onClose={() => setEditing(null)} title={editing?.row ? editing.row.label : "New shift template"}>
        {editing ? <TemplateForm key={editing.row?.id ?? "new"} existing={editing.row} onDone={() => setEditing(null)} /> : null}
      </Sheet>
    </>
  );
}

function TemplateForm({ existing, onDone }: { existing: Template | null; onDone: () => void }) {
  const { busy, save } = useSave();
  const form = useForm({
    sequence: "",
    label: existing?.label ?? "",
    // HH:MM, which is what a time input shows; the server returns seconds too.
    starts_at_local: existing?.starts_at_local.slice(0, 5) ?? "",
    ends_at_local: existing?.ends_at_local.slice(0, 5) ?? "",
    is_active: existing?.is_active ?? true,
  });

  function submit() {
    if (existing) {
      const { label, starts_at_local, ends_at_local, is_active } = form.changes();
      const body: Schemas["ShiftTemplateUpdate"] = {};
      if (label !== undefined) body.label = label;
      if (starts_at_local !== undefined) body.starts_at_local = starts_at_local;
      if (ends_at_local !== undefined) body.ends_at_local = ends_at_local;
      if (is_active !== undefined) body.is_active = is_active;
      if (!Object.keys(body).length) return onDone();
      return void save(form, () => api.patch(`/shift-templates/${existing.id}`, body), onDone);
    }
    // A shift's position in the day, a small integer: not money, so a Number is right here.
    const sequence = Number(form.values.sequence);
    if (!Number.isInteger(sequence) || sequence < 1) return form.setError("sequence", "1 for the first shift of the day, 2 for the next.");
    const body: Schemas["ShiftTemplateCreate"] = {
      sequence,
      label: form.values.label,
      starts_at_local: form.values.starts_at_local,
      ends_at_local: form.values.ends_at_local,
    };
    void save(form, () => api.post("/shift-templates", body), onDone, "Template created.");
  }

  return (
    <Fields>
      {existing ? null : (
        <TextField form={form} name="sequence" label="Shift of the day" inputMode="numeric" required hint="1 for the first shift of a day, 2 for the next. Fixed once created." />
      )}
      <TextField form={form} name="label" label="Label" required hint="Display only; nothing keys off it." />
      <div className="grid grid-cols-2 gap-3">
        <TextField form={form} name="starts_at_local" label="Starts" type="time" required />
        <TextField form={form} name="ends_at_local" label="Ends" type="time" required />
      </div>
      <p className="-mt-2 text-footnote text-ink-faint">An end earlier than the start means the shift crosses midnight.</p>
      {existing ? <CheckboxField form={form} name="is_active" label="Active" /> : null}
      <SaveButton busy={busy} creating={!existing} onClick={submit} />
    </Fields>
  );
}

/* --- bank accounts -------------------------------------------------------------------------- */

/* Phase 20. A statement is imported against one account, so there has to be one before the
 * Bank screens do anything. Only the last four digits: §5.3a, the full number reconciles
 * nothing and storing it makes the table worth stealing. */

type Account = Schemas["BankAccountResponse"];

export function BankAccountsScreen() {
  const accounts = useApiQuery<Account[]>("/bank-accounts");
  const [editing, setEditing] = useState<Editing<Account>>(null);

  return (
    <>
      <AddAction onClick={() => setEditing({ row: null })} />
      <AdminList title="Bank accounts" query={accounts}>
        {(rows) =>
          rows.length ? (
            <AdminGrid>
              {rows.map((account) => (
                <AdminCard
                  key={account.id}
                  title={account.label}
                  caption={account.account_number_last4 ? `${account.bank_name} · ending ${account.account_number_last4}` : account.bank_name}
                  status={<ActivePill active={account.is_active} />}
                  onEdit={() => setEditing({ row: account })}
                />
              ))}
            </AdminGrid>
          ) : (
            <Empty>No bank accounts yet. Add the one your statements come from.</Empty>
          )
        }
      </AdminList>
      <Sheet open={editing !== null} onClose={() => setEditing(null)} title={editing?.row ? editing.row.label : "New bank account"}>
        {editing ? <BankAccountForm key={editing.row?.id ?? "new"} existing={editing.row} onDone={() => setEditing(null)} /> : null}
      </Sheet>
    </>
  );
}

function BankAccountForm({ existing, onDone }: { existing: Account | null; onDone: () => void }) {
  const { busy, save } = useSave();
  const form = useForm({
    label: existing?.label ?? "",
    bank_name: existing?.bank_name ?? "",
    account_number_last4: existing?.account_number_last4 ?? "",
    is_active: existing?.is_active ?? true,
  });

  function submit() {
    // An untouched optional field reads as "", which fails the four-digit pattern. Blank means
    // not supplied, so it is left out rather than sent.
    if (existing) {
      const { label, bank_name, account_number_last4, is_active } = form.changes();
      const body: Schemas["BankAccountUpdate"] = {};
      if (label !== undefined) body.label = label;
      if (bank_name !== undefined) body.bank_name = bank_name;
      if (account_number_last4) body.account_number_last4 = account_number_last4;
      if (is_active !== undefined) body.is_active = is_active;
      if (!Object.keys(body).length) return onDone();
      return void save(form, () => api.patch(`/bank-accounts/${existing.id}`, body), onDone);
    }
    const body: Schemas["BankAccountCreate"] = { label: form.values.label, bank_name: form.values.bank_name };
    if (form.values.account_number_last4) body.account_number_last4 = form.values.account_number_last4;
    void save(form, () => api.post("/bank-accounts", body), onDone, "Bank account added.");
  }

  return (
    <Fields>
      <TextField form={form} name="label" label="Label" required hint="What you call this account. It appears on the import form." />
      <TextField form={form} name="bank_name" label="Bank" required />
      <TextField
        form={form}
        name="account_number_last4"
        label="Last four digits"
        inputMode="numeric"
        hint="Four digits only. The full number is never stored: it reconciles nothing and would make this table worth stealing."
      />
      {existing ? <CheckboxField form={form} name="is_active" label="Active" /> : null}
      <SaveButton busy={busy} creating={!existing} onClick={submit} />
    </Fields>
  );
}
