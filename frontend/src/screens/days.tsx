/* Days: every business date, where it stands, and the one act that moves it on. Rebuilt in
 * Phase 23 from days.js and daily_summaries.js; the rules live in src/lib/days.ts.
 *
 * ## One day, one screen
 *
 * `#/days/{date}` merges the report (provenance, cash, fuel, expenses, shifts) with the
 * summary's acts (count, finalise). §13.20 stays visible: a `snapshot` day is what a manager was
 * TOLD, a `computed` day is an estimate that can still move, and the screen says which in words.
 *
 * ## expected_closing is stored, not recomputed (§5.2)
 *
 * "If a calculation bug is fixed six months from now, you still need to know what the system
 * told the manager on that day." Every figure here is rendered as the row holds it.
 *
 * ## The opening balance chains (§6.5)
 *
 * counted (yesterday's physical count), carried (yesterday's expected closing, nobody counted),
 * or seeded (the first day, by an admin). The very first day cannot be a one-tap act: the server
 * answers OPENING_BALANCE_REQUIRED and only an admin can type what was in the locker.
 */

import { type ReactNode, useMemo, useRef, useState } from "react";
import { useParams } from "react-router";
import { CaretRightIcon } from "@phosphor-icons/react";
import { api, ApiError } from "../api/client";
import { useApiQuery, useRefreshApi } from "../api/queries";
import type { Schemas } from "../api/types";
import { ScreenActions, ScreenTitle } from "../app/chrome";
import { sharedSource, useGo } from "../app/navigation";
import { useSession } from "../app/session";
import { type Day, dayState, LIFECYCLE, mergeDays, oldestUnreconciled } from "../lib/days";
import { format, varianceLabel } from "../lib/money";
import { satisfies } from "../lib/roles";
import { businessDate, todayAtOutlet } from "../lib/time";
import { Amount } from "../ui/Amount";
import { describeSource } from "../ui/chart";
import { reportFailure } from "../ui/feedback";
import { TextField, useForm } from "../ui/form";
import { LifecycleStrip } from "../ui/lifecycle";
import { useArrival } from "../ui/motion";
import { Button, Card, Empty, ErrorCard, ListRow, Pill, type PillKind, SectionLabel, Skeleton } from "../ui/primitives";
import { Sheet } from "../ui/Sheet";
import { notify } from "../ui/toast";
import { commitTick } from "../motion/haptic";


type Summary = Schemas["app__api__v1__daily_summaries__SummaryResponse"];
type Shift = Schemas["ShiftResponse"];
export type MergedDay = Day<Shift, Summary>;

/** How many days the hub and the list ask for in one go. */
const PAGE = 40;

/** §13.20's provenance as a pill. Snapshot is the only settled state. */
export const SOURCE_PILL: Record<string, PillKind> = { snapshot: "open", computed: "closed", no_trading: "neutral", unavailable: "review" };

const OPENING_SOURCE: Record<string, string> = {
  counted: "carried from yesterday's physical count",
  carried: "carried from yesterday's expected closing, nobody counted",
  seeded: "seeded by an admin, the first day",
};

/** The two reads the merge needs, shared by this screen and the Cash hub. */
export function useDays() {
  const shifts = useApiQuery<Schemas["ShiftPage"]>("/shifts", { limit: PAGE });
  const summaries = useApiQuery<Schemas["SummaryPage"]>("/daily-summaries", { limit: PAGE });
  const days = useMemo(
    () => (shifts.data && summaries.data ? mergeDays<Shift, Summary>(shifts.data.items, summaries.data.items) : null),
    [shifts.data, summaries.data],
  );
  return {
    days,
    pending: shifts.isPending || summaries.isPending,
    error: shifts.error ?? summaries.error,
    refetch: () => Promise.all([shifts.refetch(), summaries.refetch()]),
  };
}

/* --- reconciling ----------------------------------------------------------------------- */

/** Reconcile straight from a row. The first day ever needs a seeded opening, so the server's
 * OPENING_BALANCE_REQUIRED opens the sheet that can ask for one. */
