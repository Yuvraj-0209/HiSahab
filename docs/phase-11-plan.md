# Phase 11 — Audit Retrofit & the Audit Trail Endpoint: Plan, Decisions, and Verification

> Matches the `phase-5/6/7/8/9/10-plan.md` pattern, and follows them in being written
> **before** the code rather than finalised at implementation time. §8 records what actually
> shipped where it differs from this document, and the checklist marks in §7 are filled in as
> they are met.

---

## Context

Phase 10 shipped clean and was **re-verified green at the start of this phase**: 1,135 tests
passing twice back to back against the same database, `alembic` at `0013`, no drift, working
tree clean apart from `.claude/`.

CLAUDE.md §11 describes slot 11 in the past tense and calls what remains "genuinely optional":

> ~~**Audit log**~~ — **built in Phase 4 instead.** … What remains for this slot is
> retrofitting audit writes onto the Phase 3 admin endpoints (fuel types, nozzles, prices,
> margins), which is genuinely optional.

**Two things about that sentence are out of date, and both were verified in the code rather
than inferred.**

**First, the gap is larger than four tables.** That wording was written before Phases 4, 8 and
9 existed. Counting `audit.record` calls against `@router.post` / `@router.patch` in every
router in `app/api/v1/` gives a clean split:

| Router | Write endpoints | `audit.record` calls |
|---|---:|---:|
| `fuel_types.py` | 2 | **0** |
| `nozzles.py` | 2 | **0** |
| `fuel_prices.py` | 1 | **0** |
| `fuel_margins.py` | 1 | **0** |
| `expense_categories.py` (Phase 8) | 2 | **0** |
| `credit_customers.py` (Phase 9) | 2 | **0** |
| `shift_templates.py` (Phase 4) | 2 | **0** |
| `uploads.py` | 1 | 0 — *deliberate, see M4* |
| every other router (10 of them) | 35 | 45 |

So **7 routers and 12 write endpoints** carry the same defect, not four routers and six. The
owner chose to close all seven. The three extra tables are not incidental: an admin flipping
`expense_categories.requires_receipt` changes what §6.11 demands of every expense filed from
that moment; an admin editing `credit_customers.credit_limit` changes what §6.6 refuses; an
admin editing `outlet_shift_templates` changes the default `started_at` that §6.3 prices a
shift from. Each is a control decision with money downstream and no record of who made it.

**Second, nothing can read `audit_logs` back.** No endpoint on any router selects from the
table — grep returns zero. Every phase since 4 has written to a table whose only reader is a
raw SQL prompt. §5.3 says an audit trail is *required* for a cash system; a trail nobody can
read is not one. The owner chose to add the read endpoint here.

**Why this is a phase and not a footnote.** §5.3 states the distinction the table exists for:

> `created_by` / `updated_at` columns are **change tracking**, not an audit trail. They tell
> you who last touched a row, not what it was before or how many times it changed.

All seven tables have `created_by`. None has history. `fuel_prices` and `fuel_margins` are the
sharpest case: they are append-only precisely because §4.1 says a mutable price column
"silently corrupts every historical report" — yet a **backdated** `effective_from`, which the
router explicitly permits and merely logs a warning about, can revalue a closed shift with no
record of who entered it or when. That is this project's stated primary failure mode (a
plausible wrong number) reaching the one table designed to prevent it.

---

## 1. Phase 10 audit — Step 0 (complete)

Per the standing habit, before any Phase 11 code. P6 found 3 defects, P7 3, P8 1, P9 8, P10 3.
**This found no defect and one coverage gap.**

- [x] `pytest` from clean → **1,135 passed**.
- [x] Suite run **twice back to back against the same database** → 1,135 both times. No
      leaked rows.
- [x] `alembic current` → `0013 (head)`; `alembic check` → *"No new upgrade operations
      detected."*
