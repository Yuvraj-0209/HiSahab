/* What a failed write tells the person who made it.
 *
 * A 422 is shown against the fields it names; anything that names no field on the form -- and
 * every other error -- becomes a toast quoting the request id (§9).
 *
 * Retry is offered only when the NETWORK failed: then the server may or may not have the row,
 * and re-sending the SAME submission (same Idempotency-Key) is the one retry known to be safe
 * (§6.10). After a real answer from the server -- a 409, a 403 -- retrying would only repeat it.
 *
 * A refusal can instead carry a way OUT (Phase 30): `recover` maps an error code to a button.
 * The lock's `UNREVIEWED_EXPENSES_EXIST` is the case -- it is not a dead end but an instruction,
 * so the toast offers the screen where the instruction is carried out. The button navigates and
 * nothing more; the server still decides whether the retried act is allowed.
 */

import { ApiError, explain, NetworkError, requestIdOf } from "../api/client";
import { notify } from "./toast";

interface ErrorTarget {
  showErrors: (detail: unknown) => string[];
}

export interface Recovery {
  label: string;
  onClick: () => void;
}

export function reportFailure(
  error: unknown,
  form?: ErrorTarget,
  retry?: () => void,
  recover?: Readonly<Record<string, Recovery>>,
): void {
  if (error instanceof ApiError && error.isValidation && form) {
    const unmatched = form.showErrors(error.detail);
    if (unmatched.length === 0) return;
    notify.error(unmatched.join(" "), { requestId: error.requestId });
    return;
  }
  const way = error instanceof ApiError ? recover?.[error.code] : undefined;
  if (way && error instanceof ApiError) {
    notify.error(explain(error), {
      requestId: error.requestId,
      // The override sentence says what is wrong; the server's own names which rows.
      ...(typeof error.detail === "string" ? { detail: error.detail } : {}),
      action: way,
    });
    return;
  }
  notify.error(explain(error), {
    requestId: requestIdOf(error),
    action: retry && error instanceof NetworkError ? { label: "Retry", onClick: retry } : undefined,
  });
}
