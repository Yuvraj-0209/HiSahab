/* The audit trail, made readable (CLAUDE.md §5.3, §8, §9). Rebuilt in Phase 23 from
 * audit_logs.js.
 *
 * ## Admin only, and that is a leak boundary rather than a preference
 *
 * `old_values` / `new_values` on a `credit_customers` row hold `phone` and `credit_limit`, the
 * two fields §9 restricts on the customer's own route. A manager floor here would expose them
 * through another endpoint (§8).
 *
 * ## Both snapshots arrive whole; the diff is done here
 *
 * Phase 11 deliberately left diffing to the client. Only keys that actually changed are shown:
 * an entry echoing every column hides the change itself.
 *
 * ## Money inside a snapshot is a string, and null is a word
 *
 * Values render exactly as they arrive. On `credit_limit`, null means NO limit and 0 would mean
 * the opposite (§6.6), so null is shown as the word, never as a number.
 *
 * ## An unknown record id is an empty page, not a 404
 *
 * §5.3: `record_id` is not a foreign key, so "no such row" and "never changed" look identical,
 * and the empty state says exactly that.
 *
 * ## Paging
 *
 * Cursor pages through `useInfiniteQuery`; the cursor is opaque and never shared with another
 * list's (§9). Applying filters starts a new list rather than filtering the one on screen.
 */

