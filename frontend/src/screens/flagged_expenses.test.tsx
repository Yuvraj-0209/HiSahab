/* The flagged queue, one day at a time (Phase 30). What it must get right: which day it opens
 * on, that it asks the server for that day only, and that an empty day says so in words rather
 * than reading as a day that was never looked at. */

import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FlaggedExpensesScreen } from "./flagged_expenses";

const calls: Array<{ path: string; query: Record<string, unknown> | undefined }> = [];
let counts = { items: [] as unknown[], oldest_business_date: null as string | null };
let rows: Array<Record<string, string>> = [];

vi.mock("../api/queries", () => ({
  useApiQuery: (path: string, query?: Record<string, unknown>, options?: { enabled?: boolean }) => {
    calls.push({ path, query });
    if (options?.enabled === false) return { isPending: true, isError: false, data: undefined };
    const data =
      path === "/expenses/flagged/by-date"
        ? counts
        : { items: rows.filter((row) => row.business_date === query?.business_date), next_cursor: null };
    return { isPending: false, isError: false, isSuccess: true, isPlaceholderData: false, data, refetch: vi.fn() };
  },
  useRefreshApi: () => vi.fn(),
}));
vi.mock("../lib/time", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/time")>()),
  todayAtOutlet: () => "2026-10-13",
}));

const expense = (id: string, business_date: string, description: string, amount: string) => ({
  id,
  shift_id: `shift-${business_date}`,
  business_date,
  category_id: "cat",
  category_code: "MAINTENANCE",
  amount,
  description,
});

function renderAt(url: string) {
  const router = createMemoryRouter([{ path: "/expenses/flagged", element: <FlaggedExpensesScreen /> }], {
    initialEntries: [url],
  });
  return render(<RouterProvider router={router} />);
}

beforeEach(() => {
  calls.length = 0;
  counts = { items: [], oldest_business_date: null };
  rows = [];
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: false,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
  }));
});
afterEach(() => vi.unstubAllGlobals());

describe("FlaggedExpensesScreen", () => {
  it("opens on the date it was sent, and lists only that day's rows", () => {
    rows = [
      expense("a", "2026-10-11", "Pump belt", "1500.00"),
      expense("b", "2026-10-12", "Other day", "1600.00"),
    ];
    counts = { items: [{ business_date: "2026-10-11", count: 1, total_amount: "1500.00" }], oldest_business_date: "2026-10-11" };
    renderAt("/expenses/flagged?date=2026-10-11");

    expect(screen.getByText("Pump belt")).toBeInTheDocument();
    expect(screen.queryByText("Other day")).not.toBeInTheDocument();
    const listCall = calls.find((call) => call.path === "/expenses/flagged");
    expect(listCall?.query).toMatchObject({ business_date: "2026-10-11" });
  });

  it("opens on the oldest day still waiting when no date was given", () => {
    rows = [expense("a", "2026-10-08", "Old bill", "1200.00")];
    counts = { items: [], oldest_business_date: "2026-10-08" };
    renderAt("/expenses/flagged");

    expect(screen.getByText("Old bill")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Thu 8 Oct" })).toHaveAttribute("aria-checked", "true");
  });

  it("opens on today when nothing is waiting anywhere", () => {
    renderAt("/expenses/flagged");
    expect(screen.getByRole("radio", { name: "Tue 13 Oct" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText(/Nothing to review on/)).toBeInTheDocument();
  });

  it("ignores a date that is not a date, or is in the future", () => {
    renderAt("/expenses/flagged?date=2099-01-01");
    expect(screen.getByRole("radio", { name: "Tue 13 Oct" })).toHaveAttribute("aria-checked", "true");
  });

  it("offers the way back to the shift once its day is clear", () => {
    renderAt("/expenses/flagged?date=2026-10-13&shift=abc");
    expect(screen.getByRole("button", { name: "Back to the shift" })).toBeInTheDocument();
  });

  it("does not offer the way back when it was not reached from a lock", () => {
    renderAt("/expenses/flagged?date=2026-10-13");
    expect(screen.queryByRole("button", { name: "Back to the shift" })).not.toBeInTheDocument();
  });

  it("gives every waiting row a Review button and shows its amount as sent", () => {
    rows = [expense("a", "2026-10-13", "Pump belt", "1500.00")];
    counts = { items: [{ business_date: "2026-10-13", count: 1, total_amount: "1500.00" }], oldest_business_date: "2026-10-13" };
    renderAt("/expenses/flagged?date=2026-10-13");
    expect(screen.getByRole("button", { name: "Review" })).toBeInTheDocument();
  });
});
