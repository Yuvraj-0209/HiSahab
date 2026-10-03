/* Form fields, and the form state that talks to the API (Phase 12's field.js, for React).
 *
 * Two API traps live here rather than being rediscovered in every screen:
 *
 * **A PATCH omits untouched fields; it never sends null.** Explicit-null semantics vary by
 * router -- on `credit_customers` a null *clears* `credit_limit` and `vehicle_numbers`, on
 * `credit_sales` it clears `quantity` and `vehicle_number`. Sending null for "I didn't touch
 * this" wipes real data. `changes()` returns only what the user actually edited.
 *
 * **Errors are inline and per field**, driven by the 422 envelope's `detail[].loc`. Anything
 * that names no field on this form is returned to the caller for a toast -- an error nobody sees
 * is worse than an ugly one.
 *
 * Labels sit ABOVE inputs, hints and errors below, never placeholder-as-label. Inputs are 16px
 * so iOS does not zoom the page when one is focused.
 */

import { type ReactNode, useCallback, useId, useMemo, useRef, useState } from "react";
import type { ValidationIssue } from "../api/client";

export type FormValues = Record<string, string | boolean>;

export interface FormState<T extends FormValues> {
  values: T;
  errors: Partial<Record<keyof T & string, string>>;
  set: <K extends keyof T & string>(name: K, value: T[K]) => void;
  /** Only the fields whose value differs from what the form opened with: the PATCH body. */
  changes: () => Partial<T>;
  setError: (name: keyof T & string, message: string) => void;
  clearErrors: () => void;
  /** Show a 422's field-level errors; returns the messages that matched no field. */
  showErrors: (detail: unknown) => string[];
  /** Stable per-form id prefix, so two sheets' fields never share an id. */
  idFor: (name: keyof T & string) => string;
}

export function useForm<T extends FormValues>(initial: T): FormState<T> {
  const opened = useRef(initial);
  const [values, setValues] = useState<T>(initial);
  const [errors, setErrors] = useState<Partial<Record<keyof T & string, string>>>({});
  const prefix = useId();

  const idFor = useCallback((name: keyof T & string) => `${prefix}-${name}`, [prefix]);

  const set = useCallback(<K extends keyof T & string>(name: K, value: T[K]) => {
    setValues((current) => ({ ...current, [name]: value }));
    // An error is about the value that produced it; editing the field retires it.
    setErrors((current) => {
      if (!(name in current)) return current;
      const next = { ...current };
      delete next[name];
      return next;
    });
  }, []);

  const changes = useCallback(() => {
    const out: Partial<T> = {};
    for (const key of Object.keys(values) as (keyof T & string)[]) {
      if (values[key] !== opened.current[key]) out[key] = values[key];
    }
    return out;
  }, [values]);

  const setError = useCallback((name: keyof T & string, message: string) => {
    setErrors((current) => ({ ...current, [name]: message }));
  }, []);

  const clearErrors = useCallback(() => setErrors({}), []);

  const showErrors = useCallback(
    (detail: unknown) => {
      const unmatched: string[] = [];
      if (!Array.isArray(detail)) return unmatched;
      const next: Partial<Record<keyof T & string, string>> = {};
      for (const item of detail as ValidationIssue[]) {
        const name = Array.isArray(item.loc) ? String(item.loc[item.loc.length - 1]) : null;
        if (name && name in values) next[name as keyof T & string] = item.msg ?? "Invalid value";
        else unmatched.push(item.msg ?? "Invalid value");
      }
      setErrors(next);
      // Bring the first problem into view: on a phone it is often below the fold, and an error
      // nobody can see reads as the form doing nothing.
      const first = Object.keys(next)[0];
      if (first) {
        requestAnimationFrame(() => document.getElementById(`${prefix}-${first}`)?.focus());
      }
      return unmatched;
    },
    [values, prefix],
  );

  return useMemo(
    () => ({ values, errors, set, changes, setError, clearErrors, showErrors, idFor }),
    [values, errors, set, changes, setError, clearErrors, showErrors, idFor],
  );
}

const CONTROL =
  "field-control block h-12 w-full rounded-[var(--radius-control)] border border-hairline-strong bg-surface px-3.5 text-base text-ink " +
  "placeholder:text-ink-faint transition-[border-color,box-shadow] duration-150 " +
  "focus:border-accent focus:outline-none focus:ring-4 focus:ring-accent-tint " +
  "aria-[invalid=true]:border-short aria-[invalid=true]:ring-short-tint disabled:opacity-55";

interface FieldFrameProps {
  id: string;
  label: string;
  hint?: ReactNode | undefined;
  error?: string | undefined;
  children: ReactNode;
}

