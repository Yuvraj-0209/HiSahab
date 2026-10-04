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
import { Navigate, Outlet, type RouteObject, useLocation, useParams } from "react-router";
import { satisfies, type Role } from "../lib/roles";
import { notify } from "../ui/toast";
import { Button, Card } from "../ui/primitives";
import { ScreenTitle } from "./chrome";
import { useSession } from "./session";
import { Shell } from "./Shell";
import type { RouteHandle } from "./tabs";

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

const tab = (id: RouteHandle["tab"], options: Omit<RouteHandle, "tab"> = {}): RouteHandle => ({ tab: id, ...options });
/** Today, the shift it shows by id, Readings and Summary are read at a glance on a wide screen. */
const WIDE = { wide: true } as const;

/** Phase 16 moved a customer's ledger from Admin to Credit; old links still land. */
function RedirectToLedger() {
  const { customerId = "" } = useParams();
  return <Navigate to={`/credit/customers/${customerId}`} replace />;
}

function RedirectToDay() {
  const { businessDate = "" } = useParams();
  return <Navigate to={`/days/${businessDate}`} replace />;
}

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
const cash = () => import("../screens/cash");
const days = () => import("../screens/days");
const reports = () => import("../screens/reports");
const shortfalls = () => import("../screens/shortfalls");
const flagged = () => import("../screens/flagged_expenses");
const credit = () => import("../screens/credit");
const openingBalances = () => import("../screens/credit_opening_balances");
const statement = () => import("../screens/credit_statement");
const bank = () => import("../screens/bank");
const summary = () => import("../screens/summary");
const admin = () => import("../screens/admin");
const pricing = () => import("../screens/admin_pricing");
const adminCustomers = () => import("../screens/admin_customers");
const adminUsers = () => import("../screens/admin_users");
const audit = () => import("../screens/audit_logs");

export const routes: RouteObject[] = [
  {
    path: "/",
    element: <Shell />,
    children: [
      { index: true, element: <Navigate to="/today" replace /> },

      {
        element: <RoleGate role="attendant" />,
        children: [
          { path: "today", handle: tab("today", WIDE), lazy: () => today().then((m) => ({ Component: m.TodayScreen })) },
          { path: "entry", handle: tab("entry"), lazy: () => entry().then((m) => ({ Component: m.EntryScreen })) },
          {
            path: "shifts/:shiftId/readings",
            handle: tab("entry", WIDE),
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
            handle: tab("today", WIDE),
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
          { path: "cash", handle: tab("cash"), lazy: () => cash().then((m) => ({ Component: m.CashScreen })) },
          { path: "days", handle: tab("cash"), lazy: () => days().then((m) => ({ Component: m.DaysScreen })) },
          { path: "days/:businessDate", handle: tab("cash"), lazy: () => days().then((m) => ({ Component: m.DayScreen })) },
          // Phase 15's merge: the old per-day routes redirect, so old links keep working.
          { path: "daily-summaries", element: <Navigate to="/days" replace /> },
          { path: "daily-summaries/:businessDate", Component: RedirectToDay },
          // "/reports/alerts" before "/reports/:businessDate", which would swallow it.
          { path: "reports", handle: tab("cash"), lazy: () => reports().then((m) => ({ Component: m.ReportsScreen })) },
          { path: "reports/alerts", handle: tab("cash"), lazy: () => reports().then((m) => ({ Component: m.AlertsScreen })) },
          { path: "reports/:businessDate", Component: RedirectToDay },
          {
            path: "salesmen/:salesmanId/ledger",
            handle: tab("cash"),
            lazy: () => shortfalls().then((m) => ({ Component: m.ShortfallLedgerScreen })),
          },
          { path: "expenses/flagged", handle: tab("cash"), lazy: () => flagged().then((m) => ({ Component: m.FlaggedExpensesScreen })) },

          { path: "credit", handle: tab("credit"), lazy: () => credit().then((m) => ({ Component: m.CreditHubScreen })) },
          {
            path: "credit/repayments",
            handle: tab("credit"),
            lazy: () => creditRepayments().then((m) => ({ Component: m.LedgerRepaymentsScreen })),
          },
          { path: "credit/statement", handle: tab("credit"), lazy: () => statement().then((m) => ({ Component: m.CreditStatementScreen })) },
          { path: "credit/bank", handle: tab("credit"), lazy: () => bank().then((m) => ({ Component: m.BankHubScreen })) },
          { path: "credit/bank/review", handle: tab("credit"), lazy: () => bank().then((m) => ({ Component: m.BankReviewScreen })) },
          {
            path: "credit/bank/reconciliation",
            handle: tab("credit"),
            lazy: () => bank().then((m) => ({ Component: m.BankReconciliationScreen })),
          },
          {
            path: "credit/customers/:customerId",
            handle: tab("credit"),
            lazy: () => credit().then((m) => ({ Component: m.CustomerLedgerScreen })),
          },
          { path: "admin/customers/:customerId/ledger", Component: RedirectToLedger },

          { path: "summary", handle: tab("summary", WIDE), lazy: () => summary().then((m) => ({ Component: m.SummaryScreen })) },
        ],
      },

      {
        element: <RoleGate role="admin" />,
        children: [
          // Inside the manager's Credit tab, but admin-only: nothing else can check the figure
          // (§5.2, §8). The server refuses a manager with 403 regardless.
          {
            path: "credit/opening-balances",
            handle: tab("credit"),
            lazy: () => openingBalances().then((m) => ({ Component: m.OpeningBalancesScreen })),
          },

          { path: "admin", handle: tab("admin"), lazy: () => admin().then((m) => ({ Component: m.AdminHubScreen })) },
          { path: "admin/fuel-types", handle: tab("admin"), lazy: () => admin().then((m) => ({ Component: m.FuelTypesScreen })) },
          { path: "admin/nozzles", handle: tab("admin"), lazy: () => admin().then((m) => ({ Component: m.NozzlesScreen })) },
          { path: "admin/prices", handle: tab("admin"), lazy: () => pricing().then((m) => ({ Component: m.PricesScreen })) },
          { path: "admin/margins", handle: tab("admin"), lazy: () => pricing().then((m) => ({ Component: m.MarginsScreen })) },
          { path: "admin/categories", handle: tab("admin"), lazy: () => admin().then((m) => ({ Component: m.CategoriesScreen })) },
          { path: "admin/customers", handle: tab("admin"), lazy: () => adminCustomers().then((m) => ({ Component: m.CustomersScreen })) },
          { path: "admin/shift-templates", handle: tab("admin"), lazy: () => admin().then((m) => ({ Component: m.ShiftTemplatesScreen })) },
          { path: "admin/bank-accounts", handle: tab("admin"), lazy: () => admin().then((m) => ({ Component: m.BankAccountsScreen })) },
          { path: "admin/users", handle: tab("admin"), lazy: () => adminUsers().then((m) => ({ Component: m.UsersScreen })) },
          { path: "admin/audit", handle: tab("admin"), lazy: () => audit().then((m) => ({ Component: m.AuditLogsScreen })) },
        ],
      },

      { path: "*", element: <NotFound /> },
    ],
  },
];
