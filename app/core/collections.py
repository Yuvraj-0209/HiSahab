"""How money arrived (CLAUDE.md §5.2).

Mirrors app/core/units.py, app/core/roles.py and app/core/shifts.py: a StrEnum, so the
member compares equal to its own string value and the PostgreSQL enum, the JSON API and
Python all speak one vocabulary.

**The type is `collection_mode`, not a shared `payment_mode`.** §5.2 gives
`credit_repayments.mode` a *different* set -- `cash | card | upi | bank_transfer`, with
`bank_transfer` in place of `wallet` -- because a customer settling an old bill can wire
money and a customer buying diesel at the pump cannot. One shared type would force Phase 9
to either alter a live enum or carry a value that is meaningless for it.
"""

from __future__ import annotations

from enum import StrEnum


class CollectionMode(StrEnum):
    """The payment channels money reaches this outlet through.

    `cash` is not like the other three. §5.2 and §6.4: the cash equation *derives* cash as
    the residual and never reads a cash collection row, so a `cash` row is the salesman's
    **declaration** -- what he says he counted into the locker -- held against the derived
    figure. The gap between the two is the shortfall. The other three modes are inputs to
    that equation; `cash` is the check on its answer.
    """

    cash = "cash"
    card = "card"
    upi = "upi"
    wallet = "wallet"
