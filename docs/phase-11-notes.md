# Phase 11 — Audit Retrofit: Notes

> The narrative counterpart to `phase-11-plan.md`. The plan says what was built; this says
> **why**, and records what went wrong on the way. Written to be read six months from now by
> somebody trying to understand a decision, not as a changelog.

---

## 1. The phase was mis-scoped in the spec, and the spec said so in its own words

§11's slot 11 read:

> What remains for this slot is retrofitting audit writes onto the Phase 3 admin endpoints
> (fuel types, nozzles, prices, margins), which is **genuinely optional**.

Two errors in one sentence, and the interesting thing is that both are *age* errors rather
than judgement errors. That line was written in Phase 1. It names four routers because in
Phase 1 those were the only reference-data routers that existed.

Counting `audit.record` calls against `@router.post` / `@router.patch` across every router
gave a clean split — seven routers with **zero** calls across twelve write endpoints, and ten
routers with more calls than endpoints:

| Zero audit calls | Fully audited |
|---|---|
| `fuel_types` (2), `nozzles` (2), `fuel_prices` (1), `fuel_margins` (1) — the four §11 names | `shifts`, `readings`, `collections`, `expenses`, `credit_sales`, `credit_repayments`, `non_fuel_sales`, `bank_deposits`, `shortfalls`, `daily_summaries` |
| `expense_categories` (2) — **Phase 8** | |
| `credit_customers` (2) — **Phase 9** | |
| `shift_templates` (2) — **Phase 4** | |

And "optional" was wrong because three of the four tables §11 *didn't* name are the ones that
gate money rules:

* **`expense_categories.requires_receipt`** is the knob §6.11 exists to give the admin. §6.11
  goes to real trouble to snapshot the answer onto each expense at insert *because the flag is
  editable*, so flipping it never rewrites whether history complied. That is the right design,
  and it left a hole nobody noticed: the flip itself was invisible. The system recorded what
  the rule was on the day, and not who changed the rule.
* **`credit_customers.credit_limit`** decides what §6.6 refuses. §6.6 already insists that an
  admin *override* of the limit is stored on the row **and** audit-logged, because letting one
  sale past the limit is a decision somebody answers for. Quietly **raising the limit** reaches
  the same outcome for every future sale, permanently, and recorded nothing at all.
* **`outlet_shift_templates.starts_at_local`** supplies the instant §6.3 prices a whole shift
  from. §5.1 is careful that editing a template must not revalue a shift that already traded —
  and it does not, because `started_at` is materialised onto the shift row. But the edit moves
  the valuation instant for every shift opened *afterwards*. This outlet's template starts at
  06:00 IST, which §6.3 notes is the revision moment itself, so one rate covers the whole day
  with nothing to apportion. Move it to 05:30 and every future shift sits on the **previous
  day's** rate for its entire length, and §6.3's mid-shift approximation starts applying where
  it did not before.

*The transferable lesson: a spec sentence that enumerates things is a snapshot of when it was
written. Re-derive the list from the code before trusting the count.*

The second finding was simpler and larger. **Nothing could read `audit_logs` back** — no
endpoint on any router selected from it. Seven phases of writing to a table whose only reader
was a raw SQL prompt, while §5.3 says a cash system requires an audit trail.

---

## 2. What the trail is *for*, which the append-only tables make concrete

`fuel_prices` and `fuel_margins` were the case that made this phase feel worth doing rather
than tidy.

Both are append-only — no `UPDATE`, no `DELETE`, enforced by a trigger. So `entered_by` on the
row looks like it already answers "who did this", and an audit row looks redundant. It is not,
and the reason is **backdating**.

The endpoint accepts a past `effective_from` deliberately, and its docstring gives the reason:

> Refusing it would be tidier for the audit trail, but it leaves a real failure mode with no
> remedy: if nobody enters Tuesday's revision until Thursday, a forward-only rule means Tuesday
> and Wednesday are valued at the stale rate permanently, with no legal way to correct them.

That is right. The cost is that a backdated row **silently revalues shifts that are already
closed**, because §6.3 recomputes valuation on read. §4.1 made these tables append-only in the
first place precisely because a mutable price column "silently corrupts every historical
report" — and the one operation that can still do that was leaving nothing behind but a log
line.

So `is_backdated` is folded into `new_values` rather than being a column, the same shape
`shifts.py` uses for a reopen reason and `credit_sales.py` for an override reason:
`AuditAction`'s labels are fixed at migration time and none of them says "backdated". It is the
fact that separates *setting tomorrow's rate* from *revaluing last week*.

---

## 3. Three decisions worth the words they cost

### 3.1 A global row's audit entry carries the acting admin's outlet

`audit_logs.outlet_id` is `NOT NULL` (§5.0: no parent row to derive tenancy from).
`fuel_types` is global reference data with no `outlet_id` — §5.1's "a litre is a litre at every
outlet". Those two facts collide on exactly one table.

