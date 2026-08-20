# Phase 6 — Collections: Plan, Definition of Done, and Verification Checklist

> At implementation time this file should be copied to `docs/phase-6-plan.md`, matching
> the `phase-5-plan.md` / `phase-5-notes.md` pattern already established.

---

## Context

Phase 5 made the meters produce rupees. `GET /shifts/{id}/sales` now says what the pump
*sold*. Nothing yet says what the pump *received* — and the gap between those two numbers is
the entire reason this system exists.

Phase 6 records money as it arrives, tagged by how it arrived (`cash`, `card`, `upi`,
`wallet`), and lands three things the rest of the build depends on:

1. **`collections`** — the first table carrying a rupee amount a human types.
2. **`idempotency_keys`** (§6.10) — deferred from Phase 5 with a written reason, now due.
3. **The §6.9 reversal pattern** — collections is the first table the correction rule can
   actually apply to, so the shape built here is the one expenses (Phase 7) and credit
   sales (Phase 9) will copy.

It does **not** build the cash equation. §6.4 is Phase 10.

---

## 1. Phase 5 verification — verdict

**Complete. Suite green, no drift, nothing stubbed.**

```
pytest                    ->  399 passed in 7.50s
alembic check             ->  No new upgrade operations detected
alembic heads / current   ->  0005 (head)
```

### Obligation-by-obligation

| §11 Phase 5 obligation | Status | Evidence |
|---|---|---|
| §6.2 rollover, reset, `TOTALIZER_DECREASED`, `TESTING_EXCEEDS_THROUGHPUT`, per-fuel ceiling, never-negative | ✅ | `app/services/sales.py` — pure, no `Session`; 6 error codes; 8-row parametrised never-negative sweep |
| §4.7 chain: carried opening, stored not computed, anchoring, variance reason, review flag | ✅ | `app/services/readings.py:59-71`; `chained_opening_reading` written at insert (`readings.py:491`); `ANCHOR_REQUIRES_ADMIN` 403; DB CHECK `ck_nozzle_readings_variance_has_reason` |
| §6.3 `rate_at` / `margin_at` at `started_at`, mid-shift revision warning | ✅ | `services/readings.py:317-374`; refuses with `NO_PRICE_FOR_DATE` rather than returning zero |
| §6.8 `MISSING_NOZZLE_READINGS` wired into close | ✅ | `app/api/v1/shifts.py:558-569`, correctly ordered *after* `ended_at` is applied |
| §13.10 flag-don't-recompute; `NOT_THE_LATEST_SHIFT` removed | ✅ (with a hole — see C) | `NOT_THE_LATEST_SHIFT` appears nowhere in `app/`; `flag_downstream_reading` at `services/readings.py:136-180` |
| Review columns + an endpoint that clears the flag | ✅ | `PATCH /shifts/{id}/readings/{nozzle_id}/review`, deliberately not `writable=True` |
| §13.7 profit labelling | ✅ | response key is `gross_fuel_margin`; `margin_basis` travels in the payload, not the frontend |

### Three findings folded into Step 0

**A. Both meter flags true via the API returns 500, not 422.**
No application-level mutual-exclusion check exists. `create_reading` → `_validate_math` →
`quantity_if_known` → `awaiting_override()` is True → returns `None` unvalidated
(`app/api/v1/readings.py:268-274`), then `db.flush()` hits `ck_nozzle_readings_flags_exclusive`.
`app/core/errors.py` installs no `IntegrityError` handler, so it falls through to
`unhandled_exception_handler` → 500 `INTERNAL_ERROR`. The only test for that constraint
(`tests/test_readings_api.py:594`) inserts raw SQL and never touches the route.

**This blocks Phase 6.** Every CHECK constraint migration `0006` adds fails the same way.

**B. The flow ceiling treats admin overrides two different ways.**
`app/api/v1/readings.py:288` applies the ceiling to a non-reset override;
`app/services/readings.py:433` skips it. `docs/phase-5-notes.md:153` states the intended
rule ("skips a reading carrying an admin override") — the entry-time site doesn't implement
it. So an admin's signed figure is refused at entry and never re-checked at close.

