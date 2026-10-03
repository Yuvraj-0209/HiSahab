/* The signed-in session: who this is, at which outlet, with which settings (CLAUDE.md §8).
 *
 * Provided once the boot sequence (App.tsx) has a valid token, a provisioned profile and the
 * client config. Screens read the role from here to decide what to *render*; the server decides
 * what is *allowed*, and every screen still handles a real 403 (§8: "hiding a button is UX,
 * not a control").
 */

import { createContext, useContext } from "react";
import type { ClientConfig, Me } from "../api/types";

export interface Session {
  me: Me;
  /** Null when /client-config failed: the app still works with defaults, and the server still
   * enforces every rule. Screens must treat a missing threshold as "no client-side warning",
   * never as zero. */
  config: ClientConfig | null;
  signOut: () => void;
}

export const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession() outside a signed-in session");
  return session;
}
