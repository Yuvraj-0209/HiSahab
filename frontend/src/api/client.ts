/* The one place this application talks to the API (CLAUDE.md §3 rule 10, §6.10, §9).
 *
 * Everything about the HTTP contract lives here so no screen has to remember it: the bearer
 * header, the two shapes of the error envelope, silent token refresh, and the idempotency rule
 * that actually protects money.
 *
 * ## The error envelope has two shapes
 *
 *   business error:  {"detail": "human message", "code": "MACHINE_CODE", "request_id": "..."}
 *   validation:      {"detail": [{loc, msg, ...}], "code": "VALIDATION_ERROR", ...}
 *
 * `detail` is a string in the first and an array in the second; `ApiError` keeps both, so a
 * form can show field-level errors against the fields they name.
 *
 * ## 401 is handled by code, not by status
 *
 * app/core/security.py raises TOKEN_EXPIRED distinctly from INVALID_TOKEN "so the frontend can
 * refresh silently rather than bouncing the user to a login screen".
 *
 * ## The Idempotency-Key belongs to a SUBMISSION, not to a fetch (§6.10, §14)
 *
 * A key minted per fetch makes every retry a new request, so the timeout-then-retry case --
 * the one §6.10 exists for -- creates two rows. `Submission` (submission.ts) mints one key when
 * a form opens and holds it until that submission succeeds.
 *
 * Ported from the Phase 12 `api.js`, behaviour unchanged (Phase 23).
 */

import { getAccessToken, refreshAccessToken, signOut } from "../auth/auth";

const BASE = "/api/v1";

/** Routes that require an Idempotency-Key, as patterns over the path below /api/v1.
 *
 * NOT "every POST": readings are idempotent by construction (UNIQUE (shift_id, nozzle_id) makes
 * a retry a 409 and the client PATCHes instead), uploads are not money records (§6.10's closing
 * note), and reference-data POSTs create rows a human is looking at.
 *
 * **This list is a copy of a fact the server publishes**: every route that requires a key
 * declares the header in its OpenAPI document. It drifted once -- Phase 20's bank review lost
 * every "Record" to a 400 -- so tests/test_idempotency_client_coverage.py compares the two.
 */
export const NEEDS_IDEMPOTENCY: readonly RegExp[] = [
  /^\/shifts\/[^/]+\/collections$/,
  /^\/shifts\/[^/]+\/expenses$/,
  /^\/shifts\/[^/]+\/credit-sales$/,
  /^\/shifts\/[^/]+\/credit-repayments$/,
  // Phase 16: the dated, shift-less form. Creates a money record, so it can duplicate on a retry.
  /^\/credit-repayments$/,
  /^\/shifts\/[^/]+\/non-fuel-sales$/,
  /^\/shifts\/[^/]+\/bank-deposits$/,
  /^\/shifts\/[^/]+\/shortfalls$/,
  /^\/shifts\/[^/]+\/shortfall-settlements$/,
  // Phase 20's bank review creates credit_repayments from statement lines.
  /^\/bank-transactions\/confirm-repayments$/,
  /\/reversals$/,
];

export function needsIdempotencyKey(path: string, method: string): boolean {
  if (method !== "POST") return false;
  const route = path.split("?")[0] ?? path;
  return NEEDS_IDEMPOTENCY.some((pattern) => pattern.test(route));
}

export interface ValidationIssue {
  loc?: (string | number)[];
  msg?: string;
}

/** A failed request, carrying everything needed to explain it to a person. */
export class ApiError extends Error {
  status: number;
  code: string;
  detail: string | ValidationIssue[];
  requestId: string | undefined;

  constructor({
    status,
    code,
    detail,
    requestId,
  }: {
    status: number;
    code: string;
    detail: string | ValidationIssue[];
    requestId?: string | undefined;
  }) {
    super(typeof detail === "string" ? detail : code);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.requestId = requestId;
  }

  /** True when `detail` is FastAPI's field-level array. */
  get isValidation(): boolean {
    return this.code === "VALIDATION_ERROR" && Array.isArray(this.detail);
  }
}

/** The network itself failed -- no response at all. The one case where retrying the same
 * request with the same key is exactly right. */
