/* The Summary's Phase 26 chart pieces. What they must not break is §14's: every length, offset
 * and slice is the server's string, assigned as sent -- a chart that recomputed one would be
 * money arithmetic in JavaScript. The rest is the interaction contract: one category chosen at a
 * time, reachable from the keyboard, and a pointed-at fuel lifting its slice without resizing it. */

import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Bridge, CategoryBars, Donut } from "./chart";

// jsdom has no matchMedia, and chart.tsx imports ScrollTrigger, which registers -- and asks
// for matchMedia -- the moment it is imported. So the stub must exist before the imports
// above run; `vi.hoisted` is what puts it there. Reduced motion, so no tween touches the
// geometry under test.
vi.hoisted(() => {
  window.matchMedia = ((query: string) => ({
    matches: query.includes("reduce"),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia;
});

afterEach(() => {
  vi.restoreAllMocks();
});

const rows = [
  { key: "SALARY", label: "Salaries", value: "₹60,000.00", share_pct: "67.86%", bar_pct: "100.00%" },
  { key: "ELECTRICITY", label: "Electricity", value: "₹21,870.00", share_pct: "24.73%", bar_pct: "36.45%" },
  { key: "MAINTENANCE", label: "Maintenance", value: "₹6,550.00", share_pct: null, bar_pct: "10.92%" },
];

describe("CategoryBars", () => {
  it("is a radio group with exactly the chosen category checked", () => {
    render(<CategoryBars label="Expense categories" rows={rows} selected="ELECTRICITY" onChoose={() => {}} />);
    const group = screen.getByRole("radiogroup", { name: "Expense categories" });
    expect(group).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Electricity/ })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: /Salaries/ })).toHaveAttribute("aria-checked", "false");
  });

  it("assigns each bar's length from the server's string, untouched", () => {
    const { container } = render(<CategoryBars label="c" rows={rows} selected="SALARY" onChoose={() => {}} />);
    const bars = container.querySelectorAll<HTMLElement>("[data-catbar]");
    expect(Array.from(bars, (bar) => bar.style.width)).toEqual(["100%", "36.45%", "10.92%"]);
  });

  it("says an unknowable share in words rather than drawing it as zero (§6.8)", () => {
    render(<CategoryBars label="c" rows={rows} selected="SALARY" onChoose={() => {}} />);
    expect(screen.getByText("unknown")).toBeInTheDocument();
  });

  it("moves the choice with the arrow keys, wrapping at the ends", () => {
    const choose = vi.fn();
    render(<CategoryBars label="c" rows={rows} selected="SALARY" onChoose={choose} />);
    fireEvent.keyDown(screen.getByRole("radio", { name: /Salaries/ }), { key: "ArrowDown" });
    expect(choose).toHaveBeenLastCalledWith("ELECTRICITY");
    fireEvent.keyDown(screen.getByRole("radio", { name: /Salaries/ }), { key: "ArrowUp" });
    expect(choose).toHaveBeenLastCalledWith("MAINTENANCE");
  });

  it("reports a pointer arriving separately from a pick, so the caller can fetch ahead", () => {
    const point = vi.fn();
    const choose = vi.fn();
    render(<CategoryBars label="c" rows={rows} selected="SALARY" onChoose={choose} onPoint={point} />);
    fireEvent.pointerEnter(screen.getByRole("radio", { name: /Maintenance/ }));
    expect(point).toHaveBeenCalledWith("MAINTENANCE");
    expect(choose).not.toHaveBeenCalled();
  });
});

describe("Bridge", () => {
  const steps = [
    { key: "start", label: "Owed at start", value: "₹4,12,850.00", offset_pct: "0.00%", width_pct: "67.32%" },
    { key: "given", label: "+ Given", value: "₹2,00,390.00", offset_pct: "67.32%", width_pct: "32.68%" },
    { key: "collected", label: "− Collected", value: "₹1,48,720.50", offset_pct: "75.75%", width_pct: "24.25%" },
    { key: "end", label: "= Owed at end", value: "₹4,64,519.50", offset_pct: "0.00%", width_pct: "75.75%", emphasis: true },
  ];

  it("places every bar where the server says, without subtracting anything", () => {
    const { container } = render(<Bridge rows={steps} />);
    const collected = container.querySelector<HTMLElement>('[data-bridge="collected"]');
    expect(collected?.style.left).toBe("75.75%");
    expect(collected?.style.width).toBe("24.25%");
    expect(container.querySelectorAll("[data-bridge]")).toHaveLength(4);
  });

  it("prints every figure, and draws no track at all when the server withholds the geometry", () => {
    const bare = steps.map((step) => ({ ...step, offset_pct: null, width_pct: null }));
    const { container } = render(<Bridge rows={bare} />);
    expect(container.querySelectorAll("[data-bridge]")).toHaveLength(0);
    expect(screen.getByText("₹4,64,519.50")).toBeInTheDocument();
    expect(screen.getByText("= Owed at end")).toBeInTheDocument();
  });
});

describe("Donut", () => {
  const slices = [
    { key: "petrol", share_pct: "61.20%", colour: 0 },
    { key: "diesel", share_pct: "33.93%", colour: 1 },
    { key: "cbg", share_pct: "4.87%", colour: 2 },
  ];

  it("recedes every slice but the pointed one, and resizes none", () => {
    const { container } = render(<Donut slices={slices} active="diesel" size="lg" />);
    const groups = Array.from(container.querySelectorAll("g"));
    expect(groups.map((group) => group.getAttribute("class")?.includes("opacity-25"))).toEqual([true, false, true]);
    const arcs = Array.from(container.querySelectorAll("[data-slice]"), (arc) => arc.getAttribute("stroke-dasharray"));
    const unpointed = render(<Donut slices={slices} active={null} />).container;
    expect(Array.from(unpointed.querySelectorAll("[data-slice]"), (arc) => arc.getAttribute("stroke-dasharray"))).toEqual(arcs);
  });
});
