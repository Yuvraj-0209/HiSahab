# Phase 16 — The credit ledger stands on its own: Plan, Decisions, and Test Inventory

> Follows the `phase-5` … `phase-15-plan.md` pattern: written **before** the code, with §11
> recording what actually shipped where it differs.
>
> One migration, one new table, a fifth tab — and a correction to §6.4 that was live on real
> money the whole time.

---

## Context

Phase 15 was the first phase written after somebody entered a real trading day. **This is the
first written after somebody tried to enter what customers actually owe**, and the report was
again concrete rather than theoretical.

The owner's words: *"if all of them are standing at 0 rupees on 1st july so if i add any
repayment on 3rd july their balance will become +ve but haha no ones that good to pay back in
advance."*

Three defects, and only one is a bug in the ordinary sense.

### (a) Outstanding began at zero for everyone

`services/credit.py::outstanding` computes `SUM(sales) − SUM(repayments)`, which is exactly
what §6.6 requires and rightly forbids storing. The consequence nobody had written down: **the
software's ledger begins the day the software does, and this pump's began years earlier.**

A customer already owing ₹12,400 reads as square. His first repayment drives the balance
negative — the pump appearing to owe *him* money. And `check_credit_limit` reads the same
figure, so §6.6's control was being measured against a number wrong by his entire history.

### (b) A bank transfer could not be recorded once a day was locked

`credit_repayments.shift_id` was `NOT NULL`. §5.2 says nothing referencing a `locked` shift may
be modified, and `deps.py:380` enforces it with 409 `SHIFT_LOCKED`.

So reconstructing a month of ledger history was blocked **by the very shifts that month already
had** — and a transfer arriving on a day the outlet was shut had no shift to attach to at all.
§4.7 says the day is typed in after the fact, which makes both the normal case here.

### (c) Udhaar settled on the card machine produced a phantom surplus

Confirmed with the owner that this happens. `cash.py:456` read:

```
accountable = metered + non_fuel − card − upi − wallet − credit_sales + cash_repayments + …
```

`card` is the machine's whole-day total, so a ₹1,000 settlement sits inside it. Nothing put it
back: it is not a metered sale, and `cash_repayments_total` filters to `mode = 'cash'`. So
`accountable` came out **low by exactly the settlement**, and since `gap = accountable −
declared`, the salesman read as holding a surplus nobody gave him.

This is the shape §6.4 already works through for a card-paid bottle of oil, one table over.

### And a question that turned out to be a misreading

