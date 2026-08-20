# Phase 7 — Expenses: Plan, Decisions, and Verification Checklist

> Finalised at implementation time, matching the `phase-5-plan.md` / `phase-6-plan.md`
> pattern. Written before the code; §8 records what actually shipped where it differs.

---

## Context

Phase 6 gave the system a record of what the pump *received*. Phase 7 gives it a record of
what the pump *paid out* — the last term §6.4's cash equation needs before Phase 10 can
assemble it, apart from credit repayments (Phase 9).

Phase 7 also lands §6.7's review-flagging control and the third and final shift-lifecycle
precondition, `UNREVIEWED_EXPENSES_EXIST`, which had sat as a named comment at
`app/api/v1/shifts.py` since Phase 4.

---

## 1. Phase 6 audit — verdict

**Feature-complete against its own checklist, but a deeper pass found three defects the
checklist never asked about.** Verified by running the code, not by reading the notes:

| Check | Result |
|---|---|
| `pytest` | 484 passed |
| `alembic current` / `alembic check` | `0006 (head)` / no drift |
| Coverage on every checklisted module | 100 % |
| `collections` constraints in Postgres | Exactly the four §3.1 names, no `(shift_id, mode)` unique |
| Structural greps | `float(`, `shift_sales` in `shifts.py`, hardcoded outlet in `collections.py` — all zero matches |

Three defects, all reproduced through HTTP before any fix:

| # | Defect | Consequence |
|---|---|---|
| **P6-1** | `PATCH` refused a row that *is* a reversal, not one that *has been* reversed | A cancelled original stayed editable; net cash for the mode went to −₹2,000 |
| **P6-2** | `Field(min_length=3)` measured the string *before* `.strip()` | A whitespace-only reversal reason (`"   "`) stored as `''` |
| **P6-3** | `live_collection_for_mode` ended in `scalar_one_or_none()` | Two live rows for one mode → `MultipleResultsFound` → the shift became unreadable and unclosable through the API |

Plus one latent conftest gap: `clean_shifts` swept `nozzle_readings` and `shifts` but never
`collections`, so a test creating a collection through the API and using `clean_shifts`
would fail teardown with a foreign-key violation. `expenses` would hit the same wall.

**Decision: fix all four as Step 0, test-first, before any Phase 7 code.**

---

## 2. Decisions

### D1 — `expenses` gets a `mode` column

§6.4 subtracts `cash_expenses`, implying some expenses are not cash, but §5.2's `expenses`
had no payment-mode column. Without it, Phase 10 would treat every expense as a drawer
movement — a ₹40,000 electricity bill paid online would invent a ₹40,000 phantom
shortfall, and §14 already records that this outlet books a shortfall as udhaar against
the salesman's own name.

**`mode` — `cash | card | upi | bank_transfer`, NOT NULL, no default.** An answer, never
an omission, exactly as §6.8 requires an explicit ₹0 cash declaration. Phase 10 filters
`mode == cash`.

### D2 — `fuel_purchase` removed from the category enum

Owner's reasoning: a tanker restock is paid from the bank account and settles against the
IOCL ledger. It never touches the drawer, so it was never a cash expense — it belongs to
the post-V1 bank/PAD module §12 already scopes out. Leaving the label in the dropdown
invited a salesman to record a lakh-rupee bank movement as a drawer expense, exactly what
§14 forbids for IOCL/PAD payments elsewhere.

`misc`/`other` collapsed into a single `other`: two synonymous labels can split one real
expense across both, defeating §6.7's per-category aggregate.

**Final enum: `salary | maintenance | electricity | other`.**

### D3 — the aggregate flag marks every live unreviewed row in the group

§6.7 requires a per-`(business_date, category)` aggregate flag but does not say which rows
carry it. Flagging only the row that crossed the line shows a manager a trivial-looking
figure and hides the pattern behind it.

**Every live unreviewed row in the group is flagged**, re-evaluated on create, on a
`PATCH` that changes the amount, and on a reversal's replacement. **Flags are never
auto-cleared** — a human clears them through the review route, the same shape as §13.10's
downstream reading flag.

