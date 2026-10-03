/* The Credit tab: who owes what, and one customer's account (CLAUDE.md §5.2, §6.6, §8).
 * Rebuilt in Phase 23 from credit.js.
 *
 * Every figure is computed by the server and rendered as a string. A ledger is nothing BUT money
 * arithmetic, so `balance_after` arrives per row and this file never adds two amounts.
 *
 * `opening_balance: null` means nobody has entered what this customer already owed; "0.00"
 * means somebody checked and they were square (§6.8, §14). Collapsing them would tell the owner
 * his ledger is complete when it has not been started -- so the screen says which, in words.
 */

import { type ReactNode, useMemo, useRef } from "react";
import { useNavigate, useParams } from "react-router";
import { BankIcon, CaretRightIcon, HandCoinsIcon, ReceiptIcon } from "@phosphor-icons/react";
import { useApiQuery } from "../api/queries";
import type { Schemas } from "../api/types";
import { ScreenActions, ScreenTitle } from "../app/chrome";
import { useSession } from "../app/session";
import { compareSignedMoney, format, isNegative, isZero } from "../lib/money";
import { satisfies } from "../lib/roles";
import { businessDate } from "../lib/time";
import { Amount } from "../ui/Amount";
import { useArrival } from "../ui/motion";
import { Button, Card, Empty, ErrorCard, ListRow, Pill, SectionLabel, Skeleton, TruncationNotice } from "../ui/primitives";

type Balance = Schemas["OpeningBalanceListItem"];

const KIND_LABELS: Record<string, string> = { opening: "Opening balance", sale: "Udhaar issued", repayment: "Repayment" };

/** The sentence under a name: the one place the null-versus-zero distinction is visible. */
function standing(entry: Balance): string {
  if (entry.opening_balance === null) return "No opening balance entered, so this starts from zero";
  if (isNegative(entry.outstanding) && !isZero(entry.outstanding)) {
    return `In credit · opening ${format(entry.opening_balance)} on ${businessDate(entry.as_of_date)}`;
  }
  if (isZero(entry.outstanding)) return `Settled up · opening ${format(entry.opening_balance)} on ${businessDate(entry.as_of_date)}`;
  return `Since ${businessDate(entry.as_of_date)}`;
}

export function CreditHubScreen() {
  const { me } = useSession();
  const navigate = useNavigate();
  const balances = useApiQuery<Schemas["OpeningBalancePage"]>("/credit-opening-balances");
  const list = useRef<HTMLDivElement>(null);
  useArrival(list, Boolean(balances.data));

  // Biggest debt first, compared as strings: never Number() on money (§14).
  const rows = useMemo(
    () => [...(balances.data?.items ?? [])].sort((left, right) => compareSignedMoney(right.outstanding, left.outstanding)),
    [balances.data],
  );

  if (balances.isPending) {
    return (
      <>
        <ScreenTitle title="Credit" />
        <Skeleton rows={4} />
      </>
    );
  }
  if (balances.isError || !balances.data) {
    return (
      <>
        <ScreenTitle title="Credit" />
        <ErrorCard error={balances.error} onRetry={() => void balances.refetch()} />
      </>
    );
  }

  const unanchored = rows.filter((entry) => entry.opening_balance === null).length;

  return (
    <>
      <ScreenTitle title="Credit" subtitle={`${rows.length} customer${rows.length === 1 ? "" : "s"}`} />
      <div className="flex flex-col gap-5">
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
          <HubLink icon={<HandCoinsIcon size={20} aria-hidden />} label="Record a bank payment" onClick={() => navigate("/credit/repayments")} />
          {/* Phase 21: the fortnightly bill, checked before it goes out on the 1st and the 16th. */}
          <HubLink icon={<ReceiptIcon size={20} aria-hidden />} label="Billing statement" onClick={() => navigate("/credit/statement")} />
          <HubLink icon={<BankIcon size={20} aria-hidden />} label="Bank statement" onClick={() => navigate("/credit/bank")} />
        </div>

        {unanchored ? (
          <Card className="border-warning">
            <SectionLabel>Before you trust these figures</SectionLabel>
            <p className="text-[0.9375rem] text-ink">
              {unanchored} of {rows.length} customers {unanchored === 1 ? "has" : "have"} no opening balance entered. Their ledger starts at zero here, which is almost never what they actually owed.
            </p>
            <div className="mt-4">
              {satisfies(me.role, "admin") ? (
                <Button variant="primary" onClick={() => navigate("/credit/opening-balances")}>
                  Set opening balances
                </Button>
              ) : (
                <p className="text-[0.8125rem] text-ink-muted">An admin can enter them.</p>
              )}
            </div>
          </Card>
        ) : null}

        {rows.length ? (
          <div ref={list} className="flex flex-col gap-2.5">
            {rows.map((entry) => (
              <button
                key={entry.credit_customer_id}
                type="button"
                data-arrive
                onClick={() => navigate(`/credit/customers/${entry.credit_customer_id}`)}
                className="pressable flex w-full items-center gap-3 rounded-[var(--radius-card)] border border-hairline bg-surface px-4 py-3.5 text-left shadow-1"
              >
                <span className="min-w-0 grow">
                  <span className="flex items-center gap-2">
                    <span className="truncate text-[0.9375rem] font-semibold text-ink">{entry.name}</span>
                    {entry.is_active ? null : <Pill kind="neutral">inactive</Pill>}
                  </span>
                  <span className={`block text-[0.8125rem] ${entry.opening_balance === null ? "t-absent" : "text-ink-muted"}`}>
                    {standing(entry)}
                  </span>
                </span>
                <span className="shrink-0 text-[1.125rem] font-semibold tracking-[-0.015em] text-ink">
                  <Amount value={entry.outstanding} />
                </span>
                <CaretRightIcon size={16} className="shrink-0 text-ink-faint" aria-hidden />
              </button>
            ))}
          </div>
        ) : (
          <Empty>No credit customers yet. An admin adds them under Admin, Credit customers.</Empty>
        )}
      </div>
    </>
  );
}

