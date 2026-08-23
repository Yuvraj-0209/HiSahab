/* Looking at a receipt that has already been uploaded (CLAUDE.md §7.3).
 *
 * Found by `test_every_router_is_reachable_from_a_screen`, which noticed that nothing in the
 * application called `GET /attachments/{id}/url`. Receipts could be attached and never seen
 * again -- an expense showed "receipt attached" and offered no way to check it, which makes
 * the receipt control theatre from the reviewer's side.
 *
 * ## Why the URL is fetched on demand rather than rendered into the list
 *
 * §7.3: the bucket is private and the endpoint returns a **short-lived signed URL**, five
 * minutes by default. "Expired links are useless if leaked." Building them for every row of a
 * list would mint dozens of live credentials to look at one, and most would expire unused --
 * so the URL is requested at the moment somebody actually asks to see the image.
 *
 * ## The permission rule is the server's
 *
 * §7.3 again, and it is not a rule this module reimplements: a manager or admin may read any
 * attachment at their outlet; an attendant only one they uploaded or one linked to their own
 * shift, otherwise 403 NOT_YOUR_ATTACHMENT. An id from another outlet returns **404, not
 * 403**, deliberately -- existence is not leaked across tenants. Both surface as the server
 * wrote them.
 */

import { el } from "../dom.js";
import { api, explain } from "../api.js";
import { openSheet } from "./sheet.js";
import { notify } from "./toast.js";

/**
 * A button that opens the receipt behind `attachmentId`.
 *
 * Returns null when there is no attachment, so callers can drop it straight into a children
 * array without a conditional of their own.
 */
export function receiptButton(attachmentId, { label = "View receipt" } = {}) {
  if (!attachmentId) return null;

  const button = el("button", {
    className: "btn",
    text: label,
    attrs: { type: "button" },
  });

  button.addEventListener("click", async () => {
    button.disabled = true;
    const original = button.textContent;
    button.textContent = "Opening…";
    try {
      const { url, expires_in_seconds } = await api.get(`/attachments/${attachmentId}/url`);
      showReceipt(url, expires_in_seconds);
    } catch (error) {
      notify.error(explain(error), { requestId: error.requestId });
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  });

  return button;
}

function showReceipt(url, expiresInSeconds) {
  // An <img> rather than a new tab: index.html's CSP allows img-src from Supabase, and a
  // popup would be blocked on a phone about as often as not. The sheet also means the
  // receipt is dismissed with the same gesture as everything else in the app.
  const image = el("img", {
    attrs: { src: url, alt: "Receipt" },
    style: {
      width: "100%",
      height: "auto",
      borderRadius: "var(--radius-md)",
      background: "var(--surface-raised)",
    },
  });

  const failed = el("p", { className: "t-caption text-short hidden" });
  image.addEventListener("error", () => {
    failed.textContent =
      "The image could not be loaded. The signed link may have expired — close this and try again.";
    failed.classList.remove("hidden");
  });

  openSheet({
    title: "Receipt",
    body: el("div", { className: "stack" }, [
      image,
      failed,
      el("p", {
        className: "t-caption",
        text: `This link is private and expires in about ${Math.round(
          (expiresInSeconds ?? 300) / 60,
        )} minutes.`,
      }),
    ]),
  });
}
