/* The route table (Phase 23 D7).
 *
 * Every hash path is identical to the Phase 12 frontend's, so bookmarks and deep links keep
 * working, and every redirect it carried is carried here. The role on a route is a courtesy --
 * the server enforces every floor (§8) -- so a refused route redirects to Today with a warning,
 * exactly as main.js's `setBeforeEach` did.
 *
 * Screens arrive tab by tab (Phase 23's build order). A path with no screen yet renders
 * NotFound, which is the honest answer.
 */

import { type ReactNode, useEffect } from "react";
import { Navigate, type RouteObject, useLocation } from "react-router";
import { satisfies, type Role } from "../lib/roles";
import { notify } from "../ui/toast";
import { Button, Card } from "../ui/primitives";
import { ScreenTitle } from "./chrome";
import { useSession } from "./session";
import { Shell } from "./Shell";

function Refused() {
  useEffect(() => {
    notify.warning("That section is not available for your role.");
  }, []);
  return <Navigate to="/today" replace />;
}

/** Render `children` only for a role at or above `role`. */
export function RequireRole({ role, children }: { role: Role; children: ReactNode }) {
  const { me } = useSession();
  return satisfies(me.role, role) ? children : <Refused />;
}

function NotFound() {
  const location = useLocation();
  return (
    <>
      <ScreenTitle title="Not found" />
      <Card>
        <div className="flex flex-col items-start gap-4">
          <p className="text-[0.9375rem] text-ink">There is no screen at {location.pathname}.</p>
          <Button variant="primary" onClick={() => (window.location.hash = "#/today")}>
            Go to Today
          </Button>
        </div>
      </Card>
    </>
  );
}

export const routes: RouteObject[] = [
  {
    path: "/",
    element: <Shell />,
    children: [
      { index: true, element: <Navigate to="/today" replace /> },
      { path: "*", element: <NotFound /> },
    ],
  },
];
