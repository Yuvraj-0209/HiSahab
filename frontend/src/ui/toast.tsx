/* Toasts: status, completion, warning, error.
 *
 * Two things here are requirements rather than polish:
 *
 * **The request_id is shown on every error** (§9). It is the only handle connecting "it didn't
 * work" to the log line that says why, so it is rendered selectable to be copied off a phone.
 *
 * **Errors are announced, not just drawn.** The live region in index.html means a screen reader
 * hears them.
 *
 * Motion: the spring owns these (§14: never GSAP on the same element). They rise in, critically
 * damped -- nothing threw them -- and a downward flick dismisses with the same projection the
 * sheet uses, because two dismissible surfaces behaving differently is an inconsistency.
 *
 * `notify` is an imperative store rather than a context, so the API layer and the boot sequence
 * can raise one without being inside a component.
 */

import { useEffect, useRef, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import { PRESETS, project, Spring } from "../motion/spring";
import { verticalDrag } from "../motion/gesture";
import { XIcon } from "@phosphor-icons/react";

export type ToastKind = "success" | "info" | "warning" | "error";

export interface ToastOptions {
  requestId?: string | undefined;
  detail?: string | undefined;
  action?: { label: string; onClick: () => void } | undefined;
}

interface ToastEntry extends ToastOptions {
  id: number;
  kind: ToastKind;
  message: string;
  leaving: boolean;
}

/** Errors stay until dismissed; everything else clears itself. */
const LIFETIME_MS: Record<ToastKind, number> = { success: 2600, info: 3200, warning: 5000, error: 0 };

let entries: ToastEntry[] = [];
let nextId = 1;
const listeners = new Set<() => void>();

function emit() {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function snapshot() {
  return entries;
}

function announce(message: string) {
  const live = document.getElementById("live");
  if (live) live.textContent = message;
}

/** Start a toast's exit. It is removed from the store once its spring has left. */
function dismiss(id: number) {
  entries = entries.map((entry) => (entry.id === id ? { ...entry, leaving: true } : entry));
  emit();
}

function remove(id: number) {
  entries = entries.filter((entry) => entry.id !== id);
  emit();
}

export function toast(kind: ToastKind, message: string, options: ToastOptions = {}) {
  const id = nextId++;
  entries = [...entries, { id, kind, message, leaving: false, ...options }];
  emit();
  announce(message);
  return { dismiss: () => dismiss(id) };
}

export const notify = {
  success: (message: string, options?: ToastOptions) => toast("success", message, options),
  info: (message: string, options?: ToastOptions) => toast("info", message, options),
  warning: (message: string, options?: ToastOptions) => toast("warning", message, options),
  error: (message: string, options?: ToastOptions) => toast("error", message, options),
};

/** Clear every toast, e.g. on sign-out, so one user's error does not greet the next. */
export function clearToasts() {
  entries = [];
  emit();
}

const KIND_STYLE: Record<ToastKind, string> = {
  success: "border-l-surplus",
  info: "border-l-accent",
  warning: "border-l-warning",
  error: "border-l-short",
};

function ToastCard({ entry }: { entry: ToastEntry }) {
  const node = useRef<HTMLDivElement>(null);
  const spring = useRef<Spring | null>(null);

  useEffect(() => {
    const element = node.current;
    if (!element) return;
    // progress: 0 = settled in place, 1 = off below its own edge.
    const s = new Spring({
      ...PRESETS.ui,
      value: 1,
      onChange: (progress) => {
        element.style.transform = `translate3d(0, ${progress * 24}px, 0)`;
        element.style.opacity = String(1 - Math.min(1, Math.abs(progress)));
      },
      onRest: (progress) => {
        if (progress >= 0.999) remove(entry.id);
      },
    });
    spring.current = s;
    s.to(0);

    const life = LIFETIME_MS[entry.kind];
    let timer = life > 0 ? window.setTimeout(() => dismiss(entry.id), life) : 0;

    const detach = verticalDrag(element, {
      canStart: (event) => !(event.target as Element).closest("button"),
      onStart: () => {
        window.clearTimeout(timer);
        timer = 0;
        s.stop();
      },
      // Only downward, within its own height: a toast is small and must not wander.
      onMove: (offset) => s.track(Math.max(0, offset / 24), 0),
      onEnd: ({ offset, velocity }) => {
        if (offset + project(velocity) > 24 || velocity > 300) dismiss(entry.id);
        else s.to(0, { velocity: velocity / 24 });
      },
    });

    return () => {
      window.clearTimeout(timer);
      detach();
      s.stop();
    };
  }, [entry.id, entry.kind]);

  useEffect(() => {
    if (entry.leaving) spring.current?.to(1);
  }, [entry.leaving]);

  return (
    <div
      ref={node}
      role={entry.kind === "error" ? "alert" : "status"}
      className={`pointer-events-auto touch-none rounded-[var(--radius-control)] border border-hairline border-l-4 ${KIND_STYLE[entry.kind]} bg-surface-raised px-4 py-3 shadow-3`}
      style={{ opacity: 0 }}
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 grow">
          <p className="text-body leading-snug text-ink">{entry.message}</p>
          {entry.detail ? <p className="mt-0.5 text-footnote text-ink-muted">{entry.detail}</p> : null}
        </div>
        {entry.action ? (
          <button
            type="button"
            className="pressable shrink-0 rounded-lg px-2 py-1 text-callout font-medium text-accent"
            onClick={() => {
              dismiss(entry.id);
              entry.action?.onClick();
            }}
          >
            {entry.action.label}
          </button>
        ) : (
          <button
            type="button"
            aria-label="Dismiss"
            className="pressable shrink-0 rounded-lg p-1 text-ink-faint"
            onClick={() => dismiss(entry.id)}
          >
            <XIcon size={16} weight="bold" aria-hidden />
          </button>
        )}
      </div>
      {entry.requestId ? (
        <p className="mt-1.5 text-micro tracking-wide text-ink-faint select-all">Reference {entry.requestId}</p>
      ) : null}
    </div>
  );
}

/** Mounted once, by the app root. */
export function Toaster() {
  const list = useSyncExternalStore(subscribe, snapshot, snapshot);
  const host = document.getElementById("layers");
  if (!host) return null;
  return createPortal(
    <div className="pointer-events-none fixed inset-x-0 bottom-[calc(env(safe-area-inset-bottom)+5rem)] z-50 mx-auto flex max-w-md flex-col gap-2 px-4">
      {list.map((entry) => (
        <ToastCard key={entry.id} entry={entry} />
      ))}
    </div>,
    host,
  );
}