function HubLink({ icon, label, onClick }: { icon: ReactNode; label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="pressable flex items-center gap-3 rounded-[var(--radius-card)] border border-hairline bg-surface px-4 py-3 text-left shadow-1"
    >
      <span className="text-accent">{icon}</span>
      <span className="grow text-[0.9375rem] text-ink">{label}</span>
      <CaretRightIcon size={16} className="text-ink-faint" aria-hidden />
    </button>
  );
}

/* --- one customer's account, newest first, with the balance beside every line ------------ */

export function CustomerLedgerScreen() {
  const { customerId = "" } = useParams();
  const navigate = useNavigate();
  const customer = useApiQuery<Schemas["CreditCustomerResponse"]>(`/credit-customers/${customerId}`);
  const ledger = useApiQuery<Schemas["app__api__v1__credit_customers__LedgerPage"]>(`/credit-customers/${customerId}/ledger`);
  const list = useRef<HTMLDivElement>(null);
  useArrival(list, Boolean(ledger.data));

  if (customer.isPending || ledger.isPending) {
    return (
      <>
        <ScreenTitle title="Ledger" />
        <Skeleton rows={4} />
      </>
    );
  }
  if (!customer.data || !ledger.data) {
    return (
      <>
        <ScreenTitle title="Ledger" />
        <ErrorCard error={customer.error ?? ledger.error} onRetry={() => void Promise.all([customer.refetch(), ledger.refetch()])} />
      </>
    );
  }

  const c = customer.data;
  const l = ledger.data;

  return (
    <>
      <ScreenTitle title="Ledger" subtitle={c.name} />
      <ScreenActions>
        <Button size="sm" onClick={() => navigate("/credit/repayments")}>
          Record a payment
        </Button>
      </ScreenActions>
      <div className="flex flex-col gap-5">
        <Card>
          <p className="text-[0.8125rem] font-medium text-ink-muted">Outstanding</p>
          <p className="mt-1 text-[2.25rem] leading-none font-semibold tracking-[-0.03em] text-ink">
            <Amount value={l.outstanding} />
          </p>
          <div className="mt-3">
            {/* Never `?? 0`: "not entered" and "0.00" are different facts (§6.8, §14). */}
            <ListRow label="Opening balance" value={<Amount value={l.opening_balance} absent="not entered" />} />
            <ListRow label="Credit limit" value={<Amount value={c.credit_limit} absent="no limit" />} />
            <ListRow label="Phone" value={c.phone} />
          </div>
          <p className="mt-2 text-[0.8125rem] text-ink-muted">
            {l.opening_balance === null
              ? "No opening balance has been entered, so this account starts from zero here, not from what they actually owed."
              : "Computed from the opening balance plus every sale and repayment, reversals included. Never stored, so it cannot drift."}
          </p>
        </Card>

        {l.items.length ? (
          <Card className="py-1 sm:py-1">
            <div ref={list}>
              {l.items.map((entry) => (
                <div key={entry.id} data-arrive className="flex items-center gap-3 border-b border-hairline py-3 last:border-b-0">
                  <div className="min-w-0 grow">
                    <p className="text-[0.9375rem] text-ink">{KIND_LABELS[entry.kind] ?? entry.kind}</p>
                    <p className="text-[0.8125rem] text-ink-muted">
                      {businessDate(entry.business_date)}
                      {entry.shift_id === null && entry.kind === "repayment" ? " · to the bank" : ""}
                    </p>
                  </div>
                  {entry.is_reversal ? <Pill kind="neutral">reversal</Pill> : null}
                  <div className="flex shrink-0 flex-col items-end">
                    <span className="text-[0.9375rem] text-ink">
                      <Amount value={entry.balance_delta} sign />
                    </span>
                    {/* The running balance: the column this screen exists for. Server-computed. */}
                    <span className="tabular text-[0.75rem] text-ink-muted">{format(entry.balance_after)}</span>
                  </div>
                </div>
              ))}
            </div>
          </Card>
        ) : (
          <Empty>Nothing on this account yet.</Empty>
        )}

        {l.truncated ? <TruncationNotice count={l.items.length} /> : null}
        {l.truncated ? (
          <p className="text-[0.8125rem] text-ink-muted">Older entries are not listed. The balances shown are still exact: each one is worked back from today.</p>
        ) : null}
      </div>
    </>
  );
}
