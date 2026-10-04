/* The six tabs (moved out of Shell.tsx in Phase 24, so navigation.ts can read them without
 * importing the shell that uses it).
 *
 * Direct, specific names: what is inside, rather than an umbrella like "Home". The order runs
 * entry-first -- what you do today, then what you owe, then how it went -- and it is also the
 * spatial order route transitions follow: moving to a tab on the right slides the screen in
 * from the right.
 */

import type { ComponentType } from "react";
import {
  ChartPieSliceIcon,
  CurrencyInrIcon,
  GearSixIcon,
  NotebookIcon,
  NotePencilIcon,
  type IconProps,
  SunHorizonIcon,
} from "@phosphor-icons/react";
import type { Role } from "../lib/roles";

export type TabId = "today" | "entry" | "cash" | "credit" | "summary" | "admin";

export interface RouteHandle {
  tab?: TabId;
  /** A dashboard-shaped screen that should use a wide monitor's width (Phase 25 D2). List and
   * form screens leave it unset and stay at a readable measure. */
  wide?: boolean;
}

export interface Tab {
  id: TabId;
  label: string;
  route: string;
  role: Role;
  Icon: ComponentType<IconProps>;
}

export const TABS: readonly Tab[] = [
  { id: "today", label: "Today", route: "/today", role: "attendant", Icon: SunHorizonIcon },
  { id: "entry", label: "Entry", route: "/entry", role: "attendant", Icon: NotePencilIcon },
  { id: "cash", label: "Cash", route: "/cash", role: "manager", Icon: CurrencyInrIcon },
  // Manager floor (§8): a customer's balance has never been an attendant's business.
  { id: "credit", label: "Credit", route: "/credit", role: "manager", Icon: NotebookIcon },
  { id: "summary", label: "Summary", route: "/summary", role: "manager", Icon: ChartPieSliceIcon },
  { id: "admin", label: "Admin", route: "/admin", role: "admin", Icon: GearSixIcon },
];

