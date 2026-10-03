/* Looking at a receipt that has already been uploaded (CLAUDE.md §7.3).
 *
 * The bucket is private and `GET /attachments/{id}/url` returns a short-lived signed URL (five
 * minutes). "Expired links are useless if leaked." So the URL is minted at the moment somebody
 * asks to see the image, never rendered into a list -- dozens of live credentials to look at one
 * would be the opposite of the point.
 *
 * The permission rule is the server's and is not reimplemented: an attendant who may not see a
 * receipt gets 403 NOT_YOUR_ATTACHMENT, and an id from another outlet 404s, shown as written.
 * The image renders in a sheet rather than a new tab: the CSP's `img-src` allows Supabase, and a
 * popup is blocked on a phone about as often as not.
 */

import { useState } from "react";
import { ImageIcon } from "@phosphor-icons/react";
import { api } from "../api/client";
import { reportFailure } from "./feedback";
import { Button } from "./primitives";
import { Sheet } from "./Sheet";

export function ReceiptButton({ attachmentId, label = "View receipt" }: { attachmentId: string | null | undefined; label?: string }) {
  const [busy, setBusy] = useState(false);
  const [shown, setShown] = useState<{ url: string; expires: number } | null>(null);
  const [failed, setFailed] = useState(false);

  if (!attachmentId) return null;

  async function open() {
    setBusy(true);
    try {
      const { url, expires_in_seconds } = await api.get<{ url: string; expires_in_seconds?: number }>(
        `/attachments/${attachmentId}/url`,
      );
      setFailed(false);
      setShown({ url, expires: expires_in_seconds ?? 300 });
    } catch (error) {
      reportFailure(error);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button size="sm" disabled={busy} icon={<ImageIcon size={16} aria-hidden />} onClick={() => void open()}>
        {busy ? "Opening…" : label}
      </Button>
      <Sheet open={shown !== null} onClose={() => setShown(null)} title="Receipt">
        {shown ? (
          <div className="flex flex-col gap-3">
            <img
              src={shown.url}
              alt="Receipt"
              onError={() => setFailed(true)}
              className="h-auto w-full rounded-[var(--radius-control)] bg-surface-sunken"
            />
            {failed ? (
              <p className="text-[0.8125rem] text-short">
                The image could not be loaded. The signed link may have expired: close this and try again.
              </p>
            ) : null}
            <p className="text-[0.8125rem] text-ink-muted">
              This link is private and expires in about {Math.round(shown.expires / 60)} minutes.
            </p>
          </div>
        ) : null}
      </Sheet>
    </>
  );
}
