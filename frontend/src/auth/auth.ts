/* Supabase Auth by fetch, with no SDK (CLAUDE.md §8, §13.19).
 *
 * The API has no login route and never will: §8 verifies a Supabase-signed JWT on every
 * request, and Supabase issues that token to the browser directly. `GET /api/v1/auth-config`
 * supplies the URL and anon key needed to reach it -- unauthenticated, because a login screen
 * cannot authenticate. supabase-js is not used even though npm is now admitted: the part this
 * app needs is two HTTP calls, and every dependency is code running next to the token.
 *
 * ## Where the tokens live (§13.19)
 *
 *   access token   in memory only. Lost on a page refresh, which is fine: the refresh token
 *                  below rebuilds it.
 *   refresh token  sessionStorage. Cleared when the tab closes and never shared between tabs:
 *                  a shared phone in a forecourt should not stay signed in after it is put down.
 *
 * Ported from the Phase 12 `auth.js`, behaviour unchanged (Phase 23).
 */

const REFRESH_KEY = "hisahab.refresh";

interface AuthConfig {
  supabase_url: string;
  supabase_anon_key: string;
}

interface TokenResponse {
  access_token?: string;
  refresh_token?: string;
}

export class SignInError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "SignInError";
    this.status = status;
  }
}

let accessToken: string | null = null;
let config: AuthConfig | null = null;
let onSignedOut: (() => void) | null = null;

/** The first call the app makes. A failure is fatal: without it nothing can sign in. */
export async function loadAuthConfig(): Promise<AuthConfig> {
  const response = await fetch("/api/v1/auth-config");
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail ?? "This server is not configured for Supabase authentication.");
  }
  config = (await response.json()) as AuthConfig;
  return config;
}

export function setSignedOutHandler(handler: () => void): void {
  onSignedOut = handler;
}

export function getAccessToken(): string | null {
  return accessToken;
}

export function isSignedIn(): boolean {
  return accessToken !== null;
}

function storeSession(session: TokenResponse): void {
  accessToken = session.access_token ?? null;
  if (session.refresh_token) {
    try {
      sessionStorage.setItem(REFRESH_KEY, session.refresh_token);
    } catch {
      // Private browsing or storage disabled: sign-in still works for this page load.
    }
  }
}

function readRefreshToken(): string | null {
  try {
    return sessionStorage.getItem(REFRESH_KEY);
  } catch {
    return null;
  }
}

async function tokenRequest(grantType: string, payload: object): Promise<TokenResponse> {
  if (!config) throw new Error("Auth configuration has not been loaded.");
  const response = await fetch(`${config.supabase_url}/auth/v1/token?grant_type=${grantType}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      // Public by design (§16): Supabase wants it as a header and, on some deployments, the bearer.
      apikey: config.supabase_anon_key,
      Authorization: `Bearer ${config.supabase_anon_key}`,
    },
    body: JSON.stringify(payload),
  });
  const body = (await response.json().catch(() => null)) as
    | (TokenResponse & { error_description?: string; msg?: string })
    | null;
  if (!response.ok) {
    throw new SignInError(body?.error_description ?? body?.msg ?? "Sign-in failed.", response.status);
  }
  return body ?? {};
}

/** Sign in with email and password. Errors deliberately do not distinguish "wrong password"
 * from "no such account": saying which half was right is how an account list is enumerated. */
export async function signIn(email: string, password: string): Promise<void> {
  storeSession(await tokenRequest("password", { email, password }));
}

/** Exchange the refresh token for a new access token. Called by the API client on 401
 * TOKEN_EXPIRED -- the code app/core/security.py raises separately from INVALID_TOKEN so a long
 * shift is not thrown back to the login screen every hour. */
export async function refreshAccessToken(): Promise<boolean> {
  const refreshToken = readRefreshToken();
  if (!refreshToken || !config) return false;
  try {
    storeSession(await tokenRequest("refresh_token", { refresh_token: refreshToken }));
    return true;
  } catch {
    return false;
  }
}

/** Restore a session on boot, so a page refresh mid-shift does not mean signing in again. */
export async function restoreSession(): Promise<boolean> {
  if (!readRefreshToken()) return false;
  return refreshAccessToken();
}

/** Forget everything and hand control back to the app. */
export function signOut(): void {
  accessToken = null;
  try {
    sessionStorage.removeItem(REFRESH_KEY);
  } catch {
    // Nothing to clear if storage was never available.
  }
  onSignedOut?.();
}
