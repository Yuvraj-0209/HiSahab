/* The Entry hub: everything that gets typed into a shift. Rebuilt in Phase 23 from entry.js.
 *
 * §4.7: "The sales register has one line per day. The whole day is typed in after the fact, in
 * one sitting." This screen is that sitting -- the register's lines in the order somebody works
 * down a page, so nothing is discovered missing at close time. It resolves the open shift
 * itself: "Entry" as a tab means the shift being worked on now.
 */

import { type ComponentType, useRef } from "react";
import {
  BankIcon,
  CaretRightIcon,
  DropIcon,
  GaugeIcon,
  HandCoinsIcon,
  type IconProps,
  NotebookIcon,
  ReceiptIcon,
  WalletIcon,
} from "@phosphor-icons/react";
import { useApiQuery } from "../api/queries";
import type { Schemas } from "../api/types";
import { ScreenTitle } from "../app/chrome";
import { useGo } from "../app/navigation";
import { useSession } from "../app/session";
import { satisfies, type Role } from "../lib/roles";
import { businessDate } from "../lib/time";
import { useArrival } from "../ui/motion";
import { Button, Card, ErrorCard, Skeleton } from "../ui/primitives";

interface EntryLine {
  label: string;
  hint: string;
  path: string;
  role: Role;
  Icon: ComponentType<IconProps>;
}

const LINES: readonly EntryLine[] = [
  { label: "Nozzle readings", hint: "Confirm each opening against the meter, then the closing", path: "readings", role: "attendant", Icon: GaugeIcon },
  { label: "Collections", hint: "Cash, card, UPI and wallet. Cash is a declaration", path: "collections", role: "attendant", Icon: WalletIcon },
  { label: "Expenses", hint: "What was paid out, and by what method", path: "expenses", role: "attendant", Icon: ReceiptIcon },
  { label: "Non-fuel sales", hint: "Lubricants, coolant, anything no meter counts", path: "non-fuel-sales", role: "attendant", Icon: DropIcon },
  { label: "Credit sales", hint: "Udhaar issued. A receipt is mandatory", path: "credit-sales", role: "attendant", Icon: NotebookIcon },
  { label: "Credit repayments", hint: "A customer settling an old bill", path: "credit-repayments", role: "attendant", Icon: HandCoinsIcon },
  { label: "Bank deposits", hint: "Cash taken out of the locker to the bank", path: "bank-deposits", role: "manager", Icon: BankIcon },
];

export function EntryScreen() {
  const { me } = useSession();
  const navigate = useGo();
  const current = useApiQuery<Schemas["ShiftResponse"]>("/shifts/current", undefined, { absentOn: ["NO_OPEN_SHIFT"] });
  const list = useRef<HTMLDivElement>(null);
  useArrival(list, Boolean(current.data));

  if (current.isPending) {
    return (
      <>
        <ScreenTitle title="Entry" />
        <Skeleton rows={5} />
      </>
    );
  }
  if (current.isError) {
    return (
      <>
        <ScreenTitle title="Entry" />
        <ErrorCard error={current.error} onRetry={() => void current.refetch()} />
      </>
    );
  }
  const shift = current.data;
  if (!shift) {
    return (
      <>
        <ScreenTitle title="Entry" subtitle="No open shift" />
        <Card>
          <p className="text-[0.9375rem] text-ink">There is no open shift to type into.</p>
          <div className="mt-4">
            <Button variant="primary" onClick={() => navigate("/today")}>
              Go to Today
            </Button>
          </div>
        </Card>
      </>
    );
  }

  const lines = LINES.filter((line) => satisfies(me.role, line.role));

  return (
    <>
      <ScreenTitle title="Entry" subtitle={`${businessDate(shift.business_date)} · shift ${shift.sequence}`} />
      <div ref={list} className="flex flex-col gap-2.5">
        {lines.map((line) => (
          <button
            key={line.path}
            type="button"
            data-arrive
            onClick={() => navigate(`/shifts/${shift.id}/${line.path}`)}
            className="pressable flex w-full items-center gap-4 rounded-[var(--radius-card)] border border-hairline bg-surface px-4 py-3.5 text-left shadow-1"
          >
            <span className="grid size-11 shrink-0 place-items-center rounded-[var(--radius-control)] bg-accent-tint text-accent">
              <line.Icon size={22} aria-hidden />
            </span>
            <span className="min-w-0 grow">
              <span className="block text-[0.9375rem] font-medium text-ink">{line.label}</span>
              <span className="block text-[0.8125rem] text-ink-muted">{line.hint}</span>
            </span>
            <CaretRightIcon size={18} className="shrink-0 text-ink-faint" aria-hidden />
          </button>
        ))}
      </div>
    </>
  );
}
