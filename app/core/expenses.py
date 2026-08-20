"""What money was spent on, and how it left (CLAUDE.md §5.2, §6.4, §6.7).

Mirrors app/core/collections.py: two StrEnums, so a member compares equal to its own string
value and the PostgreSQL enum, the JSON API and Python all speak one vocabulary.
"""

from __future__ import annotations

from enum import StrEnum


class ExpenseCategory(StrEnum):
    """What the money was for.

    **No `fuel_purchase`.** A tanker restock is paid from the bank account and settles
    against the IOCL ledger -- it never touches the drawer, so it was never a cash expense
    to begin with, and the label invited exactly the mistake §14 already forbids for
    IOCL/PAD payments: recording a bank movement as a drawer withdrawal. That module is
    post-V1 (§12).

    **No separate `misc`.** Two synonymous categories can split one real expense across
    both labels and silently defeat §6.7's per-category daily aggregate -- ₹600 under
    `misc` plus ₹600 under `other` never sums to the ₹1,200 that should have tripped it.
    """

    salary = "salary"
    maintenance = "maintenance"
    electricity = "electricity"
    other = "other"


class ExpenseMode(StrEnum):
    """How the money left. §5.2 / §6.4.

    A separate type from `CollectionMode`, not a shared `payment_mode` -- an expense can be
    paid by `bank_transfer`, a collection cannot, and `collections` has no analogue of
    `wallet` for money going out. §6.4 reads `mode == cash` to decide whether an expense
    touches the drawer at all; every other mode is recorded for the ledger but leaves
    `cash_expenses` untouched.
    """

    cash = "cash"
    card = "card"
    upi = "upi"
    bank_transfer = "bank_transfer"
