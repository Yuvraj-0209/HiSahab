"""Shift chain and lifecycle helpers (CLAUDE.md §4.7, §5.2, §6.8).

The chain is the point of this module. §4.7: a nozzle's opening reading is carried forward
from the most recent closing reading of that same nozzle, so an attendant only ever types a
closing value. Phase 5 builds the reading half; Phase 4 builds the shift-ordering half that
it will stand on:

* which shift is the chain's current tip (`latest_shift`)
* whether the chain is mid-link, i.e. something is still open (`open_shift`)
* what number the next link gets (`next_sequence`)
* whether a shift holds anything at all (`tables_holding`, Phase 27's void)

Kept out of the router so Phase 5's reading code, and any later management command, can ask
the same questions without going through HTTP.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.shifts import ShiftStatus
from app.models.shift import Shift


def latest_shift(db: Session, *, outlet_id: UUID) -> Shift | None:
    """The tip of this outlet's chain, or None if no shift has ever been opened.

    Ordered by (business_date, sequence), never by created_at: days are typed in after the
    fact (§4.7), often out of order relative to when they actually traded, so insertion
    order says nothing about chain order.

    Matches the ix_shifts_chain index exactly.
    """
    return db.execute(
        select(Shift)
        .where(Shift.outlet_id == outlet_id)
        .order_by(Shift.business_date.desc(), Shift.sequence.desc())
        .limit(1)
    ).scalar_one_or_none()


def open_shift(db: Session, *, outlet_id: UUID) -> Shift | None:
    """The one shift currently open at this outlet, if any.

    §5.2 permits at most one. That is what keeps §4.7's chain unambiguous -- with two open
    shifts there is no single "most recent closing reading" to carry forward, and the
    question "which drawer did this cash go into" stops having an answer (§13.12).
    """
    return db.execute(
        select(Shift).where(
            Shift.outlet_id == outlet_id,
            Shift.status == ShiftStatus.open.value,
        )
    ).scalar_one_or_none()


def next_sequence(db: Session, *, outlet_id: UUID, business_date: date) -> int:
    """The sequence number the next shift on this business date should take.

    Server-assigned, never client-supplied (§4.7). Racy in principle -- two concurrent
    opens could read the same maximum -- which is exactly what
    uq_shifts_outlet_date_sequence is for: the loser gets an IntegrityError rather than a
    duplicate. In practice `open_shift` already rejects the second caller, so this is the
    second of two locks on the same door.
    """
    highest = db.execute(
        select(func.max(Shift.sequence)).where(
            Shift.outlet_id == outlet_id,
            Shift.business_date == business_date,
        )
    ).scalar_one_or_none()
    return 1 if highest is None else int(highest) + 1


# Every foreign key in the database whose referenced table is `shifts`, one row per key, with
# the referencing table and both sides' column names in key order. `unnest(conkey, confkey)`
# walks the two column lists in step, so a composite key -- §5.0 sketches
# `(shift_id, outlet_id) REFERENCES shifts(id, outlet_id)` for later -- comes back as pairs
# rather than being assumed away.
_FOREIGN_KEYS_TO_SHIFTS = text(
    """
    SELECT n.nspname AS child_schema,
           cl.relname AS child_table,
           array_agg(child_col.attname ORDER BY k.ord) AS child_columns,
           array_agg(parent_col.attname ORDER BY k.ord) AS parent_columns
    FROM pg_constraint c
    JOIN pg_class cl ON cl.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = cl.relnamespace
    CROSS JOIN LATERAL unnest(c.conkey, c.confkey)
        WITH ORDINALITY AS k(child_attnum, parent_attnum, ord)
    JOIN pg_attribute child_col
        ON child_col.attrelid = c.conrelid AND child_col.attnum = k.child_attnum
    JOIN pg_attribute parent_col
        ON parent_col.attrelid = c.confrelid AND parent_col.attnum = k.parent_attnum
    WHERE c.contype = 'f' AND c.confrelid = 'shifts'::regclass
    GROUP BY c.oid, n.nspname, cl.relname
    ORDER BY cl.relname
    """
)


def tables_holding(db: Session, shift_id: UUID) -> list[str]:
    """The tables that have at least one row pointing at this shift, by name. Empty if none.

    Phase 27's definition of an *empty* shift, the only kind §6.8 lets an admin void: no row
    in any table that holds a foreign key to `shifts`. Nine tables answer today -- readings,
    collections, expenses, the two credit tables, non-fuel sales, deposits and the two
    shortfall tables.

    **The list is read from `pg_constraint` on every call, and that is the point.** A list in
    code would be right on the day it was written and silently wrong on the day a later phase
    added a tenth table: a void would then reach a shift a new row still points at. §6.9
    records `_CONSTRAINT_ERRORS` being forgotten twice in exactly that way. Asking the
    database means a new table is covered the day its migration lands, without anybody
    remembering to come back here.

    The table and column names come from the catalogue rather than from a caller, and they are
    quoted by the dialect's own preparer before they reach SQL anyway. The shift id is always a
    bound parameter.

    One `EXISTS` per foreign key -- nine indexed lookups. A void is an admin's rare act, so
    there is nothing here worth optimising into one dynamic statement.
    """
    quote = db.get_bind().dialect.identifier_preparer.quote
    # A set, so a table with two keys to `shifts` is named once.
    holding: set[str] = set()
    for key in db.execute(_FOREIGN_KEYS_TO_SHIFTS).mappings():
        table = key["child_table"]
        joined_on = " AND ".join(
            f"child.{quote(child)} = shift.{quote(parent)}"
            for child, parent in zip(key["child_columns"], key["parent_columns"])
        )
        found = db.execute(
            text(
                f"SELECT EXISTS (SELECT 1 FROM {quote(key['child_schema'])}.{quote(table)} "
                f"AS child JOIN shifts AS shift ON {joined_on} WHERE shift.id = :shift_id)"
            ),
            {"shift_id": shift_id},
        ).scalar_one()
        if found:
            holding.add(table)
    return sorted(holding)


def outlet_today(tz_name: str) -> date:
    """Today's date in the outlet's local timezone.

    Used to reject a future `business_date` (§6.1). Must not be `datetime.now().date()` in
    UTC: at 02:00 IST the UTC date is still yesterday, so a shift legitimately opened for
    today would be refused for five and a half hours every night.

    §13.11: the timezone comes from the global TZ_DISPLAY rather than a column on `outlets`,
    because unlike outlet_id it is derivable later -- every existing outlet backfills to
    Asia/Kolkata correctly.
    """
    return datetime.now(tz=ZoneInfo(tz_name)).date()


def local_time_on(business_date: date, local_time: time, tz_name: str) -> datetime:
    """Resolve a shift template's wall-clock time on a business date to a UTC instant.

    `outlet_shift_templates` stores "06:00 local, every day" as a TIME, which is not an
    instant and cannot be one (§5.1). This is where it becomes one, for a specific date.

    Returned in UTC because §3 rule 4 requires every stored timestamp to be UTC; display
    conversion back to Asia/Kolkata happens in the frontend only.
    """
    return datetime.combine(
        business_date, local_time, tzinfo=ZoneInfo(tz_name)
    ).astimezone(timezone.utc)


def template_window(
    business_date: date, starts: time, ends: time, tz_name: str
) -> tuple[datetime, datetime]:
    """Resolve a template's two wall-clock times into a (start, end) pair of UTC instants.

    The wrap is the whole reason this is not two calls to `local_time_on`. A 24-hour
    outlet's night shift template reads 22:00 -> 06:00, and taking both on the same
    business date produces an end *before* the start -- which the
    ck_shifts_ended_after_started constraint would refuse, correctly but unhelpfully. When
    the end time is not after the start time, the shift crosses midnight and the end
    belongs to the following calendar day.

    The `business_date` itself does not move (§6.1): the shift belongs to the date it was
    opened on, however many calendar dates its timestamps span.

    This outlet's 06:00 -> 22:00 template never wraps. The branch exists for the outlets
    that do, and is covered by tests rather than left to be discovered by one of them.
    """
    start = local_time_on(business_date, starts, tz_name)
    end_date = business_date if ends > starts else business_date + timedelta(days=1)
    end = local_time_on(end_date, ends, tz_name)
    return start, end
