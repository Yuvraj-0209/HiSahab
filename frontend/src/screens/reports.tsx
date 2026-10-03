/* Reports: the week, and what needs looking at (Phase 13, rebuilt in Phase 23).
 *
 * §13.20 splits every day into two kinds of claim: a `snapshot` is what a manager was TOLD and
 * nothing may rewrite it; a `computed` day is an estimate of a day still in motion. Every row
 * says which, in words, and the chart draws them differently.
 *
 * `variance: null` means nobody counted -- most days under §6.5's locker model -- and reads "not
 * counted", never ₹0.00. There is no arithmetic here: every figure is the server's string, and
 * bar heights arrive as CSS percentages for exactly that reason.
 *
 * `from`/`to` come from the URL and are omitted when absent, so the server picks the default
 * window anchored on the latest trading day (§13.30) -- recomputing that here would be a second
 * copy of a timezone rule.
 */

import { useRef } from "react";
import { useSearchParams } from "react-router";
import { CaretRightIcon } from "@phosphor-icons/react";
import { useApiQuery } from "../api/queries";
import type { Schemas } from "../api/types";
import { ScreenActions, ScreenTitle } from "../app/chrome";
import { sharedSource, useGo } from "../app/navigation";
import { varianceLabel } from "../lib/money";
import { businessDate } from "../lib/time";
import { Amount } from "../ui/Amount";
import { SalesBars, VarianceStrip } from "../ui/chart";
import { useArrival } from "../ui/motion";
import { Button, Card, Empty, ErrorCard, Pill, SectionLabel, Skeleton } from "../ui/primitives";
import { SOURCE_PILL } from "./days";

function useWindow() {
  const [params] = useSearchParams();
  return { from: params.get("from") ?? undefined, to: params.get("to") ?? undefined };
}

export function ReportsScreen() {
  const navigate = useGo();
  const window = useWindow();
  const report = useApiQuery<Schemas["RangeReportResponse"]>("/reports/range", window);
  const list = useRef<HTMLDivElement>(null);
  useArrival(list, Boolean(report.data));

  if (report.isPending) {
    return (
      <>
        <ScreenTitle title="Reports" />
        <Skeleton rows={4} />
      </>
    );
  }
  if (report.isError || !report.data) {
    return (
      <>
        <ScreenTitle title="Reports" />
        <ErrorCard error={report.error} onRetry={() => void report.refetch()} />
      </>
    );
  }

  const r = report.data;
  return (
    <>
      <ScreenTitle title="Reports" subtitle={`${businessDate(r.from)} to ${businessDate(r.to)}`} />
      <ScreenActions>
        <Button size="sm" onClick={() => navigate("/reports/alerts")}>
          Alerts
        </Button>
      </ScreenActions>
      <div className="flex flex-col gap-5">
        <Card>
          <SectionLabel>Total sales</SectionLabel>
          {r.days.length ? <SalesBars days={r.days} onSelect={(day) => navigate(`/days/${day.business_date}`)} /> : <Empty>No days in this window.</Empty>}
          <div className="mt-4">
            <VarianceStrip days={r.days} />
          </div>
          <p className="mt-2 text-[0.8125rem] text-ink-muted">
            Bar height is relative to the tallest day in this window. A solid bar is a reconciled day; a pale one is calculated just now. A day is flagged when its variance exceeds <Amount value={r.threshold} />.
          </p>
        </Card>

        <section>
          <SectionLabel>Day by day</SectionLabel>
          {r.days.length ? (
            <Card className="py-1 sm:py-1">
              <div ref={list}>
                {[...r.days].reverse().map((day) => {
                  const variance = varianceLabel(day.variance);
                  return (
                    <button
                      key={day.business_date}
                      type="button"
                      data-arrive
                      onClick={(event) => navigate(`/days/${day.business_date}`, { shared: sharedSource(event) })}
                      className="pressable flex w-full items-center gap-3 border-b border-hairline py-3 text-left last:border-b-0"
                    >
                      <div className="min-w-0 grow">
                        <p data-shared-source className="text-[0.9375rem] text-ink">
                          {businessDate(day.business_date)}
                        </p>
                        <div className="mt-1 flex flex-wrap gap-1.5">
                          <Pill kind={SOURCE_PILL[day.source] ?? "neutral"}>{day.source.replace("_", " ")}</Pill>
                          {day.is_finalised ? <Pill kind="locked">finalised</Pill> : null}
                          {/* A flag in words, not a tinted row: a tint under a pill blurs both. */}
                          {day.alert || day.requires_review ? <Pill kind="review">flagged</Pill> : null}
                        </div>
                      </div>
                      <div className="flex shrink-0 flex-col items-end">
                        <span className="tabular text-[0.9375rem] text-ink">
                          <Amount value={day.total_sales} absent="not known" />
                        </span>
                        <span className={`tabular text-[0.8125rem] ${variance.className}`}>{variance.text}</span>
                      </div>
                      <CaretRightIcon size={16} className="shrink-0 text-ink-faint" aria-hidden />
                    </button>
                  );
                })}
              </div>
            </Card>
          ) : (
            <Empty>No days in this window.</Empty>
          )}
        </section>

        <Card>
          <SectionLabel>Reading this</SectionLabel>
          <p className="text-[0.8125rem] text-ink-muted">{r.basis}</p>
        </Card>
      </div>
    </>
  );
}

