/* Moving between screens, with the browser's View Transitions (Phase 24 D3, CLAUDE.md §13.43).
 *
 * `useGo()` is `useNavigate()` with a sense of direction. Every navigation in the app goes
 * through it -- a structural test refuses `useNavigate` in a screen -- so every screen gets the
 * same spatial language without asking for it:
 *
 *   forward   a drill-down (a day row into its day)      the new screen comes from the right
 *   back      up a level (the day back to its list)       the old screen leaves to the right
 *   left/right  a tab change                               the screen slides the way the tab is
 *   fade      sideways at the same depth                   a crossfade, no direction to claim
 *
 * That is what the motion communicates: where you went, relative to where you were. It is CSS on
 * the `::view-transition-*` pseudo-elements (styles.css), driven by a `data-nav` attribute set
 * here just before React Router starts the transition, and it runs on the compositor.
 *
 * ## The third engine, and its territory (§14)
 *
 * A view transition animates *snapshots* of the page, never the elements themselves, so it can
 * never fight GSAP or the spring for an element's transform. It owns the route change and nothing
 * else. The chrome and the tab bar are captured separately and do not move.
 *
 * ## Shared elements
 *
 * `go(path, { shared: element })` names the tapped element `shared` for one transition, and the
 * destination's chrome subtitle carries the same name -- so a day row's date flies into the day
 * screen's header, and a customer's name into their ledger's. A view-transition name must be
 * unique when the page is captured, so the outgoing screen's own subtitle is marked as leaving
 * first (it would otherwise be a second `shared`, and the browser skips a transition with a
 * duplicate name).
 *
 * ## Where there is no API, or no wish for motion
 *
 * Browsers without `document.startViewTransition` navigate exactly as before, and the screen
 * gets the plain CSS entrance (`.screen-enter`). Under prefers-reduced-motion no transition is
 * started at all. The browser's own back and forward buttons are not navigations the router
 * starts, so they also get the plain entrance (§13.43).
 */

import { useCallback } from "react";
import { useLocation, useMatches, useNavigate } from "react-router";
import { motionAllowed } from "../motion/preference";
import { type RouteHandle, TABS } from "./tabs";

export type Direction = "forward" | "back" | "left" | "right" | "fade" | "none";

const TAB_ROUTES = TABS.map((tab) => tab.route);
const tabIndexOf = (id: string | undefined) => TABS.findIndex((tab) => tab.id === id);
const depth = (path: string) => path.split("/").filter(Boolean).length;

/**
 * Which way a navigation goes, from where you are to where you are going.
 *
 * Route depth alone is not enough: `/cash` and `/days` are both one segment deep, yet the second
 * is a list you opened *from* the first. So a tab's root is the top of its stack, and leaving it
 * for anything that is not another tab's root is always forward.
 */
export function directionOf(from: { path: string; tab: string | undefined }, to: string): Direction {
  const target = to.split("?")[0] ?? to;
  if (target === from.path) return "none"; // a query change on the same screen is not a move

  const targetTab = TAB_ROUTES.indexOf(target);
  const fromIsTabRoot = TAB_ROUTES.includes(from.path);
  const currentTab = tabIndexOf(from.tab);

  if (targetTab >= 0) {
    if (!fromIsTabRoot && targetTab === currentTab) return "back";
    if (currentTab < 0 || targetTab === currentTab) return "fade";
    return targetTab > currentTab ? "right" : "left";
  }
  if (fromIsTabRoot) return "forward";
  const delta = depth(target) - depth(from.path);
  return delta > 0 ? "forward" : delta < 0 ? "back" : "fade";
}

/** How long the `data-nav` marker outlives a transition: the longest one is --dur-medium plus a
 * short delay, and a marker left behind would only suppress the next plain entrance. */
const MARKER_LIFETIME_MS = 900;
let clearMarker: ReturnType<typeof setTimeout> | undefined;

function mark(direction: Direction, shared: Element | null) {
  const root = document.documentElement;
  root.dataset.nav = direction;
  document.querySelectorAll("[data-shared-target]").forEach((node) => node.setAttribute("data-leaving", ""));
  if (shared instanceof HTMLElement) {
    root.dataset.shared = "";
    shared.style.viewTransitionName = "shared";
  }
  clearTimeout(clearMarker);
  clearMarker = setTimeout(() => {
    delete root.dataset.nav;
    delete root.dataset.shared;
    if (shared instanceof HTMLElement && shared.isConnected) shared.style.viewTransitionName = "";
  }, MARKER_LIFETIME_MS);
}

export interface GoOptions {
  replace?: boolean;
  /** The element that should fly into the destination's header. */
  shared?: Element | null;
}

/** `navigate`, with a direction and, where the browser can, a view transition. */
export function useGo() {
  const navigate = useNavigate();
  const location = useLocation();
  const matches = useMatches();
  const tab = [...matches].reverse().map((m) => (m.handle as RouteHandle | undefined)?.tab).find(Boolean);

  return useCallback(
    (to: string, options: GoOptions = {}) => {
      const direction = directionOf({ path: location.pathname, tab }, to);
      const animate =
        direction !== "none" && motionAllowed() && typeof document !== "undefined" && "startViewTransition" in document;
      if (animate) mark(direction, options.shared ?? null);
      return navigate(to, { ...(options.replace ? { replace: true } : {}), viewTransition: animate });
    },
    [navigate, location.pathname, tab],
  );
}

/** The element to fly into the destination's header: the `[data-shared-source]` inside what was
 * clicked, or -- for a button inside a card -- inside the nearest `[data-shared-scope]`. */
export function sharedSource(event: { currentTarget: Element }): Element | null {
  return (
    event.currentTarget.querySelector("[data-shared-source]") ??
    event.currentTarget.closest("[data-shared-scope]")?.querySelector("[data-shared-source]") ??
    null
  );
}
