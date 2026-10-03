/* What a failed write tells the person who made it.
 *
 * A 422 is shown against the fields it names; anything that names no field on the form -- and
 * every other error -- becomes a toast quoting the request id (§9).
 *
 * Retry is offered only when the NETWORK failed: then the server may or may not have the row,
 * and re-sending the SAME submission (same Idempotency-Key) is the one retry known to be safe
 * (§6.10). After a real answer from the server -- a 409, a 403 -- retrying would only repeat it.
 */

import { ApiError, explain, NetworkError, requestIdOf } from "../api/client";
import { notify } from "./toast";

interface ErrorTarget {
  showErrors: (detail: unknown) => string[];
}

export function reportFailure(error: unknown, form?: ErrorTarget, retry?: () => void): void {
  if (error instanceof ApiError && error.isValidation && form) {
    const unmatched = form.showErrors(error.detail);
    if (unmatched.length === 0) return;
    notify.error(unmatched.join(" "), { requestId: error.requestId });
    return;
  }
  notify.error(explain(error), {
    requestId: requestIdOf(error),
    action: retry && error instanceof NetworkError ? { label: "Retry", onClick: retry } : undefined,
  });
}
