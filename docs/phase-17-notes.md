# Phase 17 — Notes

> The learning reference, not a changelog. The plan for this one lived in the conversation
> rather than in a `docs/phase-17-plan.md`; this is what the building of it taught.

---

## 1. The third phase caused by a user, and the sharpest one yet

Phase 15 came out of *"I did that, and I can't see the day."* Phase 16 came out of *"before I
feed any of these values, I need the ledger to say what they actually owed."* This one came
out of a screenshot and a sentence: *"this cash position part now shows 60k cash surplus
which is exactly the amount spent thru cash."*

Three phases in a row have now been caused by somebody entering real trading days, and none
of the three was found by a test. That is worth saying plainly rather than treating as a run
of bad luck: **the suite tests that the equations compute what they were written to compute,
and every one of these defects was in what they were written to compute.** A test cannot find
that. Only a person with a locker and a paper register can.

## 2. The first diagnosis was wrong, and the wrongness had a shape

I said, before reading any code, that the app was double-subtracting expenses — once in
`accountable_cash` and once in `expected_closing`. It was a plausible story that fit the
symptom exactly: the surplus equalled the expense total, and a double-subtraction would
produce precisely that.

It was also wrong. `DayTotals.cash_sales` deliberately excludes expenses (`cash.py:549-557`)
and `expected_closing` subtracts them exactly once (line 648). Reading the code took four
minutes and killed the theory outright.

**The user had already told me it was wrong and I did not hear it.** He said the expected
closing "matches perfectly" in the Today tab — which, under a double-count, it could not
have. The observation that would have falsified my hypothesis was sitting in his message
before I formed it.

The lesson is not "read the code first", which is obvious and which I would have claimed to
believe already. It is that **a hypothesis that explains the number exactly is the most
dangerous kind**, because the arithmetic coincidence feels like confirmation. ₹60,169
matching the expense total was evidence for *some* relationship between the two figures, and
I read it as evidence for the specific one I had guessed.

## 3. The code said what was wrong with it, in English, in a docstring

`shift_cash_position`'s docstring already contained this:

> Cash repayments and settlements are added; cash expenses are subtracted, because all three
> physically pass through the salesman's hands during the shift and are therefore already
> inside the figure he declares.

That is not a description of the arithmetic. It is a **stated assumption about the physical
world**, and the entire bug is that the assumption is false on days when the office pays a
bill from the locker. The docstring was not wrong when it was written and it was not
misleading; it was load-bearing, and nobody had gone back to ask whether the world still
matched it.

This project's habit of writing down *why* is what made a fifteen-minute diagnosis possible.
It also suggests something to do more of: **an assumption stated in prose is a testable
claim about reality, and it deserves to be re-checked whenever reality reports a surprise.**

## 4. The arithmetic was never wrong; the vocabulary was missing

`expenses` could say *how* money left (`mode`, since Phase 7) and could not say *whose pile
it left from*. Every figure computed from that table was correct given what the table could
express. The defect lived in the gap between what the schema could say and what had actually
happened.

Phase 7's own note makes the identical argument one column earlier — without `mode`, a
bank-paid electricity bill read as a hole in the drawer. Same table, same shape of gap, same
consequence (a phantom in a salesman's name), eight phases apart. **A column that cannot
express a real distinction produces confident wrong answers, not errors.**

## 5. One equation changed, and proving the other did not was the harder half

The temptation was to treat "expenses shouldn't be subtracted here" as a single fix. It is
not: the two equations genuinely disagree about this term, and both are right.

- `accountable_cash` — *what should this person be holding?* Locker money was never his.
- `expected_closing` — *what should be in the locker?* The locker is lighter by all of it.

So the change had to be surgical, and the test that matters most is not the regression test
for 30 July but `test_expected_closing_is_unchanged_by_paid_from`. Two days, identical but
for `paid_from`, must produce the same movement. §6.5 chains days, so a change leaking into
that equation would not have stayed local — it would have moved every opening balance after
it, silently, exactly as §13.32 describes.

**Writing two named service functions rather than one with a boolean is what makes the
distinction survive.** `cash_expenses_total` and `shift_funded_cash_expenses_total` force
every call site to say which question it is asking. A flag would have hidden that behind an
argument nobody reads at the call site.

## 6. The reversal was the bug I nearly shipped

`reverse()` copies `mode` explicitly onto both the reversal and the replacement. I added
`paid_from` to the model with a server default and moved on — and a defaulted reversal of a
locker-funded bill would have credited the salesman ₹60,170 he never held. **The same phantom
this phase exists to remove, in the opposite direction**, and it would have looked like a
correction working properly.

What caught it was going back to read `reverse()` because `mode` was there, not because
anything failed. Nothing failed: the suite was green, because no test reversed a locker-funded
expense. A default that is right for creation is not automatically right for inheritance.

## 7. A default is a decision about human behaviour, not about data

`paid_from` could have been required, matching §6.8's *"an answer, never an omission"*. It is
not, and the reasoning is §6.11's: demanding an answer for a ₹20 chai run is friction, and
friction teaches people to click through. A reflexive answer is worse than a default, because
it looks considered.

The cost is real and is now §13.33: **nothing can check this column.** A ₹60,000 bill left on
the default reproduces the exact bug. Two things blunt it — the screen shows the excluded
amount as its own line rather than netting it silently, and a gap is still never a debt until
a manager books it. Neither eliminates it, which is why it is written down as an
approximation rather than described as solved.

## 8. What this leaves for next time

**The 30 July rows still need re-tagging by hand.** Deliberately not a data migration: only
the owner knows which bills came from the locker, and a migration that guessed would be
inventing history. This is the one piece of the phase a person has to finish.

**`salesman_shortfall_settlements` has the same latent shape.** §13.15 already records that it
has no `mode` column because every settlement is cash today. If a settlement is ever paid from
somewhere other than the salesman's own pocket, the same question arrives there.

**Three user-caused phases in a row suggests the next one is out there too.** The pattern in
all three: a figure the system was confident about, that a person with the physical facts
could see was impossible. Worth asking the owner directly what *else* looks wrong, rather than
waiting for the next screenshot.
