"""Collections: the live-row rule, the totals, and §6.9's reversal (CLAUDE.md §5.2, §6.8).

The database-aware half of Phase 6, mirroring `app/services/readings.py`. There is no pure
arithmetic counterpart here because there is no arithmetic worth isolating: a collection is
a figure a human types, not a figure derived from three others. Phase 5 needed
`app/services/sales.py` because §6.2 turns readings into money; nothing in this module
turns anything into anything.

**What this module deliberately does not do:** compare collections against sales. §6.4's
cash equation is Phase 10's, and §6.8 is explicit that a mismatch is *the variance*, not an
error. See `missing_cash_declaration`.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.core.collections import CollectionMode
from app.core.errors import AppError
from app.models.collection import Collection
from app.models.shift import Shift
from app.services import readings as reading_service

logger = logging.getLogger(__name__)


def _is_reversed() -> object:
    """SQL predicate: some other row points its `reverses_id` at this one.

    Computed rather than stored. A `reversed_at` column would be an UPDATE on a financial
    row in a closed shift, which is what §6.9 forbids, and it would be a second copy of a
    fact the `reverses_id` FK already records -- two copies that would eventually disagree.
    """
    reversal = Collection.__table__.alias("reversal")
    return exists().where(reversal.c.reverses_id == Collection.id)


def all_collections(db: Session, *, shift_id: UUID) -> list[Collection]:
    """Every row, reversals included, oldest first.

    §6.9: "Both rows remain visible." A history that hides the cancelled entries answers
    the balance question and not the audit question, and the audit question is the one this
    project is for.

    Ordered by `created_at` then `id`. A reversal and its replacement are inserted in one
    transaction and PostgreSQL's `now()` is transaction-scoped, so they share a timestamp
    to the microsecond and the id tie-break between them is arbitrary. That is acceptable
    because `reverses_id` -- not position in the list -- is what says which row cancels
    which; the ordering only has to be *stable*, so that paging or re-reading does not
    shuffle them.
    """
    return list(
        db.execute(
            select(Collection)
            .where(Collection.shift_id == shift_id)
            .order_by(Collection.created_at, Collection.id)
        )
        .scalars()
        .all()
    )


def live_collection_for_mode(
    db: Session, *, shift_id: UUID, mode: CollectionMode
) -> Collection | None:
    """The one live row for this mode, if there is one.

    This is where §5.2's "one live row per mode" is enforced, because it cannot be a unique
    constraint -- see the long comment in migration 0006. One card machine and one UPI QR
    (confirmed with the owner), and a register that writes one lumped figure per mode, so a
    second live row is a mistake rather than a second genuine payment channel.

    **Two live rows raise, loudly, naming both.** Because there is no unique constraint,
    two concurrent POSTs under READ COMMITTED can both pass the live-row probe and both
    insert -- idempotency only deduplicates the *same* key, and two genuine requests carry
    different ones. This used to end in `scalar_one_or_none()` and therefore in
    `MultipleResultsFound`, which surfaced as a 500 from `GET`, from the next `POST`, and
    from §6.8's close precondition: the shift became unreadable *and* unclosable, and only
    direct SQL got it back.

    Choosing between the two figures is not this function's call -- one of them is real
    money somebody received. So it refuses, and the refusal carries both ids and both
    amounts, which is exactly what a human needs to reverse the wrong one. The reversal
    route loads a row by id and never comes through here, so the fix path stays open while
    everything else is held shut.
    """
    rows = list(
        db.execute(
            select(Collection)
            .where(
                Collection.shift_id == shift_id,
                Collection.mode == mode.value,
                Collection.reverses_id.is_(None),
                ~_is_reversed(),
            )
            .order_by(Collection.created_at, Collection.id)
        )
        .scalars()
        .all()
    )
    if len(rows) > 1:
        named = "; ".join(f"{row.id} for {row.amount}" for row in rows)
        logger.error(
            "duplicate live collections for one mode",
            extra={
                "shift_id": str(shift_id),
                "mode": mode.value,
                "collection_ids": [str(row.id) for row in rows],
            },
        )
        raise AppError(
            status_code=409,
            code="DUPLICATE_LIVE_COLLECTIONS",
            detail=(
                f"This shift holds {len(rows)} live {mode.value} figures where §5.2 "
                f"allows one: {named}. Both are real rows and the system will not pick "
                "between them. Reverse whichever should not be there (§6.9) and this "
                "shift reads normally again."
            ),
        )
    return rows[0] if rows else None


def reversal_of(db: Session, *, collection_id: UUID) -> UUID | None:
    """The id of the row that cancels this one, if any.

    Extracted so `reverse` and the `PATCH` route ask the question the same way. They used
    to disagree: `reverse` refused to reverse an already-reversed row, while `PATCH` only
    refused to edit a row that *was* a reversal -- so a cancelled original stayed editable
    and the mode's net total could be driven negative.
    """
    return db.execute(
        select(Collection.id).where(Collection.reverses_id == collection_id)
    ).scalar_one_or_none()


def totals_by_mode(db: Session, *, shift_id: UUID) -> dict[str, Decimal]:
    """Net received per mode, reversals included in the sum.

    Summed across every row rather than filtered to the live ones, so a reversal that has
    not yet been replaced shows as the reduction it is instead of vanishing.
    """
    rows = db.execute(
        select(Collection.mode, Collection.amount).where(
            Collection.shift_id == shift_id
        )
    ).all()
    totals: dict[str, Decimal] = {}
    for mode, amount in rows:
        totals[mode] = totals.get(mode, Decimal("0.00")) + amount
    return totals


def declared_cash(db: Session, *, shift_id: UUID) -> Decimal | None:
    """What the salesman says he counted, or `None` if he has not said.

    **`None` and `Decimal("0.00")` are different answers** and this function keeps them
    apart, which is the entire reason §6.8 requires an explicit zero. `None` means nobody
    has declared anything; zero means somebody declared that no cash was taken. Collapsing
    the two would let a forgotten entry look exactly like a genuinely cashless day, and a
    shortfall would disappear into that gap -- the same failure §4.7 describes for an
    assumed opening reading.
    """
    live = live_collection_for_mode(db, shift_id=shift_id, mode=CollectionMode.cash)
    if live is None:
        return None
    return live.amount


def shift_moved_any_quantity(db: Session, *, shift: Shift) -> bool:
    """Did fuel actually go through a nozzle on this shift?

    Reads `nozzle_readings` and stops there. It deliberately does **not** call
    `readings.shift_sales`, which prices the shift and raises 409 `NO_PRICE_FOR_DATE` /
    `NO_MARGIN_FOR_DATE` when a rate is missing -- and petrol and diesel margins have never
    been entered at this outlet. A close precondition that inherited that would make every
    petrol shift unclosable because of a reference-data gap, which is a very confusing way
    to be told about a missing margin.

    §4.5: `any(q > 0)`, never a sum. 500 litres of petrol plus 100 kg of CBG is not 600 of
    anything.
    """
    saved = reading_service.readings_for_shift(db, shift_id=shift.id)
    for nozzle, _fuel_type in reading_service.nozzles_in_scope(db, shift=shift):
        reading = saved.get(nozzle.id)
        if reading is None:
            continue
        quantity = reading_service.quantity_if_known(reading, nozzle)
        if quantity is not None and quantity > 0:
            return True
    return False


def missing_cash_declaration(db: Session, *, shift: Shift) -> bool:
    """§6.8's `MISSING_COLLECTIONS` predicate: fuel moved and no cash figure was declared.

    **Absence, never mismatch.** A shift whose collections total ₹40,000 against ₹95,000 of
    metered sales passes this check: udhaar issued during the shift accounts for part of
    that gap and the rest is the variance §6.4 records. Refusing to close until the two
    agree would leave the salesman in front of a form with exactly one freely adjustable
    field, and he would type whatever balanced it -- giving a perfectly reconciled system
    that reports nothing.

    An explicit ₹0 satisfies it. See `declared_cash` for why zero and nothing are different
    answers.
    """
    if not shift_moved_any_quantity(db, shift=shift):
        return False
    return declared_cash(db, shift_id=shift.id) is None


def reverse(
    db: Session,
    *,
    original: Collection,
    reason: str,
    actor_id: UUID,
    replacement_amount: Decimal | None = None,
    replacement_reference: str | None = None,
) -> tuple[Collection, Collection | None]:
    """Cancel a collection by appending, never by editing (§6.9).

    The original row is not touched. A new row carries the negated amount and points back
    at it, and both stay visible -- so "what did the system say on the day" survives the
    correction. This is how double-entry accounting has worked for 600 years and it is the
    only way to answer "who changed this, when, and what was it before".

    `replacement_amount` is applied in the same transaction rather than being left to a
    second request. Without that, a correction on a *closed* shift is impossible: the
    reversal lands, and the follow-up POST is then refused by `writable=True`. Splitting it
    would also leave a window in which the shift's cash reads as zero.
    """
    if original.reverses_id is not None:
        raise AppError(
            status_code=409,
            code="CANNOT_REVERSE_A_REVERSAL",
            detail=(
                "This row is itself a reversal. To undo a reversal, record the correct "
                "figure as a new collection rather than negating the negation."
            ),
        )

    if reversal_of(db, collection_id=original.id) is not None:
        raise AppError(
            status_code=409,
            code="ALREADY_REVERSED",
            detail="This collection has already been reversed.",
        )

    reversal = Collection(
        shift_id=original.shift_id,
        mode=original.mode,
        amount=-original.amount,
        reference=original.reference,
        reverses_id=original.id,
        reversal_reason=reason,
        created_by=actor_id,
    )
    db.add(reversal)
    db.flush()

    replacement: Collection | None = None
    if replacement_amount is not None:
        replacement = Collection(
            shift_id=original.shift_id,
            mode=original.mode,
            amount=replacement_amount,
            reference=replacement_reference,
            created_by=actor_id,
        )
        db.add(replacement)
        db.flush()

    logger.warning(
        "collection reversed",
        extra={
            "collection_id": str(original.id),
            "reversal_id": str(reversal.id),
            "shift_id": str(original.shift_id),
            "mode": original.mode,
            "amount": str(original.amount),
            "reason": reason,
            "replaced_with": str(replacement_amount) if replacement else None,
            "reversed_by": str(actor_id),
        },
    )
    return reversal, replacement
