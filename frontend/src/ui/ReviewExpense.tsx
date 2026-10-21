/* Signing off a flagged expense (CLAUDE.md §6.7). One form, two places to reach it: the
 * shift's own expenses screen, where the receipt and history sit, and (Phase 30) the flagged
 * queue, so a manager blocked from locking a shift can clear the flag where they found it.
 *
 * Same PATCH, same note. Flags are never cleared automatically; this is the one act that does,
 * and it is a decision somebody signs their name to.
 */

import { useState } from "react";
import { api } from "../api/client";
import { useRefreshApi } from "../api/queries";
import { Amount } from "./Amount";
import { reportFailure } from "./feedback";
import { TextField, useForm } from "./form";
import { Button } from "./primitives";
import { notify } from "./toast";

export function ReviewForm({ shiftId, expense, onDone }: { shiftId: string; expense: { id: string; amount: string; description: string }; onDone: () => void }) {
  const refresh = useRefreshApi();
  const form = useForm({ review_note: "" });
  const [busy, setBusy] = useState(false);

  async function submit() {
    form.clearErrors();
    setBusy(true);
    try {
      await api.patch(`/shifts/${shiftId}/expenses/${expense.id}/review`, form.values);
      onDone();
      notify.success("Reviewed.");
      await refresh();
    } catch (error) {
      reportFailure(error, form);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="rounded-[var(--radius-control)] bg-surface-sunken px-4 py-3">
        <p className="tabular text-amount text-ink">
          <Amount value={expense.amount} />
        </p>
        <p className="text-footnote text-ink-muted">{expense.description}</p>
      </div>
      <p className="text-footnote text-ink-muted">
        Flags are never cleared automatically, not even when a reversal drops the day's total back under the threshold.
      </p>
      <TextField
        form={form}
        name="review_note"
        label="Review note"
        required
        hint="3 to 500 characters. Clearing a flag is a decision somebody signs their name to."
      />
      <Button variant="primary" block disabled={busy} onClick={() => void submit()}>
        {busy ? "Saving…" : "Mark reviewed"}
      </Button>
    </div>
  );
}
