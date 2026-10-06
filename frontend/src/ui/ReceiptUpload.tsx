/* Attaching a receipt photograph to a row being written (§7.2).
 *
 * The upload takes the shift id, and the server reads the business date, the outlet and §8's
 * ownership check off that shift -- never "today", because the day is typed in after the fact
 * and the receipt would file under a date the register never mentions. No Idempotency-Key: an
 * attachment is not a money record, and a retried upload leaves an unlinked row §7.4 sweeps.
 *
 * The server sniffs the content, refuses anything over 5 MB with 413, and answers an iPhone's
 * HEIC photo with a specific sentence telling the person which camera setting to change. That
 * sentence is shown verbatim: it is already the right one.
 */

import { useId, useState } from "react";
import { CameraIcon, CheckCircleIcon } from "@phosphor-icons/react";
import { uploadReceipt } from "../api/client";
import { reportFailure } from "./feedback";

export interface ReceiptUploadProps {
  shiftId: string;
  attachmentId: string | null;
  onUploaded: (attachmentId: string) => void;
  /** Set when a receipt is already attached and may not be swapped (§5.2, 409 ATTACHMENT_ALREADY_SET). */
  locked?: boolean;
  label?: string;
}

export function ReceiptUpload({ shiftId, attachmentId, onUploaded, locked = false, label = "Receipt" }: ReceiptUploadProps) {
  const id = useId();
  const [busy, setBusy] = useState(false);

  if (locked) {
    return (
      <div className="flex flex-col gap-1.5">
        <span className="text-footnote font-medium text-ink-muted">{label}</span>
        <p className="text-footnote text-ink-muted">
          A receipt is already attached and cannot be swapped. Correct the row with a reversal instead.
        </p>
      </div>
    );
  }

  async function upload(file: File | undefined) {
    if (!file) return;
    setBusy(true);
    try {
      const result = await uploadReceipt(file, shiftId);
      onUploaded(result.attachment_id);
    } catch (error) {
      reportFailure(error);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-footnote font-medium text-ink-muted">{label}</span>
      <label
        htmlFor={id}
        className={`pressable flex cursor-pointer items-center gap-3 rounded-[var(--radius-control)] border border-dashed px-3.5 py-3 ${
          attachmentId ? "border-surplus bg-surplus-tint" : "border-hairline-strong bg-surface"
        }`}
      >
        {attachmentId ? (
          <CheckCircleIcon size={22} weight="fill" className="text-surplus" aria-hidden />
        ) : (
          <CameraIcon size={22} className="text-ink-muted" aria-hidden />
        )}
        <span className="text-body text-ink">
          {busy ? "Uploading…" : attachmentId ? "Receipt uploaded. Tap to replace it." : "Take or choose a photo"}
        </span>
        <input
          id={id}
          type="file"
          accept="image/jpeg,image/png"
          className="sr-only-text"
          disabled={busy}
          onChange={(event) => void upload(event.target.files?.[0])}
        />
      </label>
    </div>
  );
}