The value is `actor.outlet_id`, which was already resolved at every call site, so the code cost
was zero. **The whole cost was writing down what it means**: on a `fuel_types` audit row,
`outlet_id` is *the outlet whose admin made this change*, not *the outlet that owns the row*.

In V1 there is one outlet and the distinction is invisible. The day there are two, an admin at
outlet B adding XP-95 writes a row stamped B for a fuel belonging to everyone — and a reader
filtering by outlet A concludes, **from an entirely correct query**, that it never happened.
That is this project's stated primary failure mode: not a crash, a plausible wrong answer.

Rejected alternatives: nullable (weakens §5.0's guarantee on the one table with no other
tenancy signal, for a single case) and a sentinel outlet id (invents a row that does not
exist).

### 3.2 A deactivation is an `update`, never a `status_change`

`AuditAction.status_change` is documented narrowly — a shift moving `open → closed → locked`,
or an admin reopening one — because §5.2 singles out backwards shift transitions as the thing
that must be traceable.

A `PATCH` flipping `is_active` looks like a lifecycle move and is not one. And because §3
rule 6 forbids hard deletes, `is_active` is how **every** one of these seven tables retires a
row. Admitting those would make the label mean "a shift moved, *or anything at all was
deactivated*", and anyone querying for lifecycle events would have to filter it back out —
a label that means two things is a label nobody can use.

Happy consequence: no new enum label, so no enum migration, so `app/core/audit.py` is untouched.

### 3.3 Reading the trail is admin-only, and that is a consistency requirement

§8 had no row for this because nothing could read the table. Putting it at admin rather than
manager has a taste argument and a structural one, and only the second is load-bearing.

The taste argument: every other manager-floor read is a *report* — shifts, cash position,
month-end expense summary. This is the control record, and partly the record of what managers
did.

The structural one: `old_values` / `new_values` on a `credit_customers` row carry `phone` and
`credit_limit` — **precisely** the fields §8 keeps out of the attendant-facing customer list
and §9 restricts on the customer detail route. A manager floor here would expose one table's
restricted columns through a different endpoint. Admin-only is what keeps §8 and §9 agreeing
with each other.

---

## 4. The structural test is the phase; everything else is bookkeeping

Twelve `audit.record` calls took an afternoon. **The gap survived seven phases because nothing
failed when it was missing**, and nothing would have failed in Phase 12 or 13 either.

This codebase has already learned this once, and §6.9 records it in the spec:

> This has now been forgotten twice (Phase 6 on `collections`, Phase 7 on `expenses`), which is
> why `tests/test_errors.py` asserts it structurally against `pg_constraint` rather than
> trusting anyone to remember.

`tests/test_audit_coverage.py` is that shape one layer up: an `ast` walk over every module in
`app/api/v1/`, finding every `@router.post` / `@router.patch` and asserting a real
`audit.record` call in the body.

Four details are deliberate, and three of them exist to stop the test passing for the wrong
reason:

* **An AST walk, not a grep.** Phase 10's notes record a structural test tripping over the
  *comment* documenting the rule it checked, and conclude: *"Left as a text search, it would
  have taught the next person to delete the comment."* A synthetic-module test pins the
  distinction directly — a router mentioning `audit.record` only in prose still fails.
* **Routers discovered by listing the directory**, never a hardcoded list. A hardcoded list
  would reintroduce the exact failure mode the file exists to prevent: something added later
  that nobody remembers to register.
* **A floor on the number of endpoints found.** `tests/test_routes.py` documents this trap
  being sprung already — its first version enumerated `app.routes`, which FastAPI does not
  flatten, so it was *"passing vacuously, seeing nothing but the framework paths it already
  excluded"*. A discovery-based test is worth nothing until something proves discovery works.
* **The exemption list must name endpoints that still exist.** A stale licence would sit there
  silently and exempt a future function that happened to reuse the name.

One exemption: `uploads.py::upload_receipt`. An attachment is not a business row — §6.10
already argues this for idempotency, and §7.4 hard-deletes unlinked ones, so auditing a row
*designed to be swept* records a fact about garbage. The money event is the **link**, which
`expenses.py` and `credit_sales.py` already audit.

**It was verified by being broken.** One `audit.record` call was neutered with its comments
left intact; the suite went red naming `fuel_types.py::create_fuel_type`, and green again on
restore. A structural test that has never run red is a test that passes for reasons nobody has
checked.

---

## 5. Three things I got wrong, and one irony

**The plan specified a DESC index; ASC was correct.** D6 said
`(outlet_id, changed_at DESC, id DESC)` to match the sort. A PostgreSQL btree scans backwards
as cheaply as forwards, so DESC only earns its keep for a *mixed* ordering — and expressing it
needs `sa.text("changed_at DESC")` in the model's `__table_args__`, which turns a plain column
index into an expression index that autogenerate cannot reliably compare. `alembic check` would
then report drift that is not real, on every future phase, forever. Trading a permanent false
positive for a benefit that does not exist. Built ASC, verified `alembic check` clean, and
pinned by a test that fails if somebody "fixes" it back.

