/* Expenses: what left the drawer, and what needs paper (CLAUDE.md §5.1, §6.7, §6.11). Rebuilt
 * in Phase 23 from expenses.js.
 *
 * ## The receipt rule is warned about here, never enforced here
 *
 * §6.11: `receipt_required = category.requires_receipt OR amount > EXPENSE_RECEIPT_THRESHOLD`.
 * The threshold comes from /client-config, never a constant ("changing it must not require a
 * deploy"). The client's copy exists only so somebody is told before a 422. For a SAVED row the
 * screen reads `expense.receipt_required` -- the snapshot taken at insert -- never the live
 * category flag (§14), so flipping a category later cannot rewrite whether history complied.
 *
 * ## mode is an answer, and it keeps §6.4 honest
 *
 * Only `mode = cash` reduces what the drawer should hold. NOT NULL with no default, so the
 * select has no pre-selected value -- nor does the category, or an expense ends up filed under
 * whatever sorted first.
 *
 * ## What must never be filed here
 *
 * A fuel restock, a tanker settlement or an IOCL/PAD payment is a bank movement, and filing one
 * as an expense makes §6.4 invent a cash shortage that never happened (§14). Categories are
 * admin data now, so the warning sits where the choice is made.
 */

import { useRef, useState } from "react";
import { useParams } from "react-router";
import { PlusIcon } from "@phosphor-icons/react";
import { api } from "../api/client";
import { useApiQuery, useRefreshApi } from "../api/queries";
import { useSubmission } from "../api/submission";
import type { Schemas } from "../api/types";
import { ScreenActions, ScreenTitle } from "../app/chrome";
import { useSession } from "../app/session";
import { useFlipList } from "../motion/flip";
import { compareMoney } from "../lib/money";
import { satisfies } from "../lib/roles";
import { Arrive } from "../ui/Arrive";
import { Amount } from "../ui/Amount";
import { reportFailure } from "../ui/feedback";
import { SelectField, TextField, useForm } from "../ui/form";
import { Button, Card, Empty, ErrorCard, ListRow, Pill, SectionLabel, Skeleton, TruncationNotice } from "../ui/primitives";
import { ReceiptButton } from "../ui/receipt";
import { ReceiptUpload } from "../ui/ReceiptUpload";
import { isLive, ReversalBadge, ReversalForm } from "../ui/reversal";
import { Sheet } from "../ui/Sheet";
import { notify } from "../ui/toast";

type Expense = Schemas["ExpenseResponse"];
type Category = Schemas["ExpenseCategoryResponse"];

const MODES = [
  { value: "cash", label: "Cash, out of the drawer" },
  { value: "card", label: "Card" },
  { value: "upi", label: "UPI" },
  { value: "bank_transfer", label: "Bank transfer" },
];

type Action = { kind: "edit"; expense: Expense | null } | { kind: "reverse"; expense: Expense } | { kind: "review"; expense: Expense };

