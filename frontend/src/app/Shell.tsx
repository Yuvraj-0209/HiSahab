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
 */

import { type ComponentType, useCallback, useEffect, useRef, useState } from "react";
import { Outlet, useLocation, useMatches, useNavigate } from "react-router";
import { useGSAP } from "@gsap/react";
import gsap from "gsap";
import {
  ChartPieSliceIcon,
  CurrencyInrIcon,
  GearSixIcon,
  NotebookIcon,
  NotePencilIcon,
  type IconProps,
  SignOutIcon,
  SunHorizonIcon,
} from "@phosphor-icons/react";
import { satisfies, type Role } from "../lib/roles";
import { useScreenEntrance } from "../ui/motion";
import { ChromeContext, type ChromeSlots } from "./chrome";
import { useSession } from "./session";

gsap.registerPlugin(useGSAP);

export type TabId = "today" | "entry" | "cash" | "credit" | "summary" | "admin";

export interface RouteHandle {
  tab?: TabId;
}

interface Tab {
  id: TabId;
  label: string;
  route: string;
  role: Role;
  Icon: ComponentType<IconProps>;
}

/* Direct, specific names: what is inside, rather than an umbrella like "Home". The order runs
 * entry-first -- what you do today, then what you owe, then how it went. */
export const TABS: readonly Tab[] = [
  { id: "today", label: "Today", route: "/today", role: "attendant", Icon: SunHorizonIcon },
  { id: "entry", label: "Entry", route: "/entry", role: "attendant", Icon: NotePencilIcon },
  { id: "cash", label: "Cash", route: "/cash", role: "manager", Icon: CurrencyInrIcon },
  // Manager floor (§8): a customer's balance has never been an attendant's business.
  { id: "credit", label: "Credit", route: "/credit", role: "manager", Icon: NotebookIcon },
  { id: "summary", label: "Summary", route: "/summary", role: "manager", Icon: ChartPieSliceIcon },
  { id: "admin", label: "Admin", route: "/admin", role: "admin", Icon: GearSixIcon },
];

export function Shell() {
  const { me, signOut } = useSession();
  const location = useLocation();
  const matches = useMatches();
  const navigate = useNavigate();

  const visible = TABS.filter((tab) => satisfies(me.role, tab.role));
  const activeId = [...matches].reverse().map((m) => (m.handle as RouteHandle | undefined)?.tab).find(Boolean);
  const activeIndex = visible.findIndex((tab) => tab.id === activeId);

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

  // Every screen starts at the top, as the Phase 12 router did.
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [location.pathname]);

  const screen = useRef<HTMLDivElement>(null);
  useScreenEntrance(screen, location.pathname);

  return (
    <ChromeContext.Provider value={slots}>
      <div ref={sentinel} className="absolute top-0 h-px w-px" aria-hidden="true" />
      <header className="chrome sticky top-0 z-30 pt-[env(safe-area-inset-top)]" data-scrolled={scrolled ? "true" : "false"}>
        <div className="mx-auto flex h-14 max-w-[76rem] items-center gap-3 px-4">
          <div ref={titleRef} className="min-w-0 grow" />
          <div ref={actionsRef} className="shrink-0" />
          <button
            type="button"
            onClick={signOut}
            className="pressable flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1.5 text-[0.8125rem] font-medium text-ink-muted hover:bg-surface-sunken"
          >
            <SignOutIcon size={18} aria-hidden />
            <span className="hidden sm:inline">Log out</span>
            <span className="sr-only-text sm:hidden">Log out</span>
          </button>
        </div>
      </header>

      <main className="mx-auto max-w-[76rem] px-4 pt-4 pb-[calc(env(safe-area-inset-bottom)+6.5rem)]">
        <div ref={screen} key={location.pathname}>
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
  const scope = useRef<HTMLDivElement>(null);
  const indicator = useRef<HTMLSpanElement>(null);
  const placed = useRef(false);

  /* The indicator slides between tabs: the tabs are equal columns, so moving it is a pure
   * `xPercent` transform -- nothing is laid out again. The first placement is immediate; only a
   * change of tab is animated, because only a change says anything. */
  useGSAP(
    () => {
      const node = indicator.current;
      if (!node) return;
      if (activeIndex < 0) {
        gsap.set(node, { opacity: 0 });
        return;
      }
      const target = { xPercent: activeIndex * 100, opacity: 1 };
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (!placed.current || reduced) {
        gsap.set(node, target);
        placed.current = true;
      } else {
        gsap.to(node, { ...target, duration: 0.38, ease: "power3.out", overwrite: true });
      }
    },
    { dependencies: [activeIndex], scope },
  );

  return (
    <nav aria-label="Sections" className="tabbar fixed inset-x-0 bottom-0 z-30 pb-[env(safe-area-inset-bottom)]">
      <div
        ref={scope}
        className="relative mx-auto grid max-w-xl px-1.5 py-1.5"
        style={{ gridTemplateColumns: `repeat(${tabs.length}, minmax(0, 1fr))` }}
      >
        <span
          ref={indicator}
          aria-hidden="true"
          className="pointer-events-none absolute top-1.5 bottom-1.5 left-1.5 rounded-full bg-accent-tint opacity-0"
          style={{ width: `calc((100% - 0.75rem) / ${tabs.length})` }}
        />
        {tabs.map((tab, index) => {
          const active = index === activeIndex;
          return (
            <button
              key={tab.id}
              type="button"
              aria-current={active ? "page" : undefined}
              onClick={() => onSelect(tab.route)}
              className={`pressable relative flex h-12 flex-col items-center justify-center gap-0.5 rounded-full text-[0.6875rem] font-medium transition-colors ${active ? "text-accent" : "text-ink-muted"}`}
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
