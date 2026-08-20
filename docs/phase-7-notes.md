# Phase 7 — Expenses: Notes

> The phase where the system learns what the pump *paid out*, against what Phase 6 says it
> *received*. Written as a learning reference, not a spec. For the authoritative rules, see
> `CLAUDE.md`.

---

## 1. The big picture

Phase 6 recorded the money arriving. Phase 7 records the money leaving — the last piece
§6.4's cash equation needs before Phase 10 can assemble it, apart from credit repayments
(Phase 9). It also closes the last of §6.8's and §6.7's shift-lifecycle preconditions:
`UNREVIEWED_EXPENSES_EXIST` had been a named comment at `app/api/v1/shifts.py` since
Phase 4, waiting for a table that didn't exist yet.

### What shipped

| Piece | File(s) |
|---|---|
| Phase 6 fix-ups (3 defects + 2 carried gaps, test-first) | `app/api/v1/collections.py`, `app/services/collections.py`, `app/core/errors.py`, migration `0007`, `tests/test_readings_api.py` |
| Spec amendment (§5.2, §6.4, §6.7, §14) | `CLAUDE.md` |
| Migrations `0008` + `0009` — one table, two enums, five constraints, three indexes, no trigger | `alembic/versions/0008_expenses.py`, `alembic/versions/0009_expense_description_trim_fix.py` |
| ORM model + core enums | `app/models/expense.py`, `app/core/expenses.py` |
| §6.7's two flagging rules, §6.9's reversal | `app/services/expenses.py` |
| 6 routes, including a new cross-shift flagged queue | `app/api/v1/expenses.py` |
| §6.7's `UNREVIEWED_EXPENSES_EXIST` | `app/api/v1/shifts.py` |
| 96 new tests (Step 0 + Step 6 combined), 580 total, 100% on every Phase 7 module | `tests/test_expenses_api.py`, `test_expense_flagging.py`, `test_expense_reversals.py`, `test_expense_permissions.py`, `test_shift_lock_expenses.py` |

---

## 2. Auditing Phase 6 first found three defects, not one

Phase 6's own verification checklist passed every line — 484 tests, clean migrations, 100%
coverage on the modules it named. That checklist just never asked the questions that
mattered. A second pass, driven by "reproduce it through HTTP before you believe it",
found three real defects in code that had shipped as done:

**PATCH could edit money after it had been reversed.** `update_collection` refused a row
that *is* a reversal (`reverses_id IS NOT NULL`) but never checked whether the row *had
been* reversed by something else. Since §6.9's reversal route deliberately works on open
shifts too, an attendant could reverse a ₹60,000 cash figure and then `PATCH` the original
down to ₹58,000 — leaving the reversal still carrying −₹60,000, the mode netting to
−₹2,000, and the audit log claiming a row cancelled at ₹60,000 currently reads ₹58,000.
The fix extracted a `reversal_of()` helper so `reverse()` and `PATCH` ask the same
question the same way; `expenses` used it from the start.

**A reversal reason of pure whitespace passed validation and stored as empty.**
`Field(min_length=3)` measures the raw string before the handler's own `.strip()` runs, so
`"   "` passed at length 3 and was stored as `""`. A ₹60,000 reversal with no stated cause
is precisely the row §6.9 exists to prevent. Fixed by making `strip_whitespace=True` part
of the `Field` constraint itself, so stripping happens *before* the length check —
`expenses.py`'s `ReasonValue` was written this way from day one.

