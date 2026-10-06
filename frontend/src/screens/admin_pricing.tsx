/* Prices and margins: append-only, effective-dated (CLAUDE.md §4.1, §4.6, §5.1). Rebuilt in
 * Phase 23 from admin_pricing.js.
 *
 * ## No edit button, anywhere
 *
 * Both tables are append-only. §4.1: storing a "current price" as a mutable column "silently
 * corrupts every historical report the moment the price changes." A price is a dated record;
 * correcting one means adding another with a later `effective_from`, and the old row stays,
 * because a shift that closed last Tuesday was priced at last Tuesday's rate.
 *
 * ## Backdating is allowed, and warned about before the button is pressed
 *
 * The API permits an `effective_from` in the past and flags the row `is_backdated`. §11: a
 * backdated rate "can revalue a closed shift". Phase 11 made the audit trail record it; this
 * screen tells the person first.
 *
 * ## Prices and margins are separate tables, and that is not duplication
 *
 * §5.1: they revise on different schedules. One component serves both because they share every
 * RULE (append-only, effective-dated, backdating warned about), so a shared implementation keeps
 * them from drifting apart. Only nouns differ.
 */

import { useState } from "react";
import { api } from "../api/client";
import { useApiQuery } from "../api/queries";
import type { Schemas } from "../api/types";
import { format } from "../lib/money";
import { dateTime, nowLocalValue, toOffsetISO } from "../lib/time";
import { SelectField, TextField, useForm } from "../ui/form";
import { Card, Empty, ErrorCard, ListRow, Pill, SectionLabel, Skeleton } from "../ui/primitives";
import { Sheet } from "../ui/Sheet";
import { ScreenTitle } from "../app/chrome";
import { AddAction, Fields, SaveButton, useSave } from "./admin";

type FuelType = Schemas["FuelTypeResponse"];

interface Kind {
  title: string;
  path: "/fuel-prices" | "/fuel-margins";
  noun: "rate" | "margin";
  amountLabel: string;
  empty: string;
  missing: string;
}

const KINDS: Record<"price" | "margin", Kind> = {
  price: {
    title: "Prices",
    path: "/fuel-prices",
    noun: "rate",
    amountLabel: "Rate per unit",
    empty: "No rates entered. A shift cannot be valued without one: the cash engine refuses rather than valuing a day at zero.",
    missing: "Sales of these fuels cannot be valued at all until a rate exists.",
  },
  margin: {
    title: "Margins",
    path: "/fuel-margins",
    noun: "margin",
    amountLabel: "Dealer margin per unit",
    empty: "No margins entered. Reconciliation still works (§6.3 keeps valuation and profit apart), but profit stays blank for any fuel without one.",
    missing: "These fuels sell and reconcile normally; only their profit figure is missing.",
  },
};

/** One entry, price or margin: the amount lives under a different name in each table. */
interface Entry {
  id?: string;
  fuel_type_id: string;
  amount: string;
  effective_from?: string;
  is_backdated?: boolean;
}

function asEntries(items: (Schemas["FuelPriceResponse"] | Schemas["FuelMarginResponse"])[]): Entry[] {
  return items.map((item) => ({
    id: item.id,
    fuel_type_id: item.fuel_type_id,
    amount: "rate_per_unit" in item ? item.rate_per_unit : item.margin_per_unit,
    effective_from: item.effective_from,
    is_backdated: item.is_backdated,
  }));
}

export const PricesScreen = () => <PricingScreen kind={KINDS.price} />;
export const MarginsScreen = () => <PricingScreen kind={KINDS.margin} />;

