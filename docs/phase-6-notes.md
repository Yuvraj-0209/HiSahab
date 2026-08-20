# Phase 6 — Collections: Notes

> The phase where the system learns what it *received*, against what Phase 5 says it
> *sold*. Written as a learning reference, not a spec. For the authoritative rules, see
> `CLAUDE.md`.

---

## 1. The big picture

Phase 5 turned meter readings into rupees. Nothing yet said what actually arrived — and
**the gap between those two numbers is the entire reason this system exists.**

### What shipped

| Piece | File(s) |
|---|---|
| Phase 5 fix-ups (3 defects, test-first) | `app/core/errors.py`, `app/api/v1/readings.py`, `app/services/readings.py` |
| Spec amendment (§5.0, §5.2, §5.3, §6.8, §14) | `CLAUDE.md` |
| Migration `0006` — 2 tables, 1 enum, 4 constraints, 3 indexes, no trigger | `alembic/versions/0006_collections_and_idempotency.py` |
| ORM models | `app/models/collection.py`, `app/models/idempotency.py` |
| The live-row rule, the totals, §6.9's reversal | `app/services/collections.py` |
| §6.10's replay store | `app/core/idempotency.py`, `app/jobs/cleanup_idempotency_keys.py` |
| 4 routes | `app/api/v1/collections.py` |
| §6.8's `MISSING_COLLECTIONS` | `app/api/v1/shifts.py` |
| 85 new tests, 484 total, 100% on every Phase 6 module | `tests/test_collections_api.py`, `test_idempotency.py`, `test_reversals.py`, `test_shift_close_collections.py`, `test_collections_permissions.py`, `test_migrations.py` |

---

## 2. The thing that reframed the whole phase

Planning started with a question about the shape of the table and ran straight into this,
in §6.4:

```
cash_sales = total_sales − card − upi − wallet − credit_sales_amount
```

**The cash collection row is not in that formula.** Cash is derived as the residual. So
what is a `mode = cash` row *for*, given §5.2 defines one and §6.8 requires one before a
shift can close?

The answer was already in §14, in an answered open question about how the outlet runs:

> the salesman reconciles his own shift (sales vs UPI, card, credit) and puts the cash in
> the locker; a shortfall is booked as udhaar **against his own name**

That is two numbers, not one:

* **derived cash** — what the meters say he should be holding
* **declared cash** — the `cash` row: what he says he counted

and the gap between them is the shortfall. The cash row is not an *input* to §6.4's
equation, it is the **independent observation the equation's answer is checked against**.

This is the same shape as §4.7's chain — the system predicts, a human confirms, both values
are stored, and a disagreement leaves a trace instead of being absorbed. Phase 5 built it
for meter readings; Phase 6 builds it for cash.

**The consequence that had to be written down in four places** (the spec, the migration,
the model docstring, and the API payload): never sum the `cash` row together with derived
`cash_sales`. That double-counts the whole day, and the result looks entirely reasonable.

It also settles §6.8's wording. "No cash declared" and "declared ₹0" are different facts,
so the close precondition demands an **explicit zero** on a cashless day. Zero as an
answer, never zero as an omission.

---

## 3. Two owner decisions that turned out to contradict each other

The owner chose **one live row per mode** (one card machine, one UPI QR, one lumped figure
per mode in the register) and **build §6.9's reversal pattern now**. Both are right. Taken
together they are impossible as stated, and the preview shown at decision time promised
something undeliverable:

```
UNIQUE (shift_id, mode)   -- retry -> 409, no duplicate possible
```

**It cannot exist.** §6.9 keeps Monday's ₹60,000 row forever, so the corrected ₹58,000 row
collides with it on `(shift_id, mode)`. Every partial-index variant fails identically — the
replacement carries `reverses_id IS NULL`, exactly like the original. The only escape is a
`reversed_at` marker stamped onto the original, which is an `UPDATE` on a financial row in
a closed shift: precisely what §6.9 forbids.

