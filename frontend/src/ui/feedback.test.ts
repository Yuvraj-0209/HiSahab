/* What a failed write offers next (Phase 30). A refusal that names the way out should carry a
 * button to it: the lock's 409 says flagged expenses are unreviewed, so the toast takes you to
 * them. The button is navigation only -- the server still decides whether anything may proceed. */

import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, NetworkError } from "../api/client";
import { reportFailure } from "./feedback";
import { notify } from "./toast";

afterEach(() => vi.restoreAllMocks());

const refused = () =>
  new ApiError({
    status: 409,
    code: "UNREVIEWED_EXPENSES_EXIST",
    detail: "This shift has 1 flagged expense(s) still awaiting review: MAINTENANCE 1500.00.",
    requestId: "req-1",
  });

const spyOnError = () => vi.spyOn(notify, "error").mockReturnValue({ dismiss: () => {} });

describe("reportFailure with a way out", () => {
  it("offers the recovery registered for that error code", () => {
    const error = spyOnError();
    const go = vi.fn();
    reportFailure(refused(), undefined, undefined, {
      UNREVIEWED_EXPENSES_EXIST: { label: "Review expenses", onClick: go },
    });

    expect(error).toHaveBeenCalledTimes(1);
    const [message, options] = error.mock.calls[0]!;
    expect(message).toBe("This shift has flagged expenses nobody has reviewed yet.");
    expect(options?.action?.label).toBe("Review expenses");
    expect(options?.requestId).toBe("req-1");
    options?.action?.onClick();
    expect(go).toHaveBeenCalledTimes(1);
  });

  it("shows the server's own detail beside it, which names the rows", () => {
    const error = spyOnError();
    reportFailure(refused(), undefined, undefined, {
      UNREVIEWED_EXPENSES_EXIST: { label: "Review expenses", onClick: () => {} },
    });
    expect(error.mock.calls[0]![1]?.detail).toContain("MAINTENANCE 1500.00");
  });

  it("offers nothing for a different error", () => {
    const error = spyOnError();
    reportFailure(new ApiError({ status: 409, code: "SHIFT_NOT_CLOSED", detail: "no", requestId: "req-2" }), undefined, undefined, {
      UNREVIEWED_EXPENSES_EXIST: { label: "Review expenses", onClick: () => {} },
    });
    expect(error.mock.calls[0]![1]?.action).toBeUndefined();
  });

  it("still offers Retry for a network failure", () => {
    const error = spyOnError();
    reportFailure(new NetworkError(new Error("offline")), undefined, vi.fn());
    expect(error.mock.calls[0]![1]?.action?.label).toBe("Retry");
  });
});
