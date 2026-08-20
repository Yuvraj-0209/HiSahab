# Phase 5 — Nozzle Readings & Sales Math: Plan and Definition of Done

> Written before any code. Sections 1–2 verify Phase 4. Section 3 explains what Phase 5
> is in plain terms. Sections 4–5 are the decisions that must be made *before* coding.
> Section 8 is the checklist to verify the phase at the end.
>
> For authoritative rules see `CLAUDE.md`. This file is a plan, not a spec.

---

## 1. Phase 4 verification — verdict

**Complete. Every Phase 4 obligation in `CLAUDE.md` is implemented, tested and passing.**

`263 passed in 3.63s` against real Postgres. Nothing is stubbed; nothing is scaffolded ahead.

### Obligation-by-obligation

| CLAUDE.md requirement | Where | ✓ |
|---|---|:--:|
| §11 Phase 4 scope: open/close/lock/reopen lifecycle | `app/api/v1/shifts.py` | ✓ |
| §5.2 `shifts` with `outlet_id`, explicit `business_date`, server `sequence`, NOT NULL `started_at` | `0004`, `app/models/shift.py` | ✓ |
| §5.2 unique `(outlet_id, business_date, sequence)` — outlet-scoped | `uq_shifts_outlet_date_sequence` | ✓ |
| §4.7 no `shift_type` anywhere | asserted by `test_shifts_has_no_shift_type_column` | ✓ |
| §5.2 only one `open` shift per outlet | `SHIFT_ALREADY_OPEN` guard + test | ✓ |
| §5.1 `outlet_shift_templates`, `TIME` not `TIMESTAMPTZ`, unique `(outlet_id, sequence)` | `0004`, `shift_templates.py` | ✓ |
| §5.1 template read **once** at creation, never back | `_resolve_window` + `test_editing_a_template_does_not_revalue_a_shift_that_already_traded` | ✓ |
| §5.0/§5.3 `audit_logs` with own `outlet_id`, moved up from Phase 11 | `0004`, `app/models/audit.py` | ✓ |
| §5.3 audit append-only enforced in the **database** | `trg_audit_logs_append_only` + `test_audit_logs_cannot_be_updated_or_deleted` | ✓ |
| §8 ownership axis, applied **only** to attendants | `require_shift_access` in `app/api/deps.py` | ✓ |
| §6.1 `BUSINESS_DATE_IN_FUTURE` evaluated in `TZ_DISPLAY`, not UTC | `outlet_today()` + test | ✓ |
| §6.8 lock requires `closed` → `SHIFT_NOT_CLOSED`; `locked` terminal → `SHIFT_LOCKED` | `_guard_transition` | ✓ |
| §6.8 reopen: admin-only, mandatory reason, audit-logged | `reopen_shift` | ✓ |
| §13.10 reopen limited to latest shift → `NOT_THE_LATEST_SHIFT` | `reopen_shift` + test | ✓ |
| §6.8 close preconditions **not** stubbed, named comment only | `shifts.py:542-549`, `:583-587` | ✓ |
| §9 cursor pagination, keyset not offset | `app/api/cursor.py`, `test_the_list_pages_by_keyset...` | ✓ |
| §3 rule 1 Decimal never float — audit JSONB stringifies Decimal | `app/services/audit.py::_ENCODERS` | ✓ |
| §10 all three required auth tests | `test_shift_permissions.py` | ✓ |

### Things done better than the spec required

- **Audit row and business change share one transaction.** `audit.record()` deliberately
  does not commit. A separately committed audit row can describe a change that was later
  rolled back — a log that lies. Verified by `test_a_refused_transition_writes_no_audit_row`.
- **`_resolve_window`'s third source.** The spec gave two sources for a shift's clock
  (payload, template). The code adds "the previous shift's end time", which is what makes
  a relief shift work at an outlet whose template describes only its usual day.
- **`ck_shifts_ended_after_started` in the database**, not only the API. Every
  duration-based guard — including §6.2's flow-rate ceiling, which Phase 5 is about to
  build — would compute a negative window without it.
- **Decimal-as-string in audit JSONB.** `jsonable_encoder`'s default for `Decimal` is
  `float`. Left alone, the first Phase 6 collection written through `record()` would have
  silently written `5000.0` into the audit trail.