**C. §13.10's downstream flag follows one shift, not the chain.**
`flag_downstream_reading` calls `next_shift_after` (the next shift, *any* nozzle) and stops
if that shift has no reading for the nozzle (`services/readings.py:150-161`). But
`chained_opening` deliberately *skips* shifts with no closing for that nozzle
(`services/readings.py:59-71`, proven by `tests/test_reading_chain.py:122`). So: nozzle out
of order for shift 2 → shift 3's opening was carried from shift 1 → editing shift 1's
closing leaves shift 3 **silently stale**. That is precisely the guarantee §13.10 exists to
provide.

### Carried, not blocking

- No `manual_quantity_override >= 0` CHECK, unlike every sibling numeric column.
- A deactivated nozzle freezes its own historical `requires_review` flags —
  `_load_nozzle` raises `NOZZLE_INACTIVE` before `review_reading` can run.
- `testing_quantity` is silently ignored when an override is present (`sales.py:177-197`) —
  defensible, undocumented.
- No list endpoint for flagged readings, despite `ix_nozzle_readings_review` being built for
  exactly that query.
- `_warn_on_mid_shift_revision` fires per *nozzle* not per fuel type, and not at all when
  every nozzle is unread.

---

## 2. Decisions taken (owner-confirmed)

| # | Decision | Consequence |
|---|---|---|
| 1 | **One live row per mode.** One card machine, one UPI QR; the register writes one lumped figure per mode. | Enforced in the service layer, **not** by a unique constraint — see §3.1 |
| 2 | **A shift cannot close without an explicit `cash` row; ₹0 is a valid declaration.** | `MISSING_COLLECTIONS` fires on *absence*, never on a mismatch against sales |
| 3 | **Build the §6.9 reversal pattern now.** | `reverses_id` + `reversal_reason` on `collections`; a dedicated endpoint |
| 4 | **Step 0 fixes findings A, B, C test-first before any Phase 6 code.** | |
| 5 | **`other_cash_income` stays out of Phase 6.** | Lubricant/coolant money is a §6.4 term; §6.4 is Phase 10. It lands on `daily_cash_summaries` there |

### The insight that shapes the whole phase

§6.4 computes cash as a **residual**:

```
cash_sales = total_sales − card − upi − wallet − credit_sales_amount
```

