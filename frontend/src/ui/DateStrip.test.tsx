/* The date strip (Phase 30). The contract: ten days, the chosen one the centre, nothing after
 * today choosable, a count says where the work is, and it moves with the keyboard and a tap. */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DateStrip } from "./DateStrip";

const base = { value: "2026-10-13", max: "2026-10-13", onChange: () => {} };

describe("DateStrip", () => {
  it("shows ten days with exactly the chosen one checked", () => {
    render(<DateStrip {...base} />);
    const days = screen.getAllByRole("radio");
    expect(days).toHaveLength(10);
    const checked = days.filter((day) => day.getAttribute("aria-checked") === "true");
    expect(checked).toHaveLength(1);
    expect(checked[0]).toHaveAccessibleName("Tue 13 Oct");
  });

  it("keeps the chosen day five from the start, so four days follow it", () => {
    render(<DateStrip {...base} value="2026-10-10" max="2026-10-31" />);
    const names = screen.getAllByRole("radio").map((day) => day.getAttribute("aria-label"));
    expect(names[0]).toBe("Mon 5 Oct");
    expect(names[5]).toBe("Sat 10 Oct");
    expect(names[9]).toBe("Wed 14 Oct");
  });

  it("shows days after today but will not let one be chosen", () => {
    const change = vi.fn();
    render(<DateStrip {...base} onChange={change} />);
    const tomorrow = screen.getByRole("radio", { name: "Wed 14 Oct" });
    expect(tomorrow).toBeDisabled();
    fireEvent.click(tomorrow);
    expect(change).not.toHaveBeenCalled();
  });

  it("chooses a day on tap", () => {
    const change = vi.fn();
    render(<DateStrip {...base} onChange={change} />);
    fireEvent.click(screen.getByRole("radio", { name: "Sun 11 Oct" }));
    expect(change).toHaveBeenCalledWith("2026-10-11");
  });

  it("steps a day with the arrow keys, and never past today", () => {
    const change = vi.fn();
    render(<DateStrip {...base} onChange={change} />);
    const chosen = screen.getByRole("radio", { name: "Tue 13 Oct" });
    fireEvent.keyDown(chosen, { key: "ArrowLeft" });
    expect(change).toHaveBeenLastCalledWith("2026-10-12");
    fireEvent.keyDown(chosen, { key: "ArrowRight" });
    expect(change).toHaveBeenCalledTimes(1);
  });

  it("says how many are waiting in the accessible name, and a quiet day says nothing", () => {
    render(<DateStrip {...base} badges={{ "2026-10-11": 3 }} />);
    expect(screen.getByRole("radio", { name: "Sun 11 Oct, 3 flagged" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Mon 12 Oct" })).toBeInTheDocument();
  });

  it("crosses a month end without a gap", () => {
    render(<DateStrip {...base} value="2026-11-02" max="2026-11-30" />);
    const names = screen.getAllByRole("radio").map((day) => day.getAttribute("aria-label"));
    expect(names).toContain("Sat 31 Oct");
    expect(names).toContain("Sun 1 Nov");
  });
});