### Mechanical decisions

| # | Decision | Reason |
|---|---|---|
| M1 | `amount CHECK > 0` replaced by a sign rule keyed to `reverses_id`, strict (`> 0` / `< 0`) | §6.9 corrections are negative rows; unlike a cash collection, a ₹0 expense has no reason to exist |
| M2 | `attachment_id` deferred to Phase 8 | `attachments` doesn't exist yet; §11 forbids scaffolding ahead |
| M3 | `description` NOT NULL, 3–500 chars, Pydantic + DB CHECK | A blank description is the audit hole this table closes |
| M4 | Separate `expense_mode` enum, not shared with `collections.mode` | A shared enum forces a later `ALTER TYPE` on live data |
| M5 | No `outlet_id`, no `(shift_id, category)` unique, no append-only trigger | Derivable via `shift_id`; several expenses per category per shift is normal; immutability comes from shift status |
| M6 | A reversed expense stops blocking a lock | Cancelled money; blocking on it is friction with no control value |
| M7 | Every new CHECK must be unreachable through the API or added to `_CONSTRAINT_ERRORS` | The exact Phase 5 defect that blocked Phase 6 |

---

## 3. Build order

### Step 0 — Phase 6 fixes, test-first, its own commit

Fixed all three defects plus the two carried, non-blocking gaps (`uq_collections_reverses_id`
missing from the allowlist; the close-side override exemption untested). Migration
`0007_reversal_reason_not_blank.py` strengthened `ck_collections_reversal_has_reason` from
NOT NULL to a real non-blank check — the first attempt used `btrim(...) <> ''`, and the
raw-SQL test caught that one-argument `btrim` strips spaces only, missing a tab-only
reason. Landed as `reversal_reason ~ '[^[:space:]]'`.

**Consequence for numbering: Step 0 owns migration `0007`, so the expenses migration is
`0008`.**

### Step 1 — `CLAUDE.md` amendment, its own commit

§5.2 (`mode`, sign rule, category enum, `reverses_id`/`reversal_reason`, `attachment_id`
deferral), §6.4 (`cash_expenses` means `mode = cash`), §6.7 (which rows the aggregate
flags, re-evaluation, never-auto-clear, M6), §14 (new guardrail naming the removed
category). Resolved open question struck.

### Step 2 — migration `0008` + model + core enums

`expenses` table: `id`, `shift_id`, `category`, `mode`, `amount`, `description`, `paid_to`,
`reverses_id`, `reversal_reason`, the four review columns, `created_at`, `created_by`.
Constraints: `uq_expenses_reverses_id`, `ck_expenses_amount_sign`,
`ck_expenses_reversal_has_reason` (written strong from day one — the regex form, not
NOT-NULL-only), `ck_expenses_reversal_not_self`, `ck_expenses_description_length`. A
partial review index copied from `nozzle_readings`. No `(shift_id, category)` unique — the
opposite of `collections`, and both absences get a comment explaining why.

### Step 3 — `app/services/expenses.py`

`all_expenses`, `totals_by_category`, `reversal_of` (extracted so `PATCH` and `reverse`
can never drift apart the way Step 0 found on `collections`), `apply_review_flags` (§6.7's
two rules in one function, scoped to `(outlet_id, business_date, category)` across
shifts), `unreviewed_flagged_expenses` (shift-scoped, excludes reversed rows per M6),
`reverse` (mirrors `collections.reverse`, replacement carries the original's category,
mode and description).

### Step 4 — `app/api/v1/expenses.py`

Six routes: `GET`/`POST`/`PATCH` on `/shifts/{id}/expenses`, `POST .../reversals`,
`PATCH .../review`, and `GET /expenses/flagged` — a new manager-only, cursor-paginated
cross-shift queue, reusing `app/api/cursor.py`'s `encode_cursor`/`decode_cursor` on
`(created_at, id)` instead of `(effective_from, id)`. Retires a gap Phase 5 left on
`nozzle_readings`: a partial review index with no endpoint ever running the query it was
built for.

### Step 5 — wire `UNREVIEWED_EXPENSES_EXIST` into `lock_shift`