**Two live collections for one mode crashed the whole shift.** §5.2's one-live-row-per-mode
rule is deliberately not a database constraint (it can't coexist with §6.9's reversal
shape — see migration `0006`'s long comment). `live_collection_for_mode` ended in
`scalar_one_or_none()`, so the moment two concurrent requests both passed the live-row
probe and both inserted, every later call — `GET`, the next `POST`, and the close
precondition — raised `MultipleResultsFound`. The shift became unreadable *and*
unclosable through the API. The owner's instruction here was specific: **keep it loud**.
The fix raises 409 `DUPLICATE_LIVE_COLLECTIONS` naming both rows' ids and amounts, and a
companion test proves the reversal route — which loads a row by id, never through
`live_collection_for_mode` — stays reachable as the fix path while everything else is held
shut.

All three were fixed test-first, in their own commit, before any Phase 7 code — the same
shape Phase 6 used for the three Phase 5 defects it found. A fourth, smaller fix rode
along: `uq_collections_reverses_id` was missing from `_CONSTRAINT_ERRORS`, so a concurrent
double-reversal returned 500 instead of 409.

---

## 3. A CHECK constraint got fixed twice, for two different reasons

Migration `0007` (Step 0) strengthened `ck_collections_reversal_has_reason` from bare
NOT NULL to something that actually rejects blank input. The first draft used
`btrim(reversal_reason) <> ''`. The raw-SQL test that was supposed to trip it — passing a
tab character — didn't. One-argument `btrim` in PostgreSQL strips **spaces only**, not
tabs or newlines. Fixed to a regex existence check, `reversal_reason ~ '[^[:space:]]'`:
"does at least one non-whitespace character exist anywhere", with no length arithmetic to
get wrong.

`expenses.description` needed a *different* shape of check — not "contains a real
character somewhere" but "is at least 3 meaningful characters", to match the API's
`min_length=3`. Migration `0008` wrote it the same way as the just-fixed reversal-reason
regex: `char_length(regexp_replace(description, '\s', '', 'g')) >= 3` — strip every
whitespace character, then measure length.

That was wrong, and it was wrong in a way neither `btrim`'s failure mode nor manual review
caught. The API's `StringConstraints(strip_whitespace=True)` trims only the **ends** of a
string. `"a b"` — three characters, one internal space — passes it cleanly. The database
check stripped the *internal* space too, leaving `"ab"`, two characters, and refused input
the API had already accepted. Because that constraint isn't in `_CONSTRAINT_ERRORS`, the
refusal surfaced as an opaque 500, not a 422 naming the field.

Nobody typed `"a b"` into a form to find this. It surfaced while writing a coverage test
for `create_expense`'s exception-handling branch — the kind of test that exists to prove a
rollback path works, not to find a validation bug. Migration `0009` fixes it: trim only
the ends (`regexp_replace(description, '^\s+|\s+$', '', 'g')`), matching the API exactly.
Not a rewrite of `0008` — a new migration, same discipline as Step 0's fix to `0006`.

**The pattern worth naming:** a regex written for "does this field contain real content"
(`reversal_reason`) is not interchangeable with one written for "does this field have at
least N real characters after trimming" (`description`), even though both start from the
same `regexp_replace` idiom. The first only needs existence; the second needs length, and
length is exactly where "strip everything" and "strip only the ends" start disagreeing.

---

## 4. Keeping a flag loud instead of quiet was the owner's call

The plan for defect P6-3 originally proposed degrading gracefully: if two live rows exist
for one mode, pick the earliest deterministically and log a warning, so the shift stays
readable. Presented with the tradeoff — silent-but-working versus loud-but-blocked — the
owner chose loud: **raise a 409 naming both rows.**

The reasoning holds up. Silently picking one of two real money figures is itself a
plausible-but-wrong number — exactly the failure mode CLAUDE.md exists to prevent, just
moved one level up from "wrong number" to "wrong choice of which number to trust". A human
has to decide which row is the mistake; the system's job is to say so clearly, not guess
well. The fix makes the shift stop working until someone reverses the wrong row — which is
the same shape as every other guard in this codebase: refuse loudly rather than proceed
plausibly.

---

## 5. §6.7's aggregate rule spans shifts, and that has one sharp edge

CLAUDE.md is specific: the aggregate flag fires on "the sum of a single category for one
`business_date`" — not "for one shift". This outlet trades one shift a day, so the
distinction is invisible here, but a 24-hour outlet running three shifts must have all
three roll up into one group, or the control is trivially defeated by splitting one day's
maintenance spend across shift boundaries.

That means `apply_review_flags` can reach into an **already-locked** shift elsewhere on
the same business date and set `requires_review = true` on one of its rows. §5.2 says
"nothing referencing a locked shift may be modified" — so is this a violation?

It isn't, and there's a direct precedent already in the codebase: `readings.
flag_downstream_reading` (§13.10) mutates a review flag on any downstream shift with no
locked-shift guard at all, because a review flag is a signal that something needs a human's
attention, not a change to financial substance. The money on a locked shift's row is
untouched; only a metadata flag moves. `review_expense`, which *clears* a flag, still
refuses outright on a locked shift — clearing is the action §5.2 is actually protecting
against, since it's the step that says "this is settled" on a day that's supposed to be
final.

`unreviewed_flagged_expenses`, the lock precondition's predicate, stays deliberately
narrower than the rule that sets the flags: it reads only the shift being locked, not the
wider business date. Locking is a per-shift action, and a row flagged on a *different*
shift blocks that shift's lock, not this one's.

---

## 6. A decision recorded in the plan and not implemented, caught by a test's own contradiction

The plan's M6 decision states: "a reversed expense stops blocking the lock — it is money a
manager formally cancelled." `unreviewed_flagged_expenses` was written, correctly per its
own docstring, as "this shift's own flagged rows" — but it never excluded a flagged row
that had *since been reversed*. A bare reversal never clears `requires_review` (flags are
never auto-cleared, by design), so a reversed-but-still-flagged original would go on
blocking the lock forever, with nothing left to review that changes the drawer.

The way this surfaced is worth recording as a habit, not just a bug: the test written to
prove M6 was named `test_a_reversed_flagged_expense_does_not_block_a_lock`, and its own
assertion said `assert response.status_code == 409` — a passing test whose name and body
flatly disagreed with each other. The test passed. The name was the tell. Fixed by adding
`~_is_reversed()` to the predicate's query, the same exclusion `live_collection_for_mode`
already uses — and the test was rewritten to assert `200`, with a docstring explaining
*why* it passes even though the underlying flag was never cleared.

---

## 7. A worked example in the spec itself was arithmetically wrong

`CLAUDE.md`'s §6.7 amendment (committed in Step 1, before any code) originally read:
*"Otherwise three ₹400 entries followed by an edit to ₹900 never trips it."* Three ₹400
entries sum to ₹1,200 — already over the ₹1,000 threshold at the third insert, with no
edit required. The example didn't demonstrate what it claimed to.

This was caught the same way as the description-check bug: not by re-reading the prose,
but by trying to write a test that exercised exactly what the sentence described, and
discovering the numbers didn't produce the scenario. Fixed to *"two ₹300 entries (₹600,
under the line) followed by an edit of one to ₹800 (₹1,100)"* — arithmetic that actually
requires the `PATCH` re-evaluation path to catch it. The lesson generalises: a worked
example in a spec is a claim, and the cheapest way to verify a claim is to write the test
it describes.