export function useReconcile() {
  const navigate = useGo();
  const refresh = useRefreshApi();
  const [seeding, setSeeding] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function reconcile(date: string) {
    setBusy(date);
    try {
      await api.post("/daily-summaries", { business_date: date });
      commitTick();
      notify.success("Day reconciled.");
      await refresh();
      navigate(`/days/${date}`);
    } catch (error) {
      if (error instanceof ApiError && error.code === "OPENING_BALANCE_REQUIRED") setSeeding(date);
      else reportFailure(error);
    } finally {
      setBusy(null);
    }
  }

  const sheet = (
    <Sheet open={seeding !== null} onClose={() => setSeeding(null)} title="The first day">
      {seeding ? <CreateSummaryForm businessDate={seeding} onDone={() => setSeeding(null)} /> : null}
    </Sheet>
  );
  return { reconcile, busy, sheet };
}

function CreateSummaryForm({ businessDate: date, onDone }: { businessDate: string; onDone: () => void }) {
  const navigate = useGo();
  const refresh = useRefreshApi();
  const form = useForm({ business_date: date, opening_balance: "", notes: "" });
  const [busy, setBusy] = useState(false);

  async function submit() {
    form.clearErrors();
    setBusy(true);
    try {
      const body: Schemas["SummaryCreate"] = { business_date: form.values.business_date };
      if (form.values.opening_balance) body.opening_balance = form.values.opening_balance;
      if (form.values.notes) body.notes = form.values.notes;
      await api.post("/daily-summaries", body);
      onDone();
      commitTick();
      notify.success("Day reconciled.");
      await refresh();
      navigate(`/days/${form.values.business_date}`);
    } catch (error) {
      reportFailure(error, form);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <p className="text-callout text-ink-muted">
        No earlier day has been reconciled, so there is nothing to carry the opening balance from. Count what is in the locker at the start of this day. Only an admin can seed it, once.
      </p>
      <TextField form={form} name="business_date" label="Business date" type="date" max={todayAtOutlet()} required />
      <TextField
        form={form}
        name="opening_balance"
        label="Opening balance (first day only)"
        inputMode="decimal"
        hint="Supplying one when an earlier day exists is refused: from then on it chains."
      />
      <TextField form={form} name="notes" label="Notes (optional)" />
      <Button variant="primary" block disabled={busy} onClick={() => void submit()}>
        {busy ? "Reconciling…" : "Reconcile"}
      </Button>
    </div>
  );
}

/* --- rows and cards, shared with the Cash hub ------------------------------------------ */

export function DayRow({ day, unblocked }: { day: MergedDay; unblocked: string | null }) {
  const { me } = useSession();
  const navigate = useGo();
  const state = dayState(day, { role: me.role, unblocked });
  const variance = varianceLabel(day.summary?.variance ?? null);
  return (
    <button
      type="button"
      data-arrive
      onClick={(event) => navigate(`/days/${day.business_date}`, { shared: sharedSource(event) })}
      // A review flag is named by the lifecycle label ("Needs review"), not by tinting the row.
      className="pressable flex w-full items-center gap-3 border-b border-hairline py-3 text-left last:border-b-0"
    >
      <div className="min-w-0 grow">
        <p data-shared-source className="text-body text-ink">
          {businessDate(day.business_date)}
        </p>
        <LifecycleStrip state={state} />
      </div>
      <div className="flex shrink-0 flex-col items-end">
        <span className="tabular text-body text-ink">
          <Amount value={day.summary?.expected_closing ?? null} absent="not reconciled" />
        </span>
        <span className={`tabular text-footnote ${variance.className}`}>{variance.text}</span>
      </div>
      <CaretRightIcon size={16} className="shrink-0 text-ink-faint" aria-hidden />
    </button>
  );
}

export function WorklistCard({
  day,
  unblocked,
  onReconcile,
  busy,
}: {
  day: MergedDay;
  unblocked: string | null;
  onReconcile: (date: string) => void;
  busy: boolean;
}) {
  const { me } = useSession();
  const navigate = useGo();
  const state = dayState(day, { role: me.role, unblocked });
  return (
    <div data-arrive data-shared-scope>
      <Card className={day.summary?.requires_review ? "border-warning" : ""}>
        <div className="flex items-start justify-between gap-3">
          <p data-shared-source className="text-subhead text-ink">
            {businessDate(day.business_date)}
          </p>
          <Pill kind={state.kind}>{state.label}</Pill>
        </div>
        <div className="mt-2">
          <LifecycleStrip state={state} />
        </div>
        <p className="mt-2 text-footnote text-ink-muted">{state.hint}</p>
        {state.blockedBy ? (
          // §6.5: an ordering problem, not a permission one, so it names the day to do first.
          <p className="mt-2 text-footnote text-ink-muted">
            Reconcile {businessDate(state.blockedBy)} first: the opening balance chains from the day before.
          </p>
        ) : null}
        <div className="mt-4 flex flex-col gap-2 sm:flex-row">
          {state.action ? (
            <Button
              variant={state.action.primary ? "primary" : "secondary"}
              block
              disabled={busy}
              onClick={() => (state.action?.reconcile ? onReconcile(day.business_date) : state.action?.path && navigate(state.action.path))}
            >
              {busy ? "Reconciling…" : state.action.text}
            </Button>
          ) : null}
          <Button variant="plain" block onClick={(event) => navigate(`/days/${day.business_date}`, { shared: sharedSource(event) })}>
            Open the day
          </Button>
        </div>
      </Card>
    </div>
  );
}

/* --- #/days ---------------------------------------------------------------------------- */

export function DaysScreen() {
  const { days, pending, error, refetch } = useDays();
  const list = useRef<HTMLDivElement>(null);
  useArrival(list, Boolean(days));

  if (pending) {
    return (
      <>
        <ScreenTitle title="Days" />
        <Skeleton shape="list" rows={5} />
      </>
    );
  }
  if (error || !days) {
    return (
      <>
        <ScreenTitle title="Days" />
        <ErrorCard error={error} onRetry={() => void refetch()} />
      </>
    );
  }
  const unblocked = oldestUnreconciled(days);
  return (
    <>
      <ScreenTitle title="Days" subtitle={`${days.length} trading day${days.length === 1 ? "" : "s"}`} />
      <div className="flex flex-col gap-4">
        {days.length ? (
          <Card className="py-1 sm:py-1">
            <div ref={list}>
              {days.map((day) => (
                <DayRow key={day.business_date} day={day} unblocked={unblocked} />
              ))}
            </div>
          </Card>
        ) : (
          <Empty>No trading day has been entered yet.</Empty>
        )}
        <p className="text-footnote text-ink-muted">
          Every date the outlet traded appears here, whether or not it has been reconciled. The {LIFECYCLE.join(", ")} strip says how far each one has got.
        </p>
      </div>
    </>
  );
}

/* --- #/days/:businessDate -------------------------------------------------------------- */

type Act = "count" | "unfinalise" | null;

export function DayScreen() {
  const { businessDate: date = "" } = useParams();
  const { me } = useSession();
  const navigate = useGo();
  const refresh = useRefreshApi();
  const report = useApiQuery<Schemas["DailyReportResponse"]>(`/reports/daily/${date}`);
  const { days, pending, error, refetch } = useDays();
  const { reconcile, busy, sheet } = useReconcile();
  const [act, setAct] = useState<Act>(null);
  const [finalising, setFinalising] = useState(false);

  if (report.isPending || pending) {
    return (
      <>
        <ScreenTitle title="Day" subtitle={businessDate(date)} />
        <Skeleton rows={4} />
      </>
    );
  }
  if (report.isError || error || !report.data || !days) {
    return (
      <>
        <ScreenTitle title="Day" subtitle={businessDate(date)} />
        <ErrorCard error={report.error ?? error} onRetry={() => void Promise.all([report.refetch(), refetch()])} />
      </>
    );
  }

  const r = report.data;
  const cash = r.cash;
  const merged: MergedDay = days.find((day) => day.business_date === date) ?? { business_date: date, shifts: [], summary: null };
  const summary = merged.summary;
  const state = dayState(merged, { role: me.role, unblocked: oldestUnreconciled(days) });
  const isAdmin = satisfies(me.role, "admin");

  async function finalise() {
    setFinalising(true);
    try {
      await api.patch(`/daily-summaries/${date}/finalise`, {});
      commitTick();
      notify.success("Day finalised.");
      await refresh();
    } catch (failure) {
      reportFailure(failure);
    } finally {
      setFinalising(false);
    }
  }

  return (
    <>
      <ScreenTitle title="Day" subtitle={businessDate(r.business_date)} />
      <ScreenActions>
        <Button size="sm" onClick={() => navigate("/days")}>
          All days
        </Button>
      </ScreenActions>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* Where this day stands, and the one act that moves it on. */}
        <Card className="lg:col-span-2">
          <div className="flex items-center justify-between gap-3">
            <span className="text-footnote font-medium text-ink-muted">This day is</span>
            <Pill kind={state.kind}>{state.label}</Pill>
          </div>
          <div className="mt-2">
            <LifecycleStrip state={state} />
          </div>
          <p className="mt-2 text-footnote text-ink-muted">{state.hint}</p>
          {state.blockedBy ? (
            <p className="mt-2 text-footnote text-ink-muted">
              Reconcile {businessDate(state.blockedBy)} first: the opening balance chains from the day before.
            </p>
          ) : null}
          {state.action?.reconcile ? (
            <div className="mt-4">
              <Button variant="primary" block disabled={busy === date} onClick={() => void reconcile(date)}>
                {busy === date ? "Reconciling…" : state.action.text}
              </Button>
            </div>
          ) : null}
        </Card>

        {/* §13.16's flag: it says out loud that nothing moved. */}
        {summary?.requires_review ? (
          <Card className="border-warning lg:col-span-2">
            <Pill kind="review">needs review</Pill>
            <p className="mt-2 text-callout text-ink">
              {summary.review_note ?? "A shift beneath this day was reopened after it was finalised. Nothing was recomputed: the figures below are as they stood."}
            </p>
          </Card>
        ) : null}

        {/* Provenance, first and unmissable (§13.20). */}
        <Card className="lg:col-span-2">
          <span className="text-footnote font-medium text-ink-muted">These figures are</span>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <Pill kind={SOURCE_PILL[cash.source] ?? "neutral"}>{cash.source.replace("_", " ")}</Pill>
            <span className="text-body text-ink">{describeSource(cash.source)}</span>
          </div>
          {cash.unavailable_reason ? (
            <p className="mt-2 text-footnote text-ink-muted">
              This day cannot be calculated: {cash.unavailable_reason}. Enter the missing rate and it will appear.
            </p>
          ) : null}
          {cash.requires_review ? <p className="mt-2 text-footnote text-short">{cash.review_note ?? "Flagged for review."}</p> : null}
        </Card>

        {/* §13.22: a backdated price makes the live breakdown disagree with the stored total. */}
        {r.breakdown_reconciles === false ? (
          <Card className="border-warning lg:col-span-2">
            <SectionLabel>The fuel breakdown does not match</SectionLabel>
            <p className="text-body text-ink">
              This day was reconciled at one figure, but pricing it again now gives another. A fuel price was probably backdated beneath it.
            </p>
            <ListRow label="Recorded that day" value={<Amount value={r.snapshot_metered_fuel_sales} />} />
            <ListRow label="Priced again now" value={<Amount value={r.fuel_sales_total} />} />
            <p className="mt-2 text-footnote text-ink-muted">
              Neither figure has been changed. The recorded one is what the day was reconciled against; check the audit log for who revised the price.
            </p>
          </Card>
        ) : null}

        <Card>
          <span className="text-footnote font-medium text-ink-muted">Expected closing</span>
          <p className="mt-1 text-figure text-ink">
            <Amount value={cash.expected_closing} absent="not known" />
          </p>
          {cash.expected_closing === null && cash.source === "computed" ? (
            <p className="mt-2 text-footnote text-ink-muted">
              No earlier day has been reconciled, so there is no opening balance to carry from. An admin seeds the first one.
            </p>
          ) : null}
          <div className="mt-3">
            <Term label="Opening balance" value={cash.opening_balance} />
            {cash.opening_balance_source ? (
              <p className="pb-1 text-caption text-ink-muted">{OPENING_SOURCE[cash.opening_balance_source] ?? cash.opening_balance_source}</p>
            ) : null}
            <Term label="Metered fuel sales" value={cash.metered_fuel_sales} />
            <Term label="Non-fuel sales" value={cash.non_fuel_sales_total} />
            <Term label="Total sales" value={cash.total_sales} strong />
            <Term label="Card" value={cash.card_total} />
            <Term label="UPI" value={cash.upi_total} />
            <Term label="Wallet" value={cash.wallet_total} />
            <Term label="Udhaar issued" value={cash.credit_sales_total} />
            <Term label="Udhaar settled on card or UPI" value={cash.card_upi_credit_repayments} />
            <Term label="Cash repayments" value={cash.cash_credit_repayments} />
            <Term label="Shortfall settlements" value={cash.cash_shortfall_settlements} />
            <Term label="Cash expenses" value={cash.cash_expenses} />
            <Term label="Bank deposits" value={cash.bank_deposits_total} />
            <Term label="Shortfalls booked" value={cash.shortfalls_booked} />
            <Term label="Counted" value={cash.actual_counted} absent="not counted" strong />
            <VarianceRow value={cash.variance} />
          </div>
        </Card>

        <div className="flex flex-col gap-4">
          <Card>
            <SectionLabel>Fuel</SectionLabel>
            {r.fuel.length ? r.fuel.map((line) => <FuelRow key={line.fuel_type_id} line={line} />) : <Empty>Nothing was dispensed on this day.</Empty>}
            <div className="mt-1 border-t border-hairline">
              <ListRow label="Fuel sales" value={<Amount value={r.fuel_sales_total} absent="not known" />} strong />
              <ListRow label="Gross fuel margin" value={<Amount value={r.gross_fuel_margin_total} absent="not known" />} />
            </div>
            {r.fuels_missing_margin.length ? (
              <p className="mt-2 text-footnote text-ink-muted">
                No total, because no dealer commission has been entered for {r.fuels_missing_margin.join(", ")}. The figure is unknown, not zero.
              </p>
            ) : null}
            {/* §13.7's label, from the server, wherever profit is shown. */}
            <p className="mt-2 text-footnote text-ink-muted">{r.profit_basis}</p>
          </Card>

          <Card>
            <SectionLabel>Expenses</SectionLabel>
            {Object.keys(r.expenses_by_category).length ? (
              Object.entries(r.expenses_by_category).map(([code, amount]) => <ListRow key={code} label={code} value={<Amount value={amount as string} />} />)
            ) : (
              <Empty>No expenses on this day.</Empty>
            )}
            <div className="border-t border-hairline">
              <ListRow label="Total" value={<Amount value={r.expenses_total} />} strong />
            </div>
          </Card>

          <Card>
            <SectionLabel>Shifts</SectionLabel>
            {r.shifts.length ? (
              r.shifts.map((shift) => (
                <button
                  key={shift.id}
                  type="button"
                  onClick={() => navigate(`/shifts/${shift.id}`)}
                  className="pressable flex w-full items-center justify-between gap-3 border-b border-hairline py-3 text-left last:border-b-0"
                >
                  <span className="text-body text-ink">Shift {shift.sequence}</span>
                  <span className="flex items-center gap-2">
                    <Pill kind="neutral">{shift.status}</Pill>
                    <CaretRightIcon size={16} className="text-ink-faint" aria-hidden />
                  </span>
                </button>
              ))
            ) : (
              <Empty>No shifts on this day.</Empty>
            )}
          </Card>
        </div>

        {/* The acts need a summary to exist: a day with none has never been reconciled, and a
         * count field against nothing would blur an absent figure with a zero one (§14). */}
        {summary ? (
          <Card>
            <SectionLabel>The count</SectionLabel>
            <p className="text-footnote text-ink-muted">
              The physical cash in the locker is what carries forward, not the theoretical figure, so a shortage stays visible instead of disappearing into tomorrow. Most days are never counted, and that is normal.
            </p>
            {!summary.is_finalised ? (
              <div className="mt-4">
                <Button block onClick={() => setAct("count")}>
                  {summary.actual_counted === null ? "Record a count" : "Update the count"}
                </Button>
              </div>
            ) : null}
          </Card>
        ) : null}

        {summary?.notes ? (
          <Card>
            <p className="text-body text-ink">{summary.notes}</p>
          </Card>
        ) : null}

        {summary && isAdmin ? (
          <Card>
            <SectionLabel>{summary.is_finalised ? "Finalised" : "Before you finalise"}</SectionLabel>
            <ListRow label="Expected closing" value={<Amount value={summary.expected_closing} />} />
            <ListRow label="Counted" value={<Amount value={summary.actual_counted} absent="not counted" />} />
            <VarianceRow value={summary.variance} />
            <div className="mt-4">
              {summary.is_finalised ? (
                <Button block onClick={() => setAct("unfinalise")}>
                  Unfinalise
                </Button>
              ) : (
                <Button variant="primary" block disabled={finalising} onClick={() => void finalise()}>
                  {finalising ? "Finalising…" : "Finalise this day"}
                </Button>
              )}
            </div>
            <p className="mt-2 text-footnote text-ink-muted">
              {summary.is_finalised
                ? "Finalising is otherwise terminal. Unfinalising takes a mandatory reason and is audit-logged."
                : "Every shift on this date must be locked first, and the previous day finalised: the opening balance chains from it."}
            </p>
          </Card>
        ) : null}
      </div>

      {sheet}
      <Sheet open={act !== null} onClose={() => setAct(null)} title={act === "unfinalise" ? "Unfinalise day" : "Record the count"} subtitle={businessDate(date)}>
        {act === "count" && summary ? <CountForm date={date} summary={summary} onDone={() => setAct(null)} /> : null}
        {act === "unfinalise" ? <UnfinaliseForm date={date} onDone={() => setAct(null)} /> : null}
      </Sheet>
    </>
  );
}

