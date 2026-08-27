# Phase 16 — Notes

> The learning reference, not a changelog. `docs/phase-16-plan.md` is what was decided and
> why; this is what the building of it taught.

---

## 1. The second phase caused by a user, and the same shape as the first

Phase 15 came out of somebody saying *"I did that, and I can't see the day."* This one came
out of somebody saying *"before I feed any of these values, I need the ledger to say what
they actually owed."*

Both are the same kind of report: **nothing was broken in the sense the test suite
understands.** 1499 tests passed before Phase 15 and 1520 before this one. `outstanding`
computed exactly what §6.6 specifies. The repayment endpoint validated exactly what it
claimed to. Every figure was correct against its own definition.

What was wrong sat one level up — in what the definition *assumed about the world*. §6.6 says
outstanding is sales minus repayments, and that is a complete definition of a ledger **that
starts when the software starts**. Nobody had written down that this pump's ledger started
years earlier, so nobody had noticed the sentence was missing a term.

The generalisable form: a specification can be internally complete and still be silent about
its own starting conditions. Phase 6's opening reading, §6.5's seeded opening balance, and now
this are the same omission three times — and the codebase already had the answer twice.

## 2. The bug found by reading, not by testing

The card-machine defect was not reported. It surfaced while reading `cash.py:456` to work out
where a bank-transfer repayment should *not* go — and the question "which repayments are inside
`card_total`?" had an answer nobody had asked for.

Worth noticing how it hid. Every ingredient was documented:

- §5.2 says a `collections` row is one lumped figure per mode, read off the terminal.
- §5.2 says `credit_repayment_mode` includes `card`.
- §6.4 subtracts `card_collections` from `total_sales`.
- §6.4 works through the *exact same arithmetic* for a card-paid bottle of oil, and states
  the fix.

Four correct statements, and the contradiction between them lived in nobody's head. The
oil example is the tell: it is the same defect, and the document solved it once for
`non_fuel_sales` without noticing that `credit_repayments` had the identical shape.

**I also got the direction wrong the first time.** In conversation I said it would produce a
phantom *shortfall*; it produces a phantom *surplus*. `gap = accountable − declared`, and the
term is missing from `accountable`, so the derived figure is too low and the salesman appears
to hold more than the system can explain. Worth recording because a sign error in an argument
about a sign error is exactly the kind of plausible-but-wrong this project is about — and the
thing that caught it was writing the worked example out with real numbers, not re-reading the
reasoning.

## 3. The presence of a column can carry a rule

The neatest thing in this phase, and it was not the first design.

The problem: a bank transfer must reach the ledger and no cash equation. The obvious solution
is a flag — `reaches_the_drawer`, or a mode-to-side mapping table. Both are a second source of
truth for something already recorded.

What actually works is that **every §6.4 sum is already `WHERE shift_id = :shift_id`**. Make
`shift_id` nullable and the rule enforces itself: a row with no shift matches no shift's sum,
by construction rather than by a filter somebody has to remember to write. One sentence
covers it — *a repayment with a shift arrived at the pump; one with only a date arrived at the
bank* — and the sentence is the implementation.

The generalisable form: before adding a field to express a rule, check whether an existing
field's *nullability* already expresses it. A rule enforced by structure cannot be forgotten by
a future query.

## 4. Two ways of saying nothing, and the button that tells them apart

`opening_balance: null` and `"0.00"` both make `outstanding` come out at ₹0.00. They are
completely different facts: one means nobody has looked at this customer, the other means
somebody checked and found them square.

§6.8 already had the rule — *"zero as an answer, never zero as an omission"* — for the cash
declaration, and §14 already forbade `?? 0` in the client. What this phase added was noticing
that **the rule needs an affordance, not just a guard**. If the only way to record "they owed
nothing" is to type `0` into a field that also accepts being left blank, the two states are one
keystroke apart and the distinction is theatre.

So the sheet has a separate *They owed nothing* button. Saying it is a deliberate act, which is
the whole point of the distinction.

## 5. A CHECK that cannot be stated is worse than no CHECK

Every money table here carries
`(reverses_id IS NULL AND amount > 0) OR (reverses_id IS NOT NULL AND amount < 0)`. It was
tempting to copy it onto `credit_opening_balances` for consistency.

It cannot be written. §6.6 permits a negative outstanding, so an original may be negative and
its reversal positive — the sign genuinely carries no information about which row is which.
The options were to refuse a real balance, or to weaken the constraint into something that says
nothing while looking like a control.

The right answer was to leave it out **and write down why**, in the migration, the model and
the spec. A missing constraint with three explanations is a decision; a missing constraint with
none is an oversight, and nobody reading later can tell them apart.

## 6. Replacing a test is not the same as deleting one

Three ledger tests asserted cursor pagination, which this phase removed. One cash-engine test
asserted that a UPI repayment left `expected_closing` untouched, which this phase made false.

The temptation in both cases is to delete and move on, because the code is right and the test
is stale. What that loses is the *reason the test existed*. Each was replaced by a test of the
contract that took over — the running balance, the truncation guarantee, the netting — so the
coverage moved rather than evaporating.

The cash-engine one is the sharper case, because **it was passing for the wrong reason**. Its
scenario gave the shift a ₹9,000 UPI repayment and no UPI collection to net against, so the
missing term never showed. A test whose green depends on an unrealistic fixture is not testing
the rule it names.

## 7. Verify against the real database, then clean up after yourself

The suite passed and the app booted, and neither of those would have caught a wrong `TZ_DISPLAY`
default or a screen requesting a field the router does not send. Driving the real endpoints with
a real token against the dev database took five minutes and confirmed the three things that
actually mattered: the locked day accepted a bank transfer, the ledger's running balance was
right, and **2 July's cash position was byte-identical afterwards** — gap still ₹1.25.

Then the probe rows were deleted. A ₹12,400 opening balance against a real customer's name is
worse than no data, because it is exactly plausible enough to be believed. §3 rule 6's
no-hard-deletes protects *real* financial records; it was never a licence to leave fabricated
ones behind.

## 8. What this leaves for next time

- **The card-total contract is now load-bearing and undefended.**
  `card_upi_repayments_total` is correct *because* `collections.mode = 'card'` means the
  machine's whole-day total. Nothing enforces that — it is a data-entry habit. If a salesman
  ever nets settlements out of the card figure himself, the term double-counts. Worth a
  sentence on the collections screen the next time it is touched.
- **Petrol and diesel margins are still not entered.** Unchanged from Phase 13, and still the
  reason profit reporting covers CBG only.
- **§13.15's shortfall write-off is still open**, and the ledger work here makes the parallel
  sharper: a salesman's outstanding grows forever for the same reason a customer's used to
  start at zero — a missing term for money nobody will ever chase.
