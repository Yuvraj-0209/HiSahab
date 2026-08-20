# Phase 5 — Nozzle Readings & Sales Math: Notes

> The phase where the system starts producing money figures. Written as a learning
> reference, not a spec. For the authoritative rules, see `CLAUDE.md`.

---

## 1. The big picture

Phases 1–4 were scaffolding: who you are, what you sell and for how much, which day and
shift you are recording. **Phase 5 is the first phase whose output is rupees.**

### What shipped

| Piece | File(s) |
|---|---|
| Spec amendment (4 sections, its own commit) | `CLAUDE.md` §4.7, §5.2, §6.10, §13.10 |
| Migration `0005` — 1 table, 7 constraints, 3 indexes, no trigger | `alembic/versions/0005_nozzle_readings.py` |
| ORM model | `app/models/reading.py` |
| **The arithmetic, pure** | `app/services/sales.py` |
| The chain, and pricing a shift | `app/services/readings.py` |
| 6 routes | `app/api/v1/readings.py` |
| §6.8 close precondition + §13.10 resolution | `app/api/v1/shifts.py` |
| 136 new tests, 399 total, 100% on every Phase 5 module | `tests/test_sales_math.py`, `test_reading_chain.py`, `test_readings_api.py`, `test_sales_valuation.py`, `test_reading_permissions.py`, `test_migrations.py` |

---

## 2. Why the arithmetic lives in a file with no database in it

`app/services/sales.py` takes Decimals and returns Decimals. No `Session`, no ORM row, no
clock. Everything database-aware is in `readings.py` next door.

That split is not tidiness. §6.2 is the most consequential arithmetic in the project and
§10 demands it be tested exhaustively — so it had to be written somewhere a test could
drive every branch with a tuple of numbers. `tests/test_sales_math.py` has 37 tests and
runs in 0.29 seconds, because it never touches Postgres. Had the formula lived inside the
endpoint, each of those cases would have needed a user, a shift, a nozzle and a price, and
the ones that are tedious to set up would quietly not have been written.

The parametrised sweep at the bottom of that file is the clearest payoff:

```python
@pytest.mark.parametrize("opening,closing,testing,rollover", [...8 boundary rows...])
def test_no_input_combination_produces_a_negative_quantity(...):
```

§6.2 says *"quantity_sold must never be negative. If a code path can produce one, that path
is wrong."* That is a claim about **all** inputs, and it is only cheap to check as one
because the function is pure.

---

## 3. The spec could not express §4.7, so the spec changed first

§5.2 defined `nozzle_readings` with nine columns. §4.7 demands the carried opening be
*confirmed against the physical meter*, with a mismatch path that captures the real reading
and raises it for review.

**Those nine columns cannot do that.** There is nowhere to put what the chain predicted, so
a confirmed reading and an assumed one are indistinguishable the instant they are written.

§14 says *"Ask before adding any table, dependency, or column not listed here"*, so Step 0
was a spec commit — the same pattern as `22e8339` before Phase 3. Six columns added:

| Column | Without it |
|---|---|
| `chained_opening_reading` | A mismatch leaves no trace. This is the whole of §4.7. |
| `opening_variance_reason` | A mismatch is recorded but unexplained. |
| `requires_review` | Nobody is ever asked to look. |
| `reviewed_by` / `reviewed_at` / `review_note` | A flag with no way to clear it is a flag nobody looks at twice. |

The last three deliberately mirror `expenses` (§5.2). One review vocabulary, not two.

### Why the two opening values are kept side by side

An opening reading that was *carried and confirmed* and one that was *typed because the
meter disagreed* are different facts with different consequences, and a single column
collapses them. §4.7 spends four paragraphs on why that matters, and none of them are
technical:

> Fuel siphoned through a nozzle between shifts still moves the totalizer — but an assumed
> opening says it did not, so the missing quantity is absorbed into the next shift as sales
> that produced no cash. The shift then comes up short, and this outlet books a shortfall
> as **udhaar against the salesman's own name.**

So an assumed opening does not merely lose data. **It converts theft into a debt owed by
somebody who did nothing wrong**, and leaves nothing pointing at the gap. Two columns and a
CHECK constraint is a small price for that not happening.

---

## 4. The contradiction found while planning, and how it was resolved

This was the most important decision in the phase, and it was a decision *not* to build
what the spec asked for.

**§13.10 said:** Phase 4's mid-chain reopen restriction is *"lifted when Phase 5 implements
the cascade"* — i.e. recompute the following shift's carried-forward opening.

**§4.7 said:** the chained opening is *"stored on the row, not computed on read"*, because
*"computing it would mean that correcting one shift silently rewrites the next shift's
history"*.

**A recomputing cascade is exactly that rewrite.** Implementing §13.10 literally would have
broken §4.7 — and worse than the abstract violation, the recomputed figure would have
looked *identical to a reading somebody had confirmed against a physical meter*. The system
would have been manufacturing confirmations.

§14 says to flag it loudly when two rules contradict. Flagged, and resolved the other way:

```python
def flag_downstream_reading(db, *, shift, nozzle_id, note):
    """...what it does NOT do is the point: it does not touch `opening_reading`."""
```