---

## 8. Route-level choices worth a sentence each

- **`GET /expenses/flagged` reuses `app/api/cursor.py`'s existing pair.** `encode_cursor`
  and `decode_cursor` were built for `fuel_prices`/`fuel_margins`, keyed on
  `effective_from`. Nothing about them is specific to that column — they serve any
  `(TIMESTAMPTZ, UUID)` sort key — so Phase 7 reuses them unchanged on `(created_at, id)`
  rather than writing a third pair. The module's docstring now says so explicitly.
- **The flagged queue retires a gap Phase 5 carried knowingly.** `nozzle_readings` has had
  a partial review index (`ix_nozzle_readings_review`) since Phase 5 with no endpoint ever
  running the query it was built for. `expenses` ships the query from day one.
- **`PATCH` cannot change `category`.** Changing what an expense was *for* would silently
  move it out of the group §6.7's aggregate rule already scoped it under. `mode` can be
  changed — it doesn't affect the flagging math, only which mode Phase 10 nets against
  cash — so only `category` is excluded, not the whole enum surface.
- **The replacement in a reversal copies the original's category, mode, and description.**
  A correction is "the same expense, the right amount", not a new expense — the reason for
  the change lives on the reversal row, where §6.9 already puts it. Only `amount` and,
  optionally, `paid_to` can differ.
- **The review route mirrors `readings.py::review_reading` exactly**, including the
  locked-shift refusal and the append-only note (`f"{old}\n{new}"` never replaces).

---

## 9. Verification

```
580 passed
100% coverage on app/api/v1/expenses.py, app/services/expenses.py,
    app/core/expenses.py, app/models/expense.py
99% overall
alembic check                          ->  no new upgrade operations, head 0009
alembic downgrade base && upgrade head ->  clean
```

Structural assertions no value test could make:

- `expenses` has no `outlet_id`, no `(shift_id, category)` unique, no append-only trigger
- `float(` / `sa.Float` appear nowhere in the Phase 7 money path
- `attendant_id` and `DEFAULT_OUTLET_ID` appear nowhere in `expenses.py` — ownership and
  the outlet both come from the row, via `require_shift_access`
- No hardcoded `1000` threshold literal; `EXPENSE_REVIEW_THRESHOLD` is read from config
- `fuel_purchase` appears nowhere in `app/` outside explanatory prose
- `unreviewed_flagged_expenses`'s own source never mentions `business_date` — parsed via
  AST so a comment explaining the distinction cannot itself satisfy the check

---

## 10. Open items, carried forward

Nothing new falls due before Phase 8. Still open from earlier phases, unchanged:

- CBG's real max flow rate in kg/min, and confirmation of 60 L/min for petrol and diesel.
  Live on real money since Phase 5.
- Whether salesmen record testing quantities on paper, and in what unit.
- Petrol and diesel dealer commissions — until entered, `GET /shifts/{id}/sales` returns
  409 `NO_MARGIN_FOR_DATE` for those fuels.
- The salesman-shortfall vs receipt contradiction — decide before Phase 9.
- The locker model's effect on §6.5's rolling balance — decide before Phase 10.

### The check no test replaces

**Take one real day from the paper register and enter it end to end**, including that
day's expenses this time. Phase 5 left this owed; Phase 6 could exercise half a day;
Phase 7 is the first phase that can carry a complete day through to `locked`. Enter the
readings, the cash/card/UPI figures, and the expenses, close the shift, review any flagged
expense, and lock it.

### What Phase 8 inherits

- **`attachment_id` on both `credit_sales`-adjacent tables and `expenses`.** `expenses`
  already has the column named and reasoned about in `CLAUDE.md` §5.2 — it just isn't in
  the schema yet. Phase 8 adds it as a nullable FK, one `ALTER TABLE`, no backfill.
- **The `Idempotency-Key` dependency and the reversal shape**, now proven on three tables
  (`nozzle_readings` needed neither; `collections`; `expenses`) and ready to copy onto
  `credit_sales` in Phase 9.
- **§6.7's "aggregate rule spans shifts, lock check doesn't" pattern**, worth remembering
  if any future phase adds another business-date-scoped control.