The owner asked whether *"payment, and of which cash"* on the repayment screen meant how much
of a credit sale came back as cash. It does not — both are read-only summaries of what has
already been entered, and repayments do not point at sales at all (§5.2: *"one payment covering
part of three debts has no honest per-row answer"*). Worth recording because it is the second
time in two phases that a **label** was the defect rather than an endpoint.

---

## Decisions

### D1 — A repayment with a shift arrived at the pump; one with only a date arrived at the bank

The rule the whole phase rests on, and it fits in a sentence because every §6.4 sum is already
`WHERE shift_id = :shift_id`. The **presence of a shift** therefore carries the entire meaning,
and no second column has to say it.

| Mode | Shift? | §6.4 |
|---|---|---|
| `cash` | required | `+ cash_credit_repayments`, cash side |
| `card`, `upi` | optional | `+ card_upi_credit_repayments`, **sales** side — only with a shift |
| `bank_transfer` | none | nowhere |

Cash is the one mode that cannot be shift-less: cash can only land in a drawer, and a cash
repayment with nowhere to land is money §6.4 would never count. Enforced at the database by
`ck_credit_repayments_cash_needs_shift` as well as in the API — §6.6's belt-and-braces habit.

*Rejected:* a `reaches_the_drawer` boolean. It would be a second source of truth for something
`shift_id` already answers, free to disagree with it.

### D2 — `card_upi_credit_repayments` goes on the sales side, never the cash side

Algebraically the same as netting it out of `card_total`, and stated as an addition to sales
because that is where §6.4 already puts a card-paid bottle of oil, with the identical argument.
Putting it on the cash side would be wrong by the same amount in the same direction — the money
never entered the drawer.

**The dependency worth naming:** this term is correct because `collections.mode = 'card'` means
the machine's whole-day total, which §5.2 says it does. If a salesman ever typed a card figure
that already excluded settlements, it would double-count. That is a data-entry contract rather
than an arithmetic one, and it is written into `card_upi_repayments_total`'s docstring because
nothing else would say it.

### D3 — Opening balances are a table, one live row per customer, with no sign CHECK

A table rather than a column, because §6.9 corrects a money row with a reversal and §5.2's
argument for `non_fuel_sales` transfers verbatim: *you cannot reverse a column*. A mutable
`opening_balance` column would also be §6.6's forbidden denormalised total under another name,
editable from the ordinary customer form, with nothing recording what it was before.

**One live row, in the service rather than as a constraint.** §5.2 works this through for
`collections` and every word transfers: a reversed row stays forever, its replacement collides
on any natural key, and every partial-index variant fails identically because the replacement
also carries `reverses_id IS NULL`.

**No amount sign CHECK, and this is the one table where §6.9's usual shape does not apply.**
§6.6 already says an outstanding balance may legitimately be negative, so an original may be
negative and a reversal positive — **the sign carries no information about which is which.**
`reverses_id` carries it alone. A CHECK that has to be true and cannot be stated is worse than
its absence.

**Zero is permitted and is a real statement** (§6.8). "I checked Vikram and he was square on
1 July" is a different fact from "nobody has entered Vikram yet", and the screen gets a separate
*They owed nothing* button so that saying the first is a deliberate act.

### D4 — The double-count guard runs in both directions

The opening figure contains everything before `as_of_date`, so an entry dated earlier is counted
twice. Refused both ways: 409 `BEFORE_OPENING_BALANCE_DATE` when a sale or repayment is
recorded, 409 `ENTRIES_BEFORE_OPENING_BALANCE` when the opening balance is set. Strictly
*before*, so an entry on the date itself is fine — §6.6's and §6.7's boundary convention.

### D5 — The ledger is capped, not cursor-paginated

A deliberate exception to §9, in the shape `/nozzles` and `/fuel-types` already carry (§13.31).

The screen's whole value is `balance_after`, and a running balance is only meaningful as a walk
from a known anchor. The walk runs **newest-first downward from `outstanding`**, so every line's
balance depends only on lines *newer* than itself — truncating the old end cannot make a
displayed figure wrong. A cursor cannot promise that: page two has no anchor unless the token
carries a money value, and a money value in a client-held token is a figure the server would
then have to trust back.

**And it orders by business date, not `created_at`.** §4.7: the day is typed in after the fact,
so entry order is not economic order, and a running balance read in entry order is a column of
numbers that never matches the slips.

### D6 — Existing summaries are flagged, never recomputed

Migration `0015` computes each existing summary's real `card_upi_credit_repayments` into the new
column and, where non-zero, sets `requires_review` with a note. `expected_closing` and every
other stored component are left byte-identical.

§13.16's rule applied to a bug fix rather than to a reopened shift, and the reason is §5.2's: a
reader has to see what the manager was told on the day, *including on the days the system was
wrong*. §6.5 chains days, so recomputing one would silently move every opening balance after it.

### D7 — A fifth tab at the manager floor, with one admin route inside it

§8 has always kept a customer's balance above the attendant floor, and nothing here changes
that. Opening balances are admin-only on their own route — §8's usual read/write split, which is
also exactly what the owner asked for: *"other users must not get the feature to change their
balance out of nowhere."*

*Rejected:* putting opening balances under Admin. The owner named the Credit tab, and splitting
one subject across two tabs is how §11 phase 15 describes the Cash tab going wrong.

### D8 — `POST /credit-opening-balances` takes no `Idempotency-Key`

The one-live-row rule makes a retry a 409 rather than a second row, so it cannot duplicate — the
same reasoning §6.10 gives for nozzle readings, and the same `POST /daily-summaries` relies on.
The **reversal** endpoint does take one, because a retried reversal genuinely would append a
second negative row.

---

## Test inventory

Backend, written first per §10:

| Test | Rule |
|---|---|
| `test_an_opening_balance_is_the_whole_outstanding_when_nothing_else_exists` | §6.6's third term |
| `test_a_repayment_no_longer_drives_a_real_debtor_negative` | the owner's own report |
| `test_zero_is_an_answer_and_absence_is_not` | §6.8, §14 — both read ₹0.00 and the facts differ |
| `test_a_negative_opening_balance_is_accepted` | §6.6; why there is no sign CHECK |
| `test_a_second_live_opening_balance_is_refused_and_writes_nothing` | §5.2 |
| `test_a_manager_cannot_set_an_opening_balance` | §8 |
| `test_an_opening_balance_counts_toward_the_credit_limit` | §6.6, boundary exact / one paisa over |
| `test_a_credit_sale_dated_before_the_opening_balance_is_refused` | D4 |
| `test_an_opening_balance_after_earlier_entries_exist_is_refused` | D4, other direction |
| `test_an_entry_on_the_opening_balance_date_itself_is_allowed` | the boundary is strictly *before* |
| `test_a_cash_repayment_without_a_shift_is_refused_by_the_database` | D1 at the CHECK, not only the API |
| `test_a_bank_transfer_can_be_recorded_against_a_locked_day` | **the blocker this phase exists for** |
| `test_a_card_repayment_on_a_shift_no_longer_invents_a_surplus` | **the bug**, §6.4's worked example |
| `test_a_shift_less_card_repayment_touches_no_cash_term` | D1's sharp edge |
| `test_the_ledger_carries_a_running_balance` | D5 |
| `test_the_ledger_is_ordered_by_business_date_not_entry_order` | §4.7 |
| `test_truncation_does_not_corrupt_the_balances_it_does_show` | §13.31's whole argument |

Frontend, structural only (§13.18 — there is no behavioural runner, and adding one means npm):

- `test_every_router_is_reachable_from_a_screen` — a new router fails this twice over
- `test_no_money_value_is_parsed_into_a_float` — sharpest here, since a ledger is nothing but money arithmetic
- `test_every_named_import_resolves_to_a_real_export` — the real guard on moving a screen between files

---

## §11 — What shipped, where it differs

**`reverse_opening_balance` is hand-rolled rather than delegating to `cash.append_reversal`.**
The plan said it would use the shared implementation. It cannot: `app/services/cash.py` imports
`app/services/credit.py`, so importing back is a cycle. The duplication is four lines; the
alternatives were an import cycle or a third module holding one function. Noted in the
docstring so the next reader does not "fix" it.

**One existing cash-engine test asserted the old, wrong behaviour**, and its scenario was the
only reason it passed: `test_a_cash_repayment_raises_expected_cash_but_a_upi_one_does_not` gave
the shift a ₹9,000 UPI repayment and **no UPI collection to net it against**. It is now
`test_only_a_cash_repayment_reaches_the_drawer`, carries the collection, and asserts the two
cancel — which is what "does not reach the drawer" actually means.

**Three ledger cursor tests were replaced, not deleted.** D5 removed the contract they tested,
so they were swapped for tests of the contract that took over, including the truncation case
that is D5's entire argument.

**The hub sorts without arithmetic.** §14 forbids `parseFloat` and a structural test enforces
it, so `compareMoneyStrings` compares the strings by sign, then digit count, then lexically —
correct for fixed two-decimal-place values, and it needs no `Number()` anywhere.

**Two CSS classes were invented and then removed.** `card-tappable` and `stack-tight` did not
exist in `app.css`. The first became `days.js`'s pattern — a `div.card` with a `btn-plain`
inside, since a `<button>` wrapping that much structure reads as one enormous control to a
screen reader. The second became `list-row-value`, which already did the job.

**A shift-less repayment could not be reversed at all, and that was found by using the app.**
The reversal route is `/shifts/{shift_id}/credit-repayments/{id}/reversals`; a row with
`shift_id IS NULL` can never reach it. §6.9 makes a reversal the *only* correction path, so a
mistyped bank transfer would have sat in a customer's ledger permanently — the exact failure
§6.9 exists to prevent, introduced by the feature that made those rows possible.

`POST /credit-repayments/{id}/reversals` closes it, and **refuses a repayment that does have a
shift** (409 `REPAYMENT_BELONGS_TO_A_SHIFT`). That refusal is the load-bearing half: the
shift-scoped route applies §5.2's locked-shift rule, and a second route reaching the same rows
without it would be a way around the check rather than a convenience. Every row is reversible
through exactly one path.

**Verified against the dev database**, not only the test suite: an opening balance set and a
second refused, a bank transfer recorded against a date whose shift is `locked`, the ledger's
running balance correct, and 2 July's cash position byte-identical afterwards — gap still ₹1.25,
both repayment terms ₹0.00. The probe rows were then deleted, because a fake ₹12,400 against a
real customer's name is worse than no data at all.