So the guarantee moved up one level. The rule is enforced in
`live_collection_for_mode`, `POST` refuses a second live row with 409
`COLLECTION_ALREADY_EXISTS`, and retry safety comes from the `Idempotency-Key` store —
which §6.10 requires anyway, and which also protects the reversal route that a unique
constraint never would have.

`test_collections_have_no_unique_mode_constraint` asserts the **absence**, with the reason
attached, because "add the obvious unique constraint" is a realistic thing for a later
consistency pass to do, and by then the table holds real money.

---

## 4. The line in the test suite that matters most

```python
async def test_collections_far_below_sales_do_not_block_the_close(...)
```

1,000 litres through the meter against ₹40,000 declared. **It closes.**

The owner's instinct during planning was that the system should refuse until collections
match sales. That is the single most damaging thing that could have been built here, and
it is worth spelling out why:

- Collections *almost never* equal sales. Udhaar issued during the shift accounts for part
  of the gap (§6.6), and a cash repayment arriving from an old bill (§4.4) pushes it the
  other way.
- If the form will not let the salesman go home until the numbers agree, he has exactly one
  freely adjustable field — cash — and he types whatever balances it. **The result is a
  perfectly reconciled system that reports nothing.**

§6.4 says it in one line: *"Variance is recorded, never auto-corrected. The variance is the
signal."* So `MISSING_COLLECTIONS` fires on **absence** and nothing else, and both the
service function and the call site in `close_shift` carry a comment saying what the check
deliberately does not do.

---

## 5. Three defects Phase 5 shipped, found by auditing it first

All three were fixed test-first (§10) before any Phase 6 code, in their own step.

### 5.1 A CHECK constraint that produced a 500

Posting a reading with `rollover_occurred` **and** `meter_reset_occurred` both true reached
`db.flush()` — `quantity_if_known` returns `None` for a row awaiting an override, so
`_validate_math` returned without checking anything — and `app/core/errors.py` installed no
`IntegrityError` handler. The caller got a 500.

The existing test for that constraint inserted raw SQL and never exercised the route, which
is how it survived a green suite. **This blocked Phase 6 outright**: every CHECK in `0006`
would have failed the same way.

The handler **allowlists** rather than converting every `IntegrityError` to a 409. A
constraint nobody anticipated firing means the application let through something it should
have refused — that is a bug, and turning it into a tidy business error invites the client
to retry a write that will never succeed while dropping it from the logs as a handled
response. An unrecognised constraint keeps its loud 500.

### 5.2 Two sites disagreeing about the same rule

`revalidate_flow_rates` skipped any row carrying an admin override; `_validate_math` did
not. So an admin's signed figure was refused at entry and never re-checked at close. The
notes stated the intended rule; one of the two implementations of it did not.

### 5.3 §13.10's flag followed the calendar, not the chain

The worst of the three, because it silently defeated the guarantee it existed to provide.

