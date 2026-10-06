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

import { useRef, useState } from "react";
import { BellIcon, ChartBarIcon, FlagIcon } from "@phosphor-icons/react";
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
import { Button, Card, Empty, ErrorCard, LinkTile, ListCard, RowLink, SectionLabel, Skeleton } from "../ui/primitives";
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
        <Skeleton shape="cards" rows={3} />
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
            // The band Today uses (Phase 25 D3): the shift on the left, its acts on the right on a
            // wide screen and full width under the thumb on a phone (Phase 28 D6). Each act's
            // explanation stays beneath it, in the order the buttons read.
            <div className="flex flex-col gap-4">
              <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
                <div>
                  <p className="text-footnote font-medium text-ink-muted">Open shift</p>
                  <p className="text-subhead text-ink">
                    {businessDate(open.business_date)} · shift {open.sequence}
                  </p>
                </div>
                <div className="flex flex-col gap-2 sm:flex-row md:shrink-0">
                  {/* Named for what it shows, never for what a reader might wish it did (§14). */}
                  <Button className="w-full md:w-auto md:min-w-[10rem]" onClick={() => navigate(`/shifts/${open.id}/cash-position`)}>
                    Cash position
                  </Button>
                  {satisfies(me.role, "admin") ? (
                    <Button
                      className="w-full md:w-auto md:min-w-[10rem]"
                      onClick={() => {
                        setVoidTarget(open);
                        setVoiding(true);
                      }}
                    >
                      Void shift
                    </Button>
                  ) : null}
                </div>
              </div>
              <div className="flex flex-col gap-1.5 border-t border-hairline pt-3 text-footnote text-ink-muted">
                <p>
                  <span className="font-medium text-ink">Cash position</span> shows what this salesman should be holding and the gap. It is a report: nothing
                  is written, and the day is reconciled once every shift on it is closed.
                </p>
                {satisfies(me.role, "admin") ? (
                  <p>
                    <span className="font-medium text-ink">Void shift</span> is for a shift opened for the wrong day: an empty one can be voided, and the day it
                    was blocking can then be opened.
                  </p>
                ) : null}
              </div>
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
            <ListCard>
              {days.slice(0, RECENT).map((day) => (
                <DayRow key={day.business_date} day={day} unblocked={unblocked} />
              ))}
            </ListCard>
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
              <ListCard>
                {owed.map((row) => (
                  <RowLink key={row.salesman_id} onClick={() => navigate(`/salesmen/${row.salesman_id}/ledger`)}>
                    <span className="grow text-body text-ink">{row.full_name}</span>
                    <span className="text-short">
                      <Amount value={row.outstanding} />
                    </span>
                  </RowLink>
                ))}
              </ListCard>
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
            <LinkTile Icon={FlagIcon} label="Flagged expenses" onClick={() => navigate("/expenses/flagged")} />
            <LinkTile Icon={ChartBarIcon} label="The latest trading week" onClick={() => navigate("/reports")} />
            <LinkTile Icon={BellIcon} label="Alerts" onClick={() => navigate("/reports/alerts")} />
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