### Gaps found — none blocking, two to carry

1. **§6.10 idempotency is not implemented anywhere.** Only the CORS `allow_headers` list
   mentions `Idempotency-Key`; there is no key store and no handler reads it. This is
   correct for Phase 4 — `POST /shifts` is not a money record and is already protected by
   `uq_shifts_outlet_date_sequence` — but it becomes mandatory at Phase 6. See §5.4 below.
2. **Phase 4 left `NOT_THE_LATEST_SHIFT` for Phase 5 to lift, and lifting it as written
   would contradict §4.7.** This is a real spec conflict and is the single most important
   decision in Phase 5. See §5.3 below.

**Recommendation: commit Phase 4 before starting Phase 5.** It is currently 21 untracked
and modified files sitting on `main` with no commit. A phase this size should not share a
commit with the next one.

---

## 2. What Phase 4 hands to Phase 5

| Handed over | Used by Phase 5 for |
|---|---|
| `shift_service.latest_shift()` | finding the tip of the chain |
| `shift.started_at` (NOT NULL) | §6.3 — which rate values this shift's fuel |
| `shift.started_at` / `ended_at` | §6.2 — the minutes in the flow-rate ceiling |
| `require_shift_access(role, writable=True)` | every reading write; enforces §6.9 |
| `audit.record()` | meter-reset overrides, opening-mismatch flags |
| `pricing.rate_at` / `margin_at` (Phase 3) | §6.3 valuation and profit |
| `fuel_types.unit_of_measure` / `max_flow_rate_per_minute` (Phase 3) | §4.5 units, §6.2 per-fuel ceiling |
| `nozzles.totalizer_max_value` (Phase 3) | §6.2 rollover formula |
| the named comment at `shifts.py:542` | `MISSING_NOZZLE_READINGS` lands there |

---

## 3. What Phase 5 brings, in plain terms

### The one-sentence version

**Phase 5 is where the system starts producing money figures.** Everything before it was
scaffolding: who you are (2), what you sell and for how much (3), and which day and shift
you are recording (4). Phase 5 records the meter numbers and turns them into litres, kilos,
rupees of revenue and rupees of margin.

### How the pieces you already built come together

Picture one day at the pump. Phase 4 gave you an open shift for 18 Aug, 06:00–22:00.
Phase 5 adds the actual work:

```
For each nozzle at the outlet:

    opening reading    ← Phase 5 carries this forward automatically (§4.7)
                          from that nozzle's last closing reading
    closing reading    ← the only number a person types
    testing quantity   ← litres dispensed to test the pump, never sold (§4.2)

         ↓  §6.2 quantity sold — with rollover and meter-reset handled

    quantity_sold  (litres for petrol/diesel, KILOGRAMS for CBG — §4.5)

         ↓  §6.3 valuation, using Phase 3's rate_at / margin_at
            at the rate effective at shift.started_at

    sale_value     = quantity_sold × rate_at(fuel, shift.started_at)
    dealer_profit  = quantity_sold × margin_at(fuel, shift.started_at)
```

That `total_sales` figure is the input to **everything downstream**:

- **Phase 6 (collections)** — cash/card/UPI received. Only meaningful compared against sales.
- **Phase 7 (expenses)** — money paid out of the drawer.
- **Phase 9 (credit)** — udhaar, which is a sale that produced no cash today.
- **Phase 10 (cash engine)** — `cash_sales = total_sales − card − upi − wallet − credit`.
  §6.4's whole equation starts with the number Phase 5 computes. If Phase 5 is wrong by
  ₹200, Phase 10 reports a ₹200 variance that nobody can explain — and at this outlet an
  unexplained shortfall is booked as **udhaar against the salesman's own name**.

That last sentence is why this phase deserves more care than any other.

### The three things that make this phase hard

**1. The chain (§4.7).** An opening reading is never typed. It is the previous closing
reading *for that same nozzle* — across shifts, across days, across an overnight closure.
So the system pre-fills it. But `CLAUDE.md` is emphatic that pre-filled is not the same as
assumed: the attendant must **confirm it against the physical meter**. If fuel was siphoned
overnight, the meter moved but the chain says it did not — and an assumed opening quietly
absorbs the missing fuel into the next shift as "sales that produced no cash", turning
theft into a debt owed by an innocent salesman. Phase 5 must build the *confirm* step and a
*mismatch* path that captures the real reading and raises it for review.