Shift-scoped even though the flag that set a row may have looked across shifts on the
same business date — locking is a per-shift action.

### Step 6 — tests + `tests/conftest.py`

Five new test files. `conftest.py` gets `make_expense`/`clean_expenses` and a new
teardown generation in `make_shift`, `make_user`, and `clean_shifts` — the last one also
finally sweeping `collections`, closing the Phase 6 audit's latent gap.

Two real defects surfaced while writing the suite (see `docs/phase-7-notes.md` §5):
`unreviewed_flagged_expenses` didn't actually implement M6, and
`ck_expenses_description_length` (migration `0008`) stripped every whitespace character
instead of only the ends, rejecting legitimate descriptions with an internal space. Fixed
via migration `0009` — **the expenses migration count is 0008–0009, not 0008 alone.**

### Step 7 — this file and `docs/phase-7-notes.md`

---

## 4. Error codes introduced

| Code | Status | Meaning |
|---|---|---|
| `EXPENSE_NOT_FOUND` | 404 | |
| `EXPENSE_NOT_IN_SHIFT` | 409 | Id belongs to a different shift |
| `CANNOT_EDIT_A_REVERSAL` | 409 | |
| `EXPENSE_ALREADY_REVERSED` | 409 | PATCH on an expense that has been reversed |
| `EXPENSE_NOT_FLAGGED` | 409 | Review attempted on an unflagged row |
| `UNREVIEWED_EXPENSES_EXIST` | 409 | §6.7's lock precondition |

Reused unchanged: `ALREADY_REVERSED`, `CANNOT_REVERSE_A_REVERSAL`,
`LOCKED_SHIFT_REVERSAL_REQUIRES_ADMIN`, `SHIFT_LOCKED`, `SHIFT_NOT_OPEN`,
`NOT_YOUR_SHIFT`, `IDEMPOTENCY_KEY_REQUIRED`, `IDEMPOTENCY_KEY_REUSED`,
`NO_FIELDS_TO_UPDATE`.

---

## 5. Verification checklist

- [x] `pytest` fully green — 580 passed.
- [x] `alembic upgrade head` → `0009`; `alembic check` clean; downgrade base / upgrade
      head round-trip clean.
- [x] 100 % coverage on `app/api/v1/expenses.py`, `app/services/expenses.py`,
      `app/core/expenses.py`, `app/models/expense.py`.
- [x] `expenses` has no `outlet_id`, no `(shift_id, category)` unique, no append-only
      trigger — all asserted structurally.
- [x] `float(` / `sa.Float`, `attendant_id`, `DEFAULT_OUTLET_ID`, and a hardcoded `1000`
      threshold — zero matches across the Phase 7 modules.
- [x] `fuel_purchase` — zero matches outside explanatory prose in `CLAUDE.md` and
      `app/core/expenses.py`'s docstring.
- [x] §10's boundary cases: ₹999.99 / ₹1000.00 not flagged, ₹1000.01 flagged (strictly
      `>`); two same-category same-day entries trip the aggregate.
- [x] Every response error carries `{detail, code}`.
- [x] `CLAUDE.md` amended in its own commit, before any code (Step 1, commit `88d89a7`).

**The check no test replaces:** take one real day from the paper register — readings,
cash, card, UPI, **and** that day's expenses — enter it end to end, close the shift and
lock it. Owed by Phase 5, again by Phase 6; Phase 7 is the first phase that can exercise a
complete day through to `locked`.

---

## 6. Not in Phase 7

§6.4's cash equation, `expected_closing`, variance, `daily_cash_summaries` (Phase 10);
`other_cash_income` (Phase 10); `attachment_id` and receipt upload (Phase 8);
`credit_repayments` (Phase 9); anything touching bank balances or the IOCL ledger
(post-V1).

## 7. Still owed by the owner

CBG's real kg/min ceiling and confirmation of 60 L/min for petrol and diesel; whether
salesmen record testing quantities on paper and in what unit; petrol and diesel dealer
commissions; the salesman-shortfall vs receipt contradiction (before Phase 9); the locker
model's effect on §6.5's rolling balance (before Phase 10).
