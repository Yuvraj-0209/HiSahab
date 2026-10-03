/* One attempt at creating one money record, with a key that survives retries (§6.10, §14).
 *
 * "Attendants use phones on patchy rural connectivity. A retry after a timeout must not create
 * a duplicate ₹5,000 expense." The server's half of that is the idempotency_keys table; this is
 * the client's half, and it is the half that is easy to break: **the key belongs to the
 * submission, not to the fetch call.** A key minted per call makes the retry a new request, and
 * the timeout-then-retry case creates two rows.
 *
 *   - a timeout followed by a retry replays the original response and creates nothing;
 *   - the same key with a *different* body is a client bug, and the server refuses it with 422
 *     IDEMPOTENCY_KEY_REUSED rather than returning somebody else's answer;
 *   - after success the submission is spent, so a second record needs a fresh form.
 *
 * ## In React
 *
 * `useSubmission` mints the key with a `useState` initializer, which React guarantees runs
 * once for the component's lifetime (`useMemo` is only a performance hint and may recompute,
 * which would silently mint a new key mid-retry). A form mounts when its sheet opens and
 * unmounts when it closes, so **one sheet opening = one key**, every retry inside it reuses
 * the key, and opening the sheet again for the next entry gets a new one.
 */

import { useRef, useState } from "react";
import { newIdempotencyKey, request } from "./client";

export class Submission {
  readonly method: string;
  readonly path: string;
  readonly key: string;
  done = false;

  constructor(method: string, path: string) {
    this.method = method;
    this.path = path;
    this.key = newIdempotencyKey();
  }

  async run<T>(body: unknown): Promise<T> {
    if (this.done) throw new Error("This submission already succeeded; open a new form for another.");
    const result = await request<T>(this.method, this.path, { body, idempotencyKey: this.key });
    this.done = true;
    return result;
  }
}

/** The Submission for the form this component renders. Stable for the component's lifetime. */
export function useSubmission(method: "POST", path: string): Submission {
  const [submission] = useState(() => new Submission(method, path));
  return submission;
}

/** For a screen that stays open and submits more than once -- the bank review's batch confirm.
 *
 * There is no sheet whose unmounting ends the submission, so the rule is stated on the body:
 * **the same body is a retry and reuses the key; a different body is a new submission and gets
 * a new one.** That is what a retry means. Reusing the key across a change of ticks would be
 * refused with 422 IDEMPOTENCY_KEY_REUSED; minting one per call is the duplicate §6.10 forbids.
 * After a success the next call always mints, even for an identical body, because by then it is
 * a second record rather than a second attempt at the first.
 */
export function useRepeatableSubmission(method: "POST", path: string): <T>(body: unknown) => Promise<T> {
  const current = useRef<{ body: string; submission: Submission } | null>(null);
  return <T,>(body: unknown) => {
    const text = JSON.stringify(body);
    const held = current.current;
    if (!held || held.submission.done || held.body !== text) {
      current.current = { body: text, submission: new Submission(method, path) };
    }
    return (current.current as { submission: Submission }).submission.run<T>(body);
  };
}
