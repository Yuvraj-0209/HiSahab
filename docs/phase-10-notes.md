# Phase 10 — Cash Engine: Notes

> The narrative counterpart to `phase-10-plan.md`. The plan says what was built; this says
> **why**, and records what went wrong on the way. Written to be read six months from now by
> somebody trying to understand a decision, not as a changelog.

---

## 1. What this phase was actually for

Every phase since 5 has been assembling terms for one equation and deliberately refusing to
compute it. Phase 10 computes it.

That framing matters because it explains why so much of this phase was **arithmetic
archaeology** rather than new construction. Three of the four hardest problems here were not
"how do I build this" but "the spec says two things that cannot both be true, and both look
right until you work an example".

---

## 2. The three places §6.4 was wrong

This is the part worth reading twice. §6.4 has been in `CLAUDE.md` since the beginning and
has been quoted approvingly by five phases. Two of its terms were wrong and one was missing.

### 2.1 `other_cash_income` was on the wrong side of the equation

The original line read:

```
+ other_cash_income           ← non-fuel sales (V1: manual entry)
```

on the **cash** side. That is correct only if every non-fuel sale is paid in cash.

Take a ₹500 bottle of oil bought **by card**, on a day of ₹95,000 metered fuel, ₹20,000 of
fuel on the card machine, ₹10,000 UPI and ₹5,000 udhaar. The card machine reads ₹20,500 —
fuel plus oil. The salesman is holding ₹60,000.

| | |
|---|---|
| Truth | ₹60,000 |
| Cash-side term | `95,000 − 20,500 − 10,000 − 5,000 = 59,500`, plus ₹0 cash oil → **₹59,500** |
| Sales-side term | `(95,000 + 500) − 20,500 − 10,000 − 5,000` → **₹60,000** |

The cash-side form understates derived cash by exactly the value of every non-fuel sale the
customer did not pay cash for — which shows up as the salesman having a **surplus** in his own
name, every day he sells a bottle of oil to somebody paying by card.

The sales-side form is also right in the all-cash case (`(95,000 + 500) − 20,000 − 10,000 −
5,000 = 60,500`, which is what he holds). **It is correct regardless of payment mode**, which
is why `non_fuel_sales` needs no `mode` column at all — the collections rows already record
how the money arrived.

The generalisable lesson: when a term can be paid through more than one channel, putting it
on the *sales* side is unconditionally right, and putting it on the *cash* side is right only
by coincidence.

### 2.2 There was no term for a booked shortfall

§14 has always said this outlet books a cash shortfall as udhaar against the salesman's own
name. §6.4 had no way to express that.

Monday: the meters imply Ramesh should hand over ₹50,000. He declares ₹49,500. A manager
books ₹500 against him. The locker physically gains ₹49,500 — but §6.4 as written adds the
**derived** ₹50,000, so `expected_closing` says ₹50,000 and Tuesday opens ₹500 rich.

That ₹500 is now an asset **twice**: once as Ramesh's debt, once as cash that is not in the
locker. Every count from then on is off by it, and nothing in the system explains why.

`− shortfalls_booked` fixes it. Two things about the fix are worth noting:

* `derived − shortfall` is algebraically identical to just using the declared figure. That
  identity is the reassurance the change is arithmetic rather than a fudge — the two ways of
  thinking about the day agree.
* It is written as a **subtraction** deliberately. §14 forbids summing the `cash` collection
  row into a derived figure, and the subtraction form means the equation **never reads that
  row at all**. The `cash` row stays what §5.2 says it is: the independent observation the
  derived figure is checked against.

`test_a_booked_shortfall_reduces_expected_closing_by_exactly_its_amount` is the most
important test in the phase.

### 2.3 §6.5 assumed a drawer; this outlet has a locker

§6.5 said day N opens at day N−1's `actual_counted`, and that a day whose predecessor had no
count **cannot be finalised**.

§14 records the owner's actual answer to "who counts the cash, and when?": *there is no fixed
counting moment.* The locker carries a running balance that rolls forward on any day with no
bank deposit.

Both cannot hold. As written, §6.5 would have blocked finalising **every day, forever** — the
system would have been unusable on day one.

The fix is §4.7's chain applied to money:

```
opening(N) = actual_counted(N−1)      if the locker was counted
           = expected_closing(N−1)    if it was not
           = an admin's figure        if there is no N−1 at all
```

§6.5's actual principle survives intact: **the count wins wherever there is one**, so a ₹200
shortage stays visible in that day's variance and is absent from the next day's opening. A
physical count becomes an occasional **audit that re-anchors the chain**, exactly as a
confirmed meter reading re-anchors §4.7's.

`PRIOR_DAY_NOT_RECONCILED` kept its code and narrowed its meaning to "the previous day is not
finalised". `opening_balance_source` (`seeded | counted | carried`) records which branch ran,
so a reader never has to infer it — which does not work for the first day anyway.

**One detail that is easy to get wrong:** "the previous day" means *the most recent summary*,
not literally `business_date − 1`. This outlet is shut on some days. A chain built on
yesterday's date snaps on the first gap. That is the same reasoning §4.7 gives for looking up
"the most recent closing reading **for that nozzle**" rather than "the previous shift's" —
the second time this exact mistake was available and the second time the spec had already
warned about it in a different table.

---

## 3. The blocker that would have made this phase useless

`shift_sales` — the only producer of `total_sales` — called `pricing.rate_at` **and**
`pricing.margin_at`. `margin_at` raises 409 `NO_MARGIN_FOR_DATE` when no margin row exists,
and §14 records that **petrol and diesel dealer commissions have never been entered at this
outlet**.

So a cash engine built on it would have refused to reconcile every petrol day, on day one,
because of reference data that has nothing to do with cash.

This was not a subtle discovery — `tests/test_sales_valuation.py` already had a test pinning
that 409, written in Phase 5, whose docstring says *"this is the state the outlet is genuinely
in today"*. The test was right there and the implication had never been followed through.

The fix is `shift_sales(..., price_only=True)`: no margin lookup at all, and
`margin_per_unit` / `profit` come back as `None` — **never `0`**, which §13.7 says is exactly
the plausible-but-wrong number this project exists to prevent. The test asserting that uses a
fuel that *does* have a margin, so it proves the value was withheld rather than merely absent.

§6.8 had already made this argument for close preconditions:

> *"a close precondition that inherited that would make every petrol shift unclosable because
> of a reference-data gap, which is a very confusing way to be told about a missing margin."*

Reconciling the drawer is the same argument one phase later. The transferable lesson is that
**a refusal belongs to the question that needs the data, not to the function that happens to
fetch it.**

---

## 4. The Phase 9 audit, and why the third finding mattered most

The standing habit: audit the previous phase before planning the next. P6 found 3 defects, P7
found 3, P8 found 1, P9 found 8. This found 3.

**P9-1 — `may_read`'s joins were never exercised as `True`.** The existing test
`test_an_attendant_can_read_a_receipt_linked_to_their_own_shift` uploads *as the attendant*,
so `may_read` returns at the `uploaded_by` branch and neither join runs. It proved the
uploader rule while reading as though it proved the linked-to-my-shift rule. The credit-sale
join Phase 9 added for exactly that case had **no test at all** — the Phase 9 checklist marked
the item done. Both tests now use the case §7.3's docstring describes: a manager uploads on
the attendant's behalf, which is how a day typed in after the fact actually happens.

*Lesson: a test that passes for the wrong reason is worse than a missing one, because it
occupies the space where the real test would go.*

**P9-2 — page totals summed the truncated list.** `credit_repayments.py` computed `total` and
`cash_total` inline over `rows[:_MAX_ROWS]`. Every sibling router already aggregates in SQL
over the whole shift; this one file diverged. `cash_total` is a term of §6.4 and Phase 10 was
about to consume it — an understated one makes the salesman look like he is holding less than
he is.

**P9-3 — a leaked connection could hang the suite indefinitely.** `test_uploads_api.py`
called `engine.connect()` twice without a context manager. The connection stays *idle in
transaction* holding a shared lock on `attachments` until garbage collection, and
`test_migration_is_reversible` — which downgrades to base **mid-suite** — then blocks on
`DROP TABLE`. It was latent until the two tests added for P9-1 shifted the ordering; the full
suite then ran past 600 seconds and `pg_stat_activity` showed the block.

Phase 10 adds five tables to that same mid-suite downgrade path. This would have surfaced
within days and looked like the new migration's fault.