**2. The ugly meter cases (§6.2).** Totalizers roll over like an odometer, and reset to
zero when a meter is repaired. A closing reading lower than an opening reading is a real
event, not a bug — but naive subtraction gives negative litres and negative money. Three
formulas, six guards, one of which (`testing_quantity`) is described in `CLAUDE.md` as
*"the single most common bug in home-grown pump software"*: skip it and you get a small,
permanent, daily cash shortfall that is extremely hard to diagnose.

**3. Units are not litres (§4.5).** CBG is metered and priced per **kilogram**. A petrol
nozzle does ~60 L/min; the CBG dispenser does single-digit kg/min. One global "litres per
minute" sanity ceiling would either never fire or reject every real CBG sale. Every
quantity in Phase 5 is a *measure*, not a *volume*, and the unit is read from
`fuel_types.unit_of_measure` — never inferred, never named `litres` in a variable.

### What Phase 5 does **not** do

- It does not touch cash. No collections, no expenses, no drawer. That is Phases 6–10.
- It does not know stock. There is no tank, no dip, no purchase (§12).
- It does not report. `GET /shifts/{id}/sales` returns one shift's numbers; the 7-day
  rolling view is Phase 13.

---

## 4. Schema — this phase needs a spec amendment first

`CLAUDE.md` §5.2 defines `nozzle_readings` with 9 columns. **§4.7's confirm-don't-assume
rule cannot be implemented with those 9 columns**, because there is nowhere to record what
the chain predicted, nowhere to record that a human confirmed it, and nowhere to flag a
mismatch for review.

§14 says: *"Ask before adding any table, dependency, or column not listed here."* So this
is the ask. Precedent: commit `22e8339` amended the spec for CBG **before** Phase 3, and
Phase 4 amended §4.7 before writing `0004`. Same pattern here — **Step 0 is a spec commit**,
then the code.

### Columns as specified in §5.2 (no change)

| Column | Type | Note |
|---|---|---|
| `shift_id` | FK NOT NULL | §5.0 — no own `outlet_id`, derivable via shift |
| `nozzle_id` | FK NOT NULL | |
| `opening_reading` | NUMERIC(12,2) NOT NULL | the **confirmed** physical value |
| `closing_reading` | NUMERIC(12,2) NULL | null until close |
| `testing_quantity` | NUMERIC(10,3) NOT NULL DEFAULT 0 | §4.2; always 0 for CBG (§4.5) |
| `rollover_occurred` | BOOLEAN DEFAULT false | |
| `meter_reset_occurred` | BOOLEAN DEFAULT false | |
| `manual_quantity_override` | NUMERIC(10,3) NULL | admin only (§6.2) |
| `override_reason` | TEXT NULL | required if override set |
| — | UNIQUE `(shift_id, nozzle_id)` | |

### Proposed additions — **needs your approval**

| Column | Type | Why it cannot be omitted |
|---|---|---|
| `chained_opening_reading` | NUMERIC(12,2) NULL | What the chain predicted. NULL = anchor (first reading for this nozzle). Without it, "confirmed" and "assumed" are indistinguishable after the fact, and a mismatch leaves no trace — the exact failure §4.7 describes. |
| `opening_variance_reason` | TEXT NULL | Required when `opening_reading ≠ chained_opening_reading`. §4.7: *"a mismatch path that captures the real reading"*. |
| `requires_review` | BOOLEAN NOT NULL DEFAULT false | §4.7: *"raises it for review before anybody is blamed"*. Same shape as `expenses.requires_review` (§5.2), deliberately. |
| `reviewed_by` / `reviewed_at` / `review_note` | FK / TIMESTAMPTZ / TEXT, all NULL | Mirrors `expenses` exactly. A flag with no way to clear it is a flag nobody looks at twice. |

Six columns. All nullable or defaulted, so none of them constrain a future decision.

### Proposed constraints

