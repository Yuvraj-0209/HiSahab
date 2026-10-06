/* The signed-in shell: translucent chrome, the screen, and a role-filtered tab bar.
 *
 * The chrome is a floating layer with content scrolling underneath, not an opaque strip. Its
 * hairline appears only once something has actually slid under it -- detected by an
 * IntersectionObserver on a sentinel at the top of the page, never a scroll listener (a scroll
 * listener runs on every frame; the observer fires once per crossing).
 *
 * The tab bar is filtered by role, and that is UX, not a control (§8): the server enforces
 * every permission and every screen still handles a real 403. Which tab is lit comes from the
 * matched route's `handle`, so a screen never has to say where it lives.
 *
 * ## No GSAP in the shell (Phase 24 D1)
 *
 * The shell is in the first paint, and GSAP's core is 28 KB gzipped. Its two animations here are
 * a screen entrance and a sliding tab indicator, and both are a single transform -- which CSS
 * does on the compositor with no library at all. So they are CSS (`.screen-enter`,
 * `.tab-indicator` in styles.css), and GSAP arrives with the lazily loaded screens that
 * choreograph something.
 */

import { type CSSProperties, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Outlet, useLocation, useMatches } from "react-router";
import { SignOutIcon } from "@phosphor-icons/react";
import { satisfies } from "../lib/roles";
import { ChromeContext, type ChromeSlots } from "./chrome";
import { useGo } from "./navigation";
import { type RouteHandle, type Tab, TABS } from "./tabs";
import { useSession } from "./session";

export function Shell() {
  const { me, signOut } = useSession();
  const location = useLocation();
  const matches = useMatches();
  const navigate = useGo();

  const visible = TABS.filter((tab) => satisfies(me.role, tab.role));
  const activeId = [...matches].reverse().map((m) => (m.handle as RouteHandle | undefined)?.tab).find(Boolean);
  const activeIndex = visible.findIndex((tab) => tab.id === activeId);
  // Wide screens use a wide monitor; everything else keeps a readable measure (Phase 25 D2).
  const wide = matches.some((m) => (m.handle as RouteHandle | undefined)?.wide);
  const measure = wide ? "max-w-[100rem] lg:px-10" : "max-w-[76rem]";

  // Slots for <ScreenTitle> / <ScreenActions>, captured by callback refs.
  const [slots, setSlots] = useState<ChromeSlots>({ title: null, actions: null });
  const titleRef = useCallback((node: HTMLDivElement | null) => setSlots((s) => ({ ...s, title: node })), []);
  const actionsRef = useCallback((node: HTMLDivElement | null) => setSlots((s) => ({ ...s, actions: node })), []);

  // The scroll edge.
  const sentinel = useRef<HTMLDivElement>(null);
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const node = sentinel.current;
    if (!node) return;
    const observer = new IntersectionObserver(([entry]) => setScrolled(!(entry?.isIntersecting ?? true)));
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  // Every screen starts at the top, as the Phase 12 router did. A layout effect, so it happens
  // inside the view transition's update (navigation.ts) -- after the effect would be after the
  // new screen was captured, and the page would jump once the transition ended.
  useLayoutEffect(() => {
    window.scrollTo(0, 0);
  }, [location.pathname]);

  return (
    <ChromeContext.Provider value={slots}>
      <div ref={sentinel} className="absolute top-0 h-px w-px" aria-hidden="true" />
      <header className="chrome sticky top-0 z-30 pt-[env(safe-area-inset-top)]" data-scrolled={scrolled ? "true" : "false"}>
        <div className={`mx-auto flex h-14 items-center gap-3 px-4 ${measure}`}>
          <div ref={titleRef} className="min-w-0 grow" />
          <div ref={actionsRef} className="shrink-0" />
          <button
            type="button"
            onClick={signOut}
            className="pressable flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1.5 text-footnote font-medium text-ink-muted hover:bg-surface-sunken"
          >
            <SignOutIcon size={18} aria-hidden />
            <span className="hidden sm:inline">Log out</span>
            <span className="sr-only-text sm:hidden">Log out</span>
          </button>
        </div>
      </header>

      <main className={`mx-auto px-4 pt-4 pb-[calc(env(safe-area-inset-bottom)+6.5rem)] ${measure}`}>
        {/* Keyed by path, so the CSS entrance replays on every route change. */}
        <div key={location.pathname} className="screen-enter">
          <Outlet />
        </div>
      </main>

      <TabBar tabs={visible} activeIndex={activeIndex} onSelect={(route) => navigate(route)} />
    </ChromeContext.Provider>
  );
}

function TabBar({
  tabs,
  activeIndex,
  onSelect,
}: {
  tabs: readonly Tab[];
  activeIndex: number;
  onSelect: (route: string) => void;
}) {
  /* The indicator slides between tabs: the tabs are equal columns and the indicator is one
   * column wide, so moving it is `translateX(100% * index)` -- a pure transform, nothing is laid
   * out again. The first placement is immediate; only a change of tab is animated, because only
   * a change says anything. `placed` turns the transition on after the first frame. */
  const [placed, setPlaced] = useState(false);
  useEffect(() => {
    const frame = requestAnimationFrame(() => setPlaced(true));
    return () => cancelAnimationFrame(frame);
  }, []);

  // The indicator is one column wide and slides by whole columns; the pill inside it keeps a
  // fixed width, so on a wide screen it stays a pill rather than becoming a 300 px slab.
  const indicator: CSSProperties = {
    width: `calc((100% - var(--tabbar-gutter) * 2) / ${tabs.length})`,
    transform: `translateX(${Math.max(activeIndex, 0) * 100}%)`,
    opacity: activeIndex < 0 ? 0 : 1,
  };

  return (
    <nav aria-label="Sections" className="tabbar fixed inset-x-0 bottom-0 z-30 pb-[env(safe-area-inset-bottom)]">
      {/* Six equal columns across the whole window, as the Phase 12 bar had them: on a monitor the
       * tabs spread out instead of huddling in a 36rem strip in the middle (Phase 25 D2). */}
      <div className="tabbar-grid relative mx-auto grid max-w-[100rem]" style={{ gridTemplateColumns: `repeat(${tabs.length}, minmax(0, 1fr))` }}>
        <span
          aria-hidden="true"
          data-placed={placed ? "true" : "false"}
          className="tab-indicator pointer-events-none absolute top-[var(--tabbar-pad)] bottom-[var(--tabbar-pad)] left-[var(--tabbar-gutter)] flex justify-center"
          style={indicator}
        >
          <span className="tab-pill h-full w-full max-w-[7.5rem] rounded-full" />
        </span>
        {tabs.map((tab, index) => {
          const active = index === activeIndex;
          return (
            <button
              key={tab.id}
              type="button"
              aria-current={active ? "page" : undefined}
              onClick={() => onSelect(tab.route)}
              className={`pressable relative flex h-12 flex-col items-center justify-center gap-0.5 rounded-full text-micro font-medium transition-colors lg:h-14 lg:text-caption ${active ? "text-accent" : "text-ink-muted"}`}
            >
              <tab.Icon size={22} weight={active ? "fill" : "regular"} aria-hidden />
              {tab.label}
            </button>
          );
        })}
      </div>
    </nav>
  );
}