*Lesson: the audit's value is not only the defects it names in advance. Two of these three
were found by the act of writing tests for the first one.*

---

## 5. Writing the reversal once

Phases 6–9 wrote `reverse` four times: `collections`, `expenses`, `credit_sales`,
`credit_repayments`. Phase 10 adds four more tables carrying §6.9's shape.

Eight near-identical copies of the rule that decides whether a correction is recorded honestly
is not a style problem. `0012` paid for it in miniature: a quantity CHECK written **four lines
below** an already sign-aware amount rule, and still not sign-aware itself, because it was
copied rather than shared. It only failed when a test combined two features — a fuel credit
sale *and* a reversal.

So `cash.append_reversal` is written once and used by all four of Phase 10's tables. Two
design choices in it are deliberate:

* **`carry` is an explicit list of column names**, not "every column except a few". The
  exceptions are what matter — copying `id` or `created_at` would be wrong — and a blanket
  copy would silently start carrying whatever a future migration adds, including something
  that should not be inherited.
* **The four older implementations are not retrofitted.** They are tested, they work, and
  rewriting the correction path of every financial table in the codebase is its own change
  with its own tests, not a side effect of adding a table.

---

## 6. Two mistakes of mine that the tests caught

Worth recording because they are the two shapes of error this project is most exposed to.

**An arithmetic slip in a test's own expectation.** The full-equation test asserted
`expected_closing == 25900.00`; the terms give `24,900`. I had written the docstring table and
then computed as though `cash_sales` were ₹65,500 rather than ₹64,500. The code was right and
the expectation was wrong — which is the direction that test exists to distinguish, and the
reason it lists every term rather than only the total.

**A structural test tripping on the comment explaining the rule it checks.**
`test_the_salesman_is_never_taken_from_a_booking_payload` searched the source of
`ShortfallCreate` for `salesman_id`. The class carries a comment saying *"No `salesman_id`.
Read from `shifts.attendant_id`"* — so the text search failed on the comment documenting the
very rule. Left as a text search, it would have taught the next person to delete the comment.
It now parses declared `AnnAssign` fields.

---

## 7. The decisions that carry a person's name

`salesman_shortfalls` is the only table in this codebase whose rows are a debt against a named
employee. Three choices follow from that, and all three are §4.7's argument transplanted:

> *"an assumed opening converts theft into a debt owed by someone who did nothing wrong"*

* **A gap is not a debt until a human books it.** `GET /shifts/{id}/cash-position` computes
  and writes nothing; a **manager** books, with a mandatory reason. A ₹500 gap is more often a
  mistyped reading, a forgotten UPI figure or an unrecorded udhaar slip than it is theft, and
  software must not be the thing that decides which. A test asserts the read endpoint writes
  no shortfall row and no audit row.
* **`salesman_id` is never accepted from a client on a booking.** It is read from
  `shifts.attendant_id` — §5.2's "exactly one name carries the drawer" — and a payload
  carrying one is refused outright rather than ignored. There is no second source of truth
  that would catch a typo putting a debt on somebody who was not even working. A *settlement*
  is the exception and names the salesman, because money can arrive on a later shift worked by
  somebody else.
* **`computed_gap` and `amount` are both stored and may differ.** A manager may know part of a
  gap is a slip he has already corrected. A divergence **warns and writes**, never refuses —
  §6.8's reasoning: overruling a human's judgement sends the correction outside the system,
  where nothing can see it at all. §4.7's predict-and-confirm shape, applied to a debt.

Booking is also refused outright when nobody has declared cash (409 `NO_CASH_DECLARED`). §6.8
keeps "nothing declared" and "declared zero" apart precisely so this is answerable, and
booking against a null gap would be inventing the salesman's half of the comparison.

---

## 8. Two rules that met without either one intending it

While writing Step 9's tests I had a test named *"a reopen flags a finalised day too"* that
never actually reopened a shift. Working out how to make it do so showed the case **cannot
arise**:

* finalising a day requires every shift on it to be `locked`;
* §6.8 makes `locked` **terminal** — an admin may move `closed → open`, never `locked → open`.

So a shift beneath a frozen day is refused with `SHIFT_LOCKED` before any flag logic runs, and
§13.16's flag can only ever fire on a summary that has been created but not yet finalised.

