/* The drawn tick (Phase 24 D5). The motion is incidental; the rule it must not break is §14's:
 * a box that animated on its first render would look pre-ticked, and the opening confirmation
 * (§4.7) is the one control that must never look as if somebody already said yes. */

import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { gsap } from "../motion/gsap";
import { Tick } from "./Tick";

beforeEach(() => {
  // jsdom has no matchMedia; motion is welcome in these tests, so the draw would run if asked.
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query.includes("no-preference"),
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
  }));
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Tick", () => {
  it("is a real checkbox, unticked when the form says so", () => {
    render(<Tick aria-label="The meter reads exactly this" checked={false} onChange={() => {}} />);
    expect(screen.getByRole("checkbox", { name: "The meter reads exactly this" })).not.toBeChecked();
  });

  it("never draws on its first render, even when it opens ticked", () => {
    const draw = vi.spyOn(gsap, "fromTo");
    render(<Tick aria-label="box" checked={true} onChange={() => {}} />);
    expect(draw).not.toHaveBeenCalled();
  });

  it("draws once when a person ticks it, and not when they untick it", () => {
    const draw = vi.spyOn(gsap, "fromTo");
    const { rerender } = render(<Tick aria-label="box" checked={false} onChange={() => {}} />);
    rerender(<Tick aria-label="box" checked={true} onChange={() => {}} />);
    expect(draw).toHaveBeenCalledTimes(1);
    rerender(<Tick aria-label="box" checked={false} onChange={() => {}} />);
    expect(draw).toHaveBeenCalledTimes(1);
  });
});
