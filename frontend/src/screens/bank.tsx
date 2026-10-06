/* The Bank screens: import a statement, review what it found, reconcile it (§5.3a, §8).
 * Rebuilt in Phase 23 from bank.js.
 *
 * Three screens inside the Credit tab, because feeding the udhaar ledger is what this is for and
 * §8 already puts both at the manager floor.
 *
 * ## Every figure arrives as a string and is rendered as one
 *
 * The reconciliation differences, the totals and the boundary settlement are computed by the
 * server in Decimal. This file never adds, subtracts or divides an amount (§14).
 *
 * ## `null` is not zero, twice over on these screens
 *
 * `settled: null` means Paytm has not paid yet, or the line is not in this file -- not "Paytm
 * paid nothing", which would read as a discrepancy the size of a whole day. `is_expense:
 * "undecided"` means nobody has looked, which is not "not an expense" (§6.8). Both are their own
 * state on screen.
 *
 * ## The review never offers to record money that is already on a ledger (Phase 23)
 *
 * The old review listed every incoming udhaar line with no repayment linked to it, and offered
 * each one for recording. But a line with no link may still be a repayment somebody already
 * TYPED IN: §13.37's live `(date, amount)` match finds it, and the reconciliation endpoint
 * reports it as `verified_repayment_id`. Recording that line created a second repayment for the
 * same money, and the customer's balance went down twice. The same goes for an `ambiguous` line:
 * several typed-in payments match it, so recording it again would almost certainly duplicate
 * one. So this screen asks the reconciliation endpoint what each line is, and offers recording
 * only for lines that match nothing already on a ledger.
 *
 * ## A remembered sender pre-selects; it never ticks
 *
 * §5.3a and §14: `bank_sender_aliases` exist so next month's line pre-selects its customer. The
 * Confirm tick still starts empty, so nothing reaches a ledger until a person has looked.
 */

import { type ChangeEvent, type ReactNode, useEffect, useRef, useState } from "react";
import { ArrowLeftIcon, FileCsvIcon, ListChecksIcon, ScalesIcon } from "@phosphor-icons/react";
import { ApiError, api, explain, newIdempotencyKey, postMultipart } from "../api/client";
import { useApiQuery, useRefreshApi } from "../api/queries";
import { useRepeatableSubmission } from "../api/submission";
import type { Schemas } from "../api/types";
import { ScreenActions, ScreenTitle } from "../app/chrome";
import { useGo } from "../app/navigation";
import { format } from "../lib/money";
import { businessDate, businessDateRange } from "../lib/time";
import { Amount } from "../ui/Amount";
import { reportFailure } from "../ui/feedback";
import { CheckboxField, SelectField, useForm } from "../ui/form";
import { useArrival } from "../ui/motion";
import { Button, Card, Empty, ErrorCard, LinkTile, ListRow, Pill, SectionLabel, Skeleton, TruncationNotice } from "../ui/primitives";
import { notify } from "../ui/toast";

type Account = Schemas["BankAccountResponse"];
type Transaction = Schemas["TransactionResponse"];
type Review = Schemas["CreditReviewResponse"];
type Customer = Schemas["CreditCustomerListItem"];
type Settlement = Schemas["app__api__v1__bank_statements__SettlementResponse"];
type Deposit = Schemas["DepositResponse"];

const CLASSIFICATION_LABELS: Record<string, string> = {
  paytm_settlement: "Paytm settlement",
  cash_deposit: "Cash deposit",
  udhaar_repayment: "Udhaar repayment",
  iocl_ms_hsd: "IOCL · petrol and diesel",
  iocl_cbg: "IOCL · CBG",
  bank_charge: "Bank charge",
  loan: "Loan",
  self_transfer: "Self transfer",
  other: "Other",
  unclassified: "Unclassified",
};

/* Money that moved between the owner's own pockets rather than leaving the business. Said in
 * words, because §12's rule (an IOCL payment is not an expense) is the one a person is most
 * likely to get wrong while ticking through a month of debits. */
const NOT_A_COST = new Set(["iocl_ms_hsd", "iocl_cbg", "self_transfer"]);

const classificationLabel = (value: string) => CLASSIFICATION_LABELS[value] ?? value;