Reopen freely. If a reading's **closing** value then changes, the next shift's opening is
left visibly stale and flagged with a note naming the shift that moved beneath it. A human
reconciles two numbers they can both see. Nothing is invented.

`NOT_THE_LATEST_SHIFT` is gone. `CLAUDE.md` §6.8 and §13.10 were rewritten to match, and
`test_only_the_latest_shift_can_be_reopened` was **rewritten rather than deleted** — with a
docstring saying it used to assert the opposite and why that changed. A reader comparing
the tests against the spec needs to know the behaviour moved deliberately.

Note what is *not* flagged: a change to `testing_quantity` or the flags. Those change what
was **sold**, not what the **meter read**, and the chain carries the meter reading. A review
flag nobody needs is a review flag nobody reads.

---

## 5. Three guards that are easy to get subtly wrong

### 5.1 `testing_quantity` under a rollover

§6.2 words the guard as `testing_quantity > (closing − opening)`. Taken literally, that
expression is **negative** in the rollover case, so the check would fire on every rolled-over
reading including correct ones.

`sales.py` compares against gross throughput instead — the same number in the normal case,
the right one in both. A deliberate, narrow deviation from the literal text, commented at
the site and pinned by `test_testing_is_also_subtracted_under_a_rollover`.

### 5.2 The ceiling needs a shift duration that does not exist yet

§6.2's sanity ceiling is `max_flow_rate_per_minute × duration`. But `ended_at` is nullable
until close. Refusing a reading because a field the caller never sent is missing would be
user-hostile.

So the check runs **twice**: at entry when `ended_at` is known, and again for every reading
at close, where Phase 4's `SHIFT_END_TIME_REQUIRED` guarantees it. A mistyped extra digit is
caught at entry in the normal case and at close in every case.

`revalidate_flow_rates` deliberately skips a reading carrying an admin override. Holding a
human's signed figure against a mechanical flow rate would refuse the one path that exists
for when the meter itself lied.

### 5.3 A meter reset can carry a closing reading that is not an answer

The one that would have slipped through. `missing_closing_readings` started as "no closing
value", which passes a reset row that happens to have one — and §6.2 says the pair is
**meaningless** after a reset. The shift would have closed and been valued off a reading the
spec explicitly calls unusable.

```python
def _quantity_is_unknown(reading) -> bool:
    if reading is None: return True
    if reading.manual_quantity_override is not None: return False
    return reading.meter_reset_occurred or reading.closing_reading is None
```

---

## 6. Why this table has no append-only trigger

`fuel_prices`, `fuel_margins` and `audit_logs` all carry `reject_modification()`.
`nozzle_readings` does not, and that is deliberate: **a reading is corrected while its shift
is open — that is the normal workflow, not an exception.** A trigger here would make typing
a closing reading impossible.

Immutability comes from shift *status* instead. `require_shift_access(..., writable=True)`
refuses any write to a closed or locked shift (§6.9), and that dependency is Phase 4's, not
re-implemented here. `test_readings_are_not_append_only` asserts the absence of the trigger
so a future "consistency" pass does not helpfully add one.

---

## 7. The one route that must work on a closed shift

Every write route uses `writable=True`. `review_reading` does not.

A mismatch is usually noticed **while reconciling**, which happens after the shift closes.
Gating review on `writable` would make the flag permanently unclearable on exactly the
shifts that raise one. A locked shift is still refused, because §5.2 is absolute that
nothing referencing a locked shift may be modified.

---

## 8. Labelling the profit figure

§13.7 requires the figure to be labelled *wherever it is displayed*. The response therefore
carries no key called `profit` at all:

- the per-line key is `gross_fuel_margin`
- the payload carries a `margin_basis` string spelling out what it excludes

The label travels **in the payload**, not in the frontend, because §2 requires a mobile app
to be able to do everything the web page can against identical endpoints — and a label that
lives only in HTML is a label the mobile client will not have.

`test_the_profit_figure_is_never_called_profit_and_is_always_qualified` asserts `"profit"
not in body`.

Similarly, `quantity_by_unit` is a **map**, not a total. §4.5: 500 litres of petrol plus
100 kg of CBG is not 600 of anything. Money is the only thing the two have in common, so
money is the only thing summed across them.

---

## 9. Bugs the tests caught

1. **`make_nozzle` defaulted `meter_installed_at` to `now()`.** Every nozzle was therefore
   *out of scope* for any shift dated earlier — so the first worksheet tests saw zero
   nozzles and passed by describing nothing. Fixed by defaulting the fixture to 2020 and
   making it overridable, which then made `NOZZLE_NOT_YET_INSTALLED` testable too.

2. **`make_fuel_type`'s teardown deleted nozzles that now had readings.** pytest tears
   fixtures down in reverse *setup* order, so a test naming `make_fuel_type` after
   `make_reading` tore it down first and hit a foreign key. Four fixtures needed a new
   generation added to their cleanup. The lesson generalises: every new child table means
   revisiting every fixture that deletes a parent.

