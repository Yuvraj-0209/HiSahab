"""How money left the pump (CLAUDE.md §5.2, §6.4, §6.7).

Mirrors app/core/collections.py: a StrEnum, so a member compares equal to its own string
value and the PostgreSQL enum, the JSON API and Python all speak one vocabulary.

## Where `ExpenseCategory` went

There used to be a second StrEnum here listing `salary | maintenance | electricity | other`.
Phase 8 replaced it with the `expense_categories` table (§5.1) -- adding a category you
actually spend on is data entry, not a schema change. The model lives in
`app/models/expense_category.py`; a category is now an FK, not a value.

**Two rules the deleted enum used to enforce structurally, which are now discipline.** Both
are in §14 as guardrails, and both are restated here because this is where anyone looking for
"what can an expense be?" will land:

* **Never create a fuel-purchase / tanker / IOCL / PAD category.** A tanker restock is paid
  from the bank account and settles against the IOCL ledger -- it never touches the drawer,
  so it was never a cash expense to begin with. §6.4 would invent a daily cash shortage that
  never happened, and §14 already forbids the same mistake for IOCL/PAD payments. That module
  is post-V1 (§12). Deleting the label from an enum was a schema act; not creating one is a
  choice a human now has to keep making.
* **Never create two categories meaning the same thing** -- a `MISC` alongside an `OTHER`.
  Two synonymous labels split one real expense across both and silently defeat §6.7's
  per-category daily aggregate: ₹600 under one plus ₹600 under the other never sums to the
  ₹1,200 that should have tripped it. `ck_expense_categories_code_format` stops
  `TEA`/`tea`/`Tea` but cannot stop `TEA` alongside `CHAI`; only a person can.
"""

from __future__ import annotations

from enum import StrEnum


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


class ExpensePaidFrom(StrEnum):
    """Whose pile the money left from. §5.2 / §6.4, Phase 17.

    `ExpenseMode` above says *how* it left; this says *from where*, and §6.4's two equations
    need both because they ask different questions:

    * **`expected_closing`** -- what should be in the locker? Subtracts every `cash` expense
      and never reads this column: the locker is lighter by all of it, whoever paid.
    * **`accountable_cash`** -- what should this one salesman be holding? Subtracts
      `shift_cash` rows only. A bill paid from the locker never passed through his hands.

    Getting that second one wrong was a live defect: on 30 July a shift that had taken
    ₹17,600 was charged ₹60,170 of locker-funded bills and reported the salesman ₹60,169 in
    surplus -- holding money nobody gave him (§5.2's worked example).

    Defaults to `shift_cash` at the database, which is both the ordinary case and exactly
    what the pre-Phase-17 code assumed, so historical rows read correctly. §13.33 records
    that nothing can check the answer.
    """

    shift_cash = "shift_cash"
    locker_cash = "locker_cash"
