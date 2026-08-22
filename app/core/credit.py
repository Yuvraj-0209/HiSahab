"""How a customer settled an old bill (CLAUDE.md §5.2, §6.4, §6.6).

Mirrors app/core/collections.py and app/core/expenses.py: a StrEnum, so a member compares
equal to its own string value and the PostgreSQL enum, the JSON API and Python all speak one
vocabulary.

There is deliberately no enum here for a credit *sale*. A sale has no mode -- that is the
whole point of udhaar, no money changed hands -- and no status either, since §6.6 computes
outstanding from the rows rather than storing a settled flag (see §5.2 on why `is_settled`
was removed in Phase 9).
"""

from __future__ import annotations

from enum import StrEnum


class CreditRepaymentMode(StrEnum):
    """How a repayment arrived. §5.2 / §6.4 / §6.6.

    **Its own Postgres type, `credit_repayment_mode`** -- not `collection_mode`, and not
    `expense_mode` even though the four labels happen to match `expense_mode`'s today.

    `collection_mode` is wrong on both ends: it carries `wallet`, which nobody settles an old
    bill with, and lacks `bank_transfer`, which is exactly how a fleet customer pays. Sharing
    `expense_mode` would be worse than it looks -- the labels agreeing right now is a
    coincidence, not a contract, and the first phase that needs to add a mode to one of them
    would have to either alter a live enum shared by two unrelated tables or carry a value
    that is meaningless for the other. §5.2 already made this argument once, for
    `collections`; this is the same argument reaching the table it predicted.

    **Only `cash` enters §6.4's equation.** A customer settling by UPI or bank transfer moves
    no money through the drawer, so counting it as expected cash would invent a shortfall on
    the very day they paid -- and §14 records that this outlet books a shortfall as udhaar
    against the salesman's own name. Every mode is recorded for the ledger; one mode touches
    the drawer.
    """

    cash = "cash"
    card = "card"
    upi = "upi"
    bank_transfer = "bank_transfer"