function BackToBank() {
  const navigate = useGo();
  return (
    <ScreenActions>
      <Button size="sm" variant="plain" icon={<ArrowLeftIcon size={16} aria-hidden />} onClick={() => navigate("/credit/bank")}>
        Bank
      </Button>
    </ScreenActions>
  );
}

/* --- the hub: accounts, imports, and the way in ------------------------------------------ */

export function BankHubScreen() {
  const navigate = useGo();
  const accounts = useApiQuery<Account[]>("/bank-accounts");
  const imports = useApiQuery<Schemas["ImportPage"]>("/bank-statements/imports");
  const list = useRef<HTMLDivElement>(null);
  useArrival(list, Boolean(imports.data));

  if (accounts.isPending || imports.isPending) {
    return (
      <>
        <ScreenTitle title="Bank" />
        <Skeleton rows={3} />
      </>
    );
  }
  if (accounts.isError || imports.isError || !accounts.data || !imports.data) {
    return (
      <>
        <ScreenTitle title="Bank" />
        <ErrorCard
          error={accounts.error ?? imports.error}
          onRetry={() => {
            void accounts.refetch();
            void imports.refetch();
          }}
        />
      </>
    );
  }

  const active = accounts.data.filter((account) => account.is_active);
  if (active.length === 0) {
    return (
      <>
        <ScreenTitle title="Bank" />
        <Card>
          <h2 className="text-subhead text-ink">No bank account yet</h2>
          <p className="mt-1 text-body text-ink-muted">
            An admin adds the outlet's bank account on the Admin tab. A statement is imported against one account, so there has to be one first.
          </p>
        </Card>
      </>
    );
  }

  const items = imports.data.items;
  return (
    <>
      <ScreenTitle title="Bank" subtitle="Statements, checked against the books" />
      <div className="flex flex-col gap-4">
        <ImportCard accounts={active} />

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <LinkTile
            Icon={ListChecksIcon}
            label="Review what it found"
            hint="Udhaar paid into the bank, and whether each debit was a cost"
            onClick={() => navigate("/credit/bank/review")}
          />
          <LinkTile
            Icon={ScalesIcon}
            label="Reconciliation"
            hint="Paytm and cash deposits, against the days they came from"
            onClick={() => navigate("/credit/bank/reconciliation")}
          />
        </div>

        <section>
          <SectionLabel sticky>Imports</SectionLabel>
          {items.length ? (
            <div ref={list} className="flex flex-col gap-2">
              {items.map((item) => (
                <div key={item.id} data-arrive>
                  <Card>
                    <ListRow
                      label={businessDateRange(item.period_from, item.period_to)}
                      detail={
                        `${item.imported_count} imported` +
                        (item.skipped_count ? `, ${item.skipped_count} already seen` : "") +
                        (item.original_filename ? ` · ${item.original_filename}` : "")
                      }
                      // Rendered as received. A statement with no balance column is a real case
                      // (§5.3a), and it is not a ₹0.00 balance.
                      value={<Amount value={item.closing_balance} absent="no balance" />}
                    />
                  </Card>
                </div>
              ))}
            </div>
          ) : (
            <Empty>Nothing imported yet. Download the statement CSV from the bank and import it above.</Empty>
          )}
          {imports.data.next_cursor ? <TruncationNotice count={items.length} /> : null}
        </section>
      </div>
    </>
  );
}