export function ExpensesScreen() {
  const { shiftId = "" } = useParams();
  const { me } = useSession();
  const page = useApiQuery<Schemas["ExpensePage"]>(`/shifts/${shiftId}/expenses`);
  const shift = useApiQuery<Schemas["ShiftResponse"]>(`/shifts/${shiftId}`);
  const categories = useApiQuery<Category[]>("/expense-categories");
  const [action, setAction] = useState<Action | null>(null);
  // A saved expense, a reversal pair, a review: the rows move to their new places (Phase 24).
  const list = useRef<HTMLDivElement>(null);
  useFlipList(list, (page.data?.items ?? []).map((e) => `${e.id}:${e.requires_review}:${e.reviewed_at ?? ""}`).join("|"));

  if (page.isPending || shift.isPending || categories.isPending) {
    return (
      <>
        <ScreenTitle title="Expenses" />
        <Skeleton rows={3} />
      </>
    );
  }
  const failed = page.error ?? shift.error ?? categories.error;
  if (failed || !page.data || !shift.data || !categories.data) {
    return (
      <>
        <ScreenTitle title="Expenses" />
        <ErrorCard error={failed} onRetry={() => void Promise.all([page.refetch(), shift.refetch(), categories.refetch()])} />
      </>
    );
  }

  const data = page.data;
  const editable = shift.data.status === "open";
  const isManager = satisfies(me.role, "manager");
  const totals = Object.entries(data.totals_by_category ?? {});

  return (
    <>
      <ScreenTitle title="Expenses" subtitle={`Shift ${shift.data.sequence} · ${shift.data.status}`} />
      {editable ? (
        <ScreenActions>
          <Button variant="primary" size="sm" icon={<PlusIcon size={16} weight="bold" aria-hidden />} onClick={() => setAction({ kind: "edit", expense: null })}>
            Add
          </Button>
        </ScreenActions>
      ) : null}

      <Arrive items="children" className="flex flex-col gap-5">
        {totals.length ? (
          <Card>
            <div className="flex items-baseline justify-between gap-3">
              <SectionLabel className="mb-0">By category</SectionLabel>
              <span className="text-headline text-ink">
                <Amount value={data.total} />
              </span>
            </div>
            <div className="mt-2">
              {totals.map(([code, amount]) => (
                <ListRow key={code} label={code} value={<Amount value={amount as string} />} />
              ))}
            </div>
            <p className="mt-2 text-footnote text-ink-muted">
              Every mode, not just cash. Only cash expenses reduce what the drawer should be holding.
            </p>
          </Card>
        ) : null}

        {data.items.length ? (
          <div ref={list} className="flex flex-col gap-3">
            {data.items.map((expense) => (
              <ExpenseCard
                key={expense.id}
                expense={expense}
                editable={editable}
                isManager={isManager}
                onAction={(kind) => setAction({ kind, expense } as Action)}
              />
            ))}
          </div>
        ) : (
          <Empty>No expenses recorded for this shift.{editable ? " Add one with the button above." : ""}</Empty>
        )}

        {data.truncated ? <TruncationNotice count={data.items.length} /> : null}
      </Arrive>

      <Sheet
        open={action !== null}
        onClose={() => setAction(null)}
        title={
          action?.kind === "reverse"
            ? "Reverse expense"
            : action?.kind === "review"
              ? "Review expense"
              : action?.expense
                ? "Edit expense"
                : "Record an expense"
        }
      >
        {action?.kind === "edit" ? (
          <ExpenseForm shiftId={shiftId} existing={action.expense} categories={categories.data} onDone={() => setAction(null)} />
        ) : null}
        {action?.kind === "reverse" ? (
          <ReversalForm
            path={`/shifts/${shiftId}/expenses/${action.expense.id}/reversals`}
            amount={action.expense.amount}
            description={action.expense.description}
            replacementText={{ name: "replacement_paid_to", label: "Corrected payee (optional)", value: action.expense.paid_to }}
            onDone={() => setAction(null)}
          />
        ) : null}
        {action?.kind === "review" ? <ReviewForm shiftId={shiftId} expense={action.expense} onDone={() => setAction(null)} /> : null}
      </Sheet>
    </>
  );
}

function ExpenseCard({
  expense,
  editable,
  isManager,
  onAction,
}: {
  expense: Expense;
  editable: boolean;
  isManager: boolean;
  onAction: (kind: "edit" | "reverse" | "review") => void;
}) {
  const live = isLive(expense);
  const unreviewed = expense.requires_review && !expense.reviewed_at;
  return (
    <Card className={unreviewed ? "border-warning" : ""}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-body font-semibold text-ink">{expense.description}</h2>
          <p className="text-footnote text-ink-muted">
            {expense.category_code} · {expense.mode.replace("_", " ")}
            {expense.paid_to ? ` · ${expense.paid_to}` : ""}
          </p>
        </div>
        <span className="shrink-0 text-subhead text-ink">
          <Amount value={expense.amount} />
        </span>
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5">
        <ReversalBadge row={expense} />
        {/* The ROW's snapshot, never the live category flag (§6.11, §14). */}
        {expense.receipt_required ? (
          <Pill kind={expense.attachment_id ? "open" : "review"}>{expense.attachment_id ? "receipt attached" : "receipt required"}</Pill>
        ) : null}
        {expense.requires_review ? <Pill kind={expense.reviewed_at ? "neutral" : "review"}>{expense.reviewed_at ? "reviewed" : "needs review"}</Pill> : null}
      </div>
      {unreviewed ? (
        <p className="mt-2 text-footnote text-ink-muted">Flagged for a manager's eyes. A shift cannot be locked while this is unreviewed.</p>
      ) : null}
      {expense.review_note ? <p className="mt-2 text-footnote text-ink-muted">Review: {expense.review_note}</p> : null}
      {expense.reversal_reason ? <p className="mt-2 text-footnote text-ink-muted">Reason: {expense.reversal_reason}</p> : null}

      <div className="mt-4 flex flex-wrap gap-2">
        {/* A reversal row is itself a correction and cannot be edited or re-reversed. */}
        {editable && live ? (
          <Button size="sm" onClick={() => onAction("edit")}>
            Edit
          </Button>
        ) : null}
        {live && isManager ? (
          <Button size="sm" variant="danger" onClick={() => onAction("reverse")}>
            Reverse
          </Button>
        ) : null}
        <ReceiptButton attachmentId={expense.attachment_id} />
        {unreviewed && isManager ? (
          <Button size="sm" onClick={() => onAction("review")}>
            Review
          </Button>
        ) : null}
      </div>
    </Card>
  );
}

