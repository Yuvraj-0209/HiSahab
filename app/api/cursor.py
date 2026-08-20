"""Keyset cursors for the effective-dated history endpoints (CLAUDE.md §9).

§9 requires cursor pagination and §14 forbids `OFFSET`, for a specific reason: with an
offset, a row inserted while someone is paging shifts every later row down by one, so the
reader sees a duplicate and misses one entirely. A keyset cursor asks "give me what sorts
after *this exact row*", which is stable under concurrent inserts.

**Scope.** This is not a general pagination framework. It holds one encoder per sort key
that some endpoint actually uses, and nothing speculative:

* `encode_cursor` / `decode_cursor` -- `(effective_from DESC, id DESC)`, for the two
  append-only history tables `fuel_prices` and `fuel_margins`.
* `encode_shift_cursor` / `decode_shift_cursor` -- `(business_date DESC, sequence DESC)`,
  for `/shifts` (Phase 4).

`/nozzles` and `/fuel-types` deliberately return capped full lists instead. Everything lives
in one module rather than inside the routers so an encoding cannot drift between two copies;
a cursor issued by one and parsed by a divergent copy in another would be a genuinely nasty
bug to find. A second pair rather than one generic function because the two keys are
different *types* -- collapsing them behind a generic signature would trade a clear
signature for a `tuple[Any, Any]` and buy nothing.

**The sort key is `(effective_from DESC, id DESC)`, not `effective_from` alone.** Two rows
for different fuels can share an `effective_from` -- a 06:00 revision typically moves petrol
and diesel at the same instant -- so ordering on the timestamp alone is not total, and a
page boundary landing in the middle of a tie would drop or repeat rows. The id breaks the
tie and makes the order deterministic.

The cursor is base64 of `"<iso timestamp>|<uuid>"`. Opaque by convention, not by encryption:
it encodes a position, not a secret, and every row it points at is one the caller was
already authorised to read.
"""

from __future__ import annotations

import base64
import binascii
from datetime import date, datetime
from uuid import UUID

from app.core.errors import AppError

# Chosen over a generous ceiling because these are money-history endpoints read by a phone
# on rural connectivity (§6.10's reasoning), not a reporting warehouse.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def encode_cursor(effective_from: datetime, row_id: UUID) -> str:
    """Position of the last row on a page, as an opaque string."""
    raw = f"{effective_from.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Parse a cursor back into a sort key, or refuse.

    400 rather than 422: a cursor is not a user-entered field to be corrected, it is a
    token this API issued. A malformed one means the client mangled it or invented it, and
    the caller cannot fix it by editing a form. Never fall back to "start from the
    beginning" on a bad cursor -- silently restarting a page walk is how a reader ends up
    seeing the same rows twice and believing they are different records.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        timestamp_text, _, id_text = raw.partition("|")
        return datetime.fromisoformat(timestamp_text), UUID(id_text)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise AppError(
            status_code=400,
            code="INVALID_CURSOR",
            detail="The pagination cursor is not valid. Start from the first page.",
        ) from exc


def encode_shift_cursor(business_date: date, sequence: int) -> str:
    """Position of the last shift on a page.

    The key is `(business_date, sequence)` rather than `(started_at, id)`. It is already
    unique per outlet -- uq_shifts_outlet_date_sequence guarantees it -- so unlike the
    price tables no id tiebreaker is needed, and it is the same key ix_shifts_chain is
    built on, so the page walk and the §4.7 chain lookup share one index.

    Deliberately not `created_at`: days are typed in after the fact (§4.7), so insertion
    order and chain order routinely disagree, and paging by insertion order would present
    shifts in an order that means nothing to the reader.
    """
    raw = f"{business_date.isoformat()}|{sequence}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_shift_cursor(cursor: str) -> tuple[date, int]:
    """Parse a shift cursor back into a sort key, or refuse. See `decode_cursor`."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        date_text, _, sequence_text = raw.partition("|")
        return date.fromisoformat(date_text), int(sequence_text)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise AppError(
            status_code=400,
            code="INVALID_CURSOR",
            detail="The pagination cursor is not valid. Start from the first page.",
        ) from exc