```sql
UNIQUE (shift_id, nozzle_id)
CHECK (opening_reading >= 0)
CHECK (closing_reading IS NULL OR closing_reading >= 0)
CHECK (testing_quantity >= 0)
CHECK (manual_quantity_override IS NULL OR override_reason IS NOT NULL)   -- §5.2
CHECK (NOT (rollover_occurred AND meter_reset_occurred))                  -- mutually exclusive
CHECK (chained_opening_reading IS NULL
       OR opening_reading = chained_opening_reading
       OR opening_variance_reason IS NOT NULL)                            -- §4.7 mismatch path
INDEX ix_nozzle_readings_nozzle ON (nozzle_id)                            -- the chain lookup
```

No `outlet_id` — §5.0's rule: derivable via `shift_id → shifts.outlet_id`, so it waits.

---

## 5. Decisions required before coding

### 5.1 How does a nozzle with no predecessor get its first opening? — **recommendation: inline, admin-only**

§4.7: *"The first shift ever, and every newly installed nozzle, has no predecessor and
needs a one-time seeded starting reading (admin-only)."* Three options:

| Option | Verdict |
|---|---|
| **A. Inline on the first reading.** When `chained_opening_reading` resolves to NULL, `opening_reading` becomes a required payload field and the caller must be **admin**. Audit-logged. | **Recommended.** No new table, no new column, no new endpoint. The anchor *is* the first reading — which is literally true. |
| B. A column on `nozzles` (`initial_reading`) | Duplicates a value that already exists on the first reading row; the two can drift. |
| C. A separate `nozzle_anchors` table | A table with one row per nozzle, ever. YAGNI. |

Under A, a non-admin hitting an un-anchored nozzle gets **403 `ANCHOR_REQUIRES_ADMIN`** with
a message naming the nozzle.

### 5.2 Does the flow-rate ceiling run when `ended_at` is unknown? — **recommendation: run it wherever the window is known, and again at close**

§6.2's ceiling is `max_flow_rate_per_minute × shift duration in minutes`. Duration needs
`ended_at`, which is nullable until close. At this outlet the template supplies 22:00 at
open, so it is almost always present.

**Recommended:** evaluate the ceiling whenever `ended_at` is set; skip it (with a logged
warning) when it is not; **always** re-evaluate every reading at close, where `ended_at` is
guaranteed by Phase 4's `SHIFT_END_TIME_REQUIRED`. This means a mistyped extra digit is
caught at entry in the normal case and at close in every case. Refusing entry outright when
`ended_at` is unknown would be user-hostile for a field the caller never sent.

### 5.3 ⚠ The §13.10 reopen cascade — **two rules in `CLAUDE.md` conflict**

This is the one that needs your decision, not mine.

- **§13.10 says** the mid-chain reopen restriction is *"lifted when Phase 5 implements the
  cascade"* — i.e. Phase 5 should recompute downstream openings.
- **§4.7 says** the chained opening is *"stored on the row, not computed on read"*, because
  *"computing it would mean that correcting one shift silently rewrites the next shift's
  history"*.

A cascade that recomputes downstream openings **is** the silent rewrite §4.7 forbids.
Implementing §13.10 literally would break §4.7.

| Option | Effect |
|---|---|
| **A. Flag-not-rewrite cascade.** Allow a mid-chain reopen. When a reopened shift's `closing_reading` for a nozzle is subsequently changed, the *next* shift's stored `opening_reading` is **left untouched** and flagged `requires_review = true` with a note. A human reconciles. | **Recommended.** Satisfies both rules: nothing is silently rewritten, the restriction lifts, and the discrepancy surfaces exactly the way §4.7 wants a mismatch to surface. Reuses the review columns from §4 above at zero extra cost. |
| B. Keep `NOT_THE_LATEST_SHIFT`, amend §13.10 to say permanent. | Honest and cheap, but leaves a real operational gap: a mistake found two days later cannot be corrected at all. |
| C. Literal recomputing cascade. | Breaks §4.7. Not recommended. |

Under A, the reopen check in `shifts.py` is deleted and the flagging logic lands in the
reading-update path. `CLAUDE.md` §13.10 gets rewritten to describe the flag, not the block.

### 5.4 §6.10 idempotency — **recommendation: defer to Phase 6, and say so in the spec**

§6.10 is emphatic (*"This is not optional"*) and nothing implements it. But:

- A nozzle reading is **naturally idempotent**: `UNIQUE (shift_id, nozzle_id)` means a
  retried `POST` cannot create a second row. It returns 409 `READING_ALREADY_EXISTS`, and
  the retry can `PATCH` instead. No duplicate money.
- A **collection** is not. Two ₹5,000 cash collections in one shift are perfectly legal, so
  a timed-out retry genuinely does create a duplicate. **Phase 6 is where the key store
  becomes load-bearing.**

**Recommended:** Phase 5 ships without an idempotency store, states the natural-idempotency
argument in a code comment on the unique constraint, and adds a line to `CLAUDE.md` §6.10
naming Phase 6 as the phase that builds `idempotency_keys`. Building it in Phase 5 for a
POST that cannot duplicate is scaffolding ahead (§11).

### 5.5 Open question that Phase 5 now consumes — **answer needed**

**What is the real maximum flow rate of the CBG dispenser, in kg/min?** Migration `0003`
seeded a deliberately generous **15**. §6.2's ceiling reads that column, so from Phase 5 it
becomes a live guard on real money. Too high and it never fires; too low and it rejects
genuine sales. Same question for petrol/diesel: `MAX_FLOW_RATE_LPM=60` seeded the column,
and §14 forbids reading the config value in the guard — the column is now authoritative.

Also still open and now relevant: **do the salesmen record testing litres on paper today,
and in what unit?** If the answer is "no", `testing_quantity` will be 0 on every row and
§4.2's shortfall reappears — the field needs to be a deliberate entry, not a defaulted one.

---

## 6. Deliverables

### Step 0 — spec amendment (its own commit)

- `CLAUDE.md` §5.2 `nozzle_readings` — add the six columns from §4, with the reasoning
- `CLAUDE.md` §4.7 — state that the anchor is the first reading, admin-supplied (per 5.1)
- `CLAUDE.md` §13.10 — rewrite per the decision in 5.3
- `CLAUDE.md` §6.10 — name Phase 6 as the phase that builds the key store (per 5.4)
- `CLAUDE.md` §14 open questions — strike the CBG flow-rate question once answered

### Step 1 — migration

| File | Contents |
|---|---|
| `alembic/versions/0005_nozzle_readings.py` | one table, constraints and index from §4. **No enum, no trigger, no seed** — readings are mutable while a shift is open, so `reject_modification()` must *not* be attached. Downgrade drops the table only. |

### Step 2 — model and pure math

| File | Contents |
|---|---|
| `app/models/reading.py` | `NozzleReading`. No `relationship()`, matching the rest of `app/models/`. |
| `app/services/sales.py` | **The heart of the phase, and pure functions with no `Session`** so §6.2 can be tested exhaustively without a database. `quantity_sold(*, opening, closing, testing, rollover, meter_reset, override, totalizer_max) -> Decimal`; `sale_value(quantity, rate) -> Decimal`; `dealer_profit(quantity, margin) -> Decimal`. Explicit `Decimal.quantize(Decimal("0.01"), ROUND_HALF_UP)` on money; `Decimal("0.001")` on quantity. Every guard raises `AppError` with the §6.2 code. |
| `app/services/readings.py` | Database-aware chain helpers: `chained_opening(db, nozzle_id) -> Decimal \| None` (most recent closing for **that nozzle**, joined to `shifts` and ordered by `business_date DESC, sequence DESC` — never by `created_at`); `nozzles_in_scope(db, outlet_id, at)` (active, and installed on or before the shift); `shift_sales(db, shift)` returning the priced per-nozzle breakdown. |

### Step 3 — API

