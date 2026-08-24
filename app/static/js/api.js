/* The one place this application talks to the API (CLAUDE.md §3 rule 10, §6.10, §9).
 *
 * Everything about the HTTP contract lives here so no screen has to remember it: the bearer
 * header, the two shapes of the error envelope, silent token refresh, and the idempotency
 * rule that actually protects money.
 *
 * ## The error envelope has two shapes, and branching on the wrong one loses the detail
 *
 *   business error:  {"detail": "human message", "code": "MACHINE_CODE", "request_id": "..."}
 *   validation:      {"detail": [{loc, msg, ...}], "code": "VALIDATION_ERROR", "request_id": ...}
 *
 * `detail` is a **string** in the first and an **array** in the second. `ApiError` below keeps
 * both, so a caller can show field-level errors against the fields they name (§16: validate
 * inline) and fall back to a toast for anything else.
 *
 * ## 401 is handled by code, not by status
 *
 * app/core/security.py raises TOKEN_EXPIRED distinctly from INVALID_TOKEN, and says why in
 * its own comment: "so the frontend can refresh silently rather than bouncing the user to a
 * login screen". Reading the status alone throws that away and logs people out every hour.
 *
 * ## The Idempotency-Key belongs to a SUBMISSION, not to a fetch call
 *
 * This is the subtlety §6.10 exists for, and getting it wrong reintroduces in the client the
 * exact bug the server built a table to prevent. §6.10: "Attendants use phones on patchy
 * rural connectivity. A retry after a timeout must not create a duplicate ₹5,000 expense."
 *
 * A key minted per `fetch()` makes every retry a *new* request, so the timeout-then-retry
 * case -- the one this is all for -- creates two rows. So `Submission` below mints one key
 * when a form is first submitted and holds it until that submission succeeds. Every retry,
 * automatic or from the toast's Retry button, reuses it.
 */

import { getAccessToken, refreshAccessToken, signOut } from "./auth.js";

/** Endpoints that require an Idempotency-Key, as route templates.
 *
 * It is NOT "every POST": readings are idempotent by construction (UNIQUE (shift_id,
 * nozzle_id) makes a retry a 409 READING_ALREADY_EXISTS and the client PATCHes instead),
 * uploads are not money records (§6.10's closing note), and reference-data POSTs create
 * rows a human is looking at. Omitting a required key is 400 IDEMPOTENCY_KEY_REQUIRED, so
 * the list lives next to the client that sends it rather than in somebody's memory.
 */
const NEEDS_IDEMPOTENCY = [
  /^\/shifts\/[^/]+\/collections$/,
  /^\/shifts\/[^/]+\/expenses$/,
  /^\/shifts\/[^/]+\/credit-sales$/,
  /^\/shifts\/[^/]+\/credit-repayments$/,
  /^\/shifts\/[^/]+\/non-fuel-sales$/,
  /^\/shifts\/[^/]+\/bank-deposits$/,
  /^\/shifts\/[^/]+\/shortfalls$/,
  /^\/shifts\/[^/]+\/shortfall-settlements$/,
  /\/reversals$/,
];

function needsIdempotencyKey(path, method) {
  if (method !== "POST") return false;
  const route = path.split("?")[0];
  return NEEDS_IDEMPOTENCY.some((pattern) => pattern.test(route));
}

/** A failed request, carrying everything needed to explain it to a person. */
export class ApiError extends Error {
  constructor({ status, code, detail, requestId }) {
    super(typeof detail === "string" ? detail : code);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.requestId = requestId;
  }

  /** True when `detail` is FastAPI's field-level array. */
  get isValidation() {
    return this.code === "VALIDATION_ERROR" && Array.isArray(this.detail);
  }

  /** A sentence for a toast. Never the raw array. */
  get message422() {
    if (!this.isValidation) return typeof this.detail === "string" ? this.detail : this.code;
    return "Some fields need attention.";
  }
}

/** The network itself failed -- no response at all. Distinct from ApiError because it is the
 * one case where retrying the *same* request with the *same* key is exactly right. */
export class NetworkError extends Error {
  constructor(cause) {
    super("The network is unavailable.");
    this.name = "NetworkError";
    this.cause = cause;
  }
}

const BASE = "/api/v1";

