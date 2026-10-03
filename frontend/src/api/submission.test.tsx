/* The Idempotency-Key belongs to the submission, not the fetch (§6.10, §14).
 *
 * The scenario §6.10 was written for: a phone on a rural connection sends a ₹5,000 expense, the
 * response is lost, the salesman presses "Try again". If the retry carries a new key the server
 * sees a second expense. These tests drive exactly that through a mocked fetch.
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NetworkError, request } from "./client";
import { Submission, useSubmission } from "./submission";

function keysSent(fetchMock: ReturnType<typeof vi.fn>): (string | undefined)[] {
  return fetchMock.mock.calls.map(([, init]) => (init as RequestInit).headers as Record<string, string>).map(
    (headers) => headers["Idempotency-Key"],
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("Submission", () => {
  it("reuses one key across a retry after a network failure", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: "e1" }), { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);

    const submission = new Submission("POST", "/shifts/s1/expenses");
    await expect(submission.run({ amount: "5000.00" })).rejects.toBeInstanceOf(NetworkError);
    await expect(submission.run({ amount: "5000.00" })).resolves.toEqual({ id: "e1" });

    const [first, second] = keysSent(fetchMock);
    expect(first).toBeTruthy();
    expect(second).toBe(first);
  });

  it("is spent after success: a second record needs a fresh form", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 201 })));
    const submission = new Submission("POST", "/shifts/s1/expenses");
    await submission.run({ amount: "1.00" });
    await expect(submission.run({ amount: "1.00" })).rejects.toThrow(/already succeeded/);
  });

  it("two submissions never share a key", () => {
    expect(new Submission("POST", "/x").key).not.toBe(new Submission("POST", "/x").key);
  });
});

describe("request", () => {
  it("refuses a money POST with no key rather than minting one silently", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(request("POST", "/shifts/s1/collections", { body: {} })).rejects.toThrow(/Idempotency-Key/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("sends the key for the bank review's confirm-repayments (the Phase 20 bug)", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await request("POST", "/bank-transactions/confirm-repayments", { body: { items: [] }, idempotencyKey: "k-1" });
    expect(keysSent(fetchMock)).toEqual(["k-1"]);
  });
});

describe("useSubmission", () => {
  it("keeps one key for the life of the form, across re-renders", () => {
    const { result, rerender } = renderHook(() => useSubmission("POST", "/shifts/s1/expenses"));
    const first = result.current.key;
    act(() => rerender());
    act(() => rerender());
    expect(result.current.key).toBe(first);
  });

  it("a fresh form (a new mount) gets a fresh key", () => {
    const one = renderHook(() => useSubmission("POST", "/x")).result.current.key;
    const two = renderHook(() => useSubmission("POST", "/x")).result.current.key;
    expect(one).not.toBe(two);
  });
});
