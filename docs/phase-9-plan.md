# Phase 9 — Credit (Udhaar): Plan, Decisions, and Verification Checklist

> Matches the `phase-5/6/7/8-plan.md` pattern. Written **before** the code, unlike
> `phase-8-plan.md` which was finalised at implementation time; §8 records what actually
> shipped where it differs, and the checklist marks are filled in as they are met.

---

## Context

Phase 8 shipped clean — 741 tests green, `alembic` at `0011`, no drift — and it deliberately
built nothing for Phase 9. `git grep "credit_sales"` finds only spec prose and three named
comments left as insertion points. Those comments are the map for this phase:

| Where | What it says |
|---|---|
| `app/api/v1/shifts.py:563` | `CREDIT_SALE_MISSING_RECEIPT -- Phase 9, needs credit_sales` |
| `app/services/attachments.py::link` | *"this function grows an additional check -- it is not rebuilt from scratch"* |
| `app/core/collections.py` | `credit_repayments.mode` is a **different** enum from `collection_mode` |
| `app/core/errors.py:88` | `CREDIT_LIMIT_EXCEEDED` named as a future code |
| `app/models/collection.py:11` | §6.4's `credit_sales_amount` term — the number Phase 10 needs |

Phase 9 is what makes §6.4's cash equation completable. Until credit sales exist, the gap
between metered sales and declared cash has no explanation at all, and §6.8 already refuses
to block a close on that gap for exactly this reason. This phase supplies the missing term
and the ledger behind it.

**Four decisions were put to the owner before planning. Three came back:**

1. **Salesman shortfall** → keep `credit_sales` receipt-mandatory; shortfalls become their
   own record type, **built in Phase 10** where reconciliation actually produces one. Phase
   9 writes the decision into the spec and adds the §14 guardrail; it builds no table.
2. **`is_settled`** → **dropped from the spec.** Outstanding is always computed (§6.6).
3. **Read access** → attendants get a **list only, no balances**; managers and admins get
   detail, balances and the ledger.
4. **Reversal columns on the credit tables** → *not answered.* Taking the recommended path
   (D9): both tables get §6.9's quartet. This is what the codebase already forces — see D9.

---

## 1. Phase 8 audit — Step 0

Before any Phase 9 code, per the standing habit (P6 found 3 defects, P7 found 3, P8 found 1).
Run these and fix anything found **test-first**:

- [x] `pytest` from clean — expect 741.
- [x] `alembic current` → `0011 (head)`; `alembic check` → no drift; `downgrade base` /
      `upgrade head` round-trip.
- [x] Coverage on every Phase 8 module (`uploads.py`, `storage.py`, `attachments.py`,
      `expense_categories.py`, both routers, the job).
- [x] **`link()`'s 409 detail string** says *"already linked to another expense"* — a
      hardcoded table name in a message that is about to serve two tables.
- [x] **`may_read()` joins `Expense` only.** Confirm the gap is real before Phase 9 widens it.
- [x] **`ATTACHMENT_NOT_FOUND` is raised from five places** with three different detail
      strings (`deps.py:287`, `attachments.py:68`, `expenses.py:381`, `expenses.py:545`,
      `services/attachments.py:138`). Check they have not drifted in meaning.
- [x] The `_MAX_SUMMARY_RANGE_DAYS = 366` bound and `INVALID_DATE_RANGE`'s two call sites.
- [x] Whether `GET /expenses/summary` needs an outlet filter on a second outlet (it reads
      `actor.outlet_id`, which is correct — confirm).

---

## 2. Spec amendment (Step 1, its own commit)

`Spec: credit sales, repayments and the salesman shortfall, before Phase 9`

Everything below changes `CLAUDE.md` **before** any Phase 9 code, per the house workflow.
Each item is a real edit, not a restatement.

