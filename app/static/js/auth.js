/* Supabase Auth by fetch, with no SDK (CLAUDE.md §8, §13.19, §14).
 *
 * §14 forbids npm, so supabase-js is out -- which turns out to cost nothing, because the part
 * of it this application needs is three HTTP calls against a documented REST endpoint.
 *
 * The API itself has no login route and never will: §8 verifies a Supabase-signed JWT on
 * every request, and Supabase issues that token to the browser directly. `GET
 * /api/v1/auth-config` supplies the URL and the anon key needed to reach it -- unauthenticated,
 * because a login screen cannot authenticate.
 *
 * ## Where the tokens live, and the trade-off that is being made
 *
 * §13.19 records this as a known approximation rather than leaving it to be discovered:
 *
 *   access token   in memory only. Never written anywhere. Lost on refresh, which is fine --
 *                  the refresh token below rebuilds it.
 *   refresh token  sessionStorage. Cleared when the tab closes, not shared with other tabs,
 *                  and not readable by another origin.
 *
 * An httpOnly cookie would be safer but is not available: this is a bearer-token API, so the
 * token has to be readable by JavaScript to be sent at all. The mitigations are therefore
 * structural -- the strict CSP in index.html, no third-party script, and no innerHTML
 * anywhere (js/dom.js) -- rather than storage tricks that would not change the exposure.
 *
 * sessionStorage rather than localStorage is a deliberate step down in convenience: a shared
 * phone in a forecourt should not stay signed in after the tab is closed.
 */

const REFRESH_KEY = "hisahab.refresh";

let accessToken = null;
let config = null; // {supabase_url, supabase_anon_key}
let onSignedOut = null;

/** Called by main.js before anything else. */
export async function loadAuthConfig() {
  const response = await fetch("/api/v1/auth-config");
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(
      body?.detail ?? "This server is not configured for Supabase authentication.",
    );
  }
  config = await response.json();
  return config;
}

export function setSignedOutHandler(handler) {
  onSignedOut = handler;
}

export function getAccessToken() {
  return accessToken;
}

export function isSignedIn() {
  return accessToken !== null;
}

function storeSession(session) {
  accessToken = session.access_token ?? null;
  if (session.refresh_token) {
    try {
      sessionStorage.setItem(REFRESH_KEY, session.refresh_token);
    } catch {
      // Private browsing, or storage disabled. Sign-in still works for this page load; the
      // user simply has to sign in again after a refresh. Better than refusing to work.
    }
  }
}

function readRefreshToken() {
  try {
    return sessionStorage.getItem(REFRESH_KEY);
  } catch {
    return null;
  }
}

async function tokenRequest(grantType, payload) {
  if (!config) throw new Error("Auth configuration has not been loaded.");

  const response = await fetch(
    `${config.supabase_url}/auth/v1/token?grant_type=${grantType}`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        // Supabase requires the anon key on every auth call, as both a header and, for
        // some deployments, the bearer. It is public by design (§16).
        apikey: config.supabase_anon_key,
        Authorization: `Bearer ${config.supabase_anon_key}`,
      },
      body: JSON.stringify(payload),
    },
  );

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(
      body?.error_description ?? body?.msg ?? "Sign-in failed.",
    );
    error.status = response.status;
    throw error;
  }
  return body;
}

/**
 * Sign in with email and password.
 *
 * Errors are deliberately not translated into "wrong password" versus "no such account".
 * Supabase does not distinguish them either, and it should not: telling an attacker which
 * half of a credential pair was right is how an account list gets enumerated.
 */
export async function signIn(email, password) {
  const session = await tokenRequest("password", { email, password });
  storeSession(session);
  return session;
}

/**
 * Exchange the refresh token for a new access token.
 *
 * Called by api.js when a request comes back 401 TOKEN_EXPIRED -- the code
 * app/core/security.py raises separately from INVALID_TOKEN for exactly this purpose, so a
 * user working through a long shift is not thrown back to a login screen every hour.
 *
 * @returns {Promise<boolean>} whether a new token was obtained
 */
export async function refreshAccessToken() {
  const refreshToken = readRefreshToken();
  if (!refreshToken || !config) return false;

  try {
    const session = await tokenRequest("refresh_token", { refresh_token: refreshToken });
    storeSession(session);
    return true;
  } catch {
    // A dead refresh token is not an error worth showing: the user simply needs to sign in
    // again, and api.js calls signOut() immediately after this returns false.
    return false;
  }
}

/** Try to restore a session on boot, so a page refresh does not force a re-login. */
export async function restoreSession() {
  if (!readRefreshToken()) return false;
  return refreshAccessToken();
}

/** Forget everything and hand control back to the shell. */
export function signOut() {
  accessToken = null;
  try {
    sessionStorage.removeItem(REFRESH_KEY);
  } catch {
    // Nothing to clear if storage was never available.
  }
  onSignedOut?.();
}