function Term({ label, value, absent, strong = false }: { label: string; value: string | null; absent?: string; strong?: boolean }) {
  return <ListRow label={label} value={<Amount value={value} {...(absent ? { absent } : {})} />} strong={strong} />;
}

function VarianceRow({ value }: { value: string | null }) {
  // The variance sign runs opposite to a gap's (§6.4): varianceLabel, never gapLabel.
  const label = varianceLabel(value);
  return <ListRow label="Variance" value={label.text} valueClassName={label.className} strong />;
}

function FuelRow({ line }: { line: Schemas["FuelLineResponse"] }) {
  // The unit is read from the fuel, never assumed (§4.5). CBG reads kg.
  const unit = line.unit_of_measure === "kilogram" ? "kg" : "L";
  const detail: ReactNode =
    line.quantity === null ? "not entered" : `${line.quantity} ${unit} at ${format(line.rate_per_unit, { absent: "mixed rate" })} per ${unit}`;
  return (
    <ListRow
      label={line.display_name}
      detail={detail}
      value={
        <span className="flex flex-col items-end">
          <Amount value={line.sale_value} absent="not known" />
          {line.gross_fuel_margin === null ? (
            <span className="t-absent text-footnote">no commission entered</span>
          ) : (
            <span className="text-footnote text-ink-muted">{format(line.gross_fuel_margin)} margin</span>
          )}
        </span>
      }
    />
  );
}