`flag_downstream_reading` asked `next_shift_after` and stopped. But `chained_opening`
deliberately **skips** shifts with no closing reading for that nozzle (§4.7: *"the most
recent closing reading for that nozzle"*, not *"the previous shift's"*). So with a nozzle
out of order for one shift, shift 3's opening was carried from shift 1 — and editing shift
1 left shift 3 stale with nothing flagged.

The fix is the exact inverse of `chained_opening`: find the earliest reading *for that
nozzle* in any later shift, ordered `(business_date, sequence)`. `next_shift_after` had no
other caller and was deleted, per the same rule that removed `total_readings_at_outlet` in
Phase 5.

---

## 6. Why the idempotency store commits before doing the work

The reservation row is inserted and **committed** before the handler runs. That is the
whole mechanism: two simultaneous retries race on
`uq_idempotency_keys_key_endpoint_user`, and exactly one proceeds. Holding the reservation
in an uncommitted transaction would let both through, which is the failure it exists to
prevent.

The cost is a reservation that outlives a handler which then fails — and that cost is
sharp. Without `discard`, a business-rule 409 would leave a key wedged at
`REQUEST_IN_PROGRESS` **for 24 hours**, locking the attendant out of an action that never
happened. An idempotency store that does that is worse than none, so
`test_a_refused_request_releases_its_key` drives it through HTTP rather than trusting the
`except` block by inspection.

`discard` releases reservations only. A row that already carries a response is left alone,
because that response is the thing the store exists to protect —
`test_discarding_a_completed_reservation_does_nothing`.

Three other decisions worth recording:

- **A fingerprint, not just a key.** Same key with a different body is a client bug, not a
  retry. Replaying the ₹5,000 answer for a ₹9,000 request would tell the client its ₹9,000
  landed — a wrong number the caller has every reason to trust.
- **The header is required, not optional.** A client that forgets it is precisely the client
  whose retry duplicates a ₹5,000 row, and a silent fallback leaves that to be discovered in
  a cash count weeks later.
- **`default=str` in the fingerprint.** A `Decimal` is not JSON-serialisable and §3 rule 1
  forbids reaching for `float` to make it so.

---

## 7. Reversal and replacement are one request

`POST .../reversals` takes an optional `replacement_amount` and applies it in the **same
transaction**. Splitting it into two calls does not work: the reversal succeeds, and the
follow-up `POST` is then refused by `writable=True` because the shift is closed — which is
the only situation a reversal is for.

It also matters that the pair is atomic. A half-applied correction is worse than none: the
money is gone from the system with nothing replacing it, so the shift silently reads as ₹0
cash.

`test_a_failure_after_the_reversal_rolls_the_whole_thing_back` injects the failure with
`monkeypatch` rather than provoking it, and says so in its docstring — no reachable input
fails at that exact point, because every value a client can send is refused earlier by
`condecimal` or by a CHECK. The first version of that test *looked* like it exercised the
rollback and did not: `99999999999.00` has 13 digits, so Pydantic refused it before the
handler ever ran, and the test passed without touching the code it named. It is now split
in two, one for each thing it was pretending to check.

**`test_the_original_row_is_left_byte_identical` compares every column**, not just
`amount`. "We only changed a flag" is how a row starts being edited, and that test is what
refuses a future `reversed_at` marker.

---

## 8. Route-level choices worth a sentence each

- **`reverse_collection` is deliberately not `writable=True`.** A miscount surfaces during
  reconciliation, which happens *after* close. Same reasoning as Phase 5's review route.
- **A locked shift is admin-only, not refused.** §6.9 names locked shifts as needing the
  reversal path and §5.2 says nothing referencing one may be *modified* — both hold,
  because a reversal appends and modifies nothing. `app/api/deps.py`'s own `SHIFT_LOCKED`
  message already says corrections must be recorded as reversal entries.
- **`PATCH` takes no `Idempotency-Key`.** It is idempotent by construction; sending the
  same amount twice leaves the row in the same state.
- **`PATCH` cannot change `mode`.** Otherwise it would walk around the one-live-row rule by
  moving a second row's mode after the fact.
- **The close check never calls `shift_sales`.** That function prices the shift and raises
  409 `NO_MARGIN_FOR_DATE` — and petrol and diesel margins have never been entered here, so
  every petrol shift would have become unclosable, with an error naming a missing margin.
  `test_the_close_precondition_never_prices_the_shift` parses the AST rather than grepping,
  so the comment warning against it does not fail its own test.
- **`GET` is a capped list, not a cursor.** A shift holds four live rows. §9's rule exists
  because `OFFSET` skips and duplicates under concurrent inserts; a list bounded by the
  number of payment channels an outlet has cannot page at all. The cap is there so a
  runaway correction loop surfaces as a truncated list rather than an unbounded response.

---

## 9. Things the tests caught

1. **`is_reversed` was computed from the truncated page.** A reversal falling past the cap
   would have made the row it cancels read as still live. Unreachable at four rows per
   shift, wrong anyway; now computed before truncation.

2. **A dead helper, again.** `live_collections` was written for "the places that need to
   show a figure" and called by nothing. Deleted. Phase 5 learned this lesson and Phase 6
   repeated it, so it is worth stating as a habit: **write the caller first.**

3. **Two existing Phase 5 tests started failing the moment §6.8 landed** — correctly. They
   closed shifts that had sold fuel with no cash declared. Updated rather than weakened, and
   the added line carries a comment naming the phase that changed the rule.

4. **Every fixture that deletes a parent needed a new generation.** `make_user` and
   `make_shift` both delete shifts, and collections now hang off shifts while reversals hang
   off collections — so both teardowns needed two extra passes in the right order.
   Phase 5's notes predicted exactly this: *"every new child table means revisiting every
   fixture that deletes a parent."* It cost about as much as it did last time.

5. **A `uuid = character varying` error in an audit assertion.** `response.json()["id"]` is
   a string; the column is `uuid`. Postgres refuses the comparison rather than coercing,
   which is the behaviour worth having.

---

## 10. Verification

```
484 passed
100% coverage on collections.py (api, service, model), core/collections.py,
    core/idempotency.py, models/idempotency.py, core/errors.py
99% overall
alembic check                          ->  no new upgrade operations
alembic downgrade base && upgrade head ->  clean, 0006 (head)
pytest -k "idempot or revers or collection"  ->  80 select and pass
```

Structural assertions no value test could make:

- `collections` has exactly one unique constraint, and it is not `(shift_id, mode)`
- `collections` has no append-only trigger and no `outlet_id`
- `shift_sales` appears in no AST node of `shifts.py` or `services/collections.py`
- `float(` and `sa.Float` appear nowhere in the Phase 6 money path
- `attendant_id` appears nowhere in `collections.py` — ownership is not re-implemented
- `DEFAULT_OUTLET_ID` appears nowhere in `collections.py` — the outlet comes from the row
- `CREDIT_SALE_MISSING_RECEIPT` and `UNREVIEWED_EXPENSES_EXIST` are still comments (§11)

---

## 11. Open items, carried forward

**Now due before Phase 7:**

- **§6.4 vs §5.2 — does `expenses` need a `mode` column?** §6.4 subtracts `cash_expenses`,
  implying some expenses are not cash, but `expenses` has no payment-mode column. Phase 6
  makes this sharper, not softer: collections now model payment mode explicitly, so an
  expenses table that cannot is the odd one out. Cheap now, ugly once rows exist.

Still open from earlier phases:

- **CBG's real max flow rate in kg/min**, and confirmation of 60 L/min for petrol and
  diesel. Live on real money since Phase 5.
- **Do salesmen record testing quantities on paper, and in what unit?** Every row carrying
  `0` reproduces §4.2's small permanent daily shortfall with the field looking correctly
  filled in.
- **Petrol and diesel dealer commissions.** Until entered, `GET /shifts/{id}/sales` returns
  409 `NO_MARGIN_FOR_DATE` for those fuels.
- **The salesman-shortfall vs receipt contradiction** — decide before Phase 9.
- **The locker model's effect on §6.5's rolling balance** — decide before Phase 10.

### The check no test replaces

**Take one real day from the paper sales register and enter it end to end.** Phase 5 left
this owed; Phase 6 is the first phase where it exercises both halves of a day. Enter the
readings *and* the day's cash, card and UPI figures, close the shift, and check
`GET /shifts/{id}/collections` line by line against the register.

What it catches that no unit test can: a wrong assumption about how the pump actually
records its day. If the register turns out to carry two card figures from two machines, the
one-live-row-per-mode rule is wrong — and it is a migration to fix, cheapest today.

### What Phase 7 inherits

- **`total_sales` and every non-cash term of §6.4.** Only `cash_expenses`,
  `cash_credit_repayments` and `other_cash_income` are still missing before the equation
  can be assembled in Phase 10.
- **The §6.9 reversal shape**, ready to copy onto `expenses` — including the lesson that it
  and a natural unique key cannot coexist.
- **The `Idempotency-Key` dependency**, which every money-creating POST from here on should
  use. `POST /shifts/{id}/expenses` is the next one.
- **`UNREVIEWED_EXPENSES_EXIST`** — still a named comment at `app/api/v1/shifts.py`, next to
  the two close preconditions that now show what one looks like when it lands.
