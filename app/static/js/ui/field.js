/* Form fields, and the form object that talks to the API.
 *
 * Two API traps live here rather than being rediscovered in every screen (see D13 in
 * docs/phase-12-plan.md):
 *
 * **A PATCH omits untouched fields; it never sends null.** Explicit-null semantics vary by
 * router -- null is ignored almost everywhere, but on `credit_customers` it *clears*
 * `credit_limit` and `vehicle_numbers`, and on `credit_sales` it clears `quantity` and
 * `vehicle_number`. Sending null for "I didn't touch this" therefore wipes real data on
 * exactly two routers. `Form.changes()` returns only what the user actually edited, so
 * clearing is something a caller has to ask for on purpose.
 *
 * **A datetime-local input is naive, and every timestamp the API accepts must carry a UTC
 * offset.** `effective_from`, `started_at`, `ended_at` and `at` all 422 on a naive value, so
 * entering a fuel price would fail on the first attempt without `toOffsetISO` below.
 *
 * Validation is inline and per-field (§16: validate inline, not on submit), driven by the
 * 422 envelope's `detail[].loc`, which names the offending field.
 */

import { el } from "../dom.js";

/**
 * A labelled input.
 *
 * @param {object} options
 * @param {string} options.name    matches the API field name, so `loc` can find it
 * @param {string} options.label
 * @param {string} [options.type]  text | number | date | datetime-local | tel | password
 * @param {string} [options.hint]  shown under the field, for a rule the server enforces
 */
export function field({
  name,
  label,
  type = "text",
  value = "",
  required = false,
  hint = null,
  placeholder = null,
  step = null,
  min = null,
  max = null,
  autocomplete = null,
  inputMode = null,
}) {
  const input = el("input", {
    className: "field-input",
    attrs: {
      id: `f-${name}`,
      name,
      type,
      value: value ?? "",
      required: required || undefined,
      placeholder,
      step,
      min,
      max,
      autocomplete,
      inputmode: inputMode,
    },
  });

  const error = el("p", {
    className: "field-error t-caption hidden",
    attrs: { id: `e-${name}`, role: "alert" },
  });

  const wrapper = el("label", { className: "field", attrs: { for: `f-${name}` } }, [
    el("span", { className: "field-label t-caption", text: label }),
    input,
    hint ? el("p", { className: "field-hint t-caption", text: hint }) : null,
    error,
  ]);

  wrapper._input = input;
  wrapper._error = error;
  return wrapper;
}

/** A select. `options` is [{value, label}]. */
export function select({ name, label, options, value = "", required = false, hint = null }) {
  const node = el(
    "select",
    {
      className: "field-input",
      attrs: { id: `f-${name}`, name, required: required || undefined },
    },
    options.map((option) =>
      el("option", {
        text: option.label,
        attrs: { value: option.value, selected: option.value === value || undefined },
      }),
    ),
  );

  const error = el("p", {
    className: "field-error t-caption hidden",
    attrs: { id: `e-${name}`, role: "alert" },
  });

  const wrapper = el("label", { className: "field", attrs: { for: `f-${name}` } }, [
    el("span", { className: "field-label t-caption", text: label }),
    node,
    hint ? el("p", { className: "field-hint t-caption", text: hint }) : null,
    error,
  ]);

  wrapper._input = node;
  wrapper._error = error;
  return wrapper;
}

/** A checkbox. Returns a boolean from `Form.values()`. */
export function checkbox({ name, label, checked = false, hint = null }) {
  const input = el("input", {
    className: "field-checkbox",
    attrs: { id: `f-${name}`, name, type: "checkbox", checked: checked || undefined },
  });

  const wrapper = el("label", { className: "field row", attrs: { for: `f-${name}` } }, [
    input,
    el("span", { className: "grow" }, [
      el("span", { className: "t-body", text: label }),
      hint ? el("p", { className: "t-caption", text: hint }) : null,
    ]),
  ]);

  wrapper._input = input;
  wrapper._error = el("p", { className: "hidden" });
  return wrapper;
}