function CountForm({ date, summary, onDone }: { date: string; summary: Summary; onDone: () => void }) {
  const refresh = useRefreshApi();
  const form = useForm({ actual_counted: summary.actual_counted ?? "", notes: summary.notes ?? "" });
  const [busy, setBusy] = useState(false);

  async function submit() {
    form.clearErrors();
    const changes = form.changes();
    if (Object.keys(changes).length === 0) return onDone();
    setBusy(true);
    try {
      await api.patch(`/daily-summaries/${date}`, changes);
      onDone();
      notify.success("Count recorded.");
      await refresh();
    } catch (error) {
      reportFailure(error, form);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <TextField
        form={form}
        name="actual_counted"
        label="Cash actually counted"
        inputMode="decimal"
        required
        hint="A physical count re-anchors the chain: tomorrow opens at this figure rather than the arithmetic one."
      />
      <TextField form={form} name="notes" label="Notes (optional)" />
      <Button variant="primary" block disabled={busy} onClick={() => void submit()}>
        {busy ? "Saving…" : "Save count"}
      </Button>
    </div>
  );
}

function UnfinaliseForm({ date, onDone }: { date: string; onDone: () => void }) {
  const refresh = useRefreshApi();
  const form = useForm({ reason: "" });
  const [busy, setBusy] = useState(false);

  async function submit() {
    form.clearErrors();
    setBusy(true);
    try {
      await api.patch(`/daily-summaries/${date}/unfinalise`, form.values);
      onDone();
      notify.success("Unfinalised.");
      // Nothing is recomputed (§13.16). Said out loud: the absence of a change is itself news.
      notify.info("Nothing was recomputed. Every stored figure is exactly as it was.");
      await refresh();
    } catch (error) {
      reportFailure(error, form);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <TextField form={form} name="reason" label="Why is this being unfinalised?" required hint="Audit-logged. 3 to 500 characters." />
      <Button variant="primary" block disabled={busy} onClick={() => void submit()}>
        {busy ? "Saving…" : "Unfinalise"}
      </Button>
    </div>
  );
}
