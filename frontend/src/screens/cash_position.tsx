/* §6.4's equation, term by term, and the decision that carries a person's name. Rebuilt in
 * Phase 23 from cash_position.js.
 *
 * ## Every term is shown, not a single figure
 *
 * "A manager told 'you are ₹500 short' and nothing else cannot check the claim, and this is the
 * number that decides whether a debt lands on somebody's name" (cash_position.py). So the
 * equation is rendered as a worked sum in §6.4's own order, every term labelled.
 *
 * ## The two figures are never added together
 *
 * accountable_cash (what the meters and other channels say he should hold) and declared_cash
 * (what he says he counted) are computed independently by the server and only ever subtracted
 * (§5.2, §14). They sit side by side here for the same reason.
 *
 * ## gap: null is not zero
 *
 * `gap: null` means nobody has declared. Booking is unavailable then, with that reason, rather
 * than offered and refused with 409 NO_CASH_DECLARED.
 *
 * ## A shortfall is booked by a human, never by this screen
 *
 * §14: "a ₹500 gap is more often a mistyped reading, a forgotten UPI figure or an unrecorded
 * udhaar slip than it is theft, and the software must not be the thing that decides." The form
 * is pre-filled with the computed gap and stays editable: §5.2 stores `computed_gap` and `amount`
 * separately and expects them to differ sometimes. `salesman_id` is never sent -- the server reads
 * it from `shifts.attendant_id`, the one name that carries the drawer.
 *
 * This is a report, so it carries no write verb in its title or its links (§14, Phase 15).
 * Booking is the one action, and it says exactly what it writes.
 */

import { type ReactNode, useState } from "react";
import { useParams } from "react-router";
import { useApiQuery, useRefreshApi } from "../api/queries";
import { useSubmission } from "../api/submission";
import type { Schemas } from "../api/types";
import { ScreenTitle } from "../app/chrome";
import { format, gapLabel, isNegative, isZero } from "../lib/money";
import { businessDate } from "../lib/time";
import { Amount } from "../ui/Amount";
import { reportFailure } from "../ui/feedback";
import { TextField, useForm } from "../ui/form";
import { Button, Card, ErrorCard, ListRow, Pill, SectionLabel, Skeleton } from "../ui/primitives";
import { Sheet } from "../ui/Sheet";
import { notify } from "../ui/toast";

type Position = Schemas["CashPositionResponse"];

export function CashPositionScreen() {
  const { shiftId = "" } = useParams();
  const position = useApiQuery<Position>(`/shifts/${shiftId}/cash-position`);
  const [booking, setBooking] = useState(false);

  if (position.isPending) {
    return (
      <>
        <ScreenTitle title="Cash position" />
        <Skeleton rows={4} />
      </>
    );
  }
  if (position.isError || !position.data) {
    return (
      <>
        <ScreenTitle title="Cash position" />
        <ErrorCard error={position.error} onRetry={() => void position.refetch()} />
      </>
    );
  }

  const p = position.data;
  const gap = gapLabel(p.gap);

  return (
    <>
      <ScreenTitle title="Cash position" subtitle={businessDate(p.business_date)} />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card className="lg:col-span-2">
          <SectionLabel>The comparison</SectionLabel>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Figure label="Accountable" caption="What the meters imply he should hold" value={<Amount value={p.accountable_cash} />} />
            <Figure label="Declared" caption="What he counted into the locker" value={<Amount value={p.declared_cash} absent="not declared" />} />
          </div>
          {/* The figure this screen exists for, at the top of the type ramp (Phase 24 D5): the
           * comparison above explains it, and nothing on the screen should outrank it. Its words
           * ("short", "surplus", "not declared") come from gapLabel, never from a colour alone. */}
          <div className="mt-4 flex flex-col gap-1 border-t border-hairline pt-4">
            <span className="text-footnote font-medium text-ink-muted">Gap</span>
            <span className={`tabular text-title break-words ${gap.className}`}>{gap.text}</span>
          </div>
          <p className="mt-2 text-footnote text-ink-muted">
            {p.declared_cash === null
              ? "Nobody has declared cash for this shift, so there is nothing to compare against. That is not the same as a gap of zero."
              : "Positive means short, negative means a surplus. Nothing here is written: a gap becomes a debt only when a manager books it."}
          </p>
        </Card>

        {p.incomplete ? (
          <Card className="lg:col-span-2">
            <Pill kind="review">incomplete</Pill>
            <p className="mt-2 text-footnote text-ink-muted">
              Some nozzles have no closing reading yet, so the accountable figure is partial.
            </p>
          </Card>
        ) : null}

        {/* §6.4 written out, in the spec's order, so somebody holding the document can follow. */}
        <Card className="lg:col-span-2">
          <SectionLabel>How the accountable figure is built</SectionLabel>
          <Term label="Metered fuel sales" value={p.metered_fuel_sales} />
          <Term label="Non-fuel sales" value={p.non_fuel_sales} />
          {/* §6.4's twelfth term: udhaar settled on the machine is inside card/UPI and is not a sale. */}
          <Term label="Udhaar settled on card or UPI" value={p.card_upi_credit_repayments} />
          <Divider>less what did not arrive as cash</Divider>
          <Term label="Card" value={p.card_total} subtracted />
          <Term label="UPI" value={p.upi_total} subtracted />
          <Term label="Wallet" value={p.wallet_total} subtracted />
          <Term label="Credit sales (udhaar)" value={p.credit_sales_total} subtracted />
          <Divider>plus cash that arrived from elsewhere</Divider>
          <Term label="Credit repayments in cash" value={p.cash_credit_repayments} />
          <Term label="Shortfall settlements in cash" value={p.cash_shortfall_settlements} />
          <div className="mt-1 border-t-2 border-hairline-strong">
            <ListRow label="Accountable cash" value={<Amount value={p.accountable_cash} />} strong />
          </div>
          <p className="mt-2 text-footnote text-ink-muted">
            What his sales should have put in his hands. Expenses are not subtracted here: the pump's bills come out of the locker, not out of what one salesman is accountable for.
          </p>
        </Card>

        {/* Phase 17: beside the figure, not inside it (§13.33). If he paid a bill from his own
         * hand, this is the number that explains the gap. */}
        {isZero(p.cash_expenses) ? null : (
          <Card>
            <SectionLabel>Paid out during this shift</SectionLabel>
            <ListRow label="Cash expenses" value={<Amount value={p.cash_expenses} />} />
            <p className="mt-2 text-footnote text-ink-muted">
              Not part of the figure above. If he paid these from the cash in his hand, expect the gap to be about this much. The locker is lighter by it either way, and the day's expected closing subtracts it in full.
            </p>
          </Card>
        )}

        <Card>
          <SectionLabel>Booked against this shift</SectionLabel>
          <ListRow label="Shortfalls booked" value={<Amount value={p.shortfalls_booked} />} />
          <p className="mt-2 text-footnote text-ink-muted">
            Subtracted from the day's expected closing, so the same money is not counted both as a debt and as cash in the locker.
          </p>
          <div className="mt-4">
            <BookingControl position={p} onBook={() => setBooking(true)} />
          </div>
        </Card>
      </div>

      <Sheet open={booking} onClose={() => setBooking(false)} title="Book a shortfall" subtitle={businessDate(p.business_date)}>
        <BookingForm shiftId={shiftId} position={p} onDone={() => setBooking(false)} />
      </Sheet>
    </>
  );
}