function FieldFrame({ id, label, hint, error, children }: FieldFrameProps) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-[0.8125rem] font-medium text-ink-muted">
        {label}
      </label>
      {children}
      {hint ? (
        <p id={`${id}-hint`} className="text-[0.8125rem] leading-snug text-ink-faint">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={`${id}-error`} role="alert" className="text-[0.8125rem] leading-snug text-short">
          {error}
        </p>
      ) : null}
    </div>
  );
}

function describedBy(id: string, hint: unknown, error: unknown): string | undefined {
  const parts = [hint ? `${id}-hint` : null, error ? `${id}-error` : null].filter(Boolean);
  return parts.length ? parts.join(" ") : undefined;
}

type StringKeys<T> = { [K in keyof T]: T[K] extends string ? K : never }[keyof T] & string;
type BooleanKeys<T> = { [K in keyof T]: T[K] extends boolean ? K : never }[keyof T] & string;

export interface TextFieldProps<T extends FormValues> {
  form: FormState<T>;
  name: StringKeys<T>;
  label: string;
  type?: "text" | "number" | "date" | "datetime-local" | "time" | "tel" | "email" | "password";
  hint?: ReactNode;
  placeholder?: string;
  inputMode?: "text" | "decimal" | "numeric" | "tel" | "email";
  autoComplete?: string;
  required?: boolean;
  disabled?: boolean;
  step?: string;
  min?: string;
  max?: string;
}

export function TextField<T extends FormValues>({
  form,
  name,
  label,
  type = "text",
  hint,
  placeholder,
  inputMode,
  autoComplete,
  required,
  disabled,
  step,
  min,
  max,
}: TextFieldProps<T>) {
  const id = form.idFor(name);
  const error = form.errors[name];
  return (
    <FieldFrame id={id} label={label} hint={hint} error={error}>
      <input
        id={id}
        name={name}
        type={type}
        className={`${CONTROL} ${inputMode === "decimal" || inputMode === "numeric" ? "tabular" : ""}`}
        value={form.values[name] as string}
        onChange={(event) => form.set(name, event.target.value as T[typeof name])}
        placeholder={placeholder}
        inputMode={inputMode}
        autoComplete={autoComplete}
        required={required}
        disabled={disabled}
        step={step}
        min={min}
        max={max}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(id, hint, error)}
      />
    </FieldFrame>
  );
}

export interface SelectFieldProps<T extends FormValues> {
  form: FormState<T>;
  name: StringKeys<T>;
  label: string;
  options: { value: string; label: string }[];
  hint?: ReactNode;
  required?: boolean;
  disabled?: boolean;
}

/** A native select: on a phone, the system picker is the best control there is. */
export function SelectField<T extends FormValues>({ form, name, label, options, hint, required, disabled }: SelectFieldProps<T>) {
  const id = form.idFor(name);
  const error = form.errors[name];
  return (
    <FieldFrame id={id} label={label} hint={hint} error={error}>
      <select
        id={id}
        name={name}
        className={CONTROL}
        value={form.values[name] as string}
        onChange={(event) => form.set(name, event.target.value as T[typeof name])}
        required={required}
        disabled={disabled}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(id, hint, error)}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </FieldFrame>
  );
}

export interface CheckboxFieldProps<T extends FormValues> {
  form: FormState<T>;
  name: BooleanKeys<T>;
  label: ReactNode;
  hint?: ReactNode;
  disabled?: boolean;
}

/** A checkbox. Its default is whatever the form opened with -- and for anything that asserts a
 * human looked at something (§4.7's opening confirm), that must be `false` (§14). */
export function CheckboxField<T extends FormValues>({ form, name, label, hint, disabled }: CheckboxFieldProps<T>) {
  const id = form.idFor(name);
  const error = form.errors[name];
  return (
    <div className="flex flex-col gap-1">
      <label
        htmlFor={id}
        className="flex cursor-pointer items-start gap-3 rounded-[var(--radius-control)] border border-hairline bg-surface px-3.5 py-3"
      >
        <input
          id={id}
          name={name}
          type="checkbox"
          className="mt-0.5 size-5 shrink-0 accent-[var(--accent)]"
          checked={form.values[name] as boolean}
          onChange={(event) => form.set(name, event.target.checked as T[typeof name])}
          disabled={disabled}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy(id, hint, error)}
        />
        <span className="min-w-0 grow">
          <span className="block text-[0.9375rem] text-ink">{label}</span>
          {hint ? (
            <span id={`${id}-hint`} className="mt-0.5 block text-[0.8125rem] text-ink-faint">
              {hint}
            </span>
          ) : null}
        </span>
      </label>
      {error ? (
        <p id={`${id}-error`} role="alert" className="text-[0.8125rem] text-short">
          {error}
        </p>
      ) : null}
    </div>
  );
}
