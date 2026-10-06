/* The Cash tab: what needs doing, and who owes what (Phase 15's worklist, rebuilt in Phase 23).
 *
 * The top of the tab is the days that need you, oldest first, each offering the one act that
 * moves it on: §4.7's principle at the interface, the abnormal day visible rather than
 * reassigned to nobody's attention. Oldest first is not presentation: §6.5's opening balance
 * chains from the most recent summary, so the reconcile button exists on exactly one day.
 *
 * "Reconcile this shift" was once a button here that only navigated to a read-only report; a
 * manager pressed it, believed the day was settled, and nothing was written (§14). So the link
 * to a shift's cash position is named for the figure it shows.
 *
 * The open-shift card also offers an admin "Void shift" (Phase 27, §6.8): this card is where a
 * shift opened for the wrong day is noticed, because it blocks the day the worklist is asking for.
 */

import { type ReactNode, useRef, useState } from "react";
import { BellIcon, CaretRightIcon, ChartBarIcon, FlagIcon } from "@phosphor-icons/react";
import { useApiQuery } from "../api/queries";
import type { Schemas } from "../api/types";
import { ScreenTitle } from "../app/chrome";
import { useGo } from "../app/navigation";
import { useSession } from "../app/session";
import { oldestUnreconciled } from "../lib/days";
import { isNegative, isZero } from "../lib/money";
import { satisfies } from "../lib/roles";
import { businessDate } from "../lib/time";
import { Amount } from "../ui/Amount";
import { useArrival } from "../ui/motion";
import { Button, Card, Empty, ErrorCard, SectionLabel, Skeleton } from "../ui/primitives";
import { Sheet } from "../ui/Sheet";
import { DayRow, useDays, useReconcile, WorklistCard } from "./days";
import { useForgetShift, VoidShiftForm } from "./today";

const RECENT = 8;

