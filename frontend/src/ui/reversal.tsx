/* §6.9's correction path, written once (Phase 12's reversal.js, for React).
 *
 * "No UPDATE and no DELETE on financial rows in a closed or locked shift. Corrections create a
 * reversal entry: a new row with the negated amount, a `reverses_id` FK to the original, and a
 * mandatory reason. Both rows remain visible."
 *
 * Eight tables carry this shape identically, and the request is the same every time: a
 * mandatory reason, an optional replacement amount, sometimes one replacement text field.
 * Written once, so eight correction flows cannot drift.
 *
 * The replacement is offered in the same form because a correction is almost never "cancel
 * this" -- it is "this said ₹600 and should have said ₹800", and making somebody reverse and
 * then remember to re-enter invites a silent ₹800 hole. Every reversal endpoint creates both
 * rows in one transaction.
 *
 * It never asks for a receipt (§6.11): a cancellation is not a spend, and a replacement inherits
 * the original's attachment (§5.3).
 */

import { useState } from "react";
import { useSubmission } from "../api/submission";
import { useRefreshApi } from "../api/queries";
import { format } from "../lib/money";
import { reportFailure } from "./feedback";
import { TextField, useForm } from "./form";
import { Button, Pill } from "./primitives";
import { notify } from "./toast";

export interface ReplacementText {
  name: "replacement_reference" | "replacement_paid_to" | "replacement_description";
  label: string;
  value?: string | null | undefined;
}

export interface ReversalFormProps {
  /** The /reversals endpoint. */
  path: string;
  /** The original's amount, for display. */
  amount: string;
  /** What is being reversed, in words. */
  description?: string | undefined;
  replacementText?: ReplacementText | undefined;
  /** Overrides the corrected-amount hint, where a table's correction has its own rule. */
  replacementHint?: string | undefined;
  onDone: () => void;
}

export function ReversalForm({ path, amount, description, replacementText, replacementHint, onDone }: ReversalFormProps) {
  // One key for this sheet's lifetime: a timeout-then-retry must not create two reversals, which
  // is what each table's uq_<table>_reverses_id exists to lose loudly.
  const submission = useSubmission("POST", path);
  const refresh = useRefreshApi();
  const form = useForm({ reason: "", replacement_amount: "", replacement_text: replacementText?.value ?? "" });
  const [busy, setBusy] = useState(false);

  async function submit() {
    form.clearErrors();
    const reason = form.values.reason.trim();
    if (reason.length < 3) {
      form.setError("reason", "A reason is required: at least 3 characters.");
      return;
    }
    const body: Record<string, string> = { reason };
    if (form.values.replacement_amount) {
      body.replacement_amount = form.values.replacement_amount;
      if (replacementText && form.values.replacement_text) body[replacementText.name] = form.values.replacement_text;
    }

    setBusy(true);
    try {
      const result = await submission.run<{ replacement?: unknown }>(body);
      onDone();
      notify.success(
        result?.replacement ? "Reversed and re-recorded. Both rows stay in the trail." : "Reversed. The original row stays visible.",
      );
      await refresh();
    } catch (error) {
      reportFailure(error, form, () => void submit());
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="rounded-[var(--radius-control)] bg-surface-sunken px-4 py-3">
        <p className="text-[0.8125rem] text-ink-muted">Reversing</p>
        <p className="tabular text-[1.5rem] leading-tight font-semibold tracking-[-0.02em] text-ink">{format(amount)}</p>
        {description ? <p className="mt-0.5 text-[0.8125rem] text-ink-muted">{description}</p> : null}
      </div>
      <p className="text-[0.8125rem] text-ink-muted">
        Nothing is deleted. A negative row is added pointing back at the original, and both stay on the record.
      </p>
      <TextField
        form={form}
        name="reason"
        label="Why is this being reversed?"
        required
        hint="Mandatory, and stored on the row itself, so this correction can never be an unexplained figure. 3 to 500 characters."
      />
      <TextField
        form={form}
        name="replacement_amount"
        label="Corrected amount (optional)"
        inputMode="decimal"
        hint={replacementHint ?? "Leave blank to cancel outright. Enter a figure to cancel and re-record in one step."}
      />
      {replacementText ? (
        <TextField form={form} name="replacement_text" label={replacementText.label} hint="Used only when a corrected amount is entered." />
      ) : null}
      <Button variant="primary" block disabled={busy} onClick={() => void submit()}>
        {busy ? "Recording…" : "Record reversal"}
      </Button>
    </div>
  );
}

/** Both rows of a reversal stay visible (§6.9), so both must be legible at a glance. */
export function ReversalBadge({ row }: { row: { reverses_id?: string | null; is_reversed?: boolean } }) {
  if (row.reverses_id) return <Pill kind="neutral">reversal</Pill>;
  if (row.is_reversed) return <Pill kind="review">reversed</Pill>;
  return null;
}

/** Live means: not itself a reversal, and not referenced by one (§5.2). */
export function isLive(row: { reverses_id?: string | null; is_reversed?: boolean }): boolean {
  return !row.reverses_id && !row.is_reversed;
}