function ImportCard({ accounts }: { accounts: Account[] }) {
  const refresh = useRefreshApi();
  const form = useForm({ bank_account_id: accounts[0]?.id ?? "" });
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  /* §6.10, §14: the key belongs to the SUBMISSION. Every retry of one file into one account
   * reuses it, so a timed-out upload cannot become two imports. Choosing another file or
   * another account is a different submission and gets a new key; sending a different body
   * under the old one would be refused as a reused key. */
  const [key, setKey] = useState(newIdempotencyKey);
  useEffect(() => setKey(newIdempotencyKey()), [file, form.values.bank_account_id]);

  async function submit() {
    setProblem(null);
    if (!file) {
      setProblem("Choose the CSV your bank gave you.");
      return;
    }
    setBusy(true);
    try {
      const result = await postMultipart<Schemas["ImportResponse"]>(
        "/bank-statements/imports",
        { file, bank_account_id: form.values.bank_account_id },
        { extraHeaders: { "Idempotency-Key": key } },
      );
      notify.success(
        `Imported ${result.imported_count} of ${result.row_count} lines` + (result.skipped_count ? `, skipped ${result.skipped_count} already seen.` : "."),
      );
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
      await refresh();
    } catch (error) {
      // An unreadable file is answered on the card, next to the file it is about.
      if (error instanceof ApiError && error.status < 500) setProblem(explain(error));
      else reportFailure(error, undefined, () => void submit());
    } finally {
      setBusy(false);
    }
  }

  /* No <form> element: a native submit posts to the current URL, which under hash routing is
   * the static mount, and the CSP's form-action 'none' blocks it anyway. Buttons only. */
  return (
    <Card>
      <h2 className="text-subhead text-ink">Import a statement</h2>
      <div className="mt-3 flex flex-col gap-4">
        {accounts.length > 1 ? (
          <SelectField
            form={form}
            name="bank_account_id"
            label="Account"
            options={accounts.map((account) => ({ value: account.id, label: `${account.label} · ${account.bank_name}` }))}
            required
          />
        ) : (
          <p className="text-body text-ink">
            {accounts[0]?.label} <span className="text-ink-muted">· {accounts[0]?.bank_name}</span>
          </p>
        )}
        <label className="pressable flex cursor-pointer items-center gap-3 rounded-[var(--radius-control)] border border-dashed border-hairline-strong bg-surface px-4 py-4">
          <FileCsvIcon size={28} className="shrink-0 text-accent" aria-hidden />
          <span className="min-w-0 grow">
            <span className="block truncate text-body font-medium text-ink">{file ? file.name : "Choose the statement CSV"}</span>
            <span className="block text-footnote text-ink-muted">Bank of Baroda export, not the .xls</span>
          </span>
          <input
            ref={fileInput}
            type="file"
            accept=".csv,text/csv"
            className="sr-only-text"
            onChange={(event: ChangeEvent<HTMLInputElement>) => setFile(event.target.files?.[0] ?? null)}
          />
        </label>
        <p className="text-footnote text-ink-muted">
          Download a few days past the month end: the last day's card and UPI settles the next morning, and lines already imported are skipped.
        </p>
        {problem ? (
          <p role="alert" className="text-callout text-short">
            {problem}
          </p>
        ) : null}
        <Button variant="primary" block disabled={busy} onClick={() => void submit()}>
          {busy ? "Reading…" : "Import"}
        </Button>
      </div>
    </Card>
  );
}

/* --- the review list: two sections, two kinds of question -------------------------------- */

/* Credits ask WHO SENT THIS; debits ask WAS THIS A COST. A wrong credit lands on somebody's
 * ledger, a wrong debit moves the profit bridge, so they are answered separately rather than in
 * one list where the eye slides between two kinds of decision. */
export function BankReviewScreen() {
  const accounts = useApiQuery<Account[]>("/bank-accounts");
  const customers = useApiQuery<Customer[]>("/credit-customers", { include_inactive: true });
  const debits = useApiQuery<Schemas["TransactionPage"]>("/bank-transactions", { direction: "debit", limit: 200 });

  const loading = accounts.isPending || customers.isPending || debits.isPending;
  const failed = accounts.error ?? customers.error ?? debits.error;

  return (
    <>
      <ScreenTitle title="Review statement" />
      <BackToBank />
      {loading ? (
        <Skeleton rows={4} />
      ) : failed || !accounts.data || !customers.data || !debits.data ? (
        <ErrorCard
          error={failed}
          onRetry={() => {
            void accounts.refetch();
            void customers.refetch();
            void debits.refetch();
          }}
        />
      ) : (
        <ReviewBody accounts={accounts.data} customers={customers.data} debits={debits.data} />
      )}
    </>
  );
}

function ReviewBody({ accounts, customers, debits }: { accounts: Account[]; customers: Customer[]; debits: Schemas["TransactionPage"] }) {
  return (
    <div className="flex flex-col gap-6">
      {/* Every account, active or not: a line imported before an account was retired still
       * needs an answer. */}
      {accounts.map((account) => (
        <AccountCredits key={account.id} account={account} customers={customers} labelled={accounts.length > 1} />
      ))}
      <DebitsSection page={debits} />
    </div>
  );
}