export class NetworkError extends Error {
  constructor(cause: unknown) {
    super("The network is unavailable. Nothing was lost: try again.");
    this.name = "NetworkError";
    this.cause = cause;
  }
}

async function parseBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    // Something upstream answered instead of the API (a proxy error page). The text is more
    // useful than a parse error.
    return { detail: text.slice(0, 200), code: "NON_JSON_RESPONSE" };
  }
}

type Envelope = { code?: string; detail?: string | ValidationIssue[]; request_id?: string } | null;

function toApiError(status: number, payload: unknown, fallback: string): ApiError {
  const body = payload as Envelope;
  return new ApiError({
    status,
    code: body?.code ?? `HTTP_${status}`,
    detail: body?.detail ?? fallback,
    requestId: body?.request_id,
  });
}

export type Query = Record<string, string | number | boolean | null | undefined>;

export interface RequestOptions {
  body?: unknown;
  query?: Query | undefined;
  idempotencyKey?: string | undefined;
  /** Internal: prevents an infinite refresh loop. */
  retryOnExpiry?: boolean;
}

/** Make a request. Every call in the application goes through here. */
export async function request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
  const { body, query, idempotencyKey, retryOnExpiry = true } = options;

  let url = BASE + path;
  if (query) {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
    }
    const encoded = params.toString();
    if (encoded) url += `?${encoded}`;
  }

  const headers: Record<string, string> = {};
  const token = getAccessToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  if (needsIdempotencyKey(path, method)) {
    if (!idempotencyKey) {
      // Loud, and deliberately not papered over by minting one here: silently generating a key
      // would restore exactly the per-call behaviour this module exists to prevent.
      throw new Error(
        `${method} ${path} creates a money record and needs an Idempotency-Key. ` +
          "Use useSubmission() rather than request() directly.",
      );
    }
    headers["Idempotency-Key"] = idempotencyKey;
  }

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: body === undefined ? null : JSON.stringify(body),
    });
  } catch (cause) {
    throw new NetworkError(cause);
  }

  if (response.status === 204) return null as T;
  const payload = await parseBody(response);
  if (response.ok) return payload as T;

  const error = toApiError(response.status, payload, "Something went wrong.");

  if (response.status === 401) {
    // Silent refresh, exactly once. A second failure means the refresh token is dead too.
    if (error.code === "TOKEN_EXPIRED" && retryOnExpiry) {
      if (await refreshAccessToken()) {
        return request<T>(method, path, { ...options, retryOnExpiry: false });
      }
    }
    signOut();
  }

  throw error;
}

export const api = {
  get: <T>(path: string, query?: Query) => request<T>("GET", path, { query }),
  /** A POST that creates no money record. Money POSTs go through useSubmission(). */
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, { body }),
  patch: <T>(path: string, body: unknown) => request<T>("PATCH", path, { body }),
};

/**
 * A v4 UUID on a page that is **not guaranteed to be a secure context**.
 *
 * `crypto.randomUUID()` is `[SecureContext]`: `https://` and `http://localhost` qualify;
 * `http://192.168.1.23:8000` -- how the app is reached from a phone on the local network --
 * does not, and there the property is simply undefined. Phase 12 found every Add button dead on
 * a phone because of it. `getRandomValues` is not gated, so the fallback is real randomness and
 * never `Math.random`: a guessable key is a replay handed to whoever guesses it.
 */
export function newIdempotencyKey(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40; // version 4
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80; // variant 10xx
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/**
 * POST a multipart form. The one path that cannot go through `request()`: FormData must set its
 * own boundary, so no Content-Type is set here. `extraHeaders` carries the Idempotency-Key the
 * statement import requires (§6.10); receipts take none.
 */
export async function postMultipart<T>(
  path: string,
  fields: Record<string, string | Blob>,
  { extraHeaders = {} }: { extraHeaders?: Record<string, string> } = {},
): Promise<T> {
  const form = new FormData();
  for (const [name, value] of Object.entries(fields)) form.append(name, value);

  const headers: Record<string, string> = { ...extraHeaders };
  const token = getAccessToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, { method: "POST", headers, body: form });
  } catch (cause) {
    throw new NetworkError(cause);
  }
  const payload = await parseBody(response);
  if (response.ok) return payload as T;
  throw toApiError(response.status, payload, "The upload failed.");
}