| Route | Role floor | Purpose |
|---|---|---|
| `GET /api/v1/shifts/{shift_id}/readings` | attendant (own) | The worksheet. One entry per in-scope nozzle: nozzle label, fuel, **unit**, `chained_opening_reading` pre-filled, and the saved reading if one exists. This is what makes "zero typing on a normal day" true. |
| `POST /api/v1/shifts/{shift_id}/readings` | attendant (own), `writable=True` | Create one reading. Requires `opening_confirmed: true`. `opening_reading` accepted **only** when it differs from the chain (then `opening_variance_reason` is mandatory and `requires_review` is set) or when anchoring (admin). May carry `closing_reading` + `testing_quantity` in the same call — the day is typed in one sitting. |
| `PATCH /api/v1/shifts/{shift_id}/readings/{nozzle_id}` | attendant (own), `writable=True` | Set or correct `closing_reading` / `testing_quantity` / `rollover_occurred` while the shift is open. |
| `POST /api/v1/shifts/{shift_id}/readings/{nozzle_id}/override` | **admin** | §6.2 meter-reset path: `manual_quantity_override` + mandatory `override_reason`, audit-logged. Separate route so the admin gate is visible rather than buried in a PATCH field check. |
| `PATCH /api/v1/shifts/{shift_id}/readings/{nozzle_id}/review` | manager | Clear a `requires_review` flag with a note. Audit-logged. |
| `GET /api/v1/shifts/{shift_id}/sales` | manager | §6.3. Per nozzle: quantity, **unit**, rate used, `effective_from` of that rate, value, margin, profit. Plus totals. Carries the §13.7 label on every profit figure. |

### Step 4 — wire into Phase 4's lifecycle

- `shifts.py:542` — replace the named comment with the real `MISSING_NOZZLE_READINGS`
  check: every in-scope nozzle must have a `closing_reading`. 409.
- Re-run §6.2's flow-rate ceiling for every reading at close (decision 5.2).
- Reopen: implement decision 5.3 (delete `NOT_THE_LATEST_SHIFT`, add downstream flagging).
- `app/api/v1/router.py` — include the readings router.
- `app/models/__init__.py` — export `NozzleReading`.

### Step 5 — tests

| File | Covers |
|---|---|
| `tests/test_sales_math.py` | §10's entire *Sales math* block against the pure functions — no HTTP, no fixtures, exhaustive. |
| `tests/test_reading_chain.py` | §4.7: carry-forward across shifts and across days; a nozzle skipped for one shift; a nozzle installed mid-life; anchoring; the mismatch path. |
| `tests/test_readings_api.py` | Routes, error codes, close integration, review flow. |
| `tests/test_sales_valuation.py` | §10's *Pricing and margin* + *Units* blocks applied to real shifts. |
| `tests/test_reading_permissions.py` | Attendant/manager/admin boundaries on every new route. |
| `tests/test_migrations.py` | Extend: table exists, constraints present, no `outlet_id` (derivable), unique `(shift_id, nozzle_id)` enforced, CHECKs reject at the DB level. |
| `tests/conftest.py` | `make_reading` fixture, following `make_shift`'s shape. |

### Step 6 — `docs/phase-5-notes.md`

Following the Phase 3/4 pattern: what shipped, what the spec got wrong, the decisions and
why, the bugs the tests caught, open items carried forward.

---

## 7. Error codes introduced

| Code | HTTP | Trigger |
|---|:--:|---|
| `TOTALIZER_DECREASED` | 422 | `closing < opening`, neither flag set (§6.2) |
| `TESTING_EXCEEDS_THROUGHPUT` | 422 | testing > gross throughput (§6.2) |
| `IMPLIED_FLOW_RATE_TOO_HIGH` | 422 | quantity > fuel's own ceiling × minutes (§6.2) |
| `METER_RESET_REQUIRES_OVERRIDE` | 422 | `meter_reset_occurred` with no override |
| `OVERRIDE_REQUIRES_ADMIN` | 403 | non-admin sets `manual_quantity_override` |
| `ROLLOVER_NOT_APPLICABLE` | 422 | `rollover_occurred` set but `closing >= opening` |
| `OPENING_NOT_CONFIRMED` | 422 | `opening_confirmed` absent or false |
| `OPENING_VARIANCE_REASON_REQUIRED` | 422 | opening ≠ chain with no reason |
| `ANCHOR_REQUIRES_ADMIN` | 403 | non-admin anchoring an un-chained nozzle |
| `NOZZLE_NOT_AT_OUTLET` | 409 | nozzle belongs to a different outlet |
| `NOZZLE_INACTIVE` | 409 | reading against a deactivated nozzle |
| `NOZZLE_NOT_YET_INSTALLED` | 409 | `meter_installed_at` after the shift |
| `READING_ALREADY_EXISTS` | 409 | duplicate `(shift_id, nozzle_id)` |
| `READING_NOT_FOUND` | 404 | PATCH against a nozzle with no reading |
| `MISSING_NOZZLE_READINGS` | 409 | §6.8 close precondition |
| *(reused)* `SHIFT_NOT_OPEN`, `SHIFT_LOCKED`, `NOT_YOUR_SHIFT`, `NO_PRICE_FOR_DATE`, `NO_MARGIN_FOR_DATE` | | from Phases 3–4 |

