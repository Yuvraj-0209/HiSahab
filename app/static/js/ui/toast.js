/* Toasts: status, completion, warning, error (§16's four kinds of feedback).
 *
 * Two things here are requirements rather than polish.
 *
 * **The request_id is shown on every error.** §9 puts it in the error envelope specifically
 * so a user can quote it and the matching log line can be found. An error toast that drops it
 * throws away the only handle connecting "it didn't work" to what actually happened on the
 * server. It is rendered selectable (`user-select: all`) so it can be copied off a phone.
 *
 * **Errors are announced, not just drawn.** The live region in index.html means a screen
 * reader hears them; an error that exists only as a coloured rectangle is invisible to some
 * of the people who most need it.
 *
 * Motion: toasts enter and leave along the same path (§7), and can be flicked away
 * horizontally with the same velocity-projection the sheet uses -- a message that cannot be
 * dismissed early is a message that gets in the way.
 */

import { el } from "../dom.js";
import { PRESETS, project, Spring } from "../motion/spring.js";
import { verticalDrag } from "../motion/gesture.js";

const STACK_ID = "toast-stack";

/** Errors stay until dismissed; everything else clears itself. */
const DEFAULT_MS = { success: 2600, info: 3200, warning: 5000, error: 0 };

function stack() {
  let node = document.getElementById(STACK_ID);
  if (!node) {
    node = el("div", { className: "toast-stack", attrs: { id: STACK_ID } });
    document.getElementById("layers").appendChild(node);
  }
  return node;
}

function announce(message) {
  const live = document.getElementById("live");
  if (live) live.textContent = message;
}

/**
 * Show a toast.
 *
 * @param {object} options
 * @param {string} options.message
 * @param {"success"|"info"|"warning"|"error"} [options.kind]
 * @param {string} [options.requestId]  from the error envelope (§9)
 * @param {string} [options.detail]     a second line, e.g. a field-level explanation
 * @param {{label: string, onClick: Function}} [options.action]  e.g. "Retry"
 */
export function toast({ message, kind = "info", requestId, detail, action } = {}) {
  const node = el("div", {
    className: `toast ${kind === "error" ? "toast-error" : ""}`,
    attrs: { role: kind === "error" ? "alert" : "status" },
  });

  node.append(
    el("div", { className: "row-between" }, [
      el("div", { className: "grow" }, [
        el("p", { className: "t-body", text: message }),
        detail ? el("p", { className: "t-caption", text: detail }) : null,
      ]),
      action
        ? el("button", {
            className: "btn btn-plain",
            text: action.label,
            attrs: { type: "button" },
            on: {
              click: () => {
                dismiss();
                action.onClick();
              },
            },
          })
        : null,
    ]),
  );

  if (requestId) {
    // Labelled, because an unexplained hex string is noise. Quoting it is the point.
    node.appendChild(
      el("p", {
        className: "t-micro toast-request-id",
        text: `Reference ${requestId}`,
      }),
    );
  }

  stack().appendChild(node);
  announce(message);

  // Enter: rise and fade in together. Bounce is earned here -- the toast arrives on its own
  // rather than being thrown -- so this uses the critically damped `ui` preset, not `sheet`.
  const spring = new Spring({
    ...PRESETS.ui,
    value: 1,
    onChange: (progress) => {
      // progress: 0 = settled in place, 1 = off the bottom edge.
      node.style.transform = `translate3d(0, ${progress * 24}px, 0)`;
      node.style.opacity = String(1 - Math.abs(progress));
    },
    onRest: (progress) => {
      if (progress >= 0.999) node.remove();
    },
  });
  spring.to(0);

  let timer = null;
  const life = DEFAULT_MS[kind];

  function dismiss() {
    if (timer) clearTimeout(timer);
    spring.to(1);
  }

  if (life > 0) timer = setTimeout(dismiss, life);

  // Flick to dismiss. Uses projection so a quick flick clears it immediately -- the same
  // rule as the sheet, because two dismissible surfaces behaving differently is exactly the
  // inconsistency §16's Familiarity principle warns about.
  verticalDrag(node, {
    canStart: (event) => !event.target.closest("button"),
    onStart: () => {
      if (timer) clearTimeout(timer);
      spring.stop();
    },
    onMove: (offset) => {
      // Only downward, and only within its own height: a toast is small, so unconstrained
      // dragging would let it wander around the screen.
      spring.track(Math.max(0, offset / 24), 0);
    },
    onEnd: ({ offset, velocity }) => {
      const projected = offset + project(velocity);
      if (projected > 24 || velocity > 300) dismiss();
      else spring.to(0, { velocity: velocity / 24 });
    },
  });

  return { dismiss, element: node };
}

/** Convenience wrappers, so call sites read as intent rather than configuration. */
export const notify = {
  success: (message, options = {}) => toast({ ...options, message, kind: "success" }),
  info: (message, options = {}) => toast({ ...options, message, kind: "info" }),
  warning: (message, options = {}) => toast({ ...options, message, kind: "warning" }),
  error: (message, options = {}) => toast({ ...options, message, kind: "error" }),
};