function AccountCredits({ account, customers, labelled }: { account: Account; customers: Customer[]; labelled: boolean }) {
  const credits = useApiQuery<Schemas["TransactionPage"]>("/bank-transactions", {
    bank_account_id: account.id,
    direction: "credit",
    classification: "udhaar_repayment",
    limit: 200,
  });
  const lines = (credits.data?.items ?? []).filter((item) => item.credit_repayment_id === null);
  // ISO dates sort as strings; no Date is built from a business date.
  const dates = lines.map((line) => line.txn_date).sort();
  const reconciliation = useApiQuery<Schemas["ReconciliationResponse"]>(
    "/bank-statements/reconciliation",
    { bank_account_id: account.id, date_from: dates[0], date_to: dates[dates.length - 1] },
    { enabled: dates.length > 0 },
  );

  const heading = labelled ? `Money in · ${account.label}` : "Money in";

  if (credits.isPending || (lines.length > 0 && reconciliation.isPending)) {
    return (
      <section>
        <SectionLabel sticky>{heading}</SectionLabel>
        <Skeleton rows={2} />
      </section>
    );
  }
  if (credits.isError || reconciliation.isError) {
    return (
      <section>
        <SectionLabel sticky>{heading}</SectionLabel>
        <ErrorCard
          error={credits.error ?? reconciliation.error}
          onRetry={() => {
            void credits.refetch();
            void reconciliation.refetch();
          }}
        />
      </section>
    );
  }
  if (lines.length === 0) {
    return (
      <section>
        <SectionLabel sticky>{heading}</SectionLabel>
        <Empty>Every incoming udhaar line on this account has been dealt with.</Empty>
      </section>
    );
  }

  const reviews = new Map((reconciliation.data?.credits ?? []).map((review) => [review.transaction_id, review]));
  return (
    <CreditsSection
      heading={heading}
      lines={lines}
      reviews={reviews}
      customers={customers}
      truncated={credits.data?.next_cursor ? credits.data.items.length : null}
    />
  );
}

interface Choice {
  credit_customer_id: string;
  remember_sender: boolean;
  confirm: boolean;
}