function ExpenseForm({
  shiftId,
  existing,
  categories,
  onDone,
}: {
  shiftId: string;
  existing: Expense | null;
  categories: Category[];
  onDone: () => void;
}) {
  const { config } = useSession();
  const submission = useSubmission("POST", `/shifts/${shiftId}/expenses`);
  const refresh = useRefreshApi();
  const active = categories.filter((category) => category.is_active);
  const form = useForm({
    category_id: existing?.category_id ?? "",
    mode: existing?.mode ?? "",
    amount: existing?.amount ?? "",
    description: existing?.description ?? "",
    paid_to: existing?.paid_to ?? "",
  });
  const [attachmentId, setAttachmentId] = useState<string | null>(existing?.attachment_id ?? null);
  const [busy, setBusy] = useState(false);

  // §6.11's rule, as a warning. A null threshold (config failed to load) means "no client
  // warning", never "zero" -- the server still decides either way.
  const threshold = config?.expense_receipt_threshold ?? null;
  const category = active.find((entry) => entry.id === form.values.category_id);
  const typed = form.values.amount.trim();
  const overThreshold = threshold !== null && /^\d+(\.\d+)?$/.test(typed) && compareMoney(typed, threshold) > 0;
  const required = Boolean(category?.requires_receipt) || overThreshold;

  async function submit() {
    form.clearErrors();
    const values = form.values;
    if (!values.category_id) return form.setError("category_id", "Choose a category.");
    if (!values.mode) return form.setError("mode", "Choose how it was paid.");

    setBusy(true);
    try {
      if (existing) {
        // category_id is not editable (ExpenseUpdate forbids it): the category a row was filed
        // under is part of what history said.
        const { category_id: _ignored, ...changes } = form.changes();
        const body: Schemas["ExpenseUpdate"] = changes as Schemas["ExpenseUpdate"];
        // §5.2: an attachment may be added while none exists, never swapped.
        if (attachmentId && !existing.attachment_id) body.attachment_id = attachmentId;
        if (Object.keys(body).length === 0) {
          onDone();
          return;
        }
        await api.patch(`/shifts/${shiftId}/expenses/${existing.id}`, body);
      } else {
        const body: Schemas["ExpenseCreate"] = {
          category_id: values.category_id,
          mode: values.mode as Schemas["ExpenseCreate"]["mode"],
          amount: values.amount,
          description: values.description,
        };
        if (values.paid_to) body.paid_to = values.paid_to;
        if (attachmentId) body.attachment_id = attachmentId;
        await submission.run(body);
      }
      onDone();
      notify.success(existing ? "Expense updated." : "Expense recorded.");
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
        <div className="flex flex-col gap-1">
          <span className="text-footnote font-medium text-ink-muted">Category</span>
          <p className="text-body text-ink">{existing.category_code}</p>
        </div>
      ) : (
        <SelectField
          form={form}
          name="category_id"
          label="Category"
          options={[{ value: "", label: "Choose…" }, ...active.map((entry) => ({ value: entry.id, label: entry.display_name }))]}
          hint="Never file a tanker delivery or an IOCL/PAD settlement here. That money leaves the bank, not the drawer."
        />
      )}
      <SelectField
        form={form}
        name="mode"
        label="How was it paid?"
        options={[{ value: "", label: "Choose…" }, ...MODES]}
        hint="Only cash reduces what the drawer should hold. A bank-paid bill is recorded but never subtracted from it."
      />
      <TextField form={form} name="amount" label="Amount" inputMode="decimal" required />
      <TextField form={form} name="description" label="What was it for?" required hint="3 to 500 characters." />
      <TextField form={form} name="paid_to" label="Paid to (optional)" />
      <ReceiptUpload
        shiftId={shiftId}
        attachmentId={attachmentId}
        locked={Boolean(existing?.attachment_id)}
        onUploaded={(id) => setAttachmentId(id)}
      />
      <p className={`text-footnote ${required && !attachmentId ? "text-short" : "text-ink-muted"}`}>
        {required
          ? attachmentId
            ? "A receipt is required for this expense, and one is attached."
            : "A receipt is required for this expense: its category demands one, or the amount is over the threshold."
          : "No receipt is required for this expense, but you may attach one."}
      </p>
      <Button variant="primary" block disabled={busy} onClick={() => void submit()}>
        {busy ? "Saving…" : existing ? "Save" : "Record expense"}
      </Button>
    </div>
  );
}

function ReviewForm({ shiftId, expense, onDone }: { shiftId: string; expense: Expense; onDone: () => void }) {
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
