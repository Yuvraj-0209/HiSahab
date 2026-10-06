/* Collections: how the money arrived (CLAUDE.md §5.2, §6.4, §6.8). Rebuilt in Phase 23 from
 * collections.js.
 *
 * ## The cash row is a declaration, not an input to the cash equation
 *
 * §6.4 derives cash as the residual -- sales minus card, UPI, wallet and udhaar -- and never
 * reads the cash row. The cash row is the independent observation that figure is checked
 * against: derived cash is what the meters say the salesman should hold, declared cash is what
 * he says he counted into the locker, and the gap is what a manager may book against his name.
 * So this screen shows the declaration on its own and never a total mixing it with anything
 * derived (§14: summing them double-counts the day, plausibly).
 *
 * ## null and "0.00" are different answers
 *
 * `declared_cash: null` means nobody has declared; `"0.00"` means they counted zero. An explicit
 * zero satisfies the MISSING_COLLECTIONS close precondition and a blank does not (§6.8).
 *
 * ## One live row per mode
 *
 * A second POST for a mode is 409 COLLECTION_ALREADY_EXISTS (a unique index cannot coexist with
 * §6.9's reversals), so each mode is "declare" or "edit", never "add another".
 */

import { useRef, useState } from "react";
import { useParams } from "react-router";
import { api } from "../api/client";
import { useApiQuery, useRefreshApi } from "../api/queries";
import { useSubmission } from "../api/submission";
import type { Schemas } from "../api/types";
import { ScreenTitle } from "../app/chrome";
import { useSession } from "../app/session";
import { useFlipList } from "../motion/flip";
import { isZero } from "../lib/money";
import { satisfies } from "../lib/roles";
import { Amount } from "../ui/Amount";
import { reportFailure } from "../ui/feedback";
import { TextField, useForm } from "../ui/form";
import { Button, Card, ErrorCard, HeroFigure, ListCard, ListRow, SectionLabel, Skeleton, TruncationNotice } from "../ui/primitives";
import { isLive, ReversalBadge, ReversalForm } from "../ui/reversal";
import { Sheet } from "../ui/Sheet";
import { notify } from "../ui/toast";

type Collection = Schemas["CollectionResponse"];
type Mode = { value: Collection["mode"]; label: string; hint: string };

const MODES: readonly Mode[] = [
  { value: "cash", label: "Cash", hint: "What was counted into the locker" },
  { value: "card", label: "Card", hint: "The card machine's settlement total" },
  { value: "upi", label: "UPI", hint: "The QR total" },
  { value: "wallet", label: "Wallet", hint: "Paytm, PhonePe and similar" },
];

type Action = { kind: "declare"; mode: Mode; existing: Collection | null } | { kind: "reverse"; mode: Mode; existing: Collection };