3. **A grep-based test failed on its own subject matter.** `test_the_flow_ceiling_never_
   reads_the_config_constant` searched for `MAX_FLOW_RATE_LPM` as a substring — and matched
   the comment *warning against reading it*. Rewritten to parse with `ast` and check for
   `Name`/`Attribute` nodes, which is the property that actually matters: it now passes
   with the warning present and fails if somebody deletes the warning and adds the bug.

4. **A dead helper.** `total_readings_at_outlet` was written "for tests and diagnostics"
   and called by nothing. Deleted — §11 is against scaffolding, and an uncovered helper is
   how scaffolding looks after the fact.

5. **A meter reset could not be recorded at all.** The worst of the five, and it survived
   the first full green suite because every reset test used `make_reading` (straight to the
   database) rather than the API.

   `_validate_math` ran §6.2's guards on write, so a POST carrying
   `meter_reset_occurred: true` was refused with `METER_RESET_REQUIRES_OVERRIDE` — the row
   could not be created until an admin had overridden a row that did not exist.
   Chicken and egg, and it made the feature unusable by the person who actually witnesses
   the event.

   The distinction the first version missed: **"the meter was replaced" is an observation**
   and **"312.5 litres went through it" is a judgement**. §8 reserves the second for an
   admin. They are separate acts by separate people at separate times, so requiring the
   second in order to record the first was wrong. Fixed with `quantity_if_known`, which
   returns `None` for the two states that are legitimately not-yet-knowable while still
   raising on a genuine §6.2 refusal. §6.8's close precondition already blocked closing in
   that state, which is where the constraint belongs.

   Written test-first, per §10.

6. **A pre-existing flaky test in Phase 2, found by accident.** `test_tampered_token_is_
   rejected` failed roughly one run in four, which surfaced during a Phase 5 full-suite run
   and initially looked like collateral damage from these changes. It was not.

   The test flipped the **last** character of the JWT signature. An HS256 signature is 32
   bytes → 43 base64url characters carrying 258 bits, so the final character has two bits
   of slack and four different characters decode to the same signature. About a quarter of
   the time the "tampered" token was still perfectly valid, and the endpoint correctly
   returned 200.

   Now flips the first character, which carries all six of its bits. 12 consecutive runs
   green. Worth recording because the failure looked like a security bug and was a base64
   bug — and because an auth test that fails intermittently is one people learn to re-run
   rather than read.

---

## 10. Verification

```
399 passed
100% coverage on sales.py, readings.py, api/v1/readings.py, models/reading.py
99% overall
alembic revision --autogenerate  ->  empty
alembic downgrade base && upgrade head  ->  clean
pytest -k rollover|testing|chain|unit|anchor|review|ceiling  ->  all select and pass
```

Structural assertions that no value test could make:

- `MAX_FLOW_RATE_LPM` appears in no AST node of any Phase 5 module (§14)
- `float(` and `sa.Float` appear nowhere in the money path (§3 rule 1)
- `attendant_id !=` appears nowhere in `readings.py` — ownership is not re-implemented (§8)
- `MISSING_COLLECTIONS` and `CREDIT_SALE_MISSING_RECEIPT` are still comments (§11)

---

## 11. Open items, carried forward

**Two are now urgent, because Phase 5 made them live:**

- **CBG's real max flow rate, in kg/min.** `0003` seeded a generous 15. §6.2's guard now
  reads that column on real money. Too high and it never fires; too low and it refuses
  genuine sales on the busiest day. Same for the 60 L/min seeded for petrol and diesel.
- **Do the salesmen record testing quantities on paper, and in what unit?** If not, every
  row will carry 0 and §4.2's small permanent daily shortfall reappears — with the field
  looking correctly filled in.

Still open from earlier phases:

- Petrol and diesel dealer commissions. Nothing in `fuel_margins` for them, so
  `GET /shifts/{id}/sales` currently returns 409 `NO_MARGIN_FOR_DATE` for those fuels. That
  is the correct behaviour (§5.1 refuses rather than reporting zero profit) but it means
  profit reporting covers CBG only until they are entered.
- The salesman-shortfall vs receipt contradiction — decide before Phase 9.
- The `cash_expenses` payment-mode contradiction — decide before Phase 7.
- Phase 10's locker model.

### The check no test replaces

**Take one real day from the paper sales register and enter it through the API.**
`GET /shifts/{id}/sales` must match the register's rupee total. That catches a wrong
assumption about how the pump actually records its day, which unit tests cannot — and this
is the cheapest moment it will ever be to find one.

### What Phase 6 inherits

- `total_sales` — the first term of §6.4's cash equation, now computable.
- **`idempotency_keys` is Phase 6's to build.** §6.10 was amended to say so. A reading is
  idempotent by construction (`UNIQUE (shift_id, nozzle_id)`); two ₹5,000 cash collections
  in one shift are both legitimate, so collections have no natural key and a timed-out
  retry genuinely does duplicate money.
- `MISSING_COLLECTIONS` — the named comment at `app/api/v1/shifts.py`, next to the
  `MISSING_NOZZLE_READINGS` check that now shows what one looks like when it lands.