function Figure({ label, caption, value }: { label: string; caption: string; value: ReactNode }) {
  return (
    <div className="rounded-[var(--radius-control)] bg-surface-sunken px-4 py-3">
      <p className="text-footnote font-medium text-ink-muted">{label}</p>
      <p className="mt-1 text-amount text-ink">{value}</p>
      <p className="mt-0.5 text-caption text-ink-muted">{caption}</p>
    </div>
  );
}

function Term({ label, value, subtracted = false }: { label: string; value: string | null; subtracted?: boolean }) {
  // The minus is a printed sign telling the reader this term is subtracted -- a label on the
  // server's figure, never arithmetic performed on it.
  return (
    <ListRow
      label={label}
      value={
        <span className="inline-flex items-baseline gap-1">
          {subtracted ? <span className="text-ink-muted">−</span> : null}
          <Amount value={value} />
        </span>
      }
    />
  );
}

function Divider({ children }: { children: ReactNode }) {
  return <p className="pt-3 pb-1 text-caption font-semibold tracking-[0.06em] text-ink-faint uppercase">{children}</p>;
}

function BookingControl({ position, onBook }: { position: Position; onBook: () => void }) {
  if (position.gap === null) {
    return (
      <p className="text-footnote text-ink-muted">
        Booking a shortfall needs a declared cash figure first. There is no gap to book against a blank.
      </p>
    );
  }
  if (isZero(position.gap)) return <p className="text-footnote text-ink-muted">The drawer balances. Nothing to book.</p>;
  // A surplus is not a shortfall, and V1 has no record type for one.
  if (isNegative(position.gap)) {
    return (
      <p className="text-footnote text-ink-muted">
        This shift shows a surplus rather than a shortfall. V1 has no record type for a surplus: it stays visible here and in the day's variance.
      </p>
    );
  }
  return (
    <Button variant="primary" block onClick={onBook}>
      Book a shortfall
    </Button>
  );
}

function BookingForm({ shiftId, position, onDone }: { shiftId: string; position: Position; onDone: () => void }) {
  const submission = useSubmission("POST", `/shifts/${shiftId}/shortfalls`);
  const refresh = useRefreshApi();
  // Pre-filled with the computed gap, and still editable (§5.2 stores both).
  const form = useForm({ amount: position.gap ?? "", reason: "" });
  const [busy, setBusy] = useState(false);

  async function submit() {
    form.clearErrors();
    const reason = form.values.reason.trim();
    if (reason.length < 3) return form.setError("reason", "A reason is required.");
    setBusy(true);
    try {
      // No salesman_id: read from shifts.attendant_id by the server (§14).
      const body: Schemas["ShortfallCreate"] = { amount: form.values.amount, reason };
      await submission.run(body);
      onDone();
      notify.success("Shortfall booked.");
      await refresh();
    } catch (error) {
      reportFailure(error, form, () => void submit());
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-2 rounded-[var(--radius-control)] bg-warning-tint px-4 py-3 text-footnote text-ink">
        <p>This records a debt against the salesman who carried this shift's drawer. It is repaid in cash.</p>
        <p>A gap is more often a mistyped reading, a forgotten UPI figure or an unrecorded udhaar slip than it is theft. Check those first.</p>
      </div>
      <TextField
        form={form}
        name="amount"
        label="Amount to book"
        inputMode="decimal"
        required
        hint={`The system computed ${format(position.gap)}. You may book a different figure. Both are stored.`}
      />
      <TextField
        form={form}
        name="reason"
        label="Why is this being booked?"
        required
        hint="Mandatory. This becomes a debt in a named person's ledger, so the reason is part of the record. 3 to 500 characters."
      />
      <Button variant="primary" block disabled={busy} onClick={() => void submit()}>
        {busy ? "Booking…" : "Book shortfall"}
      </Button>
    </div>
  );
}