import { useEffect, useRef, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { ApiError, api } from "../api/client";
import { apiKey } from "../api/queries";
import type { Schemas } from "../api/types";
import { ScreenTitle } from "../app/chrome";
import { dateTime } from "../lib/time";
import { reportFailure } from "../ui/feedback";
import { SelectField, TextField, useForm } from "../ui/form";
import { useArrival } from "../ui/motion";
import { Button, Card, Empty, ErrorCard, type PillKind, Pill, Skeleton } from "../ui/primitives";

type Entry = Schemas["AuditLogResponse"];
type Action = Schemas["AuditAction"];

const ACTIONS: Action[] = ["insert", "update", "reversal", "status_change"];
const ACTION_PILL: Record<Action, PillKind> = { insert: "open", update: "neutral", reversal: "review", status_change: "locked" };
const actionLabel = (action: string) => action.replace("_", " ");

interface Filters {
  table_name: string;
  action: string;
  record_id: string;
}

export function AuditLogsScreen() {
  const [filters, setFilters] = useState<Filters>({ table_name: "", action: "", record_id: "" });
  const logs = useInfiniteQuery({
    queryKey: apiKey("/audit-logs", { ...filters }),
    queryFn: ({ pageParam }) => api.get<Schemas["AuditLogPage"]>("/audit-logs", { ...filters, limit: 50, cursor: pageParam }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const list = useRef<HTMLDivElement>(null);
  useArrival(list, Boolean(logs.data));

  const entries = logs.data?.pages.flatMap((page) => page.items) ?? [];

  return (
    <>
      <ScreenTitle title="Audit log" />
      <div className="flex flex-col gap-4">
        <FilterCard current={filters} onApply={setFilters} failed={logs.error} />

        {logs.isPending ? (
          <Skeleton shape="list" rows={4} />
        ) : logs.isError ? (
          <ErrorCard error={logs.error} onRetry={() => void logs.refetch()} />
        ) : entries.length ? (
          <div ref={list} className="flex flex-col gap-2.5">
            {entries.map((entry) => (
              <EntryCard key={entry.id} entry={entry} />
            ))}
          </div>
        ) : (
          <Empty>
            Nothing matches these filters.
            {filters.record_id
              ? " That is not the same as the record not existing: this table only knows about changes, so an id that was never changed looks identical to one that was never created."
              : ""}
          </Empty>
        )}

        {logs.hasNextPage ? (
          <Button
            block
            disabled={logs.isFetchingNextPage}
            onClick={() => {
              logs.fetchNextPage().catch((error: unknown) => reportFailure(error));
            }}
          >
            {logs.isFetchingNextPage ? "Loading…" : "Load more"}
          </Button>
        ) : null}
      </div>
    </>
  );
}

function FilterCard({ current, onApply, failed }: { current: Filters; onApply: (filters: Filters) => void; failed: unknown }) {
  const form = useForm({ ...current });

  // An invalid filter is a 422 from FastAPI, not an empty page (§10), so it is also shown
  // against the field it names rather than read as "no such events".
  // Each failure is shown once; editing the field then retires its message.
  const { showErrors } = form;
  const handled = useRef<unknown>(null);
  useEffect(() => {
    if (failed === handled.current) return;
    handled.current = failed;
    if (failed instanceof ApiError && failed.isValidation) showErrors(failed.detail);
  }, [failed, showErrors]);

  return (
    <Card>
      <p className="mb-3 text-footnote text-ink-muted">
        Append-only. Nothing here has ever been updated or deleted, and nothing can be: this is the record every other screen is checked against.
      </p>
      <div className="flex flex-col gap-3">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <TextField form={form} name="table_name" label="Table" hint="e.g. fuel_prices, credit_customers, shifts. Blank for everything." />
          <SelectField
            form={form}
            name="action"
            label="Action"
            options={[{ value: "", label: "Any" }, ...ACTIONS.map((action) => ({ value: action, label: actionLabel(action) }))]}
          />
        </div>
        <TextField form={form} name="record_id" label="Record id" hint="The id of one row, to see its whole history." />
        <Button
          variant="primary"
          block
          onClick={() => {
            form.clearErrors();
            onApply({ table_name: form.values.table_name.trim(), action: form.values.action, record_id: form.values.record_id.trim() });
          }}
        >
          Apply filters
        </Button>
      </div>
    </Card>
  );
}

/** Keys whose value differs between the snapshots. JSON comparison, so two equal arrays (a
 * customer's vehicle numbers) do not read as changed merely for not being the same object. */
function changedKeys(oldValues: Record<string, unknown> | null, newValues: Record<string, unknown> | null): string[] {
  const keys = new Set([...Object.keys(oldValues ?? {}), ...Object.keys(newValues ?? {})]);
  return [...keys].filter((key) => JSON.stringify(oldValues?.[key]) !== JSON.stringify(newValues?.[key]));
}

/** A snapshot value exactly as it arrived. Money is a string and stays one (§14). */
function display(value: unknown): string {
  if (value === null) return "null";
  if (value === undefined) return "-";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function EntryCard({ entry }: { entry: Entry }) {
  const oldValues = entry.old_values as Record<string, unknown> | null;
  const newValues = entry.new_values as Record<string, unknown> | null;
  const changed = changedKeys(oldValues, newValues);

  return (
    <div data-arrive>
      <Card>
        <div className="flex items-center gap-2">
          <Pill kind={ACTION_PILL[entry.action]}>{actionLabel(entry.action)}</Pill>
          <span className="truncate text-body font-medium text-ink">{entry.table_name}</span>
        </div>
        <p className="mt-1.5 text-footnote text-ink-muted">{dateTime(entry.changed_at)}</p>
        <p className="font-mono text-caption break-all text-ink-faint">record {entry.record_id}</p>
        {changed.length ? (
          <dl className="mt-3 flex flex-col gap-2 border-t border-hairline pt-3">
            {changed.map((key) => (
              <div key={key} className="grid grid-cols-1 gap-x-3 text-footnote sm:grid-cols-[minmax(0,10rem)_1fr]">
                <dt className="truncate font-mono text-ink-muted">{key}</dt>
                <dd className="min-w-0 break-words">
                  {oldValues ? (
                    <>
                      <span className="t-absent">{display(oldValues[key])}</span>
                      <span className="text-ink-faint"> → </span>
                    </>
                  ) : null}
                  <span className="text-ink">{display(newValues?.[key])}</span>
                </dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="mt-2 text-footnote text-ink-muted">{oldValues ? "No field differences recorded." : "Created."}</p>
        )}
      </Card>
    </div>
  );
}