/** Upload a receipt (§7.2). No Idempotency-Key: an attachment is not a money record, and a
 * retried upload leaves an unlinked row §7.4's sweep reclaims within 24 hours. */
export function uploadReceipt(file: File, shiftId: string): Promise<{ attachment_id: string }> {
  return postMultipart("/uploads/receipt", { file, shift_id: shiftId });
}

/**
 * Human sentences for the error codes a person will actually meet. Only codes whose `detail`
 * needs context a user does not have; anything absent falls through to `detail` verbatim, which
 * is already written for a human -- so this stays short rather than becoming a second copy of
 * the API's text that drifts.
 */
const FRIENDLY: Record<string, string> = {
  SHIFT_LOCKED: "This shift is locked, and a locked shift is never edited. A correction has to be a reversal.",
  SHIFT_NOT_OPEN: "This shift is closed. An admin can reopen it if it genuinely needs changing.",
  NOT_YOUR_SHIFT: "This shift belongs to another attendant.",
  PROFILE_NOT_PROVISIONED: "Your sign-in worked, but you have no profile at this outlet yet. An admin needs to add you.",
  MEMBERSHIP_INACTIVE: "Your access to this outlet has been switched off.",
  LAST_ADMIN_AT_OUTLET: "This is the only admin who can still sign in here. Make somebody else an admin first.",
  AUTH_PROVIDER_UNAVAILABLE: "Could not reach the sign-in service. Nothing was created, so try again.",
  CREDIT_LIMIT_EXCEEDED: "This sale would put the customer over their credit limit. An admin can override it with a reason.",
  EXPENSE_REQUIRES_RECEIPT: "This expense needs a receipt: its category requires one, or the amount is over the threshold.",
  UNREVIEWED_EXPENSES_EXIST: "This shift has flagged expenses nobody has reviewed yet.",
  MISSING_NOZZLE_READINGS: "Every active nozzle needs a closing reading before this shift can close.",
  MISSING_COLLECTIONS: "Cash has not been declared. Enter zero if the shift genuinely took none. That is a different answer from leaving it blank.",
  NO_CASH_DECLARED: "Nobody has declared cash for this shift, so there is no gap to book against.",
  TOTALIZER_DECREASED: "The closing reading is below the opening one. If the meter rolled over or was reset, say so. Otherwise check the reading.",
  TESTING_EXCEEDS_THROUGHPUT: "The testing quantity is larger than everything the nozzle dispensed.",
  IMPLIED_FLOW_RATE_TOO_HIGH: "That reading implies more fuel than this nozzle can physically dispense in the shift. Check for an extra digit.",
  ANCHOR_REQUIRES_ADMIN: "This nozzle has no previous reading, so its opening has to be set by an admin.",
  OPENING_BALANCE_REQUIRES_ADMIN: "The very first day's opening balance has to be seeded by an admin.",
  IDEMPOTENCY_KEY_REUSED: "This looks like a different request reusing an earlier key. Nothing was saved. Please try again from a fresh form.",
  REQUEST_IN_PROGRESS: "That request is still being processed. Give it a moment.",
  ATTACHMENT_ALREADY_LINKED: "That receipt is already attached to another record.",
  DAY_HAS_OPEN_SHIFTS: "Every shift on this date has to be closed first.",
  DAY_NOT_LOCKED: "Every shift on this date has to be locked before the day can be finalised.",
  PRIOR_DAY_NOT_RECONCILED: "The previous day has not been finalised yet, and the opening balance chains from it.",
};

/** The sentence to show a person for an error. */
export function explain(error: unknown): string {
  if (error instanceof NetworkError) return error.message;
  if (!(error instanceof ApiError)) return "Something went wrong.";
  const friendly = FRIENDLY[error.code];
  if (friendly) return friendly;
  if (error.isValidation) return "Some fields need attention.";
  return typeof error.detail === "string" ? error.detail : error.code;
}

/** The request id to quote, if the error carries one (§9). */
export function requestIdOf(error: unknown): string | undefined {
  return error instanceof ApiError ? error.requestId : undefined;
}