| § | Amendment |
|---|---|
| §5.2 `credit_sales` | **Remove `is_settled`** (owner's decision; it contradicts §6.6 and would drift). **Add** `reverses_id` + `reversal_reason` (D9) and `limit_override_reason` (D5). Keep `attachment_id NOT NULL` **verbatim** (D2) |
| §5.2 `credit_repayments` | **Add** `reverses_id` + `reversal_reason`. State that only `mode = cash` enters §6.4 |
| §5.1 `credit_customers` | `phone` is NOT NULL and `UNIQUE (outlet_id, phone)` (M2). Note the sale/repayment asymmetry on `is_active` (M7) |
| §6.6 | Restate the limit comparison unambiguously: `outstanding + amount > credit_limit` → 409, strictly `>`. Record the override's storage on the row, not audit-only. Record that outstanding may go **negative** (advance payment) and that this is not an error |
| §6.9 | Note the credit tables now carry the reversal shape, and that a `credit_sales` reversal **inherits** the original's `attachment_id` rather than weakening `NOT NULL` (D2) |
| §8 | Add rows: *List credit customers* (all roles, lean projection), *Read a customer's outstanding balance / ledger* (manager, admin), *Record a credit sale or repayment on an open shift* (attendant own-only / manager / admin), *Manage credit customers* (admin — already implied by "Manage users, nozzles, customers") |
| §10 | Fill in the *Credit* test block with the cases in §7 below |
| §13 | New approximation: **the credit-limit check is not serialised** (M8). New approximation: **shortfalls are not modelled in V1's credit tables** — Phase 10 |
| §14 | New "Do not": *do not book a salesman's cash shortfall as a `credit_sale`.* It is a reconciliation outcome, not a sale; it has no receipt to satisfy the `NOT NULL`; and it would pollute a real customer's outstanding balance with staff debt. New "Do not": *do not maintain a denormalised outstanding total* |
| §12 | Confirm shortfalls are **in** scope for V1 (Phase 10), not out — they are the whole reason §6.5's locker model matters |

---

## 3. Decisions

### D1 — `credit_customers` is `expense_categories`' shape, with a phone as the natural key

Admin-managed, outlet-scoped, deactivate-never-delete. Copy
`app/api/v1/expense_categories.py` wholesale, including its `resolve_outlet_from_*` resolver
living in the router module (not `deps.py` — that file holds only the shift and attachment
resolvers).

| Column | Notes |
|---|---|
| `id` | UUID PK |
| `outlet_id` | FK NOT NULL. Not derivable (§5.0) |
| `name` | text NOT NULL, non-blank CHECK |
| `phone` | text NOT NULL — §5.2 marks `vehicle_numbers` and `credit_limit` nullable and this one not |
| `vehicle_numbers` | `text[]` nullable, normalised uppercase (M3) |
| `credit_limit` | `NUMERIC(12,2)` nullable — **null means no limit**, not zero |
| `is_active` | boolean default true |
| | `UNIQUE (outlet_id, phone)` (M2) |

**Why a unique key at all.** §6.7's argument transfers exactly: two "Ramesh" rows split one
real balance across two ledgers, and the credit limit then never fires. Names genuinely
collide; phone numbers do not. This is the same middle ground `expense_categories.code`
occupies — open to new rows, closed to accidental duplicates.

**No `code` column.** A customer is not reference data an aggregate is grouped by; the
`^[A-Z][A-Z0-9_]*$` discipline has nothing to protect here.

### D2 — `credit_sales.attachment_id` stays `NOT NULL`; the reversal inherits

This is the phase's most load-bearing decision, and it resolves a collision the Phase 8
audit flagged: §6.9's reversal is a **new row**, and a bare `NOT NULL` would demand a
receipt for a cancellation — the exact thing §6.11 already refuses to do for expenses
(*"a cancellation is not a spend and there is nothing to photograph"*).

`expenses` solved its analogue by exempting reversals inside a CHECK
(`reverses_id IS NOT NULL OR ...`). **That escape is not available here and should not be
manufactured**: §5.2 and §6.6 both say `NOT NULL` *at the database level*, twice, as the
belt to the API's braces. Downgrading it to a CHECK weakens the one control the whole
document leans on.

**So the reversal inherits the original's `attachment_id`**, exactly as an expense's
*replacement* already does (`services/expenses.py::reverse`, D3 in the Phase 8 plan). The
column stays `NOT NULL`, nothing is weakened, and §5.3's one-attachment-one-**live**-row
rule still holds: the original is reversed (not live), the reversal is a reversal (not
live), the replacement — if any — is the single live claimant.

`link()` is **not** called for the reversal or the replacement. Same reasoning as D3:
by then the original is no longer live, and re-checking would only re-verify what
inheritance already guarantees.

### D3 — `credit_repayments` gets its own Postgres enum

`credit_repayment_mode = cash | card | upi | bank_transfer`. **Not** `collection_mode`
(`wallet`, no `bank_transfer`) and **not** a reuse of `expense_mode` even though the labels
happen to match today — `app/core/collections.py`'s docstring already argues this: one
shared type would force a future phase to alter a live enum or carry a meaningless value.
Python side is a `StrEnum` in `app/core/credit.py`; the column is `Mapped[str]` with a
module-private `postgresql.ENUM(..., create_type=False)`, per house style.

Only `mode = cash` repayments enter §6.4's drawer equation. Phase 9 does not build that
equation; it builds the column that makes it answerable, and says so in the model docstring.

### D4 — outstanding is computed, never stored

```
outstanding(customer) = SUM(credit_sales.amount) - SUM(credit_repayments.amount)
```

Summed over **every** row, reversals included — they carry negative amounts and net out
automatically. This is the same convention `totals_by_category_range` already uses, and its
docstring's reasoning transfers verbatim: *"a reversal that has not yet been replaced must
show as the reduction it is, not vanish from the report."*

`is_settled` is gone (owner's decision). "Fully settled" is `outstanding == 0`, derived.
**Outstanding may be negative** — a customer paying in advance is real, and refusing it
would be inventing a rule. Recorded in §6.6 rather than left to be rediscovered.

### D5 — the credit limit, and the admin override

```
if credit_limit is not None and outstanding + amount > credit_limit:  -> 409 CREDIT_LIMIT_EXCEEDED
```

Strictly `>`, matching §6.7 and §6.11's boundary convention: exactly at the limit is
allowed, one paisa over is not. `credit_limit IS NULL` means no limit — never treated as 0.

**Override.** `CreditSaleCreate.limit_override_reason: str | None`. Present and the actor is
not an admin → 403 `LIMIT_OVERRIDE_REQUIRES_ADMIN`, mirroring `ANCHOR_REQUIRES_ADMIN`
(`readings.py:580`). Present and the actor is an admin → the check is skipped, the reason is
**stored on the row** and audit-logged.

Stored on the row, not audit-only, despite §6.6's literal wording. `app/services/sales.py`
already settled this argument for the analogous manual-quantity override: *"`override_reason`
is mandatory in the database, so it can never be an unexplained number."* An audit row is
not read at report time; a column is.

`AuditAction` gains no label — there is no `override` and the enum's labels are fixed at
migration time. The override records as `AuditAction.insert` with the reason folded into
`new_values`, the same shape `shifts.py` uses for a reopen reason.

### D6 — `CREDIT_SALE_MISSING_RECEIPT` is implemented even though it cannot fire

§6.8 names it; `attachment_id NOT NULL` plus `link()` at insert means no row reachable
through the API can fail it. Implement it anyway as one query — *a credit sale on this shift
whose attachment has `linked_at IS NULL`* — with an honest comment that it is unreachable
through the API and why it still earns its place.

This is not the dead code §14 forbids and Phase 8 deleted. Those were `INSUFFICIENT_ROLE`
branches that **could not be reached by any caller at all**. This one is reachable by a row
written outside the API — a fixture, a migration, a future bulk import — and it is the same
belt-and-braces argument `_CONSTRAINT_ERRORS` makes for constraints the API refuses first.
A test provokes it by inserting a raw row with `linked_at = NULL`.

If that argument does not survive writing it, delete the check and say so in the notes
rather than shipping a check that always passes.

### D7 — `link()` and `may_read()` grow, they are not rebuilt

- New `app/services/credit.py::live_credit_sale_for_attachment(db, *, attachment_id)`,
  mirroring `live_expense_for_attachment` line for line, including the private `_is_reversed()`
  `exists()` predicate.
- `attachments.link()` gains a second `if`. Its 409 detail string generalises from
  *"another expense"* to *"another expense or credit sale"*.
- `attachments.may_read()` gains a `CreditSale` join, or an attendant cannot read the
  receipt for a credit sale on their own shift unless they personally uploaded it.
- Import direction: `services/attachments.py` → `services/credit.py`. `credit.py` must
  **not** import `attachments.py`, or the cycle that already exists with `expenses.py`
  doubles.

### D8 — read access, per the owner's answer

| Route | Floor | Shape |
|---|---|---|
| `GET /credit-customers` | attendant | **Lean**: `id`, `name`, `vehicle_numbers`, `is_active`. No `phone`, no `credit_limit`, no balance |
| `GET /credit-customers/{id}` | manager | Full, plus `outstanding` |
| `GET /credit-customers/{id}/ledger` | manager | Cursor-paginated union of sales and repayments |
| `GET /credit-customers/outstanding` | manager | Who owes what — the report the owner actually wants |
| `POST` / `PATCH /credit-customers` | admin | |
| `GET`/`POST`/`PATCH` `/shifts/{id}/credit-sales` | attendant, own shift | |
| `POST /shifts/{id}/credit-sales/{id}/reversals` | manager (admin if locked) | |
| same three for `/credit-repayments` | | |

Two response models, not one shape filtered at runtime — the projection difference is a
permission boundary and should be visible in the type.

**Cross-outlet posture: 403 `NOT_A_MEMBER`**, the ordinary `require_role(...,
resolve_outlet_from_credit_customer)` pattern, *not* §7.3's stricter 404. Phase 8's notes
asked Phase 9 to decide this explicitly rather than default by habit; the decision is that
under D8 an attendant cannot read customer detail at all, so the sensitive fields (phone,
vehicle registrations) are already manager-gated, and a bespoke 404 dependency is complexity
V1 does not buy anything with. **Flagged for the owner in §9** — this is the one place the
plan chooses the looser of two defensible options.

### D9 — both credit tables get §6.9's reversal quartet

The unanswered question, taken on the recommended path because the codebase already forces
it three ways:

1. §6.9 applies to *"financial rows in a `closed` or `locked` shift"* — both tables qualify.
2. Without it, a mistyped udhaar found after close is uncorrectable, and this outlet types
   the whole day in **after the fact** (§4.7), so "found after close" is the normal case.
3. `tests/test_errors.py::test_every_reversal_unique_constraint_is_mapped_to_a_business_error`
   reads `pg_constraint` for `uq_%_reverses_id` and asserts each is in `_CONSTRAINT_ERRORS`.
   The moment `0012` lands, that test governs.

Exact quartet copied from `expenses`: `uq_<t>_reverses_id`, `ck_<t>_reversal_has_reason`
(`~ '[^[:space:]]'`), `ck_<t>_amount_sign` (**strict** `> 0` / `< 0` — a ₹0 udhaar records
nothing, same as an expense), `ck_<t>_reversal_not_self`.

### Mechanical decisions

| # | Decision | Reason |
|---|---|---|
| M1 | `credit_repayment_mode` is a new type, never `expense_mode` reused | D3; matching labels today is a coincidence, not a contract |
| M2 | `UNIQUE (outlet_id, phone)` on customers | One customer, one ledger. A duplicate row defeats the credit limit silently |
| M3 | `vehicle_numbers` uppercased and whitespace-stripped on write; `[]` normalises to `NULL` | `MH12AB1234` vs `mh12 ab 1234` is the free-text failure §5.1 warns about, in a different column |
| M4 | Attendant customer projection omits `phone` and `credit_limit` | The owner's answer; enforced by a distinct response model, not a runtime filter |
| M5 | Limit comparison strictly `>` | §6.7 / §6.11's boundary convention, applied consistently |
| M6 | Outstanding sums every row incl. reversals; never filtered to live | `totals_by_category_range`'s stated convention |
| M7 | An **inactive** customer refuses a new sale (409) but **accepts a repayment** | You deactivate someone precisely to stop new udhaar while they pay off. Refusing the repayment would be backwards |
| M8 | No `SELECT ... FOR UPDATE` on the limit check | The codebase has zero row locks; §13.12 already says one shift is open at a time, so concurrency here is one user. Documented as a §13 approximation, not hidden |
| M9 | `_MAX_ROWS = 100` on shift-scoped lists (`truncated: bool`); cursor pagination on the ledger and the outstanding report | Matches `expenses.py` / `collections.py` for children, `expenses/flagged` for reports |
| M10 | The override records as `AuditAction.insert` with the reason in `new_values` | The enum's labels are fixed at migration time; no `ALTER TYPE` for a label used once |
| M11 | `_CONSTRAINT_ERRORS` entries land **in the same commit as `0012`** | Or the `pg_constraint` structural test goes red — which is the test working, but it should never be red in a pushed commit |
| M12 | Typed `amount` is accepted; a divergence from `quantity × rate_at(...)` **logs a warning**, never refuses | The slip is what the customer owes. `rate_at` can raise `NO_PRICE_FOR_DATE`, and §6.8's reasoning applies: a reference-data gap must not block a real write. Wrap and skip |
| M13 | `ck_credit_sales_quantity_needs_fuel_type`: `quantity IS NULL OR fuel_type_id IS NOT NULL` | A quantity with no fuel type has no unit (§4.5) and is meaningless. Not the biconditional — a rupees-only fuel slip is real |
| M14 | A repayment larger than outstanding is **allowed**; outstanding goes negative | An advance is real. Recorded in §6.6 so it is a decision, not an oversight |
| M15 | `GET /credit-customers/outstanding` is declared **before** `GET /credit-customers/{customer_id}` | Otherwise FastAPI matches `outstanding` as a `customer_id` and the report 422s on a UUID parse. `router.py` already carries ordering comments about exactly this hazard |
| M16 | A `PATCH` cannot change `credit_customer_id` on a sale, nor `shift_id` on either row | Same reasoning as M16 in Phase 8 for `category_id`: moving money to a different customer's ledger is a different row, not a correction. Correct it via §6.9 |

---

## 4. Build order

| Step | What | Commit message |
|---|---|---|
| — | This document, written before any code and left **uncommitted until Step 11** so it can be amended as decisions land. `phase-8-plan.md` was finalised at implementation time instead; writing it first is the small improvement this phase tries | (with Step 11) |
| 0 | Phase 8 audit + any fix, test-first | `Phase 9 Step 0: fix the Phase 8 defects found auditing attachments` |
| 1 | `CLAUDE.md` amendment (§2 above) | `Spec: credit sales, repayments and the salesman shortfall, before Phase 9` |
| 2 | Migration `0012_credit.py` (`down_revision = "0011"`), `app/core/credit.py`, `app/models/credit.py`, register in `app/models/__init__.py`, `_CONSTRAINT_ERRORS` entries | `Phase 9 Step 2: credit tables, models and the repayment mode enum` |
| 3 | `app/services/credit.py` — `resolve_customer`, `outstanding`, `outstanding_all`, `live_credit_sale_for_attachment`, `reverse_sale`, `reverse_repayment`, `credit_sales_missing_receipt`, `credit_sales_total` | `Phase 9 Step 3: credit service -- outstanding, limits, reversals` |
| 4 | `app/api/v1/credit_customers.py` + router registration | `Phase 9 Step 4: credit customers -- admin CRUD, attendant list` |
| 5 | `app/api/v1/credit_sales.py` | `Phase 9 Step 5: credit sales -- receipt-enforced, limit-checked` |
| 6 | `app/api/v1/credit_repayments.py` | `Phase 9 Step 6: credit repayments` |
| 7 | Extend `attachments.link()` + `may_read()` (D7) | folded into Step 5 — neither is independently testable before a credit sale exists |
| 8 | `CREDIT_SALE_MISSING_RECEIPT` at `shifts.py:563` | `Phase 9 Step 8: wire §6.8's last close precondition` |
| 9 | `GET /credit-customers/{id}/ledger`, `GET /credit-customers/outstanding` | `Phase 9 Step 9: outstanding balances and the customer ledger` |
| 10 | Tests | distributed across every step, not a separate pass |
| 11 | `docs/phase-9-plan.md` + `docs/phase-9-notes.md` | `Phase 9 Step 11: plan and notes docs` |

Steps 5 and 7 land together for the reason Phase 8's Steps 3–7 did: the `link()` extension
has nothing exercising it until a credit sale can claim an attachment.

### Files touched

**New:** `alembic/versions/0012_credit.py`, `app/core/credit.py`, `app/models/credit.py`,
`app/services/credit.py`, `app/api/v1/credit_customers.py`, `app/api/v1/credit_sales.py`,
`app/api/v1/credit_repayments.py`, ~6 test files.

**Modified:** `CLAUDE.md`, `app/models/__init__.py`, `app/api/v1/router.py`,
`app/core/errors.py` (`_CONSTRAINT_ERRORS`), `app/services/attachments.py` (`link`,
`may_read`), `app/api/v1/shifts.py` (close precondition), `tests/conftest.py`,
`tests/test_migrations.py`, `tests/test_routes.py`.

### The `conftest.py` teardown debt — the item most likely to be forgotten

`tests/conftest.py` (1,195 lines, the only conftest in the repo) has **no transaction
rollback**. Every `make_X` fixture commits through `engine.begin()` and deletes its own
rows in teardown, because the ASGI app takes its own connection from `SessionLocal` and
cannot see uncommitted work. That means every new child table adds a generation to three
existing teardown blocks, and `make_user`, `make_shift` and `clean_shifts` each already
carry a *"Phase N added a level"* comment recording the last time this was missed.

Phase 9 must add, in this order (audit rows first, with `trg_audit_logs_append_only`
disabled; then reversals `WHERE reverses_id IS NOT NULL`; then the rows themselves):

| Fixture | Add |
|---|---|
| `make_user` | `credit_sales` / `credit_repayments` audit + row passes, **before** its existing `attachments` sweep — `credit_sales.attachment_id` is `NOT NULL`, so an attachment cannot be deleted while a credit sale points at it. Also `credit_customers` created via `created_by` |
| `make_shift` | `credit_sales` / `credit_repayments`, same two-pass shape as its existing `expenses` block |
| `clean_shifts` | Both tables in the blanket sweep and in the `audit_logs WHERE table_name IN (...)` list |
| `clean_expenses` | Unchanged |

Getting this wrong does not fail the Phase 9 tests. It fails an **unrelated later test**
with a foreign-key violation, which is exactly how the Phase 7 audit found that
`clean_shifts` had never swept `collections`.

New fixtures, in the established `make_X` + `clean_X` pair shape (raw SQL over `engine`,
money as **strings cast in SQL**, never Python floats — §3 rule 1 says this applies
"including in a quick test fixture"):

`make_credit_customer` / `clean_credit_customers`, `make_credit_sale` /
`clean_credit_sales`, `make_credit_repayment` / `clean_credit_repayments`.

Any seed-lookup fixture must be **function-scoped**, like `fuel_type_ids` and
`expense_category_ids` — `tests/test_migrations.py::test_migration_is_reversible`
downgrades to base and back part-way through the run, re-seeding with fresh
`gen_random_uuid()` ids, and a session-scoped cache would hand every later test a foreign
key to a row that no longer exists.

### Test files to write

| File | Covers |
|---|---|
| `tests/test_credit_customers.py` | Admin CRUD, immutability, `(outlet_id, phone)` uniqueness, deactivation asymmetry (M7), the lean attendant projection |
| `tests/test_credit_sales_api.py` | Create / list / PATCH, the receipt requirement, `ck_credit_sales_quantity_needs_fuel_type`, the CBG unit case |
| `tests/test_credit_limits.py` | §6.6's limit, the boundary, `NULL` = unlimited, the admin override and its audit row |
| `tests/test_credit_repayments.py` | Modes, repayment against an inactive customer, over-payment going negative |
| `tests/test_credit_outstanding.py` | §6.6's arithmetic through reversals; the ledger and the outstanding report |
| `tests/test_credit_reversals.py` | §6.9 on both tables, copied from `test_expense_reversals.py` |
| `tests/test_credit_permissions.py` | §8's matrix **plus** the four structural tests (below) |
| `tests/test_shift_close_credit.py` | D6's `CREDIT_SALE_MISSING_RECEIPT` |

**House idiom to match exactly** (from `tests/test_expense_receipts.py` and
`tests/test_uploads_api.py`):

- Module docstring naming the §-sections covered, then `pytestmark = pytest.mark.anyio`,
  then a module-level `DAY = date(2026, M, D)` — **a distinct date per file**, so tests do
  not collide on `(outlet_id, business_date, sequence)`.
- Module-local `async def _helper(...)` wrappers and a module-local `_REAL_JPEG` constant.
  These are duplicated across Phase 8's four upload test files **deliberately**; do not
  factor them into a shared module (there is no `tests/helpers.py` and should not be one).
- Money as a **string** in the JSON body and asserted as a string; `Decimal(body["x"])`
  when arithmetic is needed. Never `float`.
- Assert the status code and the `code` on **separate lines**, always both.
- Every rejection test also proves **nothing was written**, by dropping to the `engine`
  fixture and asserting `SELECT count(*) == 0`.
- Full-sentence test names.
- Idempotent writes use a human-readable per-test key: `headers={**headers,
  "Idempotency-Key": "limit-boundary"}`.

**The four structural tests** every permissions file in this repo carries, pointed at
`app/api/v1/credit_sales.py`, `app/api/v1/credit_repayments.py`,
`app/api/v1/credit_customers.py` and `app/services/credit.py`:

`test_no_ownership_check_is_reimplemented_here` (no `attendant_id ==`/`!=`, and
`require_shift_access` present), `test_the_outlet_is_never_hardcoded_in_this_router` (no
`DEFAULT_OUTLET_ID`, no `get_default_outlet_id` — except in `credit_customers.py`'s
*create* route, which legitimately uses it per `deps.py`'s stated rule),
`test_no_float_appears_in_the_money_path`, and an `ast`-parsed assertion that
`credit_sales_missing_receipt` is **shift-scoped**, not business-date-scoped.

### Patterns to copy verbatim, not re-derive

- Router skeleton, `# --- schemas / helpers / routes ---` banners, inline Pydantic models
  with `ConfigDict(extra="forbid")` on requests only — `app/api/v1/expenses.py`.
- Idempotency envelope (`_require_key`, `begin`/`store`/`discard`, `except: rollback +
  discard`) — `expenses.py:337-456`. Required on all four money-creating POSTs.
- `_to_response` / `_audit_snapshot` / `_load_<row>` (404 then 409-not-in-shift) helpers.
- Reversal service shape, incl. `reversal_of()` extracted so the route and the service
  cannot drift — the Phase 7 Step 0 lesson.
- Reference-data CRUD + `resolve_outlet_from_*` — `app/api/v1/expense_categories.py`.
- Keyset pagination: `limit + 1` sentinel, `tuple_(a, b) < tuple_(x, y)`,
  `encode_cursor`/`decode_cursor` unchanged.
- Model conventions: no mixins, no `relationship()`, `sa.Text()` never `String(n)`,
  `sa.Numeric(12,2)` money / `sa.Numeric(10,3)` quantity, literal-label
  `postgresql.ENUM(..., create_type=False)`.
- New `conftest.py` fixtures in the established `make_X` + `clean_X` raw-SQL pair shape:
  `make_credit_customer`, `clean_credit_customers`, `make_credit_sale`,
  `make_credit_repayment`, `clean_credit_sales`, `clean_credit_repayments`.

---

## 5. Error codes introduced

| Code | Status | Meaning |
|---|---|---|
| `CREDIT_CUSTOMER_NOT_FOUND` | 404 | Also the resolver's 404 |
| `CREDIT_CUSTOMER_PHONE_EXISTS` | 409 | `(outlet_id, phone)` unique |
| `CREDIT_CUSTOMER_INACTIVE` | 409 | New **sale** only; a repayment is fine (M7) |
| `CREDIT_LIMIT_EXCEEDED` | 409 | §6.6, strictly `>` |
| `LIMIT_OVERRIDE_REQUIRES_ADMIN` | 403 | §6.6 / §8 |
| `CREDIT_SALE_NOT_FOUND` | 404 | |
| `CREDIT_SALE_NOT_IN_SHIFT` | 409 | |
| `CREDIT_SALE_ALREADY_REVERSED` | 409 | Route-level, mirroring `EXPENSE_ALREADY_REVERSED` |
| `CREDIT_SALE_MISSING_RECEIPT` | 409 | §6.8's last close precondition (D6) |
| `CREDIT_REPAYMENT_NOT_FOUND` | 404 | |
| `CREDIT_REPAYMENT_NOT_IN_SHIFT` | 409 | |
| `CREDIT_REPAYMENT_ALREADY_REVERSED` | 409 | |

**`_CONSTRAINT_ERRORS` additions (M11):** `uq_credit_sales_reverses_id` and
`uq_credit_repayments_reverses_id` → 409 `ALREADY_REVERSED`.

**Reused unchanged:** `ALREADY_REVERSED`, `CANNOT_REVERSE_A_REVERSAL`,
`CANNOT_EDIT_A_REVERSAL`, `LOCKED_SHIFT_REVERSAL_REQUIRES_ADMIN`, `ATTACHMENT_NOT_FOUND`,
`ATTACHMENT_ALREADY_LINKED`, `NOT_YOUR_ATTACHMENT`, `SHIFT_NOT_FOUND`, `SHIFT_NOT_OPEN`,
`SHIFT_LOCKED`, `NOT_YOUR_SHIFT`, `NOT_A_MEMBER`, `MEMBERSHIP_INACTIVE`,
`INSUFFICIENT_ROLE`, `IDEMPOTENCY_KEY_REQUIRED`, `IDEMPOTENCY_KEY_REUSED`,
`REQUEST_IN_PROGRESS`, `NO_FIELDS_TO_UPDATE`, `INVALID_CURSOR`, `FUEL_TYPE_NOT_FOUND`,
`VALIDATION_ERROR`.

---

## 6. Not in Phase 9

`salesman_shortfalls` (Phase 10 — owner's decision); §6.4's cash equation and
`daily_cash_summaries` (Phase 10); `bank_deposits` (Phase 10); statements or reminders sent
to customers; interest or ageing buckets on outstanding; per-customer credit reports beyond
the outstanding list; OCR on udhaar slips (§12); any frontend (Phase 12); GST or invoicing
(§12); retrofitting audit writes onto Phase 3's admin endpoints (§11's optional slot 11).

---

## 7. Verification checklist

### A — suite and migration health

- [x] `docker compose up -d db`, then `pytest` fully green; total ≥ 741 + the new files'
      count, and **no test deleted or weakened** to get there.
- [x] **Run the full suite, not `-k credit`.** The teardown debt above fails *other*
      tests, and a filtered run is exactly what hides it.
- [x] **Run the suite twice back to back without recreating the database.** A leaked
      credit customer collides on `(outlet_id, phone)` the second time round — the failure
      mode `clean_expense_categories` exists to prevent, in a new table.
- [x] `alembic upgrade head` → `0012`; `alembic check` reports no new operations.
- [x] `alembic downgrade base && alembic upgrade head` round-trips cleanly, and `0012`'s
      `downgrade()` drops `credit_repayment_mode` from `pg_type` — the `DROP TABLE`-does-
      not-drop-an-enum trap `0008` warned about and `0010` was caught by.
- [x] `tests/test_migrations.py::test_migration_is_reversible` still passes — it downgrades
      to base and back **mid-suite**, so any new session-scoped fixture breaks it.
- [x] 100% coverage on every new module, bar the `__main__` guard convention.
- [x] `tests/test_routes.py` still passes — every new route under `/api/v1`.
- [x] `tests/test_migrations.py` gains name-level assertions for every new table,
      constraint and index.

### B — structural assertions no value test makes

- [x] `credit_customers` carries its own `outlet_id NOT NULL`; `credit_sales` and
      `credit_repayments` do **not** (derivable via `shift_id`, §5.0).
- [x] `credit_sales.attachment_id` is `NOT NULL` in `information_schema.columns` — asserted
      directly, not inferred from a 4xx.
- [x] `uq_credit_sales_reverses_id` and `uq_credit_repayments_reverses_id` both exist and
      both appear in `_CONSTRAINT_ERRORS` — i.e.
      `test_every_reversal_unique_constraint_is_mapped_to_a_business_error` passes without
      being modified.
- [x] `credit_repayment_mode` exists as its own type with exactly
      `cash, card, upi, bank_transfer`, and is **not** `expense_mode` or `collection_mode`.
- [x] `UNIQUE (outlet_id, phone)` exists on `credit_customers`.
- [x] `grep -rn "is_settled" app/ alembic/ tests/` → **zero matches**.
- [x] `grep -rn "float(\|sa.Float\|Float(" app/models/credit.py app/services/credit.py
      app/api/v1/credit_*.py` → zero matches.
- [x] No `relationship()` in `app/models/credit.py`.
- [x] No hardcoded threshold literals; no `OFFSET`; no `DELETE` route.
- [x] `credit_customers` uses `sa.Text()`, not `String(n)`; `credit_limit` and both
      `amount` columns are `Numeric(12,2)`; `quantity` is `Numeric(10,3)`.
- [x] The four structural tests exist and pass against all four new modules.
- [x] `grep -n "credit" tests/conftest.py` shows passes in `make_user`, `make_shift` **and**
      `clean_shifts` — not just the new `make_credit_*` fixtures.
- [x] `app/api/v1/router.py` registers the new routers with a `# Phase 9 --` block comment,
      and the static-before-parameterised ordering hazard is respected
      (`/credit-customers/outstanding` before `/credit-customers/{customer_id}`).

### C — domain rules (§10's *Credit* block, filled in)

- [x] Credit sale with **no** `attachment_id` → rejected (422 from Pydantic).
- [x] Credit sale with a **non-existent** `attachment_id` → 404, and **no row written**.
- [x] Credit sale with an attachment **already claimed by a live expense** → 409
      `ATTACHMENT_ALREADY_LINKED`, and the reverse direction too (expense claiming a credit
      sale's attachment).
- [x] Credit sale with an attachment from **another outlet** → 404, not 403.
- [x] Outstanding correct after a **partial** repayment.
- [x] Outstanding correct after a **reversed sale** (the negative row nets out).
- [x] Outstanding correct after a **reversed repayment**.
- [x] Repayment **larger than outstanding** → accepted; outstanding goes negative (M14).
- [x] Credit sale exceeding the limit → 409 `CREDIT_LIMIT_EXCEEDED`; **admin override
      succeeds**, the reason lands on the row, and an `audit_logs` entry carries it.
- [x] Non-admin sending `limit_override_reason` → 403 `LIMIT_OVERRIDE_REQUIRES_ADMIN`.
- [x] **Boundary:** outstanding + amount exactly equal to `credit_limit` → accepted; one
      paisa over → 409.
- [x] `credit_limit IS NULL` → unlimited; a ₹5,00,000 sale is accepted.
- [x] A **deactivated** customer refuses a new sale (409) but **accepts a repayment** (M7),
      and historical rows still read and report.
- [x] Quantity without `fuel_type_id` → refused by `ck_credit_sales_quantity_needs_fuel_type`.
- [x] A CBG credit sale's `quantity` is read against `unit_of_measure = kilogram`; nothing
      in the credit path assumes litres (§4.5).
- [x] Amount diverging from `quantity × rate_at` logs a warning and **still writes** (M12);
      a fuel with no price row does not block the sale.
- [x] A `PATCH` cannot move a sale to a different customer or shift (M16).
- [x] `Decimal` end to end — a `test_decimal_roundtrip`-style assertion for both new tables.
- [x] A customer's `phone` cannot be duplicated within an outlet, **and can** be reused at a
      different outlet (the constraint is outlet-scoped, §5.0).

### D — permissions (§8)

- [x] Attendant creates a credit sale on **their own** open shift → 201.
- [x] Attendant writes to **another attendant's** shift → 403 `NOT_YOUR_SHIFT`.
- [x] Attendant `GET /credit-customers` → 200, and the payload contains **no `phone`, no
      `credit_limit`, no `outstanding`** — asserted on keys, not just values.
- [x] Attendant `GET /credit-customers/{id}` → 403; `.../ledger` → 403;
      `/credit-customers/outstanding` → 403.
- [x] Manager reads all four; manager `POST /credit-customers` → 403 (admin only).
- [x] Admin at another outlet → 403 `NOT_A_MEMBER` (D8's chosen posture, pinned by a test
      so a future change is deliberate).
- [x] Attendant reads the signed URL for a credit-sale receipt on **their own** shift → 200
      (this fails today; it is what the `may_read()` extension buys).
- [x] Attendant reads another attendant's credit-sale receipt → 403 `NOT_YOUR_ATTACHMENT`.

### E — idempotency, reversals, immutability

- [x] Same `Idempotency-Key` twice on `POST .../credit-sales` → **one** row, byte-identical
      response both times.
- [x] Same key, **different** body → 422 `IDEMPOTENCY_KEY_REUSED`.
- [x] Missing key → 400 `IDEMPOTENCY_KEY_REQUIRED`, on all four money-creating POSTs.
- [x] A refusal (bad customer, limit exceeded) **releases** the key — the corrected retry
      with the same key succeeds rather than being refused as a duplicate.
- [x] Reversal of a credit sale: negative amount, `reverses_id` set, non-blank reason
      required, **and the reversal row carries the original's `attachment_id`** (D2).
- [x] Double reversal → 409 `ALREADY_REVERSED`; reversing a reversal → 409
      `CANNOT_REVERSE_A_REVERSAL`; `PATCH` on a reversal → 409 `CANNOT_EDIT_A_REVERSAL`.
- [x] Any write to a **locked** shift → 409 `SHIFT_LOCKED`; a reversal on a locked shift is
      admin-only (403 `LOCKED_SHIFT_REVERSAL_REQUIRES_ADMIN` for a manager).
- [x] Any write to a **closed** shift → 409 `SHIFT_NOT_OPEN`.
- [x] Blank / whitespace-only `reversal_reason` refused by **both** Pydantic and the DB
      CHECK (assert the CHECK directly with raw SQL — the 0007 lesson).

### F — attachments and the close precondition

- [x] A credit-sale attachment is **never** swept by `cleanup_attachments`, at any age,
      including one whose sale was later reversed (§7.4).
- [x] `linked_at` is stamped on the credit sale's first link and not re-stamped.
- [x] `link()`'s 409 message no longer says "expense" when the claimant is a credit sale.
- [x] Close a shift with a valid credit sale → succeeds.
- [x] Close a shift after inserting a **raw** credit-sale row whose attachment has
      `linked_at IS NULL` → 409 `CREDIT_SALE_MISSING_RECEIPT` (D6's provocation).
- [x] All three close preconditions now exist; the `shifts.py:563` comment block is updated
      to say so rather than left naming Phase 9 as pending.

### G — the checks no test replaces

- [x] **One real day, end to end.** Open a shift, enter readings, take a real photograph of
      a real udhaar slip from an iPhone on default settings (the HEIC path), convert, upload,
      record the credit sale against a real customer, record a part repayment in cash on a
      later shift, read the outstanding back, close and lock the day. Confirm the outstanding
      figure matches what the paper register says.
- [x] **Confirm with the owner that the outstanding figure is the one they recognise.** A
      plausible-but-wrong balance is this project's stated primary failure mode, and a
      customer's udhaar total is the number they will check first.

---

## 8. What actually shipped

**931 tests, up from 741.** 175 new across 8 files, plus the Step 0 fix and two existing
files updated. 100% coverage on every Phase 9 module. `alembic` at `0012`, `check` clean,
`downgrade base` / `upgrade head` round-trips, suite green twice back to back against the
same database.

| Step | Commit |
|---|---|
| 0 | `Phase 9 Step 0: map every check-then-insert race, not just reversals` |
| 1 | `Spec: credit sales, repayments and the salesman shortfall, before Phase 9` |
| 2 | `Phase 9 Step 2: credit tables, models and the repayment mode enum` |
| 3 | `Phase 9 Step 3: credit service -- outstanding, limits, reversals` |
| 4 | `Phase 9 Step 4: credit customers -- admin CRUD, attendant list` |
| 5 (+7) | `Phase 9 Step 5: credit sales -- receipt-enforced, limit-checked` |
| 6 | `Phase 9 Step 6: credit repayments` |
| 8 | `Phase 9 Step 8: wire §6.8's last close precondition` |
| 9 | `Phase 9 Step 9: the customer ledger` |
| 10 | `Phase 9 Step 10: structural permission tests for credit` |
| 11 | this file and `docs/phase-9-notes.md` |

**Where it differs from the plan above.**

* **Step 0 found more than one defect's worth.** The plan expected a Phase 8 audit; what it
  found was that Phase 8 fixed the reversal instance of a bug and generalised its test one
  size too small. Eight unique constraints had check-then-insert races returning opaque
  500s, including `uq_expense_categories_outlet_code`, which Phase 8 introduced itself. The
  structural test now covers every `uq_*` rather than every `uq_%_reverses_id`.
* **M13's quantity CHECK was wrong as written, and Step 5's tests caught it.** The plan
  specified `quantity IS NULL OR quantity > 0`. §6.9's reversal negates the quantity
  alongside the amount, so that constraint turned every reversal of a *fuel* credit sale
  into a 500. Non-fuel sales reversed cleanly, which is why Steps 2-4 all passed. Replaced
  with a sign-aware `ck_credit_sales_quantity_sign`; `0012` amended in place, since it has
  never been applied outside development.
* **PATCH null semantics diverge from Phase 8**, which the plan did not anticipate.
  `expense_categories.py` skips every explicit null; that is right for a table whose
  editable columns are all NOT NULL and wrong here, because `credit_limit`'s null is §6.6's
  "no limit" and a cap must be liftable. `_NULLABLE_FIELDS` names the two columns where a
  null applies.
* **Steps 3 and 10 grew tests the plan filed elsewhere.** `test_credit_outstanding.py` and
  `test_credit_limits.py` landed with the service rather than waiting for the routers,
  because outstanding is the number §14 says the owner checks first and it deserved its own
  definition-level tests underneath whatever an endpoint chooses to return.
* **`QUANTITY_NEEDS_FUEL_TYPE` (422) was not in the plan's error table.** M13 specified the
  database CHECK but not the readable half in front of it.
* **The outstanding report shipped in Step 4, not Step 9**, since it is a customers route
  and splitting it from the rest of that router would have meant writing the file twice.

---

## 9. Still owed by the owner

Carried forward, plus what this phase adds:

- **D8's tenancy posture is the plan's one loose end.** `credit_customers` gets the ordinary
  403 `NOT_A_MEMBER`, not §7.3's stricter 404, on the reasoning that attendants cannot read
  customer detail at all. If a customer's phone number and vehicle registrations should be
  as protected as a receipt photograph, say so and it becomes a bespoke resolver like
  `attachments.py`'s.
- **D9 was not answered.** Both credit tables get reversal columns. Say so now if that is
  wrong — it is a migration, and it is cheap only before there are rows.
- **The real credit customer list**, and each one's credit limit. Until entered, every
  customer is unlimited.
- **Is a credit limit even used at this outlet today?** If nobody sets one, `CREDIT_LIMIT_
  EXCEEDED` never fires and the override path is untested against reality.
- **Phase 10's shortfall table needs its shape decided**: does a shortfall carry a
  salesman's `user_id` (not a `credit_customer_id`), and is it repaid, written off, or
  deducted from wages?
- `EXPENSE_RECEIPT_THRESHOLD` = ₹5,000 is still a guess, live on real money.
- The real expense category list beyond the four seeded.
- CBG's real max flow rate in kg/min; petrol and diesel dealer commissions; whether testing
  quantities are recorded on paper.
- **The locker model's effect on §6.5's rolling balance — now due, Phase 10 is next.**
