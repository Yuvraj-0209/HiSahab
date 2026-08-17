"""Roles and the role hierarchy (CLAUDE.md §8).

A role is always held *at an outlet* -- see `outlet_memberships`. There is deliberately no
"is this user an admin" concept anywhere in this codebase, because that question is
meaningless: someone can be a manager at one pump and an attendant at another.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """The three roles from §8's permission table.

    StrEnum so the member compares equal to its own string value, which keeps the
    PostgreSQL enum, the JSON API and Python all speaking the same vocabulary.
    """

    attendant = "attendant"
    manager = "manager"
    admin = "admin"


# Rank, low to high. An explicit map rather than relying on declaration order or on
# IntEnum: if someone later reorders the members above (harmless-looking edit), an
# order-dependent implementation would silently change who can do what. This cannot.
_RANK = {
    Role.attendant: 0,
    Role.manager: 1,
    Role.admin: 2,
}


def satisfies(held: Role, minimum: Role) -> bool:
    """True if `held` clears the `minimum` bar.

    The hierarchy is a strict superset ladder (§8): a manager can do everything an
    attendant can, and an admin everything a manager can. So this is a rank comparison,
    not an equality check.

    Note this covers *role floors only*. Ownership is a separate axis -- an attendant may
    write only to their own shift, while managers and admins may write to any shift at
    their outlet. That check arrives with `shifts` in Phase 4; see the note under §8's
    permission table.
    """
    return _RANK[held] >= _RANK[minimum]