function PricingScreen({ kind }: { kind: Kind }) {
  const current = useApiQuery<(Schemas["CurrentRateResponse"] | Schemas["CurrentMarginResponse"])[]>(`${kind.path}/current`);
  const history = useApiQuery<Schemas["FuelPricePage"] | Schemas["FuelMarginPage"]>(kind.path, { limit: 50 });
  const fuelTypes = useApiQuery<FuelType[]>("/fuel-types");
  const [adding, setAdding] = useState(false);

  const failed = current.error ?? history.error ?? fuelTypes.error;
  let body;
  if (current.isPending || history.isPending || fuelTypes.isPending) {
    body = <Skeleton rows={4} />;
  } else if (failed || !current.data || !history.data || !fuelTypes.data) {
    body = (
      <ErrorCard
        error={failed}
        onRetry={() => {
          void current.refetch();
          void history.refetch();
          void fuelTypes.refetch();
        }}
      />
    );
  } else {
    const byId = new Map(fuelTypes.data.map((type) => [type.id, type]));
    const inForce = current.data.map((item) => ({
      fuel_type_id: item.fuel_type_id,
      code: item.fuel_type_code,
      amount: "rate_per_unit" in item ? item.rate_per_unit : item.margin_per_unit,
    }));
    const covered = new Set(inForce.map((item) => item.fuel_type_id));
    const missing = fuelTypes.data.filter((type) => type.is_active && !covered.has(type.id));
    const rows = asEntries(history.data.items);

    body = (
      <div className="flex flex-col gap-5">
        <section>
          <SectionLabel>In force now</SectionLabel>
          {inForce.length ? (
            <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-4">
              {inForce.map((item) => (
                <Card key={item.fuel_type_id}>
                  <p className="text-caption font-semibold tracking-[0.06em] text-ink-faint uppercase">{item.code}</p>
                  <p className="tabular mt-1 text-amount text-ink">{format(item.amount)}</p>
                  {/* §4.5: per the fuel's own unit, never assumed litres. */}
                  <p className="text-footnote text-ink-muted">per {byId.get(item.fuel_type_id)?.unit_of_measure === "kilogram" ? "kg" : "litre"}</p>
                </Card>
              ))}
            </div>
          ) : (
            <Empty>{kind.empty}</Empty>
          )}
        </section>

        {/* Stated positively: "PETROL has no margin" is the fact somebody needs, and it is
         * invisible in a list that simply does not mention petrol. */}
        {missing.length ? (
          <Card>
            <SectionLabel>No {kind.noun} in force</SectionLabel>
            <div className="flex flex-wrap gap-1.5">
              {missing.map((type) => (
                <Pill key={type.id} kind="review">
                  {type.code}
                </Pill>
              ))}
            </div>
            <p className="mt-2 text-footnote text-ink-muted">{kind.missing}</p>
          </Card>
        ) : null}

        <section>
          <SectionLabel>History</SectionLabel>
          <p className="mb-2 text-footnote text-ink-muted">
            Append-only. A correction is a new row with a later effective date; the old one stays, because it is what a closed shift was priced at.
          </p>
          {rows.length ? (
            <Card>
              {rows.map((entry) => (
                <ListRow
                  key={entry.id}
                  label={byId.get(entry.fuel_type_id)?.code ?? "unknown fuel"}
                  detail={dateTime(entry.effective_from)}
                  value={
                    <span className="flex items-center gap-2">
                      {entry.is_backdated ? <Pill kind="review">backdated</Pill> : null}
                      <span>{format(entry.amount)}</span>
                    </span>
                  }
                />
              ))}
            </Card>
          ) : (
            <Empty>Nothing recorded yet.</Empty>
          )}
          {history.data.next_cursor ? <p className="mt-2 text-footnote text-ink-muted">Showing the latest 50.</p> : null}
        </section>
      </div>
    );
  }

  return (
    <>
      <ScreenTitle title={kind.title} />
      <AddAction onClick={() => setAdding(true)} />
      {body}
      <Sheet open={adding} onClose={() => setAdding(false)} title={kind.noun === "rate" ? "New rate" : "New margin"}>
        {adding ? <EntryForm kind={kind} fuelTypes={(fuelTypes.data ?? []).filter((type) => type.is_active)} onDone={() => setAdding(false)} /> : null}
      </Sheet>
    </>
  );
}

function EntryForm({ kind, fuelTypes, onDone }: { kind: Kind; fuelTypes: FuelType[]; onDone: () => void }) {
  const { busy, save } = useSave();
  const form = useForm({ fuel_type_id: "", amount: "", effective_from: nowLocalValue() });

  // A comparison of instants, not of money. The server flags a backdated row too; this is the
  // warning before the button, not the control.
  const chosen = toOffsetISO(form.values.effective_from);
  const backdated = chosen !== null && new Date(chosen).getTime() < Date.now() - 60_000;

  function submit() {
    if (!form.values.fuel_type_id) return form.setError("fuel_type_id", "Choose a fuel.");
    if (!chosen) return form.setError("effective_from", "When did this become live?");
    const write =
      kind.noun === "rate"
        ? () => api.post("/fuel-prices", { fuel_type_id: form.values.fuel_type_id, rate_per_unit: form.values.amount, effective_from: chosen } satisfies Schemas["FuelPriceCreate"])
        : () => api.post("/fuel-margins", { fuel_type_id: form.values.fuel_type_id, margin_per_unit: form.values.amount, effective_from: chosen } satisfies Schemas["FuelMarginCreate"]);
    void save(form, write, onDone, kind.noun === "rate" ? "Rate recorded." : "Margin recorded.");
  }

  return (
    <Fields>
      <SelectField
        form={form}
        name="fuel_type_id"
        label="Fuel"
        required
        options={[{ value: "", label: "Choose" }, ...fuelTypes.map((type) => ({ value: type.id, label: type.display_name }))]}
      />
      <TextField form={form} name="amount" label={kind.amountLabel} inputMode="decimal" required />
      <TextField
        form={form}
        name="effective_from"
        label="Effective from"
        type="datetime-local"
        required
        // §4.1: OMCs publish revised rates at 06:00 IST. Getting the instant right is what
        // makes §6.3 value the right shift at the right rate.
        hint="Rates revise at 06:00 IST. This is the moment it became live, not the moment you are typing it."
      />
      {backdated ? (
        <p role="status" className="rounded-[var(--radius-control)] bg-warning-tint px-3.5 py-2.5 text-footnote text-warning">
          This is in the past. A backdated entry can change what an already-closed shift was worth. It is allowed, and it is recorded against your name in the audit trail.
        </p>
      ) : null}
      <SaveButton busy={busy} creating label={`Record ${kind.noun}`} onClick={submit} />
    </Fields>
  );
}