The `mode = cash` row is **not in that formula**. So it is not an input to the cash
equation — it is the **independent observation the equation gets checked against**, exactly
matching how the outlet already works (§14: *"the salesman reconciles his own shift … a
shortfall is booked as udhaar against his own name"*).

- **Derived cash** — what the meters say he should hold.
- **Declared cash** — the `mode = cash` collection row: what he says he counted.
- The gap is the shortfall.

Same shape as §4.7's chain: the system predicts, a human confirms, **both are stored**, and
a disagreement leaves a trace. This is why decision 2 requires an explicit ₹0 rather than
accepting absence — zero as an *answer*, never zero as an *omission*.

**Write this into `app/models/collection.py`'s docstring.** If a later phase sums all
collections *including* cash and also adds derived `cash_sales`, the day is silently wrong
by the full cash figure — the plausible-but-wrong number CLAUDE.md exists to prevent.

---

## 3. Design decisions needing a note in the spec

### 3.1 ⚠ `UNIQUE (shift_id, mode)` is incompatible with §6.9 — flagged per §14

A unique constraint and reversals cannot coexist. Monday's ₹60,000 row stays forever, so
the corrected ₹58,000 row for the same `(shift_id, mode)` collides with it. Every partial-index
variant fails the same way: the replacement carries `reverses_id IS NULL`, like the original.

Preserving it would need a `reversed_at` marker stamped onto the original row — an `UPDATE`
on a financial row in a closed shift, which is the thing §6.9 forbids.

**Resolution: no unique constraint. One live row per mode is enforced in the service layer.**

- `POST` refuses when a live row for that mode exists → 409 `COLLECTION_ALREADY_EXISTS`,
  and the client `PATCH`es. Same shape as Phase 5's `READING_ALREADY_EXISTS`.
- *Live* = not itself a reversal, and not referenced by any reversal.
- Retry safety comes from the `Idempotency-Key` store — which §6.10 mandates regardless, and
  which also protects the reversal endpoint, something a unique constraint never would have.

This is consistent with the §6.10 amendment Phase 5 wrote: collections have no natural key.

### 3.2 The enum is `collection_mode`, not `payment_mode`

§5.2 gives `collections.mode` = `cash | card | upi | wallet` and `credit_repayments.mode` =
`cash | card | upi | bank_transfer`. **Different sets.** A shared `payment_mode` type would
force Phase 9 to either alter a live enum or carry a value that is invalid for it.

### 3.3 `idempotency_keys` carries no `outlet_id`

§5.0's rule is about tenancy on business rows with no correct backfill. These rows are
infrastructure, are keyed by `user_id` + `endpoint`, and expire after 24 hours — there is
never a retrofit to get wrong, because the data does not survive to be migrated. Note it in
§5.3 alongside `attachments`.

### 3.4 Reversal on a `locked` shift is admin-only, not refused

§6.9 names locked shifts as needing the reversal path; §5.2 says nothing referencing a
locked shift may be modified. Both hold, because **a reversal appends and modifies nothing**.
`app/api/deps.py`'s existing `SHIFT_LOCKED` message already says *"Corrections must be
recorded as reversal entries"* — the codebase anticipates this.

### 3.5 The close check must not call `shift_sales`

`shift_sales` raises 409 `NO_PRICE_FOR_DATE` / `NO_MARGIN_FOR_DATE`, and **petrol and diesel
margins have never been entered** (`docs/phase-5-notes.md:312-315`). Using it in
`close_shift` would make every petrol shift unclosable.

The check needs only *"did any quantity move"*, not *"what was it worth"* — so it reads
`readings_service.quantity_if_known` per reading and asks `any(q > 0)`. Never sums across
units (§4.5: 500 L + 100 kg is not 600 of anything).

---

## 4. Schema — migration `0006`

### `collections`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | `gen_random_uuid()` |
| `shift_id` | UUID FK → shifts | NOT NULL. **No `outlet_id`** — derivable (§5.0) |
| `mode` | `collection_mode` enum | `cash \| card \| upi \| wallet` |
| `amount` | NUMERIC(12,2) | NOT NULL. ≥ 0 normally, < 0 only on a reversal |
| `reference` | Text | nullable — settlement / batch reference |
| `reverses_id` | UUID FK → collections.id | nullable. **New — §5.2 amendment** |
| `reversal_reason` | Text | nullable, required when `reverses_id` set. **New** |
| `created_at`, `created_by` | | standard |

**Constraints**

- `uq_collections_reverses_id UNIQUE (reverses_id)` — a row can be reversed once, ever.
- `ck_collections_reversal_has_reason` — `reverses_id IS NULL OR reversal_reason IS NOT NULL`
  (mirrors `ck_nozzle_readings_override_has_reason`).
- `ck_collections_amount_sign` —
  `(reverses_id IS NULL AND amount >= 0) OR (reverses_id IS NOT NULL AND amount <= 0)`.
  A negative amount is *only* ever a reversal.
- `ck_collections_reversal_not_self` — `reverses_id IS NULL OR reverses_id <> id`.
- **No unique constraint on `(shift_id, mode)`** — §3.1. Comment the migration with why, so
  a future consistency pass does not helpfully add one (same defensive move as
  `test_readings_are_not_append_only`).
- **No append-only trigger** — a collection is corrected while its shift is open; that is
  the normal workflow. Immutability comes from shift status via `require_shift_access`,
  exactly as Phase 5 argued for `nozzle_readings`.

**Indexes:** `ix_collections_shift (shift_id)`, `ix_collections_shift_mode (shift_id, mode)`.

### `idempotency_keys`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `idempotency_key` | Text | client-supplied |
| `endpoint` | Text | route template, e.g. `POST /shifts/{shift_id}/collections` |
| `user_id` | UUID FK → user_profiles | |
| `request_fingerprint` | Text | SHA-256 of path params + canonical JSON body |
| `response_status` | SMALLINT nullable | NULL = in flight |
| `response_body` | JSONB nullable | |
| `created_at` | TIMESTAMPTZ | |

- `uq_idempotency_keys UNIQUE (idempotency_key, endpoint, user_id)` — §6.10's exact tuple.
- `ix_idempotency_keys_created_at` — for expiry sweeps.

---

## 5. Deliverables, in order

### Step 0 — Phase 5 fixes, test-first (§10), its own commit

1. **`IntegrityError` handler** in `app/core/errors.py`. Maps *known* constraint names to
   `(status, code, detail)`; anything unrecognised **stays 500**, so an unexpected
   constraint failure is never silently downgraded to a business error. First entry:
   `ck_nozzle_readings_flags_exclusive` → 422 `METER_FLAGS_MUTUALLY_EXCLUSIVE`.
   Failing test drives the route, not raw SQL.
2. **Flow ceiling, one rule at both sites.** `_validate_math` (`app/api/v1/readings.py:288`)
   skips the ceiling when `manual_quantity_override is not None`, matching
   `revalidate_flow_rates` (`app/services/readings.py:433`) and the notes' stated rule.
3. **`flag_downstream_reading` follows the chain.** Replace `next_shift_after` with a lookup
   for the *earliest reading of that nozzle* in any later shift, ordered
   `(business_date, sequence)` — the inverse of `chained_opening`'s query. Failing test: a
   three-shift scenario where shift 2 has no reading for the nozzle.
4. **`ck_collections_...`-adjacent tidy:** add the missing
   `ck_nozzle_readings_override_non_negative` CHECK in `0006` while the migration is open.

### Step 1 — spec amendment, its own commit

Same pattern as `22e8339` (pre-Phase-3) and Phase 5's Step 0.

- §5.2 `collections`: add `reverses_id`, `reversal_reason`; replace *"one row per payment
  mode used"* with the §3.1 resolution.
- §5.2: record that `mode = cash` is the **declared** figure, not a §6.4 input.
- §5.3: add `idempotency_keys` and the §3.3 no-`outlet_id` note.
- §6.8: state the `MISSING_COLLECTIONS` rule as decided — absence, never mismatch.
- §14: add *"Do not block a shift close because collections ≠ sales — that gap is §6.4's
  variance, and blocking it teaches staff to type figures that balance."*

### Step 2 — migration `0006` + models

`alembic/versions/0006_collections_and_idempotency.py`, `app/models/collection.py`,
`app/models/idempotency.py`, exports in `app/models/__init__.py`.
Follow `app/models/reading.py` exactly: column-level FKs, no `relationship()`,
`Mapped[...] = mapped_column(...)`, `server_default=sa.text(...)`.

### Step 3 — services

**`app/services/collections.py`** — DB-aware, mirrors `app/services/readings.py`:

```
live_collections(db, *, shift_id)          -> list[Collection]   # excludes reversals + reversed
live_collection_for_mode(db, *, shift_id, mode) -> Collection | None
totals_by_mode(db, *, shift_id)            -> dict[CollectionMode, Decimal]
declared_cash(db, *, shift_id)             -> Decimal | None      # None = never declared
shift_moved_any_quantity(db, *, shift)     -> bool                # §3.5, no pricing
missing_cash_declaration(db, *, shift)     -> bool                # the §6.8 predicate
reverse(db, *, collection, reason, actor)  -> Collection
```

**`app/core/idempotency.py`** — a dependency, not scattered per-route:

- Reads the `Idempotency-Key` header (already allowed in CORS, `app/main.py:50`).
- Inserts a reservation row; on `IntegrityError`, re-reads:
  - fingerprint differs → 422 `IDEMPOTENCY_KEY_REUSED`
  - `response_status IS NULL` → 409 `REQUEST_IN_PROGRESS`
  - otherwise **replay the stored response verbatim**, creating nothing.
- Ignores rows older than 24 h on read (§6.10).
- `app/jobs/cleanup_idempotency_keys.py` — a ~15-line command, same posture as §7.4's
  attachment sweep. Written now because this table is Phase 6's and grows without bound.

### Step 4 — API, `app/api/v1/collections.py`

Schemas inline in the module, matching `readings.py`. `MoneyValue =
condecimal(max_digits=12, decimal_places=2)`, `ConfigDict(extra="forbid")` on every input.

| Method | Path | Role floor | `writable` | Idempotent |
|---|---|---|---|---|
| GET | `/shifts/{shift_id}/collections` | attendant | no | — |
| POST | `/shifts/{shift_id}/collections` | attendant | **yes** | **yes** |
| PATCH | `/shifts/{shift_id}/collections/{collection_id}` | attendant | **yes** | — |
| POST | `/shifts/{shift_id}/collections/{collection_id}/reversals` | manager (admin if locked) | **no** | **yes** |

- Ownership is **never re-implemented** — `require_shift_access` only, per §8 and the
  structural test at `tests/test_reading_permissions.py:332`.
- The reversal route takes an optional `replacement_amount`, applied in the **same
  transaction**. Without it, a correction on a closed shift is impossible: the reversal
  succeeds and the replacement `POST` is then refused by `writable=True`.
- `GET` returns rows plus `totals_by_mode` and `declared_cash`. **Capped full list, no
  cursor** — a shift holds ≤ 4 live rows. Follow `app/api/v1/shift_templates.py:41`'s
  `_MAX_ROWS = 100` and carry the one-line justification §9 deserves.
- Every mutation writes an `audit_logs` row via `app/services/audit.py::record`,
  `table_name="collections"`.
- Register in `app/api/v1/router.py` after `readings.router`.

### Step 5 — wire §6.8 into `close_shift`

At `app/api/v1/shifts.py:553`, replacing the named comment, **after**
`MISSING_NOZZLE_READINGS` (an unread meter is the more fundamental failure):

```python
if collections_service.missing_cash_declaration(db, shift=shift):
    raise AppError(status_code=409, code="MISSING_COLLECTIONS", detail=...)
```

The detail string must tell the user the fix: *"enter the cash figure, and enter ₹0 if no
cash was taken."* Leave `CREDIT_SALE_MISSING_RECEIPT` as a named comment (§11).

**Add a comment stating what this check deliberately does not do:** it never compares
collections against sales. That difference is §6.4's variance.

### Step 6 — tests

New files, mirroring Phase 5's layout:

| File | Covers |
|---|---|
| `tests/test_collections_api.py` | CRUD, one-live-row-per-mode, ₹0 cash, mode isolation, `reference` |
| `tests/test_collections_permissions.py` | §8 matrix + structural tests (no re-implemented ownership, no `float`) |
| `tests/test_idempotency.py` | §10's required case + fingerprint reuse + in-flight |
| `tests/test_reversals.py` | §6.9: original untouched, sum correct, double-reversal refused, locked/admin |
| `tests/test_shift_close_collections.py` | `MISSING_COLLECTIONS` fires and doesn't |
| `tests/test_migrations.py` | additions: constraints, no `(shift_id, mode)` unique, no trigger |
| `tests/conftest.py` | `make_collection` fixture — **and a new teardown generation in every fixture that deletes a parent**, per `phase-5-notes.md:225-230` |

### Step 7 — `docs/phase-6-notes.md`

---

## 6. Error codes introduced

| Code | Status | Meaning |
|---|---|---|
| `COLLECTION_ALREADY_EXISTS` | 409 | A live row for that mode exists; `PATCH` it |
| `COLLECTION_NOT_FOUND` | 404 | |
| `COLLECTION_NOT_IN_SHIFT` | 409 | Id belongs to a different shift |
| `MISSING_COLLECTIONS` | 409 | §6.8 — fuel moved, no cash declared |
| `ALREADY_REVERSED` | 409 | `uq_collections_reverses_id` |
| `CANNOT_REVERSE_A_REVERSAL` | 409 | |
| `REVERSAL_REASON_REQUIRED` | 422 | |
| `IDEMPOTENCY_KEY_REQUIRED` | 400 | Header absent on a money-creating POST |
| `IDEMPOTENCY_KEY_REUSED` | 422 | Same key, different payload |
| `REQUEST_IN_PROGRESS` | 409 | Reservation row exists, response not yet stored |
| `METER_FLAGS_MUTUALLY_EXCLUSIVE` | 422 | Step 0 finding A |

Reused unchanged: `SHIFT_NOT_OPEN`, `SHIFT_LOCKED`, `NOT_YOUR_SHIFT`, `SHIFT_NOT_FOUND`.

---

## 7. Explicitly NOT in Phase 6

- §6.4's cash equation, `expected_closing`, variance, `daily_cash_summaries` — Phase 10.
- `other_cash_income` — Phase 10, on `daily_cash_summaries`.
- `credit_repayments` — Phase 9. Repayment cash is **not** a collection (§4.4).
- Any endpoint comparing declared cash to derived cash — needs `credit_sales` (Phase 9).
- `expenses.mode` — the §6.4-vs-§5.2 contradiction is Phase 7's to decide.

---

## 8. Verification checklist

Tick before calling Phase 6 done. Every line is checkable without reading the diff.

### A. Phase 5 fixes (Step 0)

- [ ] `POST` a reading with `rollover_occurred` **and** `meter_reset_occurred` both true →
      **422 `METER_FLAGS_MUTUALLY_EXCLUSIVE`**, not 500. Test drives the HTTP route.
- [ ] An unmapped `IntegrityError` still returns 500 — the handler allowlists, never
      blanket-converts.
- [ ] A reading with `manual_quantity_override` set and no meter reset is exempt from the
      flow ceiling at **both** entry and close.
- [ ] Three-shift test: nozzle unread in shift 2; edit shift 1's closing → **shift 3's**
      reading is flagged `requires_review`.
- [ ] `manual_quantity_override >= 0` CHECK exists in the database.

### B. Schema

- [ ] `alembic upgrade head` → `0006`; `alembic check` reports no drift.
- [ ] `alembic downgrade base && alembic upgrade head` is clean.
- [ ] `collections` has **no** `outlet_id` (§5.0 — derivable via `shift_id`).
- [ ] `collections` has **no** unique constraint on `(shift_id, mode)`, and the migration
      says why. A test asserts its absence.
- [ ] `collections` has **no** append-only trigger, and a test asserts its absence.
- [ ] All four `collections` CHECKs present; each has a test that trips it in raw SQL.
- [ ] `amount` is `NUMERIC(12,2)`; `grep -rn "float(\|sa.Float" app/` finds nothing in the
      money path.
- [ ] The enum is named `collection_mode`, not `payment_mode`.
- [ ] `idempotency_keys` has `UNIQUE (idempotency_key, endpoint, user_id)`.

### C. Collections behaviour

- [ ] `POST` cash on an open shift → 201, row created, audit row written.
- [ ] A second `POST` for the same mode → **409 `COLLECTION_ALREADY_EXISTS`**.
- [ ] `PATCH` corrects the amount on an open shift; audit row carries old *and* new values.
- [ ] `POST` with `amount = 0` for cash → **201**. Zero is a declaration.
- [ ] `POST` with a negative amount and no `reverses_id` → 422.
- [ ] `GET` returns `totals_by_mode` and `declared_cash`, and `declared_cash` is `null` —
      **not `0`** — when no cash row exists.
- [ ] `GET` never returns a total summing cash together with derived sales.

### D. Idempotency (§6.10)

- [ ] Same `Idempotency-Key` twice → **one row**, byte-identical response both times.
- [ ] Same key, *different* body → 422 `IDEMPOTENCY_KEY_REUSED`, nothing created.
- [ ] Same key, different **user** → both succeed. The tuple is `(key, endpoint, user_id)`.
- [ ] Same key, different **endpoint** → both succeed.
- [ ] A key older than 24 h is not replayed.
- [ ] `POST` without the header → 400 `IDEMPOTENCY_KEY_REQUIRED`.
- [ ] The reversal endpoint is idempotent too, not just create.
- [ ] `python -m app.jobs.cleanup_idempotency_keys` deletes expired rows and nothing else.

### E. Reversals (§6.9)

- [ ] Reversing a row leaves the original **byte-identical** — assert every column.
- [ ] The reversal row has the negated amount, `reverses_id`, and a reason.
- [ ] `SUM(amount)` for the mode equals the corrected figure.
- [ ] `reversal_reason` omitted → 422; blank string → 422.
- [ ] Reversing the same row twice → 409 `ALREADY_REVERSED`.
- [ ] Reversing a reversal → 409 `CANNOT_REVERSE_A_REVERSAL`.
- [ ] Reversal on a **closed** shift succeeds for a manager.
- [ ] Reversal on a **locked** shift: refused for a manager, allowed for an admin,
      audit-logged.
- [ ] Reversal + `replacement_amount` is atomic — force a failure on the replacement and
      confirm the reversal rolled back too.
- [ ] After a reversal, `POST` for that mode succeeds again (the mode has no live row).

### F. Shift close (§6.8)

- [ ] Shift with fuel sold and **no** cash row → 409 `MISSING_COLLECTIONS`.
- [ ] Same shift with a **₹0** cash row → **closes**.
- [ ] Shift where collections are ₹40,000 against ₹95,000 of sales → **closes**. Variance is
      recorded, never enforced. *(The single most important line in this checklist.)*
- [ ] Shift with no readings and no quantity moved → closes without a cash row.
- [ ] `MISSING_NOZZLE_READINGS` still fires **before** `MISSING_COLLECTIONS`.
- [ ] `grep -n "shift_sales" app/api/v1/shifts.py` → **no match**. Close must not price a
      shift (§3.5 — petrol/diesel margins are still unentered).
- [ ] `CREDIT_SALE_MISSING_RECEIPT` is still a named comment, not a stub (§11).

### G. Permissions (§8)

- [ ] Attendant creates a collection on **their own** open shift → 201.
- [ ] Attendant on **another attendant's** shift → 403 `NOT_YOUR_SHIFT`.
- [ ] Attendant attempts a reversal → 403.
- [ ] Manager creates/reverses on any shift at the outlet → allowed.
- [ ] Any write to a **locked** shift → 409 `SHIFT_LOCKED` (§10 immutability case).
- [ ] `grep -n "attendant_id !=" app/api/v1/collections.py` → **no match**. Ownership comes
      only from `require_shift_access`.
- [ ] `require_role` is never called with a hardcoded outlet in `collections.py`.

### H. Suite and hygiene

- [ ] `pytest` fully green; total ≥ 399 + new tests.
- [ ] 100 % coverage on `app/services/collections.py`, `app/core/idempotency.py`,
      `app/api/v1/collections.py`, `app/models/collection.py`.
- [ ] `pytest -k "idempot or revers or collection"` selects and passes.
- [ ] No `Base.metadata.create_all()` outside test fixtures (§3 rule 9).
- [ ] Every response error carries `{"detail", "code"}` (§3 rule 10).
- [ ] `app/models/collection.py`'s docstring states that `mode = cash` is the **declared**
      figure and is **not** a §6.4 input.
- [ ] `CLAUDE.md` §5.2, §5.3, §6.8, §14 amended in **their own commit**, before the code.

### I. The check no test replaces

- [ ] **Take one real day from the paper register.** Enter the readings *and* the day's cash,
      card and UPI figures. `GET /shifts/{id}/collections` must match the register line for
      line, and the shift must close. Phase 5 left this check unperformed — Phase 6 is the
      first phase where it exercises both halves of the day.

---

## 9. Open questions this phase does not close

Unchanged and still owed, from `docs/phase-5-notes.md:299-318`:

- **CBG's real max flow rate in kg/min**, and confirmation of 60 L/min for petrol/diesel.
  Live on real money since Phase 5.
- **Do salesmen record testing quantities on paper, and in what unit?** Every row carrying
  `0` reproduces §4.2's permanent daily shortfall with the field looking correctly filled in.
- **Petrol and diesel dealer commissions** — until entered, `GET /shifts/{id}/sales` returns
  409 `NO_MARGIN_FOR_DATE` for those fuels.
- **§6.4 vs §5.2, `expenses.mode`** — must be decided before Phase 7.
- **The salesman-shortfall vs receipt contradiction** — before Phase 9.
- **The locker model's effect on §6.5's rolling balance** — before Phase 10.
