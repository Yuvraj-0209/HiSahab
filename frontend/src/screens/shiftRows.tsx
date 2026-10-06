/* The frame every per-shift money list shares (non-fuel sales, bank deposits, credit sales,
 * credit repayments): the shift's status decides whether anything may be added, a total sits at
 * the top as the server computed it (§14: never summed here), the rows follow, and a capped list
 * says so rather than letting a reader trust a partial total.
 */

import { Children, isValidElement, type ReactNode, useRef } from "react";
import { PlusIcon } from "@phosphor-icons/react";
import type { Schemas } from "../api/types";
import { ScreenActions, ScreenTitle } from "../app/chrome";
import { useFlipList } from "../motion/flip";
import { Amount } from "../ui/Amount";
import { Arrive } from "../ui/Arrive";
import { Button, Card, Empty, HeroFigure, TruncationNotice } from "../ui/primitives";

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
  // A saved row or a reversal pair slides the rest down rather than teleporting them (Phase 24).
  const list = useRef<HTMLDivElement>(null);
  useFlipList(list, Children.toArray(children).map((child) => (isValidElement(child) ? String(child.key) : "")).join("|"));
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
      {/* The total arrives, then the list as one block: its rows belong to Flip (ui/Arrive.tsx). */}
      <Arrive className="flex flex-col gap-5">
        <div data-arrive>
          <Card>
            <HeroFigure label={totalLabel}>
              <Amount value={total} />
            </HeroFigure>
            {extra}
            <p className="mt-3 max-w-[60ch] text-callout text-ink-muted">{explanation}</p>
          </Card>
        </div>
        <div data-arrive>
          {count ? (
            <div ref={list} className="flex flex-col gap-3">
              {children}
            </div>
          ) : (
            <Empty>{emptyText}</Empty>
          )}
        </div>
        {truncated ? <TruncationNotice count={count} /> : null}
      </Arrive>
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
          <h2 className="text-body font-semibold text-ink">{title}</h2>
          {caption ? <p className="text-footnote text-ink-muted">{caption}</p> : null}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {badges}
          <span className="text-subhead text-ink">
            <Amount value={amount} />
          </span>
        </div>
      </div>
      {notes}
      {actions ? <div className="mt-4 flex flex-wrap gap-2">{actions}</div> : null}
    </Card>
  );
}
