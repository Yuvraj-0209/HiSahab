/* The application shell: translucent chrome, a role-filtered tab bar, and the scroll edge.
 *
 * §12 of the design vocabulary: chrome is a translucent *floating layer* with content
 * scrolling underneath, not an opaque strip that consumes the top of the screen. The
 * `backdrop-filter` lives in app.css; what lives here is the part CSS cannot express -- the
 * scroll edge effect, which fades a hairline in only once content actually slides under the
 * chrome, instead of a permanent 1px border that is a lie while the page is at the top.
 *
 * §8 and §12 of the plan: the tab bar is filtered by role, and that is **UX, not a control**.
 * The server enforces every permission; hiding a tab an attendant cannot use is politeness,
 * and every screen still handles a real 403. `satisfies` mirrors app/core/roles.py's
 * hierarchy -- attendant < manager < admin, a minimum-role comparison and never an equality
 * check, because a manager can do everything an attendant can.
 */

import { el } from "../dom.js";

const RANK = { attendant: 0, manager: 1, admin: 2 };

/** Mirrors app/core/roles.py::satisfies. A floor, never an exact match. */
export function satisfies(held, minimum) {
  return (RANK[held] ?? -1) >= (RANK[minimum] ?? 99);
}

/* Direct, specific names (§16): "Today", "Entry", "Cash", "Admin" -- what is inside, rather
 * than a vague umbrella like "Home". Specificity is what makes a destination predictable. */
export const TABS = [
  { id: "today", label: "Today", glyph: "◎", route: "#/today", role: "attendant" },
  { id: "entry", label: "Entry", glyph: "✎", route: "#/entry", role: "attendant" },
  { id: "cash", label: "Cash", glyph: "₹", route: "#/cash", role: "manager" },
  { id: "admin", label: "Admin", glyph: "⚙", route: "#/admin", role: "admin" },
];

/**
 * Build the persistent shell.
 *
 * Returns the pieces the router needs: a `screen` element to render into, and `setTitle` /
 * `setTab` so a screen can describe itself without knowing how the chrome is built.
 */
export function buildShell({ role, onNavigate }) {
  const title = el("h1", { className: "t-title truncate grow" });
  const subtitle = el("p", { className: "t-caption truncate" });
  const actions = el("div", { className: "row" });

  const chrome = el("header", { className: "chrome" }, [
    el("div", { className: "grow" }, [title, subtitle]),
    actions,
  ]);

  const screen = el("main", { className: "screen" });

  const visibleTabs = TABS.filter((tab) => satisfies(role, tab.role));

  const tabNodes = new Map();
  const tabbar = el(
    "nav",
    { className: "tabbar", attrs: { role: "tablist", "aria-label": "Sections" } },
    visibleTabs.map((tab) => {
      const node = el(
        "button",
        {
          className: "tab",
          attrs: { role: "tab", "aria-selected": "false", type: "button" },
          on: { click: () => onNavigate(tab.route) },
        },
        [
          el("span", { className: "tab-glyph", text: tab.glyph, attrs: { "aria-hidden": "true" } }),
          el("span", { text: tab.label }),
        ],
      );
      tabNodes.set(tab.id, node);
      return node;
    }),
  );

  /* The scroll edge. Reading scrollTop on every scroll event is cheap, but writing to the DOM
   * from it is not -- so the attribute is only touched when the boolean actually flips, which
   * turns a per-frame style write into one write per crossing. */
  let scrolled = false;
  function handleScroll() {
    const next = window.scrollY > 2;
    if (next === scrolled) return;
    scrolled = next;
    chrome.setAttribute("data-scrolled", String(next));
  }
  window.addEventListener("scroll", handleScroll, { passive: true });

  return {
    nodes: [chrome, screen, tabbar],
    screen,

    setTitle(text, sub = "") {
      title.textContent = text;
      subtitle.textContent = sub;
      subtitle.classList.toggle("hidden", !sub);
      // Wayfinding (§16): the document title answers "where am I?" in the tab strip and in
      // browser history, not only on screen.
      document.title = sub ? `${text} · HiSahab` : `${text} · HiSahab`;
    },

    /** Replace the chrome's trailing actions. Screens own their own verbs. */
    setActions(...nodes) {
      actions.replaceChildren(...nodes.filter(Boolean));
    },

    setTab(id) {
      for (const [tabId, node] of tabNodes) {
        node.setAttribute("aria-selected", String(tabId === id));
      }
    },

    /** Tabs this role can actually see, so the router can refuse an unknown one honestly. */
    visibleTabs,
  };
}