function CreditsSection({
  heading,
  lines,
  reviews,
  customers,
  truncated,
}: {
  heading: string;
  lines: Transaction[];
  reviews: Map<string, Review>;
  customers: Customer[];
  truncated: number | null;
}) {
  const refresh = useRefreshApi();
  const send = useRepeatableSubmission("POST", "/bank-transactions/confirm-repayments");
  const choices = useRef(new Map<string, () => Choice>());
  const [lineErrors, setLineErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const list = useRef<HTMLDivElement>(null);
  useArrival(list, true);

  const recordable = lines.filter((line) => {
    const review = reviews.get(line.id);
    return !review || (review.verified_repayment_id === null && !review.ambiguous);
  });
  const settled = lines.length - recordable.length;

  async function confirm() {
    const ticked: (Choice & { transaction_id: string })[] = [];
    for (const line of recordable) {
      const choice = choices.current.get(line.id)?.();
      if (choice?.confirm) ticked.push({ ...choice, transaction_id: line.id });
    }
    if (ticked.length === 0) {
      notify.info("Tick the lines you want to record.");
      return;
    }
    const missing = ticked.filter((item) => !item.credit_customer_id);
    if (missing.length) {
      setLineErrors(Object.fromEntries(missing.map((item) => [item.transaction_id, "Choose who sent this before recording it."])));
      return;
    }

    setLineErrors({});
    setBusy(true);
    try {
      const body: Schemas["ConfirmRequest"] = {
        items: ticked.map(({ transaction_id, credit_customer_id, remember_sender }) => ({ transaction_id, credit_customer_id, remember_sender })),
      };
      const result = await send<Schemas["ConfirmResponse"]>(body);
      // Partial success is a real outcome and is reported line by line (§6.8: a form that
      // hides a problem teaches people to stop looking for one).
      if (result.failed.length) {
        setLineErrors(Object.fromEntries(result.failed.map((failure) => [failure.transaction_id, failure.detail])));
        notify.warning(`Recorded ${result.created.length}. ${result.failed.length} could not be recorded; the reason is on each line.`);
      } else {
        notify.success(`Recorded ${result.created.length} repayment${result.created.length === 1 ? "" : "s"}.`);
      }
      await refresh();
    } catch (error) {
      reportFailure(error, undefined, () => void confirm());
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <SectionLabel sticky>{heading}</SectionLabel>
      <p className="mb-3 text-callout text-ink-muted">
        {recordable.length
          ? `${recordable.length} incoming ${recordable.length === 1 ? "line looks" : "lines look"} like a customer settling up. Nothing is recorded until you tick it.`
          : "Nothing here needs recording."}
        {settled ? ` ${settled === 1 ? "1 already matches a payment" : `${settled} already match payments`} typed in by hand.` : ""}
      </p>
      <div ref={list} className="flex flex-col gap-2.5">
        {lines.map((line) => (
          <CreditLine
            key={line.id}
            line={line}
            review={reviews.get(line.id)}
            customers={customers}
            error={lineErrors[line.id]}
            register={(get) => {
              choices.current.set(line.id, get);
              return () => void choices.current.delete(line.id);
            }}
          />
        ))}
      </div>
      {truncated !== null ? <TruncationNotice count={truncated} /> : null}
      {recordable.length ? (
        <div className="mt-4">
          <Button variant="primary" block disabled={busy} onClick={() => void confirm()}>
            {busy ? "Recording…" : "Record ticked repayments"}
          </Button>
        </div>
      ) : null}
    </section>
  );
}

function CreditLine({
  line,
  review,
  customers,
  error,
  register,
}: {
  line: Transaction;
  review: Review | undefined;
  customers: Customer[];
  error: string | undefined;
  register: (get: () => Choice) => () => void;
}) {
  const proposals = review?.proposals ?? [];
  // §5.3a: only a REMEMBERED sender pre-selects, and only when it names one customer. A name
  // merely appearing in the narration is a suggestion, shown but not chosen.
  const remembered = proposals.filter((proposal) => proposal.confidence === "high");
  const preselect = remembered.length === 1 ? (remembered[0]?.credit_customer_id ?? "") : "";

  const form = useForm({ credit_customer_id: preselect, remember_sender: true, confirm: false });
  const latest = useRef<Choice>(form.values);
  useEffect(() => {
    latest.current = form.values;
  }, [form.values]);
  // Registered once; the getter reads the latest values through the ref.
  useEffect(() => register(() => latest.current), []);

  const header = (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <p className="text-body break-words text-ink">{line.narration}</p>
        <p className="text-footnote text-ink-muted">{businessDate(line.txn_date)}</p>
      </div>
      <span className="shrink-0 text-body font-semibold text-ink">
        <Amount value={line.amount} />
      </span>
    </div>
  );

  if (review?.verified_repayment_id) {
    return (
      <div data-arrive>
        <Card>
          {header}
          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            <Pill kind="open">already on the ledger</Pill>
            <span className="text-footnote text-ink-muted">Matches a bank payment typed in for this date and amount.</span>
          </div>
        </Card>
      </div>
    );
  }
  if (review?.ambiguous) {
    return (
      <div data-arrive>
        <Card>
          {header}
          <div className="mt-2.5 flex flex-col items-start gap-1.5">
            <Pill kind="review">matches more than one</Pill>
            <span className="text-footnote text-ink-muted">
              Several payments typed in by hand share this date and amount, so the app cannot tell which this is, and recording it again would count the money twice.
              Check the customers' ledgers.
            </span>
          </div>
        </Card>
      </div>
    );
  }

  const known = new Set(customers.map((customer) => customer.id));
  const suggestions = proposals.filter((proposal) => known.has(proposal.credit_customer_id));

  return (
    <div data-arrive>
      <Card>
        {header}
        <div className="mt-3 flex flex-col gap-3">
          {suggestions.length ? (
            <div className="flex flex-wrap gap-2">
              {suggestions.map((proposal) => (
                <button
                  key={proposal.credit_customer_id}
                  type="button"
                  onClick={() => form.set("credit_customer_id", proposal.credit_customer_id)}
                  aria-pressed={form.values.credit_customer_id === proposal.credit_customer_id}
                  className="pressable rounded-[var(--radius-control)] border border-hairline bg-surface px-3 py-2 text-left aria-pressed:border-accent aria-pressed:bg-accent-tint"
                >
                  <span className="block text-callout font-medium text-ink">{proposal.name}</span>
                  <span className="block text-caption text-ink-muted">{proposal.reason}</span>
                </button>
              ))}
            </div>
          ) : null}
          <SelectField
            form={form}
            name="credit_customer_id"
            label="Who sent it"
            options={[
              { value: "", label: "Choose a customer" },
              ...customers.map((customer) => ({ value: customer.id, label: customer.is_active ? customer.name : `${customer.name} (inactive)` })),
            ]}
          />
          <CheckboxField form={form} name="remember_sender" label="Remember this sender" hint="Next time this narration pre-selects them. You still confirm." />
          {/* §14: starts unticked. A person has to look before money lands on a ledger. */}
          <CheckboxField form={form} name="confirm" label="Record this repayment" />
          {error ? (
            <p role="alert" className="text-callout text-short">
              {error}
            </p>
          ) : null}
        </div>
      </Card>
    </div>
  );
}

function DebitsSection({ page }: { page: Schemas["TransactionPage"] }) {
  const undecided = page.items.filter((item) => item.is_expense === "undecided");
  const list = useRef<HTMLDivElement>(null);
  useArrival(list, true);

  return (
    <section>
      <SectionLabel>Money out</SectionLabel>
      <p className="mb-3 text-callout text-ink-muted">
        Was this a cost, or money moved between your own pockets? An IOCL payment is not an expense: it sits with them as an advance and comes back as fuel.
      </p>
      {undecided.length ? (
        <div ref={list} className="flex flex-col gap-2.5">
          {undecided.map((item) => (
            <DebitLine key={item.id} item={item} />
          ))}
        </div>
      ) : (
        <Empty>Every outgoing line has been answered.</Empty>
      )}
      {page.next_cursor ? <TruncationNotice count={page.items.length} /> : null}
    </section>
  );
}

function DebitLine({ item }: { item: Transaction }) {
  const refresh = useRefreshApi();
  const [busy, setBusy] = useState(false);

  async function answer(value: "yes" | "no") {
    setBusy(true);
    try {
      // Not a money record, so no Idempotency-Key: answering twice gives the same answer.
      const body: Schemas["TransactionUpdate"] = { is_expense: value };
      await api.patch(`/bank-transactions/${item.id}`, body);
      await refresh();
    } catch (error) {
      reportFailure(error);
      setBusy(false);
    }
  }

  const notACost = NOT_A_COST.has(item.classification);
  return (
    <div data-arrive>
      <Card>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-body break-words text-ink">{item.narration}</p>
            <div className="mt-1 flex flex-wrap items-center gap-2">
              <span className="text-footnote text-ink-muted">{businessDate(item.txn_date)}</span>
              <Pill kind={notACost ? "open" : "neutral"}>{classificationLabel(item.classification)}</Pill>
            </div>
          </div>
          <span className="shrink-0 text-body font-semibold text-ink">
            <Amount value={item.amount} />
          </span>
        </div>
        {notACost ? <p className="mt-2 text-footnote text-ink-muted">Money moved between your own accounts, not spent.</p> : null}
        {/* The suggestion is the filled button; the answer is still the person's (§5.3a). */}
        <div className="mt-3 grid grid-cols-2 gap-2">
          <Button size="sm" variant={item.suggested_expense === "yes" ? "primary" : "secondary"} disabled={busy} onClick={() => void answer("yes")}>
            An expense
          </Button>
          <Button size="sm" variant={item.suggested_expense === "no" ? "primary" : "secondary"} disabled={busy} onClick={() => void answer("no")}>
            Not an expense
          </Button>
        </div>
      </Card>
    </div>
  );
}

/* --- reconciliation: three comparisons, discrepancies first ------------------------------ */

export function BankReconciliationScreen() {
  const navigate = useGo();
  const imports = useApiQuery<Schemas["ImportPage"]>("/bank-statements/imports");
  const latest = imports.data?.items[0];
  const report = useApiQuery<Schemas["ReconciliationResponse"]>(
    "/bank-statements/reconciliation",
    latest ? { bank_account_id: latest.bank_account_id, date_from: latest.period_from, date_to: latest.period_to } : undefined,
    { enabled: Boolean(latest) },
  );

  let body: ReactNode;
  if (imports.isPending || (latest && report.isPending)) {
    body = <Skeleton rows={4} />;
  } else if (imports.isError || report.isError) {
    body = (
      <ErrorCard
        error={imports.error ?? report.error}
        onRetry={() => {
          void imports.refetch();
          void report.refetch();
        }}
      />
    );
  } else if (!latest) {
    body = <Empty>Import a statement first. Reconciliation compares its lines with the days they came from.</Empty>;
  } else if (report.data) {
    const r = report.data;
    const awaiting = r.credits.filter((credit) => credit.verified_repayment_id === null && !credit.ambiguous).length;
    const ambiguous = r.credits.filter((credit) => credit.ambiguous).length;
    body = (
      <div className="flex flex-col gap-4">
        <Card>
          <SectionLabel>The statement</SectionLabel>
          <ListRow label="From" value={businessDate(r.date_from)} />
          <ListRow label="To" value={businessDate(r.date_to)} />
          <ListRow
            label="Settling the next morning"
            detail={r.boundary_settled_on ? `Arrived ${businessDate(r.boundary_settled_on)}` : "The statement stops before it arrived"}
            value={<Amount value={r.boundary_settlement} absent="not in this file" />}
          />
        </Card>
        <SettlementsCard items={r.settlements} />
        <DepositsCard items={r.deposits} />
        <Card>
          <SectionLabel>Udhaar paid into the bank</SectionLabel>
          <ListRow label="Already on a ledger" value={String(r.credits.length - awaiting - ambiguous)} />
          <ListRow label="Waiting for you to say who sent it" value={String(awaiting)} strong={awaiting > 0} />
          {ambiguous ? <ListRow label="Match more than one typed-in payment" value={String(ambiguous)} /> : null}
          {awaiting ? (
            <div className="mt-3">
              <Button variant="primary" block onClick={() => navigate("/credit/bank/review")}>
                Review them
              </Button>
            </div>
          ) : null}
        </Card>
      </div>
    );
  }

  return (
    <>
      <ScreenTitle title="Reconciliation" subtitle={latest ? businessDateRange(latest.period_from, latest.period_to) : undefined} />
      <BackToBank />
      {body}
    </>
  );
}

function SettlementsCard({ items }: { items: Settlement[] }) {
  const mismatched = items.filter((item) => !item.matches);
  return (
    <Card>
      <SectionLabel>Card and UPI against Paytm</SectionLabel>
      <p className="text-callout text-ink-muted">
        {items.length - mismatched.length} of {items.length} days settled exactly. Paytm pays a whole day's card and UPI together the next morning.
      </p>
      {mismatched.length ? (
        <div className="mt-2">
          {mismatched.map((item) => (
            <ListRow
              key={item.business_date}
              label={businessDate(item.business_date)}
              detail={
                // `settled: null` is its own sentence: "nothing arrived" and "₹0 arrived" are
                // different facts, and only one of them is a discrepancy (§6.8).
                (item.settled === null
                  ? `Nothing has arrived yet for this day (expected ${format(item.expected)})`
                  : `Expected ${format(item.expected)}, received ${format(item.settled)}`) +
                // §13.20: an unreconciled day's expected figure is live, not a record.
                (item.source === "computed" ? " · day not reconciled yet" : "")
              }
              value={<Amount value={item.difference} sign absent="pending" />}
            />
          ))}
        </div>
      ) : (
        <p className="mt-2 text-body text-ink">Every day agrees.</p>
      )}
    </Card>
  );
}

const DEPOSIT_KINDS: Record<string, string> = {
  missing_from_books: "In the bank, not in the app",
  missing_from_bank: "In the app, never reached the bank",
};

function DepositsCard({ items }: { items: Deposit[] }) {
  const problems = items.filter((item) => item.kind !== "matched");
  // Matched but slow: worth a glance, not an alarm. Cash normally reaches the branch the next
  // morning, so anything longer is a deposit that sat somewhere.
  const slow = items.filter((item) => item.kind === "matched" && item.days_late !== null && item.days_late > 1);
  return (
    <Card>
      <SectionLabel>Cash deposits</SectionLabel>
      <p className="text-callout text-ink-muted">
        {items.length - problems.length} of {items.length} matched
        {slow.length ? `, ${slow.length} took more than a day to reach the bank.` : "."}
      </p>
      {problems.length ? (
        <div className="mt-2">
          {problems.map((item) => (
            <ListRow
              key={`${item.kind}-${item.transaction_id ?? item.bank_deposit_id ?? item.txn_date}`}
              label={businessDate(item.txn_date)}
              detail={DEPOSIT_KINDS[item.kind] ?? item.kind}
              value={<Amount value={item.amount} />}
            />
          ))}
        </div>
      ) : (
        <p className="mt-2 text-body text-ink">Every deposit agrees.</p>
      )}
    </Card>
  );
}