export function CashScreen() {
  const navigate = useGo();
  const { me } = useSession();
  // The shift being voided is held here rather than read from `shift.data`, because a successful
  // void makes `shift.data` empty -- and the sheet must still have something to close over.
  const [voidTarget, setVoidTarget] = useState<Schemas["ShiftResponse"] | null>(null);
  const [voiding, setVoiding] = useState(false);
  const forgetShift = useForgetShift();
  const shift = useApiQuery<Schemas["ShiftResponse"]>("/shifts/current", undefined, { absentOn: ["NO_OPEN_SHIFT"] });
  const outstanding = useApiQuery<Schemas["OutstandingReport"]>("/salesman-shortfalls/outstanding");
  const { days, pending, error, refetch } = useDays();
  const { reconcile, busy, sheet } = useReconcile();
  const work = useRef<HTMLDivElement>(null);
  useArrival(work, Boolean(days));

  if (pending || shift.isPending || outstanding.isPending) {
    return (
      <>
        <ScreenTitle large title="Cash" />
        <Skeleton rows={4} />
      </>
    );
  }
  const failed = error ?? shift.error ?? outstanding.error;
  if (failed || !days) {
    return (
      <>
        <ScreenTitle large title="Cash" />
        <ErrorCard error={failed} onRetry={() => void Promise.all([refetch(), shift.refetch(), outstanding.refetch()])} />
      </>
    );
  }

  const unblocked = oldestUnreconciled(days);
  // Unfinished days, oldest first. A day with an open shift is being typed in, not waiting.
  const needsYou = days
    .filter((day) => !(day.summary && day.summary.is_finalised))
    .filter((day) => !day.shifts.some((entry) => entry.status === "open"))
    .reverse();
  const owed = (outstanding.data?.items ?? []).filter((row) => !isNegative(row.outstanding) && !isZero(row.outstanding));
  const open = shift.data;

  return (
    <>
      <ScreenTitle large title="Cash" subtitle={needsYou.length ? `${needsYou.length} day${needsYou.length === 1 ? "" : "s"} need you` : "Nothing waiting"} />
      <div className="flex flex-col gap-6">
        <Card>
          {open ? (
            <div className="flex flex-col gap-3">
              <div>
                <p className="text-footnote font-medium text-ink-muted">Open shift</p>
                <p className="text-subhead text-ink">
                  {businessDate(open.business_date)} · shift {open.sequence}
                </p>
              </div>
              {/* Named for what it shows, never for what a reader might wish it did (§14). */}
              <Button block onClick={() => navigate(`/shifts/${open.id}/cash-position`)}>
                Cash position
              </Button>
              <p className="text-footnote text-ink-muted">
                Shows what this salesman should be holding and the gap. It is a report: nothing is written, and the day is reconciled once every shift on it is closed.
              </p>
              {satisfies(me.role, "admin") ? (
                <>
                  <Button
                    block
                    onClick={() => {
                      setVoidTarget(open);
                      setVoiding(true);
                    }}
                  >
                    Void shift
                  </Button>
                  <p className="text-footnote text-ink-muted">Opened for the wrong day? An empty shift can be voided, and the day it was blocking can then be opened.</p>
                </>
              ) : null}
            </div>
          ) : (
            <p className="text-callout text-ink-muted">No shift is currently open.</p>
          )}
        </Card>

        <section>
          <SectionLabel sticky>Needs you</SectionLabel>
          <div ref={work} className="flex flex-col gap-3">
            {needsYou.length ? (
              needsYou.map((day) => (
                <WorklistCard key={day.business_date} day={day} unblocked={unblocked} onReconcile={(date) => void reconcile(date)} busy={busy === day.business_date} />
              ))
            ) : (
              <Empty>Every day that traded has been reconciled and finalised.</Empty>
            )}
          </div>
        </section>

        <section>
          <SectionLabel sticky>Trading days</SectionLabel>
          {days.length ? (
            <Card className="py-1 sm:py-1">
              {days.slice(0, RECENT).map((day) => (
                <DayRow key={day.business_date} day={day} unblocked={unblocked} />
              ))}
            </Card>
          ) : (
            <Empty>No trading day has been entered yet.</Empty>
          )}
          <div className="mt-3">
            <Button block onClick={() => navigate("/days")}>
              All days
            </Button>
          </div>
        </section>

        <section>
          <SectionLabel sticky>Salesman balances</SectionLabel>
          {owed.length ? (
            <>
              <Card className="py-1 sm:py-1">
                {owed.map((row) => (
                  <button
                    key={row.salesman_id}
                    type="button"
                    onClick={() => navigate(`/salesmen/${row.salesman_id}/ledger`)}
                    className="pressable flex w-full items-center justify-between gap-3 border-b border-hairline py-3 text-left last:border-b-0"
                  >
                    <span className="text-body text-ink">{row.full_name}</span>
                    <span className="flex items-center gap-2 text-short">
                      <Amount value={row.outstanding} />
                      <CaretRightIcon size={16} className="text-ink-faint" aria-hidden />
                    </span>
                  </button>
                ))}
              </Card>
              {/* §13.15, where somebody will see it. */}
              <p className="mt-2 text-footnote text-ink-muted">
                A shortfall can only be repaid in cash. V1 has no way to write one off, so a small figure nobody will chase stays here.
              </p>
            </>
          ) : (
            <Empty>Nobody owes a shortfall.</Empty>
          )}
        </section>

        {/* Deliberately last: this tab is for doing the reconciliation; these look back at it. */}
        <section>
          <SectionLabel sticky>Look back</SectionLabel>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <LookBack icon={<FlagIcon size={20} aria-hidden />} label="Flagged expenses" onClick={() => navigate("/expenses/flagged")} />
            <LookBack icon={<ChartBarIcon size={20} aria-hidden />} label="The latest trading week" onClick={() => navigate("/reports")} />
            <LookBack icon={<BellIcon size={20} aria-hidden />} label="Alerts" onClick={() => navigate("/reports/alerts")} />
          </div>
        </section>
      </div>
      {sheet}
      {/* Outside the open-shift card, like the reconcile sheet above it: a void empties that
          card, and a sheet inside it would vanish mid-close instead of sliding away. Here, unlike
          on Today, the refresh can run at once -- the card changes behind the leaving sheet. */}
      {voidTarget ? (
        <Sheet open={voiding} onClose={() => setVoiding(false)} title="Void shift" subtitle={`${businessDate(voidTarget.business_date)} · shift ${voidTarget.sequence}`}>
          <VoidShiftForm
            shift={voidTarget}
            onVoided={() => {
              setVoiding(false);
              void forgetShift(voidTarget.id);
            }}
          />
        </Sheet>
      ) : null}
    </>
  );
}

function LookBack({ icon, label, onClick }: { icon: ReactNode; label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="pressable liftable flex items-center gap-3 rounded-[var(--radius-card)] border border-hairline bg-surface px-4 py-3.5 text-left shadow-1"
    >
      <span className="text-accent">{icon}</span>
      <span className="grow text-body text-ink">{label}</span>
      <CaretRightIcon size={16} className="text-ink-faint" aria-hidden />
    </button>
  );
}