const ALERT_LABEL: Record<string, string> = {
  variance_exceeds_threshold: "Cash variance",
  day_not_reconciled: "Never reconciled",
  summary_requires_review: "Summary flagged",
  unreviewed_flagged_expenses: "Expenses awaiting review",
  reading_requires_review: "Reading flagged",
  open_shift_on_a_past_date: "Shift still open",
};

export function AlertsScreen() {
  const navigate = useGo();
  const window = useWindow();
  const report = useApiQuery<Schemas["AlertsResponse"]>("/reports/variance-alerts", window);
  const list = useRef<HTMLDivElement>(null);
  useArrival(list, Boolean(report.data));

  if (report.isPending) {
    return (
      <>
        <ScreenTitle title="Alerts" />
        <Skeleton rows={3} />
      </>
    );
  }
  if (report.isError || !report.data) {
    return (
      <>
        <ScreenTitle title="Alerts" />
        <ErrorCard error={report.error} onRetry={() => void report.refetch()} />
      </>
    );
  }

  const r = report.data;
  return (
    <>
      <ScreenTitle title="Alerts" subtitle={`${businessDate(r.from)} to ${businessDate(r.to)}`} />
      <ScreenActions>
        <Button size="sm" onClick={() => navigate("/reports")}>
          Week
        </Button>
      </ScreenActions>
      <div className="flex flex-col gap-5">
        {r.items.length ? (
          <div ref={list} className="flex flex-col gap-2.5">
            {r.items.map((alert, index) => (
              <button
                key={`${alert.kind}-${alert.business_date}-${index}`}
                type="button"
                data-arrive
                onClick={(event) => navigate(`/days/${alert.business_date}`, { shared: sharedSource(event) })}
                className="pressable flex w-full items-center gap-3 rounded-[var(--radius-card)] border border-warning bg-surface px-4 py-3.5 text-left shadow-1"
              >
                <div className="min-w-0 grow">
                  <p className="text-[0.9375rem] font-medium text-ink">{ALERT_LABEL[alert.kind] ?? alert.kind}</p>
                  <p className="text-[0.8125rem] text-ink-muted">{alert.detail}</p>
                  <p data-shared-source className="text-[0.75rem] text-ink-faint">
                    {businessDate(alert.business_date)}
                  </p>
                </div>
                {alert.amount !== null ? (
                  <span className="shrink-0 text-[0.9375rem] text-ink">
                    <Amount value={alert.amount} />
                  </span>
                ) : null}
                <CaretRightIcon size={16} className="shrink-0 text-ink-faint" aria-hidden />
              </button>
            ))}
          </div>
        ) : (
          <Empty>Nothing needs attention in this window.</Empty>
        )}
        <Card>
          <SectionLabel>Reading this</SectionLabel>
          <p className="text-[0.8125rem] text-ink-muted">{r.basis}</p>
          {/* §13.23's limitation, on the screen rather than only in the spec. */}
          <p className="mt-2 text-[0.8125rem] text-ink-muted">
            There is no dismiss. An alert clears when the thing it points at is dealt with: review the expense, reconcile the day, close the shift.
          </p>
        </Card>
      </div>
    </>
  );
}
