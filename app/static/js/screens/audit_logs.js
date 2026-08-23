/* The audit trail, made readable (CLAUDE.md §5.3, §8, §9).
 *
 * Phase 11 built `GET /audit-logs` because "every phase since 4 has written to a trail whose
 * only reader was a raw SQL prompt, and a trail nobody can read is not one." This screen is
 * the other half of that sentence.
 *
 * ## Admin only, and that is a leak boundary rather than a preference
 *
 * §8 explains why this sits above the manager floor even though every other read is at it:
 * `old_values` / `new_values` on a `credit_customers` row contain `phone` and `credit_limit`
 * -- exactly the two fields §9 restricts to manager-and-above on the customer's own detail
 * route, and which an attendant may never see. A manager floor here would expose one table's
 * restricted columns through a different endpoint.
 *
 * ## Both snapshots are shown, and the diff is done here
 *
 * §5.3 stores `old_values` and `new_values` whole; Phase 11 deliberately did not add diffing
 * to the API ("the client receives both snapshots and can diff them"). So the comparison
 * below is this screen's job, and it shows **only the keys that actually changed** -- an
 * entry echoing every column makes the change itself hard to find, which is the same reason
 * `_audit_snapshot` in each router is a subset rather than the whole row.
 *
 * ## Money inside a snapshot is a string
 *
 * §10's Phase 11 block requires it: "Money inside `old_values` / `new_values` round-trips as
 * a **string**, never a float, and a null `credit_limit` stays null rather than becoming 0."
 * So values are rendered exactly as they arrive, with `null` shown as the word -- because on
 * a credit limit, null means *no limit* and 0 would mean the opposite (§6.6).
 *
 * ## An unknown record id is an empty page, not a 404
 *
 * §5.3: `record_id` is deliberately not a foreign key, so "nothing can tell the difference
 * between 'no such row' and 'that row was never changed', and inventing a 404 would claim
 * knowledge the table does not have." The empty state below says exactly that rather than
 * implying the record does not exist.
 */

import { el, empty, pill, render } from "../dom.js";
import { api, explain } from "../api.js";
import { dateTime } from "../time.js";
import { field, Form, select } from "../ui/field.js";
import { notify } from "../ui/toast.js";
import { errorCard } from "./today.js";

const ACTIONS = ["insert", "update", "reversal", "status_change"];

const ACTION_PILL = {
  insert: "open",
  update: "neutral",
  reversal: "review",
  status_change: "locked",
};

