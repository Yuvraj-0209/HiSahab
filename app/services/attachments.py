"""Attachments: create the row and the object together, link, read-permission, sweep.

The database-and-storage-aware half of Phase 8, mirroring the shape `app/services/
expenses.py` and `app/services/collections.py` already established: pure orchestration,
`AppError` for every refusal, no FastAPI import anywhere.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.roles import Role
from app.core.uploads import ValidatedUpload
from app.models.attachment import Attachment
from app.models.cash import BankDeposit
from app.models.credit import CreditSale
from app.models.expense import Expense
from app.models.shift import Shift
from app.services.cash import live_deposit_for_attachment
from app.services.credit import live_credit_sale_for_attachment
from app.services.expenses import live_expense_for_attachment
from app.services.storage import StorageBackend

logger = logging.getLogger(__name__)

# §7.4: an unlinked upload older than this is an abandoned attempt, not evidence of
# anything. Defined once, here, next to `orphans()` -- the function that defines what
# "orphaned" means is the natural home for how old counts as orphaned, and
# app/jobs/cleanup_attachments.py imports this rather than writing `timedelta(hours=24)` a
# second time (M9 in the Phase 8 plan; matches how app/core/idempotency.py's own TTL is a
# single imported constant, not a value repeated between the store and its cleanup job).
ORPHAN_TTL = timedelta(hours=24)


def create(
    db: Session,
    *,
    storage: StorageBackend,
    bucket: str,
    outlet_id: UUID,
    business_date: date,
    uploaded_by: UUID,
    original_filename: str,
    validated: ValidatedUpload,
    data: bytes,
) -> Attachment:
    """§7.2 steps 3-5: compute the path, write the row, then the object -- in that order.

    **The row is inserted and flushed *before* the storage write is attempted (M3).** A
    failed upload then rolls back cleanly and leaves nothing: no attachment row pointing at
    an object that was never written. The reverse order -- upload first, insert after --
    risks the opposite failure, an object in the bucket with no row and no way for
    `app/api/v1/expense_categories.py`-style validation to ever find it again except
    through §7.4's sweep, which is built for abandoned *uploads*, not abandoned writes.

    **The one remaining window is a commit failure after a successful upload (M4).** Rare
    -- it means the database went away in the instant between the object landing in
    storage and the transaction committing -- but when it happens the object is real and
    nothing in the database points at it. Logged loudly as `orphan_storage_object` with the
    full path so it can be reclaimed by hand; there is no automatic recovery for a database
    that is currently down.

    The attachment's own id doubles as the uuid4 in its storage path (§7.2:
    `{outlet_id}/{YYYY}/{MM}/{DD}/{uuid4}.{ext}`) -- generated here in Python rather than
    left to the database's `gen_random_uuid()` default, because the path has to be known
    *before* the row is written, not after.
    """
    attachment_id = uuid4()
    storage_path = (
        f"{outlet_id}/{business_date:%Y}/{business_date:%m}/{business_date:%d}/"
        f"{attachment_id}.{validated.extension}"
    )

    row = Attachment(
        id=attachment_id,
        outlet_id=outlet_id,
        bucket=bucket,
        storage_path=storage_path,
        original_filename=original_filename,
        mime_type=validated.mime_type,
        size_bytes=validated.size_bytes,
        checksum_sha256=validated.checksum_sha256,
        uploaded_by=uploaded_by,
    )
    db.add(row)
    db.flush()

    try:
        storage.upload(
            bucket=bucket,
            path=storage_path,
            data=data,
            content_type=validated.mime_type,
        )
    except Exception:
        db.rollback()
        raise

    try:
        db.commit()
    except Exception:
        logger.error(
            "orphan_storage_object",
            extra={"bucket": bucket, "storage_path": storage_path},
        )
        raise

    db.refresh(row)
    return row


def link(db: Session, *, attachment: Attachment, outlet_id: UUID) -> None:
    """§7.2 step 7 / §5.3's one-attachment-one-*live*-row rule.

    Call this **before** writing `attachment_id` onto the claiming row, so a refusal here
    leaves nothing written -- the same ordering `resolve_category` uses in
    `app/services/expenses.py`. Raises `AppError` on both failure cases; the caller does
    not need to check a return value.

    Stamps `linked_at` only on an attachment's *first* link, ever. Inheriting into a
    reversal's replacement (§6.9, D3) is not a new link -- the same photograph, the same
    piece of evidence -- and `linked_at` records when the attachment stopped being an
    abandoned upload, permanently, for §7.4's sweep to read.

    **Checks `expenses`, `credit_sales` (Phase 9) and `bank_deposits` (Phase 10)** -- the
    three tables that can claim an attachment. Each contributes one `live_*_for_attachment`
    query and this function asks all of them; the rule itself has not changed as tables
    landed, exactly as the Phase 8 version of this docstring predicted.
    """
    if attachment.outlet_id != outlet_id:
        # Existence is not leaked across tenants -- the same posture §7.3 takes for reads,
        # and `resolve_category` takes for expense categories.
        raise AppError(
            status_code=404,
            code="ATTACHMENT_NOT_FOUND",
            detail="No attachment with that id at this outlet.",
        )

    claimed_by_expense = live_expense_for_attachment(db, attachment_id=attachment.id)
    claimed_by_sale = live_credit_sale_for_attachment(db, attachment_id=attachment.id)
    claimed_by_deposit = live_deposit_for_attachment(db, attachment_id=attachment.id)
    if (
        claimed_by_expense is not None
        or claimed_by_sale is not None
        or claimed_by_deposit is not None
    ):
        raise AppError(
            status_code=409,
            code="ATTACHMENT_ALREADY_LINKED",
            detail=(
                "This attachment is already linked to another expense, credit sale or "
                "bank deposit. One photograph may only justify one of them."
            ),
        )

    if attachment.linked_at is None:
        attachment.linked_at = datetime.now(timezone.utc)


def may_read(
    db: Session, *, user_id: UUID, role: Role, attachment: Attachment
) -> bool:
    """§7.3's permission rule -- the ownership half. The role/outlet floor has already been
    enforced by the router's `require_role` dependency before this ever runs; this decides
    only whether an *attendant* specifically may read *this* attachment.

    An attendant may read it if they uploaded it, or if it is linked to a business row on a
    shift they are the attendant of. The upload case matters on its own: between §7.2's
    steps 5 and 6 the attachment is linked to nothing at all, and the person who just
    uploaded it must still be able to see what they uploaded.

    **All three claiming tables are checked.** Before `credit_sales` existed this
    asked only about expenses, which would have left an attendant unable to open the receipt
    for an udhaar slip on their own shift unless they happened to have uploaded it personally
    -- a manager entering the day on their behalf would have locked them out of their own
    paperwork.
    """
    if role is not Role.attendant:
        return True

    if attachment.uploaded_by == user_id:
        return True

    linked_to_own_expense = db.execute(
        select(Expense.id)
        .join(Shift, Shift.id == Expense.shift_id)
        .where(Expense.attachment_id == attachment.id, Shift.attendant_id == user_id)
    ).first()
    if linked_to_own_expense is not None:
        return True

    linked_to_own_credit_sale = db.execute(
        select(CreditSale.id)
        .join(Shift, Shift.id == CreditSale.shift_id)
        .where(CreditSale.attachment_id == attachment.id, Shift.attendant_id == user_id)
    ).first()
    if linked_to_own_credit_sale is not None:
        return True

    # Phase 10. A deposit slip is not an attendant's own paperwork the way a receipt is --
    # §8 makes recording a deposit a manager's act -- but the attendant of the shift the
    # deposit was made on can still legitimately need to see the slip when the day is
    # queried. Withholding it would be the one gap in "you can see the evidence attached to
    # your own shift".
    linked_to_own_deposit = db.execute(
        select(BankDeposit.id)
        .join(Shift, Shift.id == BankDeposit.shift_id)
        .where(BankDeposit.attachment_id == attachment.id, Shift.attendant_id == user_id)
    ).first()
    return linked_to_own_deposit is not None


def orphans(db: Session, *, cutoff: datetime) -> Sequence[Attachment]:
    """§7.4's sweep predicate: unlinked, and created before `cutoff`.

    Extracted so `app/jobs/cleanup_attachments.py` and its own tests share one definition
    of "orphaned" rather than each writing the `WHERE` clause separately and risking drift.
    """
    return (
        db.execute(
            select(Attachment).where(
                Attachment.linked_at.is_(None), Attachment.created_at < cutoff
            )
        )
        .scalars()
        .all()
    )