async function parseBody(response) {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    // A non-JSON body from an API that always returns JSON means something upstream
    // answered instead -- a proxy error page, or the static mount if the path were wrong.
    // Surfacing the text is more useful than a parse error.
    return { detail: text.slice(0, 200), code: "NON_JSON_RESPONSE" };
  }
}

/**
 * Make a request. Every call in the application goes through here.
 *
 * @param {string} method
 * @param {string} path            below /api/v1, e.g. "/shifts/current"
 * @param {object} [options]
 * @param {object} [options.body]
 * @param {object} [options.query]
 * @param {string} [options.idempotencyKey]  from a Submission; see the module header
 * @param {boolean} [options.retryOnExpiry]  internal, prevents an infinite refresh loop
 */
export async function request(method, path, options = {}) {
  const { body, query, idempotencyKey, retryOnExpiry = true } = options;

  let url = BASE + path;
  if (query) {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== "") {
        params.set(key, String(value));
      }
    }
    const encoded = params.toString();
    if (encoded) url += `?${encoded}`;
  }

  const headers = {};
  const token = getAccessToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  if (body !== undefined) headers["Content-Type"] = "application/json";

  if (needsIdempotencyKey(path, method)) {
    if (!idempotencyKey) {
      // Loud, and deliberately not papered over by minting one here. A missing key means a
      // caller built a money POST without a Submission, and silently generating one would
      // restore exactly the per-call behaviour this module exists to prevent.
      throw new Error(
        `${method} ${path} creates a money record and needs an Idempotency-Key. ` +
          "Use submit() / Submission rather than request() directly.",
      );
    }
    headers["Idempotency-Key"] = idempotencyKey;
  }

  let response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    throw new NetworkError(cause);
  }

  if (response.status === 204) return null;

  const payload = await parseBody(response);

  if (response.ok) return payload;

  const error = new ApiError({
    status: response.status,
    code: payload?.code ?? `HTTP_${response.status}`,
    detail: payload?.detail ?? "Something went wrong.",
    requestId: payload?.request_id,
  });

  if (response.status === 401) {
    // Silent refresh, exactly once. A second failure means the refresh token is dead too,
    // and looping would spin forever against a server that is answering correctly.
    if (error.code === "TOKEN_EXPIRED" && retryOnExpiry) {
      const refreshed = await refreshAccessToken();
      if (refreshed) {
        return request(method, path, { ...options, retryOnExpiry: false });
      }
    }
    signOut();
  }

  throw error;
}

export const api = {
  get: (path, query) => request("GET", path, { query }),
  post: (path, body, idempotencyKey) => request("POST", path, { body, idempotencyKey }),
  patch: (path, body) => request("PATCH", path, { body }),
};

/**
 * One attempt at creating one money record, with a key that survives retries.
 *
 * Construct it when the user presses the button, then call `run()` as many times as needed.
 * The key is minted once, in the constructor, so:
 *
 *   - a timeout followed by a retry replays the original response and creates nothing
 *   - the same key with the same body is safe by design (§6.10)
 *   - the same key with a *different* body is a client bug, and the server says so with 422
 *     IDEMPOTENCY_KEY_REUSED rather than returning somebody else's answer
 *
 * `crypto.randomUUID` is available in every browser that supports ES modules over HTTPS
 * (and on localhost), so there is no fallback to write.
 */
export class Submission {
  constructor(method, path) {
    this.method = method;
    this.path = path;
    this.key = crypto.randomUUID();
    this.done = false;
  }

  async run(body) {
    if (this.done) {
      throw new Error("This submission already succeeded; build a new one.");
    }
    const result = await request(this.method, this.path, {
      body,
      idempotencyKey: this.key,
    });
    this.done = true;
    return result;
  }
}

/** Upload a receipt (§7.2). The only multipart endpoint, and the only one taking `shift_id`
 * in the body rather than the path.
 *
 * Content-Type is deliberately NOT set: the browser has to write it itself, because it alone
 * knows the multipart boundary. Setting it by hand produces a body the server cannot parse.
 *
 * No Idempotency-Key, per §6.10's closing note -- an attachment is not a money record, and a
 * retried upload leaves an unlinked row that §7.4's sweep reclaims within 24 hours. The step
 * where a retry *would* duplicate money is linking it, inside POST /shifts/{id}/expenses,
 * and that carries a key.
 */