export async function renderAuditLogs(container, { session, navigate, query = {} }) {
  const { shell } = session;
  shell.setTab("admin");
  shell.setTitle("Audit log");
  shell.setActions();

  render(container, el("div", { className: "t-caption", text: "Loading…" }));

  // Cursor state lives in this closure, keyed to this endpoint. §9's two cursor encodings are
  // not interchangeable, so a cursor is never shared between list screens.
  const filters = {
    table_name: query.table_name ?? "",
    action: query.action ?? "",
    record_id: query.record_id ?? "",
  };

  const rows = [];
  let nextCursor = null;

  const listNode = el("div", { className: "list" });
  const moreWrap = el("div", {});

  async function loadPage(cursor = null) {
    const page = await api.get("/audit-logs", {
      ...filters,
      limit: 50,
      cursor: cursor ?? undefined,
    });
    rows.push(...page.items);
    nextCursor = page.next_cursor;
    paint();
  }

  function paint() {
    listNode.replaceChildren(
      ...(rows.length ? rows.map(entryRow) : [emptyNode()]),
    );
    moreWrap.replaceChildren(
      nextCursor
        ? el("button", {
            className: "btn btn-block",
            text: "Load more",
            attrs: { type: "button" },
            on: {
              click: async (event) => {
                event.currentTarget.disabled = true;
                try {
                  await loadPage(nextCursor);
                } catch (error) {
                  notify.error(explain(error), { requestId: error.requestId });
                }
              },
            },
          })
        : null,
    );
  }

  function emptyNode() {
    return el("div", { className: "empty t-body" }, [
      el("p", { text: "Nothing matches these filters." }),
      filters.record_id
        ? el("p", {
            className: "t-caption",
            // §5.3's read-side consequence, stated rather than implied.
            text: "That is not the same as saying the record does not exist — this table only knows about changes, so an id that was never changed looks identical to one that was never created.",
          })
        : null,
    ]);
  }

  const tableFilter = field({
    name: "table_name",
    label: "Table",
    value: filters.table_name,
    hint: "e.g. fuel_prices, credit_customers, shifts. Leave blank for everything.",
  });

  const actionFilter = select({
    name: "action",
    label: "Action",
    options: [
      { value: "", label: "Any" },
      ...ACTIONS.map((action) => ({ value: action, label: action.replace("_", " ") })),
    ],
    value: filters.action,
  });

  const recordFilter = field({
    name: "record_id",
    label: "Record id",
    value: filters.record_id,
    hint: "The UUID of one row, to see its whole history.",
  });

  const filterForm = new Form({
    table_name: tableFilter,
    action: actionFilter,
    record_id: recordFilter,
  });

  const apply = el("button", {
    className: "btn btn-primary btn-block",
    text: "Apply filters",
    attrs: { type: "button" },
  });

  apply.addEventListener("click", async () => {
    filterForm.clearErrors();
    const values = filterForm.values();
    filters.table_name = values.table_name.trim();
    filters.action = values.action;
    filters.record_id = values.record_id.trim();
    rows.length = 0;
    nextCursor = null;
    apply.disabled = true;
    try {
      await loadPage();
    } catch (error) {
      // An invalid action is a 422 from FastAPI rather than an empty page, which §10 asserts
      // explicitly -- silently returning nothing would look like "no such events".
      const unmatched = error.isValidation ? filterForm.showErrors(error.detail) : [];
      if (!error.isValidation || unmatched.length) {
        notify.error(explain(error), { requestId: error.requestId });
      }
    } finally {
      apply.disabled = false;
    }
  });

  try {
    await loadPage();
  } catch (error) {
    render(container, errorCard(error, () => renderAuditLogs(container, { session, navigate })));
    return;
  }

  render(
    container,
    el("div", { className: "stack" }, [
      el("div", { className: "card stack" }, [
        el("p", {
          className: "t-caption",
          text: "Append-only. Nothing has ever been updated or deleted here, and nothing can be — this is the record every other screen is checked against.",
        }),
        ...filterForm.nodes(),
        apply,
      ]),
      listNode,
      moreWrap,
    ]),
  );
}

function entryRow(entry) {
  const changed = changedKeys(entry.old_values, entry.new_values);

  return el("div", { className: "list-row", style: { alignItems: "flex-start" } }, [
    el("div", { className: "list-row-main stack" }, [
      el("div", { className: "row" }, [
        pill(entry.action.replace("_", " "), ACTION_PILL[entry.action] ?? "neutral"),
        el("span", { className: "t-body", text: entry.table_name }),
      ]),
      el("div", { className: "t-caption", text: dateTime(entry.changed_at) }),
      el("div", {
        className: "t-micro toast-request-id",
        text: `record ${entry.record_id}`,
      }),

      changed.length
        ? el("div", { className: "stack" }, changed.map((key) =>
            el("div", { className: "t-caption" }, [
              el("span", { className: "t-micro", text: `${key}  ` }),
              el("span", { className: "t-absent", text: display(entry.old_values?.[key]) }),
              el("span", { text: "  →  " }),
              el("span", { text: display(entry.new_values?.[key]) }),
            ]),
          ))
        : el("div", {
            className: "t-caption",
            text: entry.old_values ? "no field differences recorded" : "created",
          }),
    ]),
  ]);
}

/** Keys whose value actually differs between the two snapshots.
 *
 * Compared with JSON.stringify rather than `!==` so nested objects and arrays -- a customer's
 * `vehicle_numbers`, for instance -- do not read as "changed" every time merely because two
 * arrays are never identity-equal.
 */
function changedKeys(oldValues, newValues) {
  const keys = new Set([
    ...Object.keys(oldValues ?? {}),
    ...Object.keys(newValues ?? {}),
  ]);
  return [...keys].filter(
    (key) => JSON.stringify(oldValues?.[key]) !== JSON.stringify(newValues?.[key]),
  );
}

/** Render a snapshot value exactly as it arrived.
 *
 * Money is a string on the wire and stays one -- §14 forbids parsing it, and this screen has
 * no reason to. `null` becomes the word, because on `credit_limit` it means *no limit* and
 * showing 0 would state the opposite (§6.6).
 */
function display(value) {
  if (value === null) return "null";
  if (value === undefined) return "—";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