**My route-inspection test passed vacuously on the first draft** — and it is documented in this
very repo. `test_the_endpoint_exposes_no_way_to_write` enumerated `app.routes` looking for
`/api/v1/audit-logs`, found nothing, and asserted `set() == {"GET"}`, which failed loudly only
by luck: I had asserted equality rather than absence. `tests/test_routes.py`'s own module
docstring explains that FastAPI does not flatten included routers into `app.routes`. Rewritten
against `app.openapi()["paths"]`, which is what a client actually sees.

**I ran two pytest processes against the same database, twice.** The first produced a phantom
409 on `POST /shifts` in an unrelated file; the second produced a coverage report full of
`relation "audit_logs" does not exist`, because `test_migration_is_reversible` downgrades to
base **mid-suite** and the other process was running against the stripped schema. Neither was a
code defect and both cost time to rule out. The rule: **this suite owns the database
exclusively while it runs.**

**The irony worth recording.** The plan's own verification checklist item E said to grep
`audit_logs.py` for `DEFAULT_OUTLET_ID` and expect zero. It returns one — a comment reading
*"Never `DEFAULT_OUTLET_ID` — the outlet comes from the actor"*. A text-search check tripping
over the prose documenting the rule it checks, in the same phase whose central test exists
precisely because that happens. It is a good argument that §7's checklist should be read as
prompts for a human, not as commands to run literally.

---

## 6. Verification

```
pytest                                 ->  1213 passed  (1135 before Phase 11)
pytest, second run, same database      ->  1213 passed
100% coverage on all eight touched routers, and on app/api/v1/audit_logs.py
alembic check                          ->  no new upgrade operations, head 0014
alembic downgrade base && upgrade head ->  clean, index dropped and recreated
tests/test_errors.py                   ->  passes unmodified (0014 adds no constraint)
```

Structural assertions no value test makes:

- 48 write endpoints across 18 routers; 47 audited, 1 exempt with a stated reason.
- `app/api/cursor.py`, `app/core/audit.py` and `app/services/audit.py` are **byte-identical**
  to their Phase 10 state — the helper was always correct, only its callers were missing.
- No `float(`, no `sa.Float`, no `.offset(`, no `relationship()` anywhere new.
- `GET` is the only method on `/api/v1/audit-logs`, and there is no parameterised sibling.
- All seven retrofitted routers import `audit` from `app.services`, not a local copy.

---

## 7. Open items, carried forward

**New:**

- **Who should read the audit log?** Admin only, on §3.3's reasoning. If the owner expects a
  manager to review it, §8 needs amending *and* §9's restriction on customer phone numbers and
  limits needs a separate answer, because the trail contains both. Worth asking before anybody
  relies on it.
- **The audit log has no retention policy and no archival** (§13.17, new). It is append-only,
  unbounded, and now the fastest-growing table in the schema. `0014`'s index keeps reads fast
  at any size; nothing keeps the table small. A policy needs a real row count behind it to be
  anything but a guess — revisit after a week of production data. Note that §3 rule 6 would
  apply in full to anything reconstructable from an audit row, so "just delete old rows" is not
  automatically available.

**Carried forward from Phase 10**, all still open:

- **There is no way to write off a shortfall** (§13.15) — a ₹20 gap nobody will chase stays on
  a salesman's balance permanently, and the balance only grows. A `mode` column and a filter;
  cheap while the table is small.
- **Petrol and diesel dealer margins have never been entered.** Reconciliation works without
  them (§6.3's split), but profit reporting covers **CBG only**, and Phase 13 is built on it.
- **The first opening balance must be seeded** before any day can be finalised.
- **Does a salesman hold a change float overnight?** If it is inside the locker figure, fine.
  If it is in his pocket, every count is short by it.
- **Is a surplus ever booked?** A negative gap is reported and has no record type.
- CBG's real max flow rate in kg/min; whether testing quantities are recorded on paper, and in
  what unit; `EXPENSE_RECEIPT_THRESHOLD` = ₹5,000 is still a guess; the real expense category
  and credit customer lists.

---

## 8. The check no test replaces

**Change a real price and read the trail back.** Enter a rate as the admin, then
`GET /audit-logs?table_name=fuel_prices`, and confirm the row names the right person, the right
instant and the right rate — as a string, to the paisa.

**Edit a credit limit and confirm the change is legible.** This is the one that matters. §6.6
refuses a sale above the limit, and raising the limit is how that refusal stops happening. The
before-and-after have to be readable *by the owner*, not merely present in JSONB.

**Then decide who is allowed to look.** §3.3 put it at admin on a consistency argument. If that
is wrong for how this outlet actually runs, it is wrong now, before anybody relies on it.