---

## 8. Definition of Done

Phase 5 is complete when **every** box below is checked. Each is independently verifiable.

### A. Schema

- [ ] `alembic upgrade head` reaches `0005`; `alembic downgrade -1` and re-upgrade both succeed cleanly (no leaked types, no leaked triggers)
- [ ] `nozzle_readings` exists with all 15 columns and **no `outlet_id`** (§5.0 — derivable via `shift_id`)
- [ ] `opening_reading` and `closing_reading` are `NUMERIC(12,2)`; `testing_quantity` and `manual_quantity_override` are `NUMERIC(10,3)` — verified by querying `information_schema.columns`, not by reading the migration
- [ ] `UNIQUE (shift_id, nozzle_id)` rejects a duplicate at the **database** level
- [ ] `CHECK (manual_quantity_override IS NULL OR override_reason IS NOT NULL)` rejects at the database level
- [ ] `CHECK (NOT (rollover_occurred AND meter_reset_occurred))` rejects at the database level
- [ ] The mismatch CHECK rejects `opening_reading ≠ chained_opening_reading` with no reason, at the database level
- [ ] No `reject_modification()` trigger on this table — readings are correctable while a shift is open
- [ ] Nothing anywhere on this table is named `litres`, `volume`, `ltr` or similar (§4.5) — `grep -ri "litre\|liter\|volume" app/models/reading.py app/services/sales.py` returns only comments explaining the rule

### B. Sales math (§6.2) — the block that matters most

- [ ] Normal pair: `closing − opening − testing`, exact to 3 decimal places
- [ ] Rollover: `closing < opening` with `rollover_occurred=true` → `(max − opening) + closing − testing`, **positive**
- [ ] `closing < opening` with **no** flag → 422 `TOTALIZER_DECREASED`, **and no row is written** (verified by row count, not just status code)
- [ ] `testing_quantity` is subtracted in the normal case **and** in the rollover case
- [ ] `testing_quantity` exceeding **gross throughput** → 422 `TESTING_EXCEEDS_THROUGHPUT` — including under rollover, where naive `closing − opening` is negative
- [ ] Implied flow rate above the fuel's ceiling → 422; the ceiling comes from `fuel_types.max_flow_rate_per_minute`
- [ ] `grep -rn "MAX_FLOW_RATE_LPM" app/services/ app/api/` returns **nothing** (§14 — config seeds the column, the guard never reads it)
- [ ] A CBG reading and a petrol reading of identical magnitude produce **different** ceiling verdicts — proving the ceiling is per-fuel, not global
- [ ] Meter reset requires `manual_quantity_override` **and** `override_reason`, **and** admin role; a manager attempt → 403; the successful override writes an `audit_logs` row
- [ ] There is no input combination — fuzzed or enumerated — for which `quantity_sold` returns a negative value
- [ ] `grep -rn "float(" app/services/sales.py app/services/readings.py app/api/v1/readings.py` returns nothing (§3 rule 1)

### C. The chain (§4.7)

- [ ] Opening is pre-filled from **that nozzle's** most recent closing reading, not from the previous shift's row set
- [ ] A nozzle out of service for one shift still chains correctly to its own last closing — the shift in between is skipped, not treated as a break
- [ ] A whole skipped day chains correctly
- [ ] A nozzle installed mid-life is absent from earlier worksheets and anchors on its first shift
- [ ] The chained value is **stored on the row** (`chained_opening_reading` is populated), never computed on read — verified by SQL against the table after a close
- [ ] `opening_confirmed: true` is required; omitting it → 422 `OPENING_NOT_CONFIRMED`
- [ ] A mismatch stores the **observed** reading in `opening_reading`, keeps the predicted value in `chained_opening_reading`, requires a reason, and sets `requires_review = true`
- [ ] A flagged reading appears in `GET /shifts/{id}/readings` as flagged, and a manager can clear it with a note; clearing writes an audit row
- [ ] Anchoring an un-chained nozzle requires admin (403 `ANCHOR_REQUIRES_ADMIN` otherwise) and writes an audit row
- [ ] The chain query orders by `shifts.business_date DESC, shifts.sequence DESC` — **never** `created_at`. A test enters an older business date *after* a newer one and asserts the chain still resolves correctly

