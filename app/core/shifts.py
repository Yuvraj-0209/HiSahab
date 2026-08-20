"""Shift status and its transition rules (CLAUDE.md §5.2, §6.8).

Mirrors app/core/units.py and app/core/roles.py: a StrEnum, so the member compares equal
to its own string value and the PostgreSQL enum, the JSON API and Python all speak one
vocabulary.

There is deliberately **no ShiftType**. §4.7: the original `morning | night` enum described
a station this outlet does not run, and was not general enough for the 24-hour outlets this
software will also serve. Shifts are sequence-numbered per business date instead.
"""

from __future__ import annotations

from enum import StrEnum


class ShiftStatus(StrEnum):
    """Where a shift is in its lifecycle.

    `open` -- being entered. The only status in which financial rows may be written.
    `closed` -- reconciled by a manager. Frozen, but an admin can still reopen it.
    `locked` -- finalised by an admin. Terminal (§6.8): the whole point of locking is that
                it cannot be undone, so nothing here reopens it.
    """

    open = "open"
    closed = "closed"
    locked = "locked"


# The forward lifecycle plus the one sanctioned reversal. Written as an explicit map
# rather than inferred from declaration order for the same reason app/core/roles.py uses an
# explicit rank map: reordering the members above is a harmless-looking edit that would
# otherwise silently change which transitions are legal.
#
# `locked` is absent as a source on purpose. It is terminal -- see the class docstring.
_ALLOWED_TRANSITIONS: dict[ShiftStatus, frozenset[ShiftStatus]] = {
    ShiftStatus.open: frozenset({ShiftStatus.closed}),
    ShiftStatus.closed: frozenset({ShiftStatus.open, ShiftStatus.locked}),
    ShiftStatus.locked: frozenset(),
}


def can_transition(current: ShiftStatus, target: ShiftStatus) -> bool:
    """True if `current -> target` is a legal move.

    Deliberately dumb: it knows nothing about who is asking or whether the shift's
    preconditions are met. Role floors live in app/api/deps.py and preconditions live in
    the endpoints, so that each can be tested without standing up the others.
    """
    return target in _ALLOWED_TRANSITIONS[current]