/**
 * Convert a `datetime-local` value into an ISO string carrying the browser's UTC offset.
 *
 * A `datetime-local` input yields "2026-08-23T06:00" -- no zone. Every timestamp field in
 * this API rejects that with 422 `NAIVE_TIMESTAMP`, deliberately: §3 rule 4 stores instants
 * in UTC, and a wall-clock time with no zone is not an instant.
 *
 * `new Date(local).toISOString()` is the right conversion. The browser parses a zoneless
 * string as *local* time -- which is what the user meant, since they typed the time on the
 * clock in front of them -- and toISOString renders the same instant in UTC with an explicit
 * `Z`. The server then converts back for display using TZ_DISPLAY from /client-config.
 */
export function toOffsetISO(localValue) {
  if (!localValue) return null;
  const parsed = new Date(localValue);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toISOString();
}

/** A `datetime-local` value for "now", for pre-filling a field. */
export function nowLocalValue() {
  const now = new Date();
  // toISOString would give UTC; the input needs local wall-clock. Subtracting the offset
  // first makes the ISO string read as local time, which is what the control expects.
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

/**
 * A collection of fields, plus what the API needs from them.
 *
 * Tracks the values the form was *created* with, so `changes()` can return only what the
 * user actually edited -- which is what makes a PATCH safe on the two routers where an
 * explicit null clears a column.
 */
export class Form {
  constructor(fields = {}) {
    this.fields = fields;
    this.initial = {};
    for (const [name, node] of Object.entries(fields)) {
      this.initial[name] = this._read(node);
    }
  }

  _read(node) {
    const input = node._input;
    if (!input) return null;
    if (input.type === "checkbox") return input.checked;
    return input.value;
  }

  /** Every value, as typed. Empty strings stay empty strings -- the caller decides what
   * absence means, because it differs by endpoint. */
  values() {
    const out = {};
    for (const [name, node] of Object.entries(this.fields)) out[name] = this._read(node);
    return out;
  }

  /**
   * Only the fields whose value differs from what the form opened with.
   *
   * This is the PATCH body. A field the user never touched is *absent* rather than null, so
   * `credit_customers.credit_limit` is not silently cleared by an edit to somebody's name.
   */
  changes() {
    const out = {};
    for (const [name, node] of Object.entries(this.fields)) {
      const value = this._read(node);
      if (value !== this.initial[name]) out[name] = value;
    }
    return out;
  }

  /** Clear every inline error. Called before each submit, so stale messages never linger. */
  clearErrors() {
    for (const node of Object.values(this.fields)) {
      node._error.classList.add("hidden");
      node._error.textContent = "";
      node._input?.setAttribute("aria-invalid", "false");
    }
  }

  /**
   * Show a 422's field-level errors against the fields they name.
   *
   * FastAPI's `detail` is a list of `{loc: ["body", "amount"], msg: "..."}`. The field name
   * is the last element of `loc`; anything that does not match a field on this form is
   * returned so the caller can surface it in a toast rather than swallowing it -- an error
   * nobody sees is worse than an ugly one.
   *
   * @returns {string[]} messages that had no matching field
   */
  showErrors(detail) {
    const unmatched = [];
    if (!Array.isArray(detail)) return unmatched;

    for (const item of detail) {
      const name = Array.isArray(item.loc) ? item.loc[item.loc.length - 1] : null;
      const node = name ? this.fields[name] : null;
      if (!node) {
        unmatched.push(item.msg ?? "Invalid value");
        continue;
      }
      node._error.textContent = item.msg ?? "Invalid value";
      node._error.classList.remove("hidden");
      node._input?.setAttribute("aria-invalid", "true");
    }

    // Bring the first problem into view. On a phone the offending field is often below the
    // fold, and an error the user cannot see reads as the form doing nothing at all.
    const firstBad = Object.values(this.fields).find(
      (node) => !node._error.classList.contains("hidden"),
    );
    firstBad?._input?.focus({ preventScroll: false });

    return unmatched;
  }

  /** The node list, for appending into a sheet body. */
  nodes() {
    return Object.values(this.fields);
  }
}