- [x] **Bare `engine.connect()` sweep across the whole `tests/` tree** — P10's third and most
      dangerous finding was a connection left *idle in transaction*, blocking
      `test_migration_is_reversible`'s `DROP TABLE` and hanging the suite past 600s. Phase 11
      adds a migration to that same mid-suite downgrade path, so a recurrence would have
      looked like `0014`'s fault. **Zero occurrences tree-wide** — the only two textual hits
      are the comments explaining the rule. The fix was applied systematically, not just where
      it was found.
- [x] Coverage on every Phase 10 module still **100%**.
- [x] **`services/cash.py::append_reversal` vs the four hand-written reversals** (Phases 6–9).
      They cannot disagree, and the reason is worth recording because it is not the one P10's
      notes gave. P10 said the four were "not retrofitted… they are tested, they work". The
      stronger fact is that **two of them cannot be**: `collections.reverse` takes a separate
      `replacement_reference`, and `expenses.reverse` *recomputes* `receipt_required` for the
      replacement and can raise 422 `EXPENSE_REQUIRES_RECEIPT`. That is **logic**, not values,
      and `append_reversal`'s `carry` / `replacement_values` signature expresses values only.
      A future phase attempting the retrofit would get halfway and discover this; it is now
      written down.
- [x] §13.15's write-off gap is still a limitation, not a live problem — nothing is in
      production yet (P10's own list still owes the seeded opening balance).

### The one finding: the Phase 3 routers are not at 100%

Every Phase 5–10 module is at 100%. The four Phase 3 routers are not — **10 uncovered lines,
all reachable branches**, because they were built before the 100% habit was established:

| Line | Branch |
|---|---|
| `fuel_types.py:182` | `PATCH` a non-existent fuel type → 404 `FUEL_TYPE_NOT_FOUND` |
| `fuel_types.py:200`, `nozzles.py:235` | `PATCH` sending an explicit `null` → the field is skipped |
| `nozzles.py:227` | `PATCH {}` → 422 `NO_FIELDS_TO_UPDATE` |
| `fuel_prices.py:120`, `fuel_margins.py:113` | `POST` against a missing/inactive fuel type → 409 |
| `fuel_prices.py:198`, `fuel_margins.py:188` | `?at=` without a timezone → 422 `NAIVE_TIMESTAMP` |
| `fuel_prices.py:255`, `fuel_margins.py:236` | the `?fuel_type_id=` list filter — **never exercised** |

**No separate Step 0 commit**, because there is no defect to fix test-first — this is missing
coverage, not wrong behaviour. The tests fold into Steps 3–4, where these files are being
edited anyway. Two of the branches are on Phase 11's critical path regardless: `fuel_prices:120`
and `fuel_margins:113` are refusals, and §7's checklist requires proving **a refused write
records no audit row**.

---

## 2. Spec amendment (Step 1, its own commit)

`Spec: what the audit retrofit actually covers, before Phase 11`

`CLAUDE.md` is amended **before any code**, per the standing habit. §14 requires asking first;
both scope questions were put to the owner and answered.

| § | Amendment |
|---|---|
| **§11 slot 11** | Restate. Phase 11 is **seven** routers, not four — the sentence names only the Phase 3 tables because it predates Phases 4, 8 and 9. Drop "genuinely optional": §5.3 requires an audit trail for a cash system, and three of the seven tables gate money rules (§6.11, §6.6, §6.3). Add the read endpoint to the slot's scope |
| **§8** | New row: **Read the audit log** — ❌ attendant, ❌ manager, ✅ admin (D5), beside "Lock a shift / finalise a day" |
| **§5.3** | Document what `audit_logs.outlet_id` means for a row in a **global** reference table (D2): `fuel_types` has no `outlet_id`, so the audit row carries **the outlet whose admin made the change**, not the outlet that owns the row |
| **§5.3** | Record that `record_id` is **not** an FK (already true, already commented in the model) and that the read endpoint therefore returns an **empty page**, not 404, for an id that never existed |
| **§9** | Note the read endpoint's sort key `(changed_at DESC, id DESC)` and that it reuses `encode_cursor` / `decode_cursor` unchanged — documented as serving any `(TIMESTAMPTZ, UUID)` key |
| **§10** | New test block *Audit trail*: every reference-data write records a row; both sides of a `PATCH` are captured; a **refused** write records nothing; the read endpoint is outlet-scoped and admin-only; money inside `old_values` / `new_values` round-trips as a **string** |
| **§13** | New approximation **§13.17**: the audit log has **no retention policy and no archival**. Append-only, unbounded, and the fastest-growing table in the schema. `0014`'s index keeps reads fast; nothing keeps the table small |
| **§14** | New "Do not": **do not add an admin write endpoint without an `audit.record` call in the same transaction** — pinned structurally by D4, so a new router fails the suite rather than the review. New "Do not": **do not use `AuditAction.status_change` for a reference-data deactivation** — it means a *shift* lifecycle move (§5.2), and blurring it makes the label unqueryable (D3) |

---

## 3. Decisions

### D1 — All seven routers, because the wording is older than the gap

**The owner's answer.** §11's list names the Phase 3 tables because it was written in Phase 1.
Each of the three later tables has the identical defect and a sharper consequence:

* **`expense_categories.requires_receipt`** is the knob §6.11 exists to give the admin. §6.11
  goes to considerable trouble to snapshot the answer onto each expense at insert *precisely
  because the flag is editable* — but the flip itself is invisible. The system records what the
  rule was that day, and not who changed it.
* **`credit_customers.credit_limit`** decides what §6.6 refuses. Raising a limit is how a
  ₹5,000-over sale becomes a legal one. §6.6 already insists an *override* is both stored and
  audit-logged; quietly raising the limit instead achieves the same outcome with neither.
* **`outlet_shift_templates.starts_at_local`** supplies the default `started_at` that §6.3
  prices a whole shift from. §5.1 already warns that "editing a template must not revalue a
  shift that already happened" — it does not, because the value is materialised onto the shift
  row. But the edit still moves every *future* shift's valuation instant, unrecorded.

`uploads.py` is the one write endpoint deliberately left unaudited — see M4.

### D2 — A global row's audit entry carries the acting admin's outlet

`audit_logs.outlet_id` is `NOT NULL` (§5.0: no parent row to derive tenancy from).
`fuel_types` is **global reference data** with no `outlet_id` — §5.1's "a litre is a litre at
every outlet". The two facts collide on the one table where they meet.

The answer is `actor.outlet_id`, already resolved by `require_role(...)` at every one of the
twelve call sites, so nothing new is plumbed in. **What matters is that the semantics are
written down**: on a `fuel_types` audit row, `outlet_id` means *the outlet whose admin made
this change*, not *the outlet this row belongs to*. In V1 there is one outlet and the
distinction is invisible. The day there are two, an admin at outlet B adding XP-95 writes a row
stamped B for a fuel that belongs to everyone — and a reader filtering by outlet A would
conclude, from an entirely correct query, that it never happened.

Rejected: making the column nullable (weakens §5.0's tenancy guarantee on the one table with no
other tenancy signal, to accommodate a single case); a sentinel outlet id (invents a row that
does not exist). Documenting the meaning costs a comment and is the only option that leaves the
column honest.

### D3 — Every reference-data write is `insert` or `update`, never `status_change`

`app/core/audit.py` documents `status_change` narrowly:

> a lifecycle move, e.g. a shift going open -> closed -> locked, or an admin reopening one.
> Distinguished from `update` because §5.2 singles out backwards transitions as the thing that
> must be traceable.

A `PATCH` flipping `is_active` to `false` looks like a lifecycle move and is not one.
Deactivating a nozzle is an ordinary field change, already fully legible in `old_values` /
`new_values` — and because §3 rule 6 forbids hard deletes, `is_active` is how **every** one of
these seven tables retires a row. Admitting it as a `status_change` would make that label mean
"a shift moved, or anything at all was deactivated", and anyone querying for shift lifecycle
events would have to filter it back out.

So: **`insert` on create, `update` on `PATCH`, including an `is_active` flip.** The enum's
labels are fixed at migration time, and this decision means Phase 11 needs no enum change.

### D4 — The structural test is the actual deliverable

Twelve `audit.record` calls are an afternoon. **The reason this gap survived seven phases is
that nothing failed when it was missing** — and nothing will fail in Phase 12 or 13 either.
This codebase already knows the shape of the answer: §6.9 records that a `_CONSTRAINT_ERRORS`
entry was forgotten *twice*, and the fix was not more care but `tests/test_errors.py` asserting
structurally against `pg_constraint`.

So `tests/test_audit_coverage.py` parses every module in `app/api/v1/` with `ast`, finds every
function decorated with `@router.post` or `@router.patch`, and asserts each body contains a call
to `audit.record`. The exemption list is **explicit, short, and reasoned in the test's own
docstring** — currently one entry, `uploads.py::upload_receipt` (M4).

An AST walk, not a grep, for the reason P10 recorded when a structural test tripped over the
comment documenting the rule it checked: *"Left as a text search, it would have taught the next
person to delete the comment."*

This test is what makes the phase durable. Everything else in it is bookkeeping.

### D5 — `GET /audit-logs` is admin-only, cursor-paginated, outlet-scoped

§8 has no row for reading the audit log because nothing could. Adding one at **admin**:

* §5.3 frames `audit_logs` as the control record, not a report. §8's manager floor covers
  reports — "Read all shifts / reports", "Read the month-end expense summary". This is the
  record of what managers did, and a manager reading it is a different act.
* It sits with §8's existing admin-only rows: lock a shift, finalise a day, manage users and
  customers, override a credit limit.
* **It is also a leak boundary.** `old_values` / `new_values` for `credit_customers` contain
  `phone` and `credit_limit` — exactly the two fields §8 forbids an attendant from seeing in the
  customer list ("**name and vehicles only**"). A manager floor here would route around a §9
  restriction through a different endpoint. Admin-only is not convention; it is the only level
  that does not contradict a rule already in the spec.

Shape, copied from `fuel_prices.py::list_fuel_prices` rather than re-derived:

```
GET /api/v1/audit-logs
  ?table_name=credit_customers      # optional
  &record_id=<uuid>                 # optional
  &changed_by=<uuid>                # optional
  &action=update                    # optional, AuditAction
  &limit=50&cursor=<opaque>
```

Always `WHERE outlet_id = actor.outlet_id`. Sorted `(changed_at DESC, id DESC)`, keyset
predicate via `tuple_(...) < tuple_(...)`, `limit + 1` sentinel, `encode_cursor` /
`decode_cursor` **unchanged** — `app/api/cursor.py` already states the pair "serves any
`(TIMESTAMPTZ, UUID)` sort key, not only `effective_from`", and Phase 7 reused it for
`created_at`. A third encoder would be the drift that module exists to prevent.

**No `GET /audit-logs/{id}`.** A single audit row is meaningless alone; the question is always
"what happened to this record", which `?record_id=` answers. §11's rule against scaffolding.

### D6 — One migration, for the index the read endpoint needs

`audit_logs` has `ix_audit_logs_record` on `(table_name, record_id)` and
`ix_audit_logs_changed_at` on `changed_at` alone. D5's default query — no filters,
outlet-scoped, sorted `(changed_at DESC, id DESC)` — matches neither, and this is the
fastest-growing table in the schema.

`0014_audit_log_read_index.py` adds `ix_audit_logs_outlet_changed_at` on
`(outlet_id, changed_at DESC, id DESC)`. That is the whole migration: no columns, no enums, no
tables. `down_revision = "0013"`.

Deliberately **not** included: a partial index per `table_name`, or a GIN index on the JSONB
columns. Both are speculative until a query needs them (§11), and the JSONB columns are read,
never filtered on.

### Mechanical decisions

| # | Decision | Reason |
|---|---|---|
| M1 | `audit.record(...)` is called **before** `db.commit()` in all twelve endpoints, never after | `services/audit.py`'s contract: *"the caller commits"*, so the audit row and the change land in one transaction or neither. A separately committed audit row can describe a change that was rolled back — "a log that lies" |
| M2 | Each of the seven routers gains a private `_audit_snapshot(row) -> dict[str, object]` | The house pattern, in nine routers already (`shifts.py:169`, `credit_sales.py:180`, …). A deliberate subset of columns, not the whole row |
| M3 | `PATCH` handlers capture `before = _audit_snapshot(row)` **before** mutating | `credit_sales.py:527` verbatim. Capturing after records the change twice and the history never |
| M4 | `uploads.py::upload_receipt` stays unaudited, as the structural test's single documented exemption | An attachment is not a business row. §6.10 argues this for idempotency (*"a retried upload creates a second row the client simply does not use"*), and §7.4 hard-deletes unlinked ones — auditing a row designed to be swept records a fact about garbage. The **link** is the money event, and `expenses.py` / `credit_sales.py` already audit it |
| M5 | No `Idempotency-Key` on any of the twelve | §6.10 covers POSTs creating a **money record**. These are reference data, and each has a natural key already refusing a duplicate (`uq_fuel_types_code`, `uq_nozzles_outlet_label`, `uq_credit_customers_outlet_phone`, …). Building it here is scaffolding ahead (§11) |
| M6 | The response exposes `old_values` / `new_values` as `dict \| None`, passed through unchanged | `services/audit.py::_encode` already stringifies `Decimal` on the way in, so money is a string in JSONB and stays one. Re-encoding at read time would be a second place a money value could be floated |
| M7 | Registered in `router.py` under a `# Phase 11 --` block comment; static path, no `/{id}` sibling | P10's M9 — the static-before-parameterised hazard does not arise with no parameterised route, and not creating one keeps it that way |
| M8 | No `services/audit_logs.py` | The read is one `select()` with four optional `where`s. P10 recorded that this repo deletes an uncovered helper rather than carrying it (§11); a service layer with one caller and no logic is that helper |
| M9 | `action` is typed `AuditAction \| None`, so an invalid value is a **422**, not a silent empty page | The `FuelTypeUpdate` reasoning: *"the silent version is the dangerous one"* — an admin filtering on a typo and getting zero rows would reasonably conclude nothing happened |
| M10 | Money in fixtures as **strings cast in SQL**, never Python floats | §3 rule 1, "including in a quick test fixture" |

---

## 4. Build order

| Step | What | Commit message |
|---|---|---|
| 0 | Phase 10 audit — **no defect, no commit** (§1) | — |
| 1 | `CLAUDE.md` amendment (§2) **and** this document | `Spec: what the audit retrofit actually covers, before Phase 11` |
| 2 | `alembic/versions/0014_audit_log_read_index.py` + `tests/test_migrations.py` assertions | `Phase 11 Step 2: an index for the audit read path` |
| 3 | `fuel_types.py`, `nozzles.py` — `_audit_snapshot` + 4 `audit.record` calls, with tests, **plus Step 0's coverage gaps in these two files** | `Phase 11 Step 3: audit writes on fuel types and nozzles` |
| 4 | `fuel_prices.py`, `fuel_margins.py` — insert-only, with tests, **plus Step 0's coverage gaps in these two** | `Phase 11 Step 4: audit writes on the append-only price and margin tables` |
| 5 | `expense_categories.py`, `credit_customers.py`, `shift_templates.py`, with tests | `Phase 11 Step 5: audit writes on the reference data §11 forgot` |
| 6 | `app/api/v1/audit_logs.py` + `router.py` registration | `Phase 11 Step 6: GET /audit-logs -- the trail becomes readable` |
| 7 | `tests/test_audit_coverage.py` — D4's structural test | `Phase 11 Step 7: a new admin write endpoint without an audit row fails the suite` |
| 8 | This document's final state + `docs/phase-11-notes.md` | `Phase 11 Step 8: plan and notes docs` |

Tests are distributed across every step, never a separate pass.

### Files touched

**New:** `alembic/versions/0014_audit_log_read_index.py`, `app/api/v1/audit_logs.py`,
`tests/test_audit_logs_api.py`, `tests/test_audit_coverage.py`,
`tests/test_reference_data_audit.py`, `docs/phase-11-plan.md`, `docs/phase-11-notes.md`.

**Modified:** `CLAUDE.md`, `app/api/v1/router.py`, the seven routers (`fuel_types.py`,
`nozzles.py`, `fuel_prices.py`, `fuel_margins.py`, `expense_categories.py`,
`credit_customers.py`, `shift_templates.py`), plus `tests/test_migrations.py`,
`tests/test_routes.py`, and the four Phase 3 test files gaining Step 0's coverage.

**The per-router edit is one pattern, twelve times.** Add two imports (`from app.core.audit
import AuditAction`, `from app.services import audit`), add `_audit_snapshot`, and insert an
`audit.record(...)` between the mutation and the `db.commit()` — plus `db.flush()` first on
creates, to materialise the id. `app/api/v1/credit_sales.py:418-444` (insert) and `:527-562`
(update) are the two shapes to copy verbatim.

### The `conftest.py` teardown question — verified, not assumed

The standing habit says every new child table means revisiting every fixture that deletes a
parent. **Checked, and little changes** — recorded so the executor does not over-engineer it:

`tests/conftest.py::make_user`'s teardown already ends with a blanket
`DELETE FROM audit_logs WHERE … OR changed_by = ANY(:ids)` (line ~239), inside a
disable/re-enable of `trg_audit_logs_append_only`. Every audit row Phase 11 writes goes through
an authenticated request, so `changed_by` is always a `make_user` id, and that sweep already
catches them **whatever `table_name` they carry**. `audit_logs.record_id` is deliberately not an
FK, so the reference-data test files' raw `DELETE FROM fuel_types WHERE code = …` teardowns
cannot FK-error either.

So the work is **verification, not construction**:

- [ ] Confirm the `changed_by` sweep covers every path, including tests that create a fuel type
      via the API and tear it down in a `finally` block.
- [ ] Confirm no reference-data test authenticates as a user created outside `make_user`.
- [ ] Run the suite **twice back to back** — a leaked audit row is exactly what that catches.

---

## 5. Error codes introduced

**None**, and that is worth stating rather than leaving as an absence. The twelve retrofits add
no new failure mode, and the read endpoint's only refusals already exist: `INVALID_CURSOR`
(from `decode_cursor`, unchanged), `INSUFFICIENT_ROLE`, `NOT_A_MEMBER`, `MEMBERSHIP_INACTIVE`
(from `require_role`, unchanged), and FastAPI's own 422 for a malformed `action` or `record_id`.

**No `_CONSTRAINT_ERRORS` additions** — `0014` adds an index, not a unique constraint.
`tests/test_errors.py`'s two structural assertions must pass **unmodified**.

---

## 6. Not in Phase 11

Audit retention, archival or partitioning (§13.17 records the unbounded growth; a policy needs
a real row count behind it); a GIN index on `old_values` / `new_values`; diffing or rendering
changes in the API (the client receives both snapshots and can diff them); audit rows for
**reads** (§5.3 scopes the table to changes, and logging reads would multiply the row count by
an order of magnitude for a control nobody asked for); the frontend (Phase 12); reporting
(Phase 13); a shortfall write-off `mode` column (§13.15 — a decision the owner still owes).

---

## 7. Verification checklist

### A — suite and migration health

- [ ] `docker compose up -d db`, then **full** `pytest` green — not `-k audit`. Total
      ≥ 1,135 + the new files' count, with **no test deleted or weakened** to get there.
- [ ] Suite run **twice back to back against the same database** — the leaked-audit-row check.
- [ ] `alembic upgrade head` → `0014`; `alembic check` reports no new operations.
- [ ] `alembic downgrade base && alembic upgrade head` round-trips; `0014`'s `downgrade()`
      drops the index and leaves nothing in `pg_indexes`.
- [ ] `tests/test_migrations.py::test_migration_is_reversible` still passes — it downgrades
      mid-suite, so any new session-scoped fixture breaks it.
- [ ] `tests/test_migrations.py` gains a name-level assertion for
      `ix_audit_logs_outlet_changed_at`.
- [ ] 100% coverage on `app/api/v1/audit_logs.py`, **and on all four Phase 3 routers** —
      Step 0's ten lines closed, taking the whole app to 100%.
- [ ] `tests/test_routes.py` passes — the new route is under `/api/v1`.
- [ ] `tests/test_errors.py`'s two structural constraint tests pass **unmodified**.

### B — the structural guarantee (D4)

- [ ] `tests/test_audit_coverage.py` exists and **parses with `ast`**, not `grep`.
- [ ] It walks **every** module in `app/api/v1/`, discovered by directory listing, not a
      hardcoded list — a new router must be picked up without editing the test.
- [ ] It finds every `@router.post` / `@router.patch` function and asserts an `audit.record`
      call in the body.
- [ ] **Deliberately break it**: comment out one `audit.record` call and confirm the test fails
      naming that endpoint. A structural test never run red is a test that passes for unknown
      reasons.
- [ ] The exemption list has exactly one entry (`uploads.py::upload_receipt`) with M4's reason
      in the test's docstring.
- [ ] It asserts a **floor on the number of audited write endpoints**, so a router silently
      dropped from discovery fails rather than passing vacuously.

### C — the twelve retrofits

- [ ] **Each of the twelve writes exactly one audit row on success**, asserted per endpoint
      (`SELECT count(*) FROM audit_logs WHERE table_name = :t AND record_id = :id` → 1). Twelve
      assertions, not one loop — a loop that silently skips is the failure mode.
- [ ] `table_name` matches the real table for each (`fuel_types`, `nozzles`, `fuel_prices`,
      `fuel_margins`, `expense_categories`, `credit_customers`, `outlet_shift_templates`).
- [ ] Creates record `action = insert`, `new_values` set, `old_values` **NULL**.
- [ ] `PATCH`es record `action = update` with **both** sides populated and genuinely different —
      assert `old_values != new_values`, which a mis-ordered snapshot (M3) would fail.
- [ ] **No endpoint records `status_change`** (D3) — asserted across all seven `table_name`s.
- [ ] **A refused write records nothing.** Per router: a 409 duplicate, a 422 immutable field
      (`fuel_types.code`, `expense_categories.code`, `nozzles.fuel_type_id`,
      `shift_templates.sequence`), and a 403 non-admin → `count(*) == 0` on `audit_logs`. This
      is `tests/test_audit.py::test_a_refused_transition_writes_no_audit_row`'s shape, and it is
      what proves M1's single-transaction contract.
- [ ] **`changed_by` is the acting admin**, not the row's `created_by` — they differ when an
      admin edits a row another admin created; test that case explicitly.
- [ ] **`outlet_id` on a `fuel_types` audit row is the acting admin's outlet** (D2), with the
      semantics in a comment at the call site.
- [ ] `request_id` round-trips an inbound `X-Request-ID` header — `test_audit.py`'s pattern,
      applied to one reference-data endpoint.
- [ ] **Money survives as a string.** `credit_customers.credit_limit` and
      `fuel_prices.rate_per_unit` appear in `new_values` as `"1000.00"`, not `1000.0` — asserted
      end to end through the API, not at the helper.
- [ ] A `credit_limit` of `None` (unlimited, §6.6) survives as JSON `null` and is **not**
      coerced to `0` anywhere in the snapshot.

### D — `GET /audit-logs` (D5)

- [ ] Admin → 200; **manager → 403**; attendant → 403.
- [ ] Admin at **another outlet** → 403 `NOT_A_MEMBER` (Phase 9's D8 posture).
- [ ] Results are **outlet-scoped**: a row written at another outlet is absent, asserted by
      inserting one directly rather than inferring from an empty page.
- [ ] `?table_name=`, `?record_id=`, `?changed_by=`, `?action=` each filter correctly and
      **combine** — at least one two-filter case.
- [ ] An unknown `record_id` returns an **empty page**, not 404 (§5.3's non-FK consequence).
- [ ] An invalid `action` → **422**, not an empty page (M9).
- [ ] Cursor pagination: page through more rows than `limit`; assert **no duplicate and no
      skipped id** across pages, and `next_cursor is None` on the last page.
- [ ] **Insert a row mid-walk and confirm the reader neither repeats nor skips** — the failure
      §9 forbids `OFFSET` for, and the only test that proves the keyset is real.
- [ ] Rows come back **newest first**.
- [ ] A malformed cursor → 422 `INVALID_CURSOR`, from `decode_cursor` unchanged.
- [ ] `limit` above `MAX_LIMIT` → 422; at `MAX_LIMIT` → 200.
- [ ] `old_values` / `new_values` come back as objects with money as **strings** (M6).
- [ ] **No `@router.post`, `@router.patch` or `@router.delete` on this router** — the read
      endpoint must not become a way to write the append-only table.

### E — structural assertions no value test makes

- [ ] `float(` / `sa.Float` / `Float(` → zero matches in `app/api/v1/audit_logs.py`.
- [ ] `.offset(` → zero (§14).
- [ ] No `relationship()` anywhere new.
- [ ] `app/api/cursor.py` **unmodified** — D5 reuses the existing pair, and a third encoder
      would be the drift that module exists to prevent.
- [ ] `app/core/audit.py` **unmodified** — D3 means no new enum label, so no enum migration.
- [ ] `app/services/audit.py` **unmodified** — the helper was correct; only its callers were
      missing.
- [ ] `DEFAULT_OUTLET_ID` appears nowhere in `audit_logs.py` — the outlet comes from `actor`.
- [ ] All seven routers import `audit` from `app.services`, not a local copy.

### F — the checks no test replaces

- [ ] **Change a real price and read the trail back.** Enter a fuel price as the admin, then
      `GET /audit-logs?table_name=fuel_prices`, and confirm the row names the right person, the
      right instant, and the right rate — as a string, to the paisa.
- [ ] **Edit a credit limit and confirm the change is visible.** This is the one that matters:
      §6.6 refuses a sale above a limit, and raising the limit is how that refusal is bypassed.
      Confirm the before-and-after are both legible to the owner, not merely present.
- [ ] **Confirm with the owner who should read this.** D5 puts it at admin, reasoning that it is
      a control record and that manager access would route around §9's restriction on customer
      phone numbers and limits. If the owner expects a manager to review it, D5 is wrong and §8
      needs revisiting **before** the endpoint is live.
- [ ] **Look at the row count after a week of real use**, and decide whether §13.17's
      no-retention-policy note needs to become a task rather than a note.

---

## 8. What actually shipped

*(Filled in as the phase lands, including where it differs from this document and why.)*

---

## 9. Still owed by the owner

**New:**

- **Who reads the audit log?** D5 says admin only. If a manager is expected to review it, §8
  needs amending and §9's customer-field restriction needs a separate answer, because the trail
  contains phone numbers and credit limits.
- **How long is the audit trail kept?** §13.17 records that there is no answer today and the
  table grows without bound. Not urgent; cheap now, expensive at ten million rows.

**Carried forward from Phase 10** — all still open, all still load-bearing:

- **There is no way to write off a shortfall** (§13.15). A ₹20 gap nobody will chase stays on a
  salesman's balance permanently, and the balance only ever grows. A `mode` column and a filter;
  cheap only while the table is small.
- **Petrol and diesel dealer margins have never been entered.** Reconciliation works without
  them (§6.3's split), but **profit reporting covers CBG only** — and Phase 13 is built on it.
- **The first opening balance must be seeded** before any day can be finalised.
- **Does a salesman hold a change float overnight?** If it is inside the locker figure, fine.
  If it is in his pocket, every count is short by it.
- **Is a surplus ever booked?** A negative gap is reported and has no record type.
- CBG's real max flow rate in kg/min; whether testing quantities are recorded on paper, and in
  what unit; `EXPENSE_RECEIPT_THRESHOLD` = ₹5,000 is still a guess; the real expense category
  and credit customer lists.
