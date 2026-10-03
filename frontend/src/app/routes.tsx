/* The route table (Phase 23 D7).
 *
 * Every hash path is identical to the Phase 12 frontend's, so bookmarks and deep links keep
 * working, and every redirect it carried is carried here. The role on a route is a courtesy --
 * the server enforces every floor (§8) -- so a refused route redirects to Today with a warning,
 * exactly as main.js's `setBeforeEach` did. Roles are layout routes, so a screen cannot be
 * registered without one.
 *
 * Each tab is its own lazily loaded chunk (`lazy`), so the first paint pays only for the shell.
 * The `handle.tab` on a route is what lights its tab in the bar.
 */

import { useEffect } from "react";
import { Navigate, Outlet, type RouteObject, useLocation } from "react-router";
import { satisfies, type Role } from "../lib/roles";
import { notify } from "../ui/toast";
import { Button, Card } from "../ui/primitives";
import { ScreenTitle } from "./chrome";
import { useSession } from "./session";
import { type RouteHandle, Shell } from "./Shell";

function Refused() {
  useEffect(() => {
    notify.warning("That section is not available for your role.");
  }, []);
  return <Navigate to="/today" replace />;
}

/** A layout route: its children render only for a role at or above `role`. */
function RoleGate({ role }: { role: Role }) {
  const { me } = useSession();
  return satisfies(me.role, role) ? <Outlet /> : <Refused />;
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

const tab = (id: RouteHandle["tab"]): RouteHandle => ({ tab: id });

/* --- the tabs, one chunk each ---------------------------------------------------------- */

const today = () => import("../screens/today");
const entry = () => import("../screens/entry");
const readings = () => import("../screens/readings");
const collections = () => import("../screens/collections");
const expenses = () => import("../screens/expenses");
const nonFuelSales = () => import("../screens/non_fuel_sales");
const creditSales = () => import("../screens/credit_sales");
const creditRepayments = () => import("../screens/credit_repayments");
const bankDeposits = () => import("../screens/bank_deposits");
const cashPosition = () => import("../screens/cash_position");

export const routes: RouteObject[] = [
  {
    path: "/",
    element: <Shell />,
    children: [
      { index: true, element: <Navigate to="/today" replace /> },

      {
        element: <RoleGate role="attendant" />,
        children: [
          { path: "today", handle: tab("today"), lazy: () => today().then((m) => ({ Component: m.TodayScreen })) },
          { path: "entry", handle: tab("entry"), lazy: () => entry().then((m) => ({ Component: m.EntryScreen })) },
          {
            path: "shifts/:shiftId/readings",
            handle: tab("entry"),
            lazy: () => readings().then((m) => ({ Component: m.ReadingsScreen })),
          },
          {
            path: "shifts/:shiftId/collections",
            handle: tab("entry"),
            lazy: () => collections().then((m) => ({ Component: m.CollectionsScreen })),
          },
          {
            path: "shifts/:shiftId/expenses",
            handle: tab("entry"),
            lazy: () => expenses().then((m) => ({ Component: m.ExpensesScreen })),
          },
          {
            path: "shifts/:shiftId/non-fuel-sales",
            handle: tab("entry"),
            lazy: () => nonFuelSales().then((m) => ({ Component: m.NonFuelSalesScreen })),
          },
          {
            path: "shifts/:shiftId/credit-sales",
            handle: tab("entry"),
            lazy: () => creditSales().then((m) => ({ Component: m.CreditSalesScreen })),
          },
          {
            path: "shifts/:shiftId/credit-repayments",
            handle: tab("entry"),
            lazy: () => creditRepayments().then((m) => ({ Component: m.CreditRepaymentsScreen })),
          },
        ],
      },

      {
        element: <RoleGate role="manager" />,
        children: [
          // A shift stops being "current" the moment it closes; this is the only page that can
          // still reach it -- to lock it, reopen it, or just look.
          {
            path: "shifts/:shiftId",
            handle: tab("today"),
            lazy: () => today().then((m) => ({ Component: m.ShiftByIdScreen })),
          },
          {
            path: "shifts/:shiftId/bank-deposits",
            handle: tab("entry"),
            lazy: () => bankDeposits().then((m) => ({ Component: m.BankDepositsScreen })),
          },
          {
            path: "shifts/:shiftId/cash-position",
            handle: tab("cash"),
            lazy: () => cashPosition().then((m) => ({ Component: m.CashPositionScreen })),
          },
        ],
      },

      { path: "*", element: <NotFound /> },
    ],
  },
];
