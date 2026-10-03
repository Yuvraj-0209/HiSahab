/* The frame every per-shift money list shares (non-fuel sales, bank deposits, credit sales,
 * credit repayments): the shift's status decides whether anything may be added, a total sits at
 * the top as the server computed it (§14: never summed here), the rows follow, and a capped list
 * says so rather than letting a reader trust a partial total.
 */

import type { ReactNode } from "react";
import { PlusIcon } from "@phosphor-icons/react";
import type { Schemas } from "../api/types";
import { ScreenActions, ScreenTitle } from "../app/chrome";
import { Amount } from "../ui/Amount";
import { Button, Card, Empty, TruncationNotice } from "../ui/primitives";

export function ShiftRowsFrame({
  title,
  shift,
  totalLabel,
  total,
  explanation,
  extra,
  onAdd,
  emptyText,
  count,
  truncated,
  children,
}: {
  title: string;
  shift: Schemas["ShiftResponse"];
  totalLabel: string;
  total: string;
  explanation: string;
  extra?: ReactNode;
  onAdd: () => void;
  emptyText: string;
  count: number;
  truncated: boolean;
  children: ReactNode;
}) {
  const editable = shift.status === "open";
  return (
    <>
      <ScreenTitle title={title} subtitle={`Shift ${shift.sequence} · ${shift.status}`} />
      {editable ? (
        <ScreenActions>
          <Button variant="primary" size="sm" icon={<PlusIcon size={16} weight="bold" aria-hidden />} onClick={onAdd}>
            Add
          </Button>
        </ScreenActions>
      ) : null}
      <div className="flex flex-col gap-5">
        <Card>
          <p className="text-[0.8125rem] font-medium text-ink-muted">{totalLabel}</p>
          <p className="mt-1 text-[2.25rem] leading-none font-semibold tracking-[-0.03em] text-ink">
            <Amount value={total} />
          </p>
          {extra}
          <p className="mt-3 max-w-[60ch] text-[0.875rem] text-ink-muted">{explanation}</p>
        </Card>
        {count ? <div className="flex flex-col gap-3">{children}</div> : <Empty>{emptyText}</Empty>}
        {truncated ? <TruncationNotice count={count} /> : null}
      </div>
    </>
  );
}

/** One money row as a card: a title, a caption, the amount, its reversal state, and actions. */
export function MoneyRowCard({
  title,
  caption,
  amount,
  badges,
  notes,
  actions,
}: {
  title: ReactNode;
  caption?: ReactNode;
  amount: string;
  badges?: ReactNode;
  notes?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <Card>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-[0.9375rem] font-semibold text-ink">{title}</h2>
          {caption ? <p className="text-[0.8125rem] text-ink-muted">{caption}</p> : null}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {badges}
          <span className="text-[1.125rem] font-semibold tracking-[-0.015em] text-ink">
            <Amount value={amount} />
          </span>
        </div>
      </div>
      {notes}
      {actions ? <div className="mt-4 flex flex-wrap gap-2">{actions}</div> : null}
    </Card>
  );
}
