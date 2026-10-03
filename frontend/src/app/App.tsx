/* Boot, the auth gate, and the signed-in app (CLAUDE.md §2, §8).
 *
 * ## The boot sequence, and why it is in this order (unchanged from Phase 12)
 *
 *   1. `/api/v1/auth-config` -- unauthenticated. Without it nothing can reach Supabase, so a
 *      failure here is fatal and says so plainly.
 *   2. Restore a session from the refresh token, if there is one: a page refresh mid-shift must
 *      not mean signing in again.
 *   3. `/api/v1/me` -- is this token valid, and is this person *provisioned at this outlet*? A
 *      valid Supabase identity with no profile 403s with PROFILE_NOT_PROVISIONED, which is
 *      explained rather than reported as a failed sign-in.
 *   4. `/api/v1/client-config` -- the timezone and thresholds the UI must not hardcode. Not
 *      fatal: the app works with defaults, and the server enforces every rule regardless.
 *
 * Signing out clears the query cache and every toast, so one person's figures and errors never
 * greet the next person to pick up a shared phone.
 */

import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createHashRouter, RouterProvider } from "react-router";
import { useQueryClient } from "@tanstack/react-query";
import { api, explain, requestIdOf } from "../api/client";
import type { ClientConfig, Me } from "../api/types";
import { isSignedIn, loadAuthConfig, restoreSession, setSignedOutHandler, signOut } from "../auth/auth";
import { setZone } from "../lib/time";
import { Button } from "../ui/primitives";
import { clearToasts, notify, Toaster } from "../ui/toast";
import { routes } from "./routes";
import { SessionContext, type Session } from "./session";

/* The login screen is its own chunk: it alone needs ScrollTrigger, and a signed-in session
 * restored from a refresh token never shows it. */
const Login = lazy(() => import("../screens/Login").then((module) => ({ default: module.Login })));

type Phase =
  | { kind: "booting" }
  | { kind: "fatal"; title: string; detail: string }
  | { kind: "login" }
  | { kind: "ready"; me: Me; config: ClientConfig | null };

/** Codes from /me that mean "your password was right, but you cannot work here (yet)". */
const NOT_HERE = new Set(["PROFILE_NOT_PROVISIONED", "NOT_A_MEMBER", "PROFILE_INACTIVE", "MEMBERSHIP_INACTIVE"]);

export function App() {
  const [phase, setPhase] = useState<Phase>({ kind: "booting" });
  const [outletName, setOutletName] = useState<string | undefined>();
  const queryClient = useQueryClient();
  const booted = useRef(false);

  const startSession = useCallback(async () => {
    let me: Me;
    try {
      me = await api.get<Me>("/me");
    } catch (error) {
      const code = (error as { code?: string }).code;
      notify.error(explain(error), NOT_HERE.has(code ?? "") ? {} : { requestId: requestIdOf(error) });
      signOut();
      return;
    }

    let config: ClientConfig | null = null;
    try {
      config = await api.get<ClientConfig>("/client-config");
      // §3 rule 4: display conversion happens here, but the zone is server configuration.
      setZone(config.tz_display);
      setOutletName(config.outlet_name ?? undefined);
    } catch {
      notify.warning("Could not load outlet settings; using defaults. Server rules are unaffected.");
    }
    setPhase({ kind: "ready", me, config });
  }, []);

  useEffect(() => {
    // StrictMode runs effects twice in development; booting twice would refresh the token twice.
    if (booted.current) return;
    booted.current = true;

    setSignedOutHandler(() => {
      queryClient.clear();
      clearToasts();
      setPhase({ kind: "login" });
    });

    void (async () => {
      try {
        await loadAuthConfig();
      } catch (error) {
        setPhase({
          kind: "fatal",
          title: "Sign-in is not available",
          detail:
            error instanceof Error
              ? error.message
              : "This server is not configured for Supabase authentication. Set SUPABASE_URL and SUPABASE_ANON_KEY.",
        });
        return;
      }
      if ((await restoreSession()) && isSignedIn()) {
        await startSession();
        return;
      }
      setPhase({ kind: "login" });
    })();
  }, [queryClient, startSession]);

  return (
    <>
      {phase.kind === "booting" ? <Splash /> : null}
      {phase.kind === "fatal" ? <Fatal title={phase.title} detail={phase.detail} /> : null}
      {phase.kind === "login" ? (
        <Suspense fallback={<Splash />}>
          <Login outletName={outletName} onSignedIn={startSession} />
        </Suspense>
      ) : null}
      {phase.kind === "ready" ? <SignedIn me={phase.me} config={phase.config} /> : null}
      <Toaster />
    </>
  );
}

function SignedIn({ me, config }: { me: Me; config: ClientConfig | null }) {
  // One router per session: created when the shell first mounts, discarded on sign-out.
  const router = useMemo(() => createHashRouter(routes), []);
  const session = useMemo<Session>(() => ({ me, config, signOut }), [me, config]);
  return (
    <SessionContext.Provider value={session}>
      <RouterProvider router={router} />
    </SessionContext.Provider>
  );
}

function Splash() {
  return (
    <div className="flex min-h-dvh items-center justify-center">
      <p className="wordmark text-[1.375rem] text-ink-faint">HiSahab</p>
    </div>
  );
}

/* Distinct from a toast: if the app cannot start there is no shell for a toast to sit on. */
function Fatal({ title, detail }: { title: string; detail: string }) {
  return (
    <main className="mx-auto flex min-h-dvh max-w-md flex-col justify-center gap-4 px-6">
      <h1 className="text-[1.375rem] font-semibold tracking-[-0.02em] text-ink">{title}</h1>
      <p className="text-[0.9375rem] text-ink-muted">{detail}</p>
      <div>
        <Button variant="primary" onClick={() => window.location.reload()}>
          Try again
        </Button>
      </div>
    </main>
  );
}