Neither section mentions the other. It is a happy accident of two independent rules, so it is
now pinned by a test that fails if either half changes — at which point whoever changed it has
to decide what a reopen means underneath a frozen day.

*Lesson: when a test is hard to write, the difficulty is sometimes telling you the scenario is
impossible rather than that the test is hard.*

---

## 9. Verification

```
pytest                                 ->  1135 passed  (931 before Phase 10)
pytest, second run, same database      ->  1135 passed
100% coverage on all ten Phase 10 modules
alembic check                          ->  no new upgrade operations, head 0013
alembic downgrade base && upgrade head ->  clean; zero leaked enums, zero leaked tables
```

Structural assertions no value test could make:

- `daily_cash_summaries` carries its own `outlet_id`; the four shift-scoped tables
  deliberately do not (§5.0), asserted in both directions.
- `salesman_shortfalls.salesman_id` targets **`user_profiles`**, asserted against
  `information_schema` — plus a negative sweep proving *no* cash-engine table references
  `credit_customers` at all, so a future column cannot smuggle the link back under a different
  name.
- `variance` is `is_generated = 'ALWAYS'`, and is NULL when nothing was counted.
- All four new reversal quartets exist by name, and the sign rule is **strict** (`> 0` / `< 0`),
  not `>=` — a ₹0 deposit, non-fuel sale, shortfall or settlement records nothing.
- `expected_closing` and `day_totals` never mention `declared_cash` or `CollectionMode.cash`,
  parsed per-function — because `shift_cash_position` legitimately *does* read the declared
  figure, and grepping the file would confuse the comparison with the equation.
- `non_fuel_sales_total` appears in `cash_sales` and **not** in `expected_closing`.
- `float(` / `sa.Float` / `.offset(` / `@router.delete` / `relationship(` — zero matches across
  every Phase 10 module.

---

## 10. Open items, carried forward

**New, and load-bearing:**

- **There is no way to write off a shortfall.** The answer was cash repayment only, so a ₹20
  gap nobody will ever chase stays on a salesman's balance permanently and the balance only
  ever grows. It is a `mode` column plus a filter, and cheap only while the table is small.
  §13.15 records it; recommend revisiting before the first quarter closes.
- **Petrol and diesel dealer margins are still not entered.** §6.3's split means a missing
  margin no longer blocks reconciliation — but **profit reporting still covers CBG only**, and
  Phase 13 is built on it.
- **The first opening balance must be seeded before anything can be finalised.** Somebody has
  to count the locker once, on a known date, and an admin has to enter that figure.
- **Does a salesman hold a change float overnight, and is it counted separately?** If the float
  is inside the locker figure it is fine; if it is in his pocket, every count is short by it.
- **Is a surplus ever booked?** A negative gap is reported and refused a record type. If a
  salesman consistently declares more than the meters imply, that is a signal too and it
  currently has nowhere to go.

**Carried forward from earlier phases:** Phase 9's `credit_customers` tenancy posture (403 not
404); the real customer list and their credit limits; `EXPENSE_RECEIPT_THRESHOLD` = ₹5,000 is
still a guess; the real expense category list; CBG's real max flow rate in kg/min; whether
testing quantities are recorded on paper.

---

## 11. The check no test replaces

**Reconcile one real day, end to end, against the paper register.** Enter the real readings,
the real UPI and card figures, the real udhaar, the real expenses, and the real cash
declaration. Read the cash position. Compare the derived figure to what the salesman actually
handed over.

Then **before booking anything against a real person's name, confirm the shortfall figure with
the owner.** That is the number with a human consequence, and §4.7's whole argument is that it
must be a question before it is a debt.

Then **count the locker once, physically, and enter it.** Confirm the variance is a figure the
owner recognises and can explain, and that the next day opens at the counted figure rather than
the arithmetic one.

Finally, **run three consecutive days — one uncounted, one counted, one uncounted** — and
confirm the opening balances chain the way §6.5 now says they do.

The arithmetic is tested to the paisa and the coverage is 100%, and neither fact says the
numbers mean what the owner thinks they mean. `expected_closing` dropping when a shortfall is
booked is the assertion to check hardest: if that reads wrong to him, the whole equation needs
revisiting before real money touches it.