### D. Valuation (§6.3)

- [ ] `sale_value = quantity_sold × rate_at(outlet, fuel, shift.started_at)` — via Phase 3's helper, not a re-implementation
- [ ] A sale is valued at the **historical** rate; revising the price afterwards does not change a recorded shift's sales
- [ ] A price revision falling inside the shift window logs a **warning**, and the code carries the §13.1 approximation comment at the point it happens
- [ ] `dealer_profit = quantity_sold × margin_at(...)`, and a price revision does **not** change it (§4.6)
- [ ] A missing price → 409 `NO_PRICE_FOR_DATE`; a missing margin → 409 `NO_MARGIN_FOR_DATE`. **Never zero.**
- [ ] Every profit figure in the response is labelled as gross fuel margin excluding stock revaluation (§13.7) — an unlabelled `profit` key is a fail
- [ ] Money is quantised to 2dp with an explicit rounding mode, stated in a comment
- [ ] `GET /shifts/{id}/sales` returns the **unit** for every line, sourced from `fuel_types.unit_of_measure`

### E. Lifecycle integration

- [ ] Closing a shift with any in-scope nozzle missing a `closing_reading` → 409 `MISSING_NOZZLE_READINGS`; the named comment at `shifts.py:542` is gone, replaced by the real check
- [ ] "In scope" excludes inactive nozzles and nozzles installed after the shift — with a test for each
- [ ] Every reading is re-checked against the flow-rate ceiling at close
- [ ] Writing a reading to a `closed` shift → 409 `SHIFT_NOT_OPEN`; to a `locked` shift → 409 `SHIFT_LOCKED` (via `require_shift_access(writable=True)`, not a re-implementation)
- [ ] Decision 5.3 is implemented and `CLAUDE.md` §13.10 matches the code
- [ ] `git grep "MISSING_COLLECTIONS\|CREDIT_SALE_MISSING_RECEIPT"` still finds only the named comments — Phase 5 did **not** scaffold ahead (§11)

### F. Permissions (§8)

- [ ] Attendant writes a reading to their **own** open shift → 201
- [ ] Attendant writes to **another attendant's** shift → 403 `NOT_YOUR_SHIFT`
- [ ] Manager and admin write to any shift at the outlet → 201
- [ ] Attendant sets `manual_quantity_override` → 403; admin → 201
- [ ] Attendant clears a review flag → 403; manager → 200
- [ ] Attendant reads `GET /shifts/{id}/sales` → 403 (manager floor)
- [ ] Every ownership check reuses `require_shift_access`; `grep -c "attendant_id !=" app/api/v1/readings.py` returns 0

### G. Suite and hygiene

- [ ] `pytest` — everything green, Phase 1–4's 263 included, none modified to accommodate Phase 5
- [ ] `pytest --cov=app` — Phase 5 modules at or above Phase 4's 98%
- [ ] `pytest -k "rollover"`, `-k "testing"`, `-k "chain"`, `-k "unit"` each select a meaningful set and pass
- [ ] `alembic revision --autogenerate` produces an **empty** migration — model and database agree
- [ ] `docs/phase-5-notes.md` written
- [ ] `CLAUDE.md` §14's open-questions list updated: CBG flow rate struck once answered, anything new added
- [ ] Committed as `Phase 5 : nozzle readings and sales math`, with the Step 0 spec amendment as its own preceding commit

### H. The one manual check no test replaces

- [ ] Take one real day from the outlet's paper sales register. Enter it through the API —
      opening confirmations, closing readings, testing quantities. **`GET /shifts/{id}/sales`
      must match the register's rupee total.** If it does not, the discrepancy is understood
      and explained before this phase is called done.

      This is the check that catches what unit tests cannot: a wrong assumption about how
      the pump actually records its day. It is also the cheapest possible moment to find one.