export function CollectionsScreen() {
  const { shiftId = "" } = useParams();
  const { me } = useSession();
  const page = useApiQuery<Schemas["CollectionPage"]>(`/shifts/${shiftId}/collections`);
  const shift = useApiQuery<Schemas["ShiftResponse"]>(`/shifts/${shiftId}`);
  const [action, setAction] = useState<Action | null>(null);
  // A declaration changes its card's height; the other cards move rather than jump (Phase 24).
  const grid = useRef<HTMLDivElement>(null);
  useFlipList(grid, (page.data?.items ?? []).map((item) => item.id).join("|"));

  if (page.isPending || shift.isPending) {
    return (
      <>
        <ScreenTitle title="Collections" />
        <Skeleton rows={4} />
      </>
    );
  }
  if (page.isError || shift.isError || !page.data || !shift.data) {
    return (
      <>
        <ScreenTitle title="Collections" />
        <ErrorCard error={page.error ?? shift.error} onRetry={() => void (page.refetch(), shift.refetch())} />
      </>
    );
  }

  const data = page.data;
  const editable = shift.data.status === "open";
  const live = data.items.filter(isLive);
  const history = data.items.filter((item) => !isLive(item));
  const declared = data.declared_cash;

  return (
    <>
      <ScreenTitle title="Collections" subtitle={`Shift ${shift.data.sequence} · ${shift.data.status}`} />
      <div className="flex flex-col gap-5">
        <Card>
          <HeroFigure label="Cash declared">
            <Amount value={declared} absent="not declared" />
          </HeroFigure>
          <p className="mt-3 max-w-[60ch] text-callout text-ink-muted">
            {declared === null
              ? "Nobody has declared cash for this shift yet. That is different from declaring zero: the shift cannot close until somebody answers."
              : isZero(declared)
                ? "Zero was declared as an answer. That is a different fact from having entered nothing."
                : "What the salesman says he counted into the locker. The Cash tab compares this against what the meters imply."}
          </p>
        </Card>

        <section>
          <SectionLabel>By mode</SectionLabel>
          <div ref={grid} className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {MODES.map((mode) => {
              const existing = live.find((item) => item.mode === mode.value) ?? null;
              return (
                <Card key={mode.value}>
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <h2 className="text-body font-semibold text-ink">{mode.label}</h2>
                      <p className="text-footnote text-ink-muted">{mode.hint}</p>
                    </div>
                    <span className="text-headline text-ink">
                      <Amount value={existing?.amount ?? null} absent="not entered" />
                    </span>
                  </div>
                  {existing?.reference ? <p className="mt-2 text-footnote text-ink-muted">Reference {existing.reference}</p> : null}
                  {editable ? (
                    <div className="mt-4 flex gap-2">
                      <Button block onClick={() => setAction({ kind: "declare", mode, existing })}>
                        {existing ? "Edit" : `Declare ${mode.label.toLowerCase()}`}
                      </Button>
                      {existing && satisfies(me.role, "manager") ? (
                        <Button variant="danger" onClick={() => setAction({ kind: "reverse", mode, existing })}>
                          Reverse
                        </Button>
                      ) : null}
                    </div>
                  ) : null}
                </Card>
              );
            })}
          </div>
        </section>

        {history.length ? (
          <section>
            <SectionLabel>Corrections</SectionLabel>
            <ListCard>
              {history.map((item) => (
                <ListRow
                  key={item.id}
                  label={<span className="capitalize">{item.mode}</span>}
                  detail={item.reversal_reason ?? undefined}
                  value={
                    <span className="flex items-center justify-end gap-2">
                      <ReversalBadge row={item} />
                      <Amount value={item.amount} />
                    </span>
                  }
                />
              ))}
            </ListCard>
          </section>
        ) : null}

        {data.truncated ? <TruncationNotice count={data.items.length} /> : null}
      </div>

      <Sheet
        open={action !== null}
        onClose={() => setAction(null)}
        title={
          action?.kind === "reverse"
            ? `Reverse ${action.mode.label.toLowerCase()}`
            : action?.existing
              ? `Edit ${action.mode.label.toLowerCase()}`
              : `Declare ${action?.mode.label.toLowerCase() ?? ""}`
        }
      >
        {action?.kind === "declare" ? (
          <DeclareForm shiftId={shiftId} mode={action.mode} existing={action.existing} onDone={() => setAction(null)} />
        ) : null}
        {action?.kind === "reverse" ? (
          <ReversalForm
            path={`/shifts/${shiftId}/collections/${action.existing.id}/reversals`}
            amount={action.existing.amount}
            description={`${action.mode.label} collection`}
            replacementText={{ name: "replacement_reference", label: "Corrected reference (optional)", value: action.existing.reference }}
            onDone={() => setAction(null)}
          />
        ) : null}
      </Sheet>
    </>
  );
}

function DeclareForm({ shiftId, mode, existing, onDone }: { shiftId: string; mode: Mode; existing: Collection | null; onDone: () => void }) {
  // A PATCH is naturally idempotent; the key exists for the create path only (§6.10).
  const submission = useSubmission("POST", `/shifts/${shiftId}/collections`);
  const refresh = useRefreshApi();
  const form = useForm({ amount: existing?.amount ?? "", reference: existing?.reference ?? "" });
  const [busy, setBusy] = useState(false);

  async function submit() {
    form.clearErrors();
    if (form.values.amount.trim() === "") {
      form.setError("amount", "Enter an amount. Zero is valid.");
      return;
    }
    setBusy(true);
    try {
      if (existing) {
        // Only what changed, never an explicit null.
        const changes = form.changes();
        if (Object.keys(changes).length === 0) {
          onDone();
          return;
        }
        await api.patch(`/shifts/${shiftId}/collections/${existing.id}`, changes);
      } else {
        const body: Schemas["CollectionCreate"] = { mode: mode.value, amount: form.values.amount };
        if (form.values.reference) body.reference = form.values.reference;
        await submission.run(body);
      }
      onDone();
      notify.success(`${mode.label} recorded.`);
      await refresh();
    } catch (error) {
      reportFailure(error, form, () => void submit());
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <TextField
        form={form}
        name="amount"
        label={`${mode.label} amount`}
        inputMode="decimal"
        required
        hint={
          mode.value === "cash"
            ? "Enter 0 if the shift genuinely took no cash. Zero is an answer; leaving it blank is not."
            : "The settlement total for this channel."
        }
      />
      <TextField form={form} name="reference" label="Reference (optional)" hint="Settlement or batch number, if there is one." />
      <Button variant="primary" block disabled={busy} onClick={() => void submit()}>
        {busy ? "Saving…" : existing ? "Save" : "Declare"}
      </Button>
    </div>
  );
}