export async function uploadReceipt(file, shiftId) {
  const form = new FormData();
  form.append("file", file);
  form.append("shift_id", shiftId);

  const headers = {};
  const token = getAccessToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  let response;
  try {
    response = await fetch(`${BASE}/uploads/receipt`, {
      method: "POST",
      headers,
      body: form,
    });
  } catch (cause) {
    throw new NetworkError(cause);
  }

  const payload = await parseBody(response);
  if (response.ok) return payload;

  throw new ApiError({
    status: response.status,
    code: payload?.code ?? `HTTP_${response.status}`,
    detail: payload?.detail ?? "The upload failed.",
    requestId: payload?.request_id,
  });
}

/**
 * Human sentences for the error codes a person will actually meet.
 *
 * Only codes where the server's own `detail` needs context a user does not have. Anything
 * absent falls through to `detail` verbatim, which is written for a human already -- so this
 * map stays short rather than becoming a second copy of the API's error text that drifts.
 */
const FRIENDLY = {
  SHIFT_LOCKED: "This shift is locked. Locked shifts can never be edited — a correction has to be a reversal.",
  SHIFT_NOT_OPEN: "This shift is closed. An admin can reopen it if it genuinely needs changing.",
  NOT_YOUR_SHIFT: "This shift belongs to another attendant.",
  PROFILE_NOT_PROVISIONED: "Your sign-in worked, but you have no profile at this outlet yet. An admin needs to add you.",
  MEMBERSHIP_INACTIVE: "Your access to this outlet has been switched off.",
  // Phase 14. The server's own detail names the exact CLI command to run, which is right
  // for a log and far too much for a toast -- this is one of the few codes where the
  // shorter sentence is genuinely the more useful one.
  LAST_ADMIN_AT_OUTLET:
    "This is the only admin who can still sign in here. Make somebody else an admin first.",
  AUTH_PROVIDER_UNAVAILABLE: "Could not reach the sign-in service. Nothing was created — try again.",
  CREDIT_LIMIT_EXCEEDED: "This sale would put the customer over their credit limit. An admin can override it with a reason.",
  EXPENSE_REQUIRES_RECEIPT: "This expense needs a receipt — either its category requires one, or the amount is over the threshold.",
  UNREVIEWED_EXPENSES_EXIST: "This shift has flagged expenses nobody has reviewed yet.",
  MISSING_NOZZLE_READINGS: "Every active nozzle needs a closing reading before this shift can close.",
  MISSING_COLLECTIONS: "Cash has not been declared. Enter zero if the shift genuinely took none — that is a different answer from leaving it blank.",
  NO_CASH_DECLARED: "Nobody has declared cash for this shift, so there is no gap to book against.",
  TOTALIZER_DECREASED: "The closing reading is below the opening one. If the meter rolled over or was reset, say so — otherwise check the reading.",
  TESTING_EXCEEDS_THROUGHPUT: "The testing quantity is larger than everything the nozzle dispensed.",
  IMPLIED_FLOW_RATE_TOO_HIGH: "That reading implies more fuel than this nozzle can physically dispense in the shift. Check for an extra digit.",
  ANCHOR_REQUIRES_ADMIN: "This nozzle has no previous reading, so its opening has to be set by an admin.",
  OPENING_BALANCE_REQUIRES_ADMIN: "The very first day's opening balance has to be seeded by an admin.",
  IDEMPOTENCY_KEY_REUSED: "This looks like a different request reusing an earlier key. Nothing was saved — please try again from a fresh form.",
  REQUEST_IN_PROGRESS: "That request is still being processed. Give it a moment.",
  ATTACHMENT_ALREADY_LINKED: "That receipt is already attached to another record.",
  DAY_HAS_OPEN_SHIFTS: "Every shift on this date has to be closed first.",
  DAY_NOT_LOCKED: "Every shift on this date has to be locked before the day can be finalised.",
  PRIOR_DAY_NOT_RECONCILED: "The previous day has not been finalised yet, and the opening balance chains from it.",
};

/** The sentence to show a person for an error. */
export function explain(error) {
  if (error instanceof NetworkError) return error.message;
  if (!(error instanceof ApiError)) return "Something went wrong.";
  if (FRIENDLY[error.code]) return FRIENDLY[error.code];
  if (error.isValidation) return error.message422;
  return typeof error.detail === "string" ? error.detail : error.code;
}
