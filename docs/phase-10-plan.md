# Phase 10 — Cash Engine: Plan, Decisions, and Verification Checklist

> Matches the `phase-5/6/7/8/9-plan.md` pattern, and follows `phase-9-plan.md` in being
> written **before** the code rather than finalised at implementation time. §8 records what
> actually shipped where it differs from this document, and the checklist marks in §7 are
> filled in as they are met.

---

## Context

Phase 9 shipped clean: **931 tests**, `alembic` at `0012`, `check` clean, `downgrade base` /
`upgrade head` round-trips, suite green twice back to back. Working tree is clean apart from
`.claude/`.

Phase 10 is the capstone. Every phase since 5 has been assembling terms for one equation and
deliberately refusing to compute it:

| Where | What it left for Phase 10 |
|---|---|
| `app/services/credit.py:443` | `credit_sales_total(shift_id)` — written, tested, **no caller** |
| `app/api/v1/credit_repayments.py:112,208` | `cash_total`, already filtered to `mode = cash` |
| `app/services/collections.py:10` | *"§6.4's cash equation is Phase 10's"* |
| `app/services/collections.py::declared_cash` | `None` vs `0.00` kept apart, for exactly this |
| `app/models/collection.py:11-27` | derived cash vs declared cash, and the shortfall between |
| `app/services/expenses.py` | `mode = cash` on every expense, added in Phase 7 for §6.4 |
| `app/api/v1/readings.py:822` | `GET /shifts/{id}/sales` — `total_sales`, derived, never typed |
| `app/core/errors.py::AppError` | `PRIOR_DAY_NOT_RECONCILED` named as a future code |
| `app/api/v1/credit_customers.py:365` | *"Phase 10 gives shortfalls their own record type"* |
| `tests/test_credit_permissions.py:148` | a test that **pins** that guardrail in place |

This phase is also where the project's stated primary failure mode is most dangerous. Every
number here is plausible. A shortfall double-counted, a locker balance carried forward from
the wrong figure, a lubricant sale booked as a phantom surplus against a salesman's name —
none of them crash, and all of them look like money.

### Five decisions were put to the owner. Four came back.

1. **Rolling balance under the locker model** → carry the arithmetic forward; a physical
   count re-anchors the chain. (D1)
2. **Who books a shortfall** → a manager, explicitly, with the computed figure pre-filled.
   Never automatic. (D4)
3. **How a shortfall is settled** → **cash repayment only.** No write-off, no wage
   deduction. (D5, and see §9 — this one has a consequence worth knowing about)
4. **Non-fuel income** → yes, it is real here; give it a **per-shift** entry. (D6)
5. **Non-fuel income belongs on the sales side, not the cash side** — not asked, because the
   arithmetic settles it in both directions. Worked through in D6.

---

## 1. Phase 9 audit — Step 0

Before any Phase 10 code, per the standing habit. P6 found 3 defects, P7 found 3, P8 found 1,
P9 found 8. Run these and fix anything found **test-first** (§10, no exceptions):

- [x] `docker compose up -d db`, then `pytest` from clean — expect **931**.
- [x] `alembic current` → `0012 (head)`; `alembic check` → no drift; `downgrade base` /
      `upgrade head` round-trip; `credit_repayment_mode` gone from `pg_type` after downgrade.
- [x] Run the suite **twice back to back** against the same database — the leaked-row check.
- [x] Coverage on every Phase 9 module (`core/credit.py`, `models/credit.py`,
      `services/credit.py`, all three `credit_*` routers).
- [x] **`services/credit.py::outstanding` vs `outstanding_by_customer`** — two
      implementations of one §6.6 rule. Confirm they cannot disagree, or make one call the
      other. This is the `reversal_of()` lesson from Phase 7 Step 0 in a new table.
- [x] **`credit_sales_total` and `cash_total` are computed in two different layers** — one in
      `services/credit.py`, one inline in `api/v1/credit_repayments.py`. Phase 10 is the
      first caller of both; move the repayments one into the service so the equation reads
      from one place. A router is not where a term of §6.4 should live.
- [x] **`shift_sales` raises on a missing margin** — confirmed live blocker, see D2.
- [x] `_NOT_REACHED_BY_THE_ERROR_HANDLER`'s exclusion list in `tests/test_errors.py` — read
      each exclusion and confirm it is still true before adding five tables' worth of
      constraints next to it.
- [x] `tests/conftest.py` is now **1,575 lines** with `clean_credit`. Confirm `make_user`,
      `make_shift` and `clean_shifts` genuinely sweep the Phase 9 tables (P7's audit found
      `clean_shifts` had never swept `collections`).

---

## 2. Spec amendment (Step 1, its own commit)

`Spec: the locker, the shortfall record and non-fuel income, before Phase 10`

Everything below edits `CLAUDE.md` **before** any Phase 10 code. Each is a real change, and
three of them are §14 "flag it loudly" contradictions rather than additions.

| § | Amendment |
|---|---|
| **§6.4** | **Add the `− shortfalls_booked` term** (D3). Restate `other_cash_income` as `+ non_fuel_sales` **on the sales side, not the cash side** (D6) — the current wording is only correct when every non-fuel sale is cash. Rename the term accordingly and keep the old name in a note so the change is traceable |
| **§6.5** | **Restate for a running locker** (D1). Day N opens at day N−1's `actual_counted` **when there is one**, and at day N−1's `expected_closing` otherwise. `PRIOR_DAY_NOT_RECONCILED` narrows to "the previous day is not finalised". Record that a physical count is an occasional **audit that re-anchors the chain**, in the same shape as §4.7 |
| **§5.2** | New table **`salesman_shortfalls`** and **`salesman_shortfall_settlements`** (D4, D5). New table **`non_fuel_sales`** (D6). `bank_deposits` gains §6.9's reversal quartet (M3) and a note that `business_date` is **server-set from the shift**, never client-supplied (M4). `daily_cash_summaries` gains the component snapshot columns and `opening_balance_source` (D7) |
| **§5.0** | Add the per-phase landing rows: `non_fuel_sales`, `salesman_shortfalls`, `salesman_shortfall_settlements` — **no** own `outlet_id` (derivable via `shift_id`); `daily_cash_summaries` — **yes**, as already scheduled |
| **§6.3** | Note that **valuation and profit are separable**: the cash equation needs `rate_at` only, and a missing margin must not make a day unreconcilable (D2). Same reasoning §6.8 already gives for close preconditions |
| **§8** | Add rows: *Record a non-fuel cash sale* (attendant own-only / manager / admin); *Book a salesman shortfall* (manager, admin); *Record a shortfall settlement* (manager, admin); *Read the shortfall ledger* (manager, admin); *Create / update a daily summary* (manager, admin); *Finalise or unfinalise a day* (admin) |
| **§10** | Fill in the *Cash* test block with §7's cases; add a *Shortfalls* block |
| **§13** | §13.14 rewritten: shortfalls **are** modelled, here is how. New approximation: **a shortfall has no write-off path in V1** (§9). New approximation: the daily summary is a **snapshot** and a reopened shift beneath it flags rather than recomputes (§13.10's shape) |
| **§14** | New "Do not": *do not carry day N's opening from `expected_closing` when day N−1 was actually counted* — the count wins, and §6.5 exists because a ₹200 shortage must not vanish. New "Do not": *do not add `non_fuel_sales` to the cash side of §6.4* — it belongs to sales, or a card-paid oil sale understates derived cash. New "Do not": *do not book a shortfall automatically at shift close* (D4) |
| **§5.4** | Fix the stale "aggregates one business date across **both** shifts" — §4.7 removed the two-shift assumption and this sentence survived it |

---

## 3. Decisions

### D1 — The locker rolls forward arithmetically; a count re-anchors it

**Owner's answer.** §6.5 as written assumes a drawer counted nightly. §14 records that this
outlet has no fixed counting moment and the locker carries a running balance. Both cannot
hold, and §6.5 as written blocks finalising every day forever.

```
opening_balance(day N) = actual_counted(day N-1)      if day N-1 was counted
                       = expected_closing(day N-1)    otherwise
                       = <admin-seeded figure>        if there is no day N-1 at all
```

This is §4.7's chain, applied to money instead of a meter: the system predicts, a human
occasionally confirms, **both values are stored**, and a disagreement is recorded as that
day's variance rather than absorbed. §6.5's actual principle — *"the physical cash actually
in the drawer is what carries forward, not the theoretical figure"* — survives intact,
because on every day a physical figure exists it is the one that wins.

**`PRIOR_DAY_NOT_RECONCILED` keeps its code and changes its meaning**: day N cannot be
finalised while day N−1 exists and is not finalised. It no longer fires merely because
nobody counted.

**The anchor is the first summary row itself, not a separate record** — §4.7's exact
argument, transplanted. When no prior summary exists for an outlet, `opening_balance` becomes
a **required** payload field and the caller must be an admin (403
`OPENING_BALANCE_REQUIRES_ADMIN` otherwise). Supplying one when a prior day *does* exist is
refused with 409 `OPENING_BALANCE_IS_CHAINED` — the figure is derived, not typed. A separate
seed table would duplicate a value that already exists on that first row, and the two copies
would eventually disagree about where the locker started.

### D2 — Valuation is split from profit, or this outlet cannot reconcile a single day

**A live blocker, found in the code.** `readings.shift_sales` — the only producer of
`total_sales` — calls `pricing.rate_at` **and** `pricing.margin_at`, and `margin_at` raises
409 `NO_MARGIN_FOR_DATE` when no margin row exists. §14 records that **petrol and diesel
dealer commissions have never been entered here.** A cash engine built straight on top would
409 on every petrol shift on day one.

§6.4 needs the **price**. Profit (§6.3, §4.6) is a separate question and a separate figure.
So:

```python
def shift_sales(db, *, shift, price_only: bool = False) -> list[SalesLine]:
```

`price_only=True` skips the margin lookup entirely and leaves `margin_per_unit` and `profit`
as `None`. Every existing caller keeps the default and `GET /shifts/{id}/sales` still 409s on
a missing margin, so `tests/test_sales_valuation.py:293` is untouched.

This is the same reasoning `collections.shift_moved_any_quantity` already applies to §6.8:
*"a close precondition that inherited that would make every petrol shift unclosable because
of a reference-data gap, which is a very confusing way to be told about a missing margin."*
Reconciling the drawer is that argument again, one phase later.

### D3 — §6.4 grows a `shortfalls_booked` term, or the money is counted twice

The single most consequential piece of arithmetic in this phase.

Monday: meters imply Ramesh should hand over ₹50,000. He declares ₹49,500. A manager books
₹500 against him. The locker physically gains ₹49,500 — but §6.4 as written adds the
**derived** ₹50,000, so `expected_closing` says ₹50,000 and Tuesday opens ₹500 rich. That
₹500 is now an asset twice: once as Ramesh's debt, once as cash that is not there. Every
locker count from then on is off by it and nothing explains why.

```
expected_closing = opening_balance
                 + cash_sales               ← derived, per §6.4
                 + cash_credit_repayments
                 − cash_expenses            ← mode = cash only
                 − bank_deposits
                 − shortfalls_booked        ← NEW
```

(with `non_fuel_sales` already inside `cash_sales` via D6's `total_sales`.)

**Why subtract the shortfall rather than just use the declared figure.** They are
algebraically identical — `derived − shortfall == declared` — which is the reassurance that
this is right rather than a fudge. But §14 forbids summing the `cash` collection row into a
derived figure, and writing it as a subtraction means the equation **never reads the cash
row at all**. The cash row stays what §5.2 says it is: the independent observation the
derived figure is checked against.

**When no shortfall is booked, nothing is subtracted** and the gap resurfaces at the next
locker count as a variance with no name on it. That is the correct and honest outcome of a
manager choosing not to book, not a hole.

### D4 — A shortfall is computed by the system and booked by a human

**Owner's answer.** The per-shift accountability figure:

```
accountable_cash = total_sales (fuel, priced)  + non_fuel_sales
                 − card − upi − wallet         ← collections, net of reversals
                 − credit_sales_total
                 + cash_credit_repayments
                 − cash_expenses

shortfall        = accountable_cash − declared_cash
```

Positive is short; negative is a surplus. **Cash repayments are added and cash expenses
subtracted** because both physically pass through the salesman's hands during the shift and
are therefore already inside the figure he declares.

`GET /shifts/{id}/cash-position` returns every term and the gap. It **writes nothing.**
`POST /shifts/{id}/shortfalls` books it, manager floor, mandatory non-blank reason.

**Nothing is booked automatically**, and this is the phase's §4.7 moment. That section's
argument — *"an assumed opening converts theft into a debt owed by someone who did nothing
wrong"* — applies with more force here, because here the debt is explicit and has a name on
it. A ₹500 gap is more often a mistyped reading, a forgotten UPI figure or an unrecorded
udhaar slip than it is theft, and the system must not decide which.

**`salesman_id` is not a payload field.** It is read from `shifts.attendant_id`, which §5.2
already defines as *"the one person accountable for this shift's cash… exactly one name
carries the drawer, and a shortfall is booked against it."* This removes an entire class of
error — a shortfall cannot be booked against the wrong person by a typo.

**The booked amount is not forced to equal the computed gap.** A divergence **logs a
warning** and writes, never refuses — M12's shape from Phase 9. A manager may know the ₹500
is partly a ₹200 slip he has already fixed, and refusing his judgement would send the
correction outside the system.

### D5 — Settlement mirrors the credit model, and is cash-only in V1

**Owner's answer: repaid in cash.** Not written off, not deducted from wages.

Two tables, exactly the `credit_sales` / `credit_repayments` shape the codebase and the owner
already understand:

```
outstanding(salesman) = SUM(salesman_shortfalls.amount)
                      − SUM(salesman_shortfall_settlements.amount)
```

Summed over **every** row, reversals included — they carry negative amounts and net out.
Never filtered to "live", never denormalised. §6.6's rule and §5.2's deleted `is_settled`
flag both transfer verbatim, and the same §14 guardrail against a stored running total
applies here.

A settlement points at the **salesman**, not at a specific shortfall — for §5.2's stated
reason that one repayment covering part of three bills has no honest per-row answer.

**No `mode` column.** Every settlement is cash and every one enters §6.4. §5.0's rule
justifies leaving it out rather than guessing: a `mode` column added later backfills to
`'cash'` correctly for every existing row, because every existing row genuinely is cash — so
by that rule it can wait. Building it now for a single label would be scaffolding ahead
(§11).

### D6 — `non_fuel_sales` is per shift, and it belongs on the **sales** side

**Owner's answer: yes, per-shift entry.** §6.4 names `other_cash_income` and **§5.2 gives it
no column anywhere** — there is nowhere in the schema to put it today.

**Why per shift and not per day.** A ₹500 bottle of oil is in the salesman's hand and *not*
in the meter-derived figure, but it is in the cash he counts. Compare derived fuel cash
against his declaration and he looks ₹500 **over** — a phantom surplus in his name, every day
he sells one. The non-fuel figure has to sit beside the declaration it is compared against.

**Why the sales side, not the cash side — the correction §6.4 needs.** §6.4 currently reads
`+ other_cash_income` as a cash term. That is only right if every non-fuel sale is cash. Take
a ₹500 oil sale paid by **card**:

| | fuel ₹95,000 · oil ₹500 on card · card ₹20,500 · UPI ₹10,000 · udhaar ₹5,000 |
|---|---|
| Truth | cash taken = ₹60,000 |
| Cash-side term | 95,000 − 20,500 − 10,000 − 5,000 = 59,500, **+ 0** cash oil = **₹59,500 ✗** |
| Sales-side term | (95,000 **+ 500**) − 20,500 − 10,000 − 5,000 = **₹60,000 ✓** |

And the all-cash case still agrees: `(95,000 + 500) − 20,000 − 10,000 − 5,000 = 60,500`,
which is what he holds. **The sales-side formulation is correct regardless of how the
non-fuel sale was paid**, so it needs no extra question and no mode column.

Its own table rather than a column on `shifts`, because it is a money row on a shift and
§6.9 governs money rows: a mistyped ₹500 on a closed shift must be correctable by a reversal,
and you cannot reverse a column. §13.2's *"a manual entry field, not an itemised sales
module"* is honoured — an amount and an optional description, no catalogue, no stock, no unit
price.

### D7 — The daily summary stores its components, not just its total

§5.2 names `expected_closing` and gives the reason: *"if a calculation bug is fixed six
months from now, you still need to know what the system told the manager on that day."*

Every word of that applies to the terms as well as the sum. A manager looking at a ₹300
variance needs the breakdown **as it stood**, not as recomputed after a reversal landed
underneath it. So `daily_cash_summaries` snapshots each term: `total_sales`,
`non_fuel_sales_total`, `card_total`, `upi_total`, `wallet_total`, `credit_sales_total`,
`cash_credit_repayments`, `cash_expenses`, `bank_deposits_total`, `shortfalls_booked`.

Flagged as a departure from §5.2's literal column list, per §14.

Also `opening_balance_source` — enum `seeded | counted | carried` — so the row itself says
which of D1's three branches produced its opening figure. Derivable in principle from the
previous row, but not for the seeded first day, and it is the fact a reader most wants next
to the number.

### D8 — Finalising requires every shift on the date to be **locked**

Creating a summary requires every shift on the business date to be `closed` **or** `locked`
(409 `DAY_HAS_OPEN_SHIFTS`). Setting `is_finalised` requires them all **locked** (409
`DAY_NOT_LOCKED`).

**Why locked and not merely closed.** §6.5 chains days together, so a stale `expected_closing`
does not stay local — it propagates into every opening balance after it. §5.2 already says
*"nothing referencing a `locked` shift may be modified"*, and §6.8 makes `locked` terminal.
Locking is therefore the only state in which the snapshot is guaranteed to remain true. It
also inherits §6.7's quality gate for free: a shift cannot lock while a flagged expense is
unreviewed.

**Unfinalising** is admin-only with a mandatory reason, audit-logged — §6.8's shift-reopen
shape. A finalised summary is otherwise immutable.

**A reopened shift beneath a finalised day flags, never recomputes** — §13.10's rule, which
exists precisely so that correcting one row cannot silently rewrite history downstream. The
summary gets `requires_review = true` and a note naming the shift that moved.

### Mechanical decisions

| # | Decision | Reason |
|---|---|---|
| M1 | `daily_cash_summaries.variance` is `GENERATED ALWAYS AS (actual_counted - expected_closing) STORED` | §5.2 says generated. Null propagates correctly when nothing was counted |
| M2 | `actual_counted` nullable; `expected_closing` NOT NULL | D1 — most days have no count. A null count is a real state, not a missing value |
| M3 | `bank_deposits`, `non_fuel_sales`, `salesman_shortfalls`, `salesman_shortfall_settlements` all carry §6.9's quartet: `uq_<t>_reverses_id`, `ck_<t>_reversal_has_reason`, `ck_<t>_amount_sign` (strict `> 0` / `< 0`), `ck_<t>_reversal_not_self` | §6.9 governs financial rows in closed shifts and all four qualify. Phase 9's D9 argument, unchanged. **Every one needs a `_CONSTRAINT_ERRORS` entry in the same commit as the migration** (M11 from Phase 9) |
| M4 | `bank_deposits.business_date` is **set server-side from the shift**, never accepted from a client | §3 rule 7 — it is derivable, so recompute it. §5.2 lists the column so it stays, but a client-supplied copy is a drift source with no upside. Flagged as a §14 schema observation rather than silently dropped |
| M5 | `daily_cash_summaries` is **not** shift-scoped and carries its own `outlet_id` | Already §5.0's schedule. It has no parent shift to derive from |
| M6 | The three shift-scoped new tables carry **no** `outlet_id` | §5.0's derivability rule, same as `collections` and `expenses` |
| M7 | `POST /daily-summaries` takes **no `Idempotency-Key`** | `UNIQUE (outlet_id, business_date)` makes it naturally idempotent — a retry gets 409, exactly §6.10's stated reasoning for nozzle readings. The four money-creating POSTs (deposit, non-fuel sale, shortfall, settlement) **do** require one |
| M8 | `GET /shifts/{id}/cash-position` writes nothing and is manager-floor | It is a report (§8: attendants read own shift, not reports), and it is the same read `POST /shortfalls` pre-fills from |
| M9 | Static paths before parameterised ones: `/daily-summaries/current`, `/salesman-shortfalls/outstanding` declared before any `/{id}` route | Phase 9's M15 hazard. `router.py` already carries the ordering comments |
| M10 | Money in fixtures as **strings cast in SQL**, never Python floats | §3 rule 1, "including in a quick test fixture" |
| M11 | A shortfall booked for an amount other than the computed gap **warns and writes** | D4. Phase 9's M12 shape — a reference-data or judgement divergence must not block a real write |
| M12 | A shortfall may be booked on a `closed` shift, not only an open one | The gap is only knowable once the shift is closed and its readings are final. `writable=True` would make the feature unreachable. Locked stays admin-only, as everywhere else |
| M13 | `_MAX_ROWS = 100` with `truncated: bool` on shift-scoped lists; cursor pagination on `/daily-summaries`, the shortfall ledger and the outstanding report | Matches `expenses.py` / `collections.py` for children, `expenses/flagged` for reports |
| M14 | No `SELECT … FOR UPDATE` anywhere | §13.13's existing approximation. The codebase holds zero row locks and §13.12 means one shift is open at a time |
| M15 | `attachments.link()` and `may_read()` grow a **third** claimant (`bank_deposits`), they are not rebuilt | Phase 9's D7 pattern. Import direction `services/attachments.py → services/cash.py`, never back |
| M16 | A `PATCH` cannot move a row to a different shift, nor a settlement to a different salesman | Phase 8's M16 and Phase 9's M16. Moving money to another person's ledger is a different row, not a correction — use §6.9 |
| M17 | `business_date` in the future → 422 `BUSINESS_DATE_IN_FUTURE`, evaluated in `TZ_DISPLAY` | §6.1's existing rule and existing code path, reused not reimplemented |

---

## 4. Build order

| Step | What | Commit message |
|---|---|---|
| 0 | Phase 9 audit + any fix, test-first | `Phase 10 Step 0: fix the Phase 9 defects found auditing credit` |
| 1 | `CLAUDE.md` amendment (§2 above) | `Spec: the locker, the shortfall record and non-fuel income, before Phase 10` |
| 2 | Migration `0013_cash_engine.py` (`down_revision = "0012"`), `app/core/cash.py`, `app/models/cash.py`, `app/models/shortfall.py`, register in `app/models/__init__.py`, **all five `_CONSTRAINT_ERRORS` entries** | `Phase 10 Step 2: cash engine tables and models` |
| 3 | D2's `price_only` split in `services/readings.py` + `services/sales.py`, with its tests | `Phase 10 Step 3: separate valuation from profit so a missing margin cannot block a day` |
| 4 | `app/api/v1/non_fuel_sales.py` + service half in `services/cash.py` | `Phase 10 Step 4: non-fuel sales -- the missing term in §6.4` |
| 5 | `app/api/v1/bank_deposits.py`; `link()` / `may_read()` extension (M15) | `Phase 10 Step 5: bank deposits, receipt-linked` |
| 6 | `services/cash.py::shift_cash_position` + `GET /shifts/{id}/cash-position` | `Phase 10 Step 6: §6.4's per-shift cash position` |
| 7 | `app/services/shortfalls.py`, `app/api/v1/shortfalls.py` — book, reverse, settle, outstanding, ledger | `Phase 10 Step 7: salesman shortfalls -- booked by a human, settled in cash` |
| 8 | `services/cash.py::expected_closing` + `opening_balance_for` (D1, D3), `app/api/v1/daily_summaries.py` — create, patch, finalise, unfinalise, list | `Phase 10 Step 8: the daily cash summary and §6.5's rolling balance` |
| 9 | §13.10 flag: reopening a shift under a finalised summary marks it for review | `Phase 10 Step 9: a reopened shift flags its day, it does not rewrite it` |
| 10 | Tests | distributed across every step, never a separate pass |
| 11 | `docs/phase-10-plan.md` + `docs/phase-10-notes.md` | `Phase 10 Step 11: plan and notes docs` |

### Files touched

**New:** `alembic/versions/0013_cash_engine.py`, `app/core/cash.py`, `app/models/cash.py`,
`app/models/shortfall.py`, `app/services/cash.py`, `app/services/shortfalls.py`,
`app/api/v1/non_fuel_sales.py`, `app/api/v1/bank_deposits.py`, `app/api/v1/shortfalls.py`,
`app/api/v1/daily_summaries.py`, ~10 test files.

**Modified:** `CLAUDE.md`, `app/models/__init__.py`, `app/api/v1/router.py`,
`app/core/errors.py`, `app/services/readings.py` (D2), `app/services/attachments.py`
(`link`, `may_read`), `app/services/credit.py` (audit item: fold in `cash_total`),
`app/api/v1/credit_repayments.py`, `app/api/v1/shifts.py` (Step 9's flag),
`tests/conftest.py`, `tests/test_migrations.py`, `tests/test_routes.py`,
`tests/test_errors.py`.

### The `conftest.py` teardown debt

`tests/conftest.py` is **1,575 lines** with no transaction rollback — every `make_X` fixture
commits through `engine.begin()` and deletes its own rows, because the ASGI app takes its own
connection from `SessionLocal` and cannot see uncommitted work. Five new child tables means
five new generations in the existing teardown blocks.

Getting this wrong does **not** fail the Phase 10 tests. It fails an unrelated later test with
a foreign-key violation — which is exactly how P7's audit found that `clean_shifts` had never
swept `collections`.

Add, in this order (audit rows first with `trg_audit_logs_append_only` disabled; then
reversals `WHERE reverses_id IS NOT NULL`; then the rows):

| Fixture | Add |
|---|---|
| `make_user` | `salesman_shortfalls` / `..._settlements` (they FK `user_profiles` **directly** via `salesman_id`, a level no previous phase had), `non_fuel_sales`, `bank_deposits`, `daily_cash_summaries` — the deposits pass **before** the existing `attachments` sweep |
| `make_shift` | all four shift-scoped tables, two-pass shape like the existing `expenses` block |
| `clean_shifts` | all four in the blanket sweep **and** in the `audit_logs WHERE table_name IN (...)` list |
| new | `make_bank_deposit`, `make_non_fuel_sale`, `make_shortfall`, `make_shortfall_settlement`, `make_daily_summary`, and `clean_cash` / `clean_shortfalls` pairs |

Any seed-lookup fixture must be **function-scoped**, like `fuel_type_ids` and
`expense_category_ids` — `test_migration_is_reversible` downgrades to base and back mid-suite
and a session-scoped cache would hand every later test a dead foreign key.

### Test files

| File | Covers |
|---|---|
| `tests/test_cash_position.py` | D4's per-shift arithmetic, every term non-zero, the surplus case |
| `tests/test_cash_engine.py` | §6.4's full equation including D3's shortfall term |
| `tests/test_rolling_balance.py` | D1's three branches; the seeded anchor; `PRIOR_DAY_NOT_RECONCILED` |
| `tests/test_daily_summaries_api.py` | create / patch / finalise / unfinalise, D8's preconditions |
| `tests/test_bank_deposits.py` | CRUD, receipt link, M4's server-set `business_date` |
| `tests/test_non_fuel_sales.py` | D6, including the card-paid oil case that proves the sales-side placement |
| `tests/test_shortfalls.py` | D4/D5 booking, outstanding through reversals, the ledger |
| `tests/test_shortfall_permissions.py` | §8's matrix + the four structural tests |
| `tests/test_cash_permissions.py` | §8's matrix for summaries and deposits |
| `tests/test_shift_reopen_flags_summary.py` | Step 9 / §13.10 |

**House idiom to match exactly** — module docstring naming the §-sections; `pytestmark =
pytest.mark.anyio`; a module-level `DAY = date(2026, M, D)` **distinct per file** so tests do
not collide on `(outlet_id, business_date, sequence)`; module-local `async def _helper(...)`
wrappers duplicated deliberately (there is no `tests/helpers.py` and there should not be
one); money as a **string** in JSON and asserted as a string, `Decimal(body["x"])` when
arithmetic is needed, never `float`; status code and `code` asserted on **separate lines**;
every rejection test also proving `SELECT count(*) == 0`; full-sentence test names; a
human-readable per-test `Idempotency-Key`.

**The four structural tests** every permissions file carries, pointed at all four new routers
and both new services: `test_no_ownership_check_is_reimplemented_here`,
`test_the_outlet_is_never_hardcoded_in_this_router`,
`test_no_float_appears_in_the_money_path`, and an `ast`-parsed assertion that
`expected_closing` **subtracts** `shortfalls_booked` and **never reads a `cash` collection
row** — the §14 guardrail, pinned structurally.

### Patterns to copy verbatim, not re-derive

- Router skeleton, `# --- schemas / helpers / routes ---` banners, inline Pydantic models with
  `ConfigDict(extra="forbid")` on requests only — `app/api/v1/expenses.py`.
- Idempotency envelope (`_require_key`, `begin`/`store`/`discard`, `except: rollback +
  discard`) — `expenses.py:337-456`.
- `_to_response` / `_audit_snapshot` / `_load_<row>` (404 then 409-not-in-shift).
- Reversal service shape with `reversal_of()` extracted so route and service cannot drift —
  the Phase 7 Step 0 lesson.
- Outstanding-balance shape and its ledger — `app/services/credit.py` and
  `app/api/v1/credit_customers.py`, which are the direct precedent for D5.
- Keyset pagination: `limit + 1` sentinel, `tuple_(a, b) < tuple_(x, y)`,
  `encode_cursor` / `decode_cursor` from `app/api/cursor.py`, unchanged.
- Model conventions: no mixins, no `relationship()`, `sa.Text()` never `String(n)`,
  `sa.Numeric(12,2)` money / `sa.Numeric(10,3)` quantity, literal-label
  `postgresql.ENUM(..., create_type=False)`.
- Audit: `audit.record()` staged in the same transaction as the change it describes; the
  caller commits.

---

## 5. Error codes introduced

| Code | Status | Meaning |
|---|---|---|
| `PRIOR_DAY_NOT_RECONCILED` | 409 | §6.5, D1's narrowed meaning |
| `OPENING_BALANCE_REQUIRES_ADMIN` | 403 | D1's anchor, mirroring `ANCHOR_REQUIRES_ADMIN` |
| `OPENING_BALANCE_IS_CHAINED` | 409 | An opening balance supplied when a prior day exists |
| `DAY_HAS_OPEN_SHIFTS` | 409 | D8 — cannot summarise a day still being traded |
| `DAY_NOT_LOCKED` | 409 | D8 — cannot finalise until every shift is locked |
| `SUMMARY_NOT_FOUND` | 404 | |
| `SUMMARY_ALREADY_EXISTS` | 409 | Also `uq_daily_cash_summaries_outlet_date`'s mapping |
| `SUMMARY_FINALISED` | 409 | Editing a finalised day |
| `SUMMARY_NOT_FINALISED` | 409 | Unfinalising one that is not |
| `SHORTFALL_NOT_FOUND` / `_NOT_IN_SHIFT` / `_ALREADY_REVERSED` | 404 / 409 / 409 | |
| `SETTLEMENT_NOT_FOUND` / `_NOT_IN_SHIFT` / `_ALREADY_REVERSED` | 404 / 409 / 409 | |
| `DEPOSIT_NOT_FOUND` / `_NOT_IN_SHIFT` / `_ALREADY_REVERSED` | 404 / 409 / 409 | |
| `NON_FUEL_SALE_NOT_FOUND` / `_NOT_IN_SHIFT` / `_ALREADY_REVERSED` | 404 / 409 / 409 | |
| `CASH_POSITION_UNAVAILABLE` | 409 | A shift whose readings cannot yet yield a quantity |

**`_CONSTRAINT_ERRORS` additions (all in the same commit as `0013`):**
`uq_daily_cash_summaries_outlet_date`, `uq_bank_deposits_reverses_id`,
`uq_non_fuel_sales_reverses_id`, `uq_salesman_shortfalls_reverses_id`,
`uq_salesman_shortfall_settlements_reverses_id`.

**Reused unchanged:** `BUSINESS_DATE_IN_FUTURE`, `ALREADY_REVERSED`,
`CANNOT_REVERSE_A_REVERSAL`, `CANNOT_EDIT_A_REVERSAL`,
`LOCKED_SHIFT_REVERSAL_REQUIRES_ADMIN`, `NO_PRICE_FOR_DATE`, `ATTACHMENT_NOT_FOUND`,
`ATTACHMENT_ALREADY_LINKED`, `NOT_YOUR_ATTACHMENT`, `SHIFT_NOT_FOUND`, `SHIFT_NOT_OPEN`,
`SHIFT_LOCKED`, `NOT_YOUR_SHIFT`, `NOT_A_MEMBER`, `MEMBERSHIP_INACTIVE`,
`INSUFFICIENT_ROLE`, `IDEMPOTENCY_KEY_REQUIRED`, `IDEMPOTENCY_KEY_REUSED`,
`REQUEST_IN_PROGRESS`, `NO_FIELDS_TO_UPDATE`, `INVALID_CURSOR`, `VALIDATION_ERROR`.

---

## 6. Not in Phase 10

Bank balances, the IOCL virtual account, the PAD statement ledger and net-position reporting
(§12 — one post-V1 module, built together); tank dips and stock reconciliation; stock
revaluation in profit (§13.7); fuel purchase / tanker intake; an itemised non-fuel sales
module with products or stock (§13.2 — the manual field is the whole of V1); shortfall
write-offs and wage deductions (owner's D5 answer — see §9); interest or ageing on shortfall
balances; statements sent to staff or customers; any frontend (Phase 12); the 7-day rolling
view and variance alerts (Phase 13); retrofitting audit writes onto the admin reference-data
endpoints (§11's optional slot 11).

---

## 7. Verification checklist

### A — suite and migration health

- [x] `docker compose up -d db`, then `pytest` fully green; total ≥ 931 + the new files'
      count, and **no test deleted or weakened** to get there.
- [x] **Run the full suite, not `-k cash`.** The teardown debt fails *other* tests, and a
      filtered run is exactly what hides it.
- [x] **Run the suite twice back to back without recreating the database.** A leaked
      `daily_cash_summaries` row collides on `(outlet_id, business_date)` the second time.
- [x] `alembic upgrade head` → `0013`; `alembic check` reports no new operations.
- [x] `alembic downgrade base && alembic upgrade head` round-trips, and `0013`'s `downgrade()`
      drops **every enum it created** from `pg_type` — the `DROP TABLE`-does-not-drop-an-enum
      trap `0008` warned about, `0010` was caught by, and `0012` had to be checked for.
- [x] `tests/test_migrations.py::test_migration_is_reversible` still passes — it downgrades to
      base and back **mid-suite**, so any new session-scoped fixture breaks it.
- [x] 100% coverage on every new module, bar the `__main__` guard convention.
- [x] `tests/test_routes.py` passes — every new route under `/api/v1`.
- [x] `tests/test_migrations.py` gains name-level assertions for all five tables, every
      constraint and every index.
- [x] `pytest --cov=app` shows no regression on any pre-Phase-10 module.

### B — structural assertions no value test makes

- [x] `daily_cash_summaries` carries its own `outlet_id NOT NULL`; `bank_deposits`,
      `non_fuel_sales`, `salesman_shortfalls` and `salesman_shortfall_settlements` do
      **not** — asserted in both directions (§5.0).
- [x] `UNIQUE (outlet_id, business_date)` exists on `daily_cash_summaries`.
- [x] `variance` is a **generated** column in `information_schema.columns`
      (`is_generated = 'ALWAYS'`), not an application-computed one.
- [x] All five new `uq_*` constraints appear in `_CONSTRAINT_ERRORS`, i.e.
      `test_every_unique_constraint_the_api_pre_checks_is_mapped_to_a_business_error` and
      `test_every_reversal_unique_constraint_is_mapped_to_a_business_error` both pass
      **without being modified**.
- [x] All four reversal-carrying tables have the full quartet (`uq_*_reverses_id`,
      `ck_*_reversal_has_reason`, `ck_*_amount_sign` **strict**, `ck_*_reversal_not_self`).
- [x] `salesman_shortfalls.salesman_id` FKs `user_profiles`, **not** `credit_customers` —
      §14's guardrail, asserted against `information_schema` rather than inferred.
- [x] `grep -rn "float(\|sa.Float\|Float(" app/models/cash.py app/models/shortfall.py
      app/services/cash.py app/services/shortfalls.py app/api/v1/{daily_summaries,bank_deposits,shortfalls,non_fuel_sales}.py`
      → zero matches.
- [x] No `relationship()` in either new model module.
- [x] No `OFFSET`, no `@router.delete`, no hardcoded threshold literal anywhere new.
- [x] All money columns `Numeric(12,2)`; `sa.Text()` not `String(n)`.
- [x] The four structural permission tests exist and pass against all four new routers.
- [x] **The §14 guardrail is pinned by an `ast` test**: `expected_closing` subtracts
      `shortfalls_booked` and never reads a `mode = cash` collection row.
- [x] `grep -n "shortfall\|non_fuel\|bank_deposit\|daily_cash" tests/conftest.py` shows
      passes in `make_user`, `make_shift` **and** `clean_shifts` — not only the new `make_*`
      fixtures.
- [x] `app/api/v1/router.py` registers all four routers with a `# Phase 10 --` block comment,
      and the static-before-parameterised ordering is respected (M9).

### C — §6.4 and §6.5, the arithmetic (§10's *Cash* block, filled in)

- [x] **Full expected-cash calculation with every term non-zero** — §10 names this
      explicitly; it is the one test that proves the equation as a whole.
- [x] A **cash** udhaar repayment increases expected cash; a **UPI** repayment does not; a
      **bank_transfer** repayment does not.
- [x] A **cash** expense reduces expected cash; a `card` / `upi` / `bank_transfer` expense
      does not — §6.4's `mode = cash` clause.
- [x] `total_sales` is derived from readings and **cannot** be influenced by anything a
      client sent (§3 rules 7 and 8).
- [x] **Rolling balance uses the prior day's `actual_counted`, not `expected_closing`,
      whenever a count exists** — §6.5's own named test, and D1 does not weaken it.
- [x] With no prior count, the opening carries from `expected_closing` and
      `opening_balance_source = carried`.
- [x] A count that disagrees re-anchors: the next day opens at the **counted** figure and
      `opening_balance_source = counted`.
- [x] The very first day requires an admin-seeded opening (403 for a manager, 422 if absent);
      `opening_balance_source = seeded`.
- [x] Supplying an opening balance when a prior day exists → 409 `OPENING_BALANCE_IS_CHAINED`.
- [x] **Day N cannot finalise while day N−1 is unfinalised → 409 `PRIOR_DAY_NOT_RECONCILED`.**
- [x] `variance = actual_counted − expected_closing`, and is **NULL** when nothing was counted.
- [x] Variance is **recorded, never auto-corrected** — assert `expected_closing` is unchanged
      after a count lands.
- [x] A ₹200 shortage on Monday appears in Monday's variance and is **absent** from Tuesday's
      opening — §6.5's worked example, as a test.

### D — the shortfall (D3, D4, D5)

- [x] `GET /shifts/{id}/cash-position` returns every term and the gap, and **writes nothing**
      (assert `count(*) == 0` on `salesman_shortfalls` afterwards).
- [x] Booking a shortfall attributes it to `shifts.attendant_id`; a payload attempting to name
      a different salesman is rejected by `extra="forbid"`.
- [x] Booking an amount **different** from the computed gap succeeds and **logs a warning**
      (M11) — assert on the log record, not just the 201.
- [x] **A booked shortfall reduces `expected_closing` by exactly its amount** — D3's
      double-count test, and the single most important assertion in this phase.
- [x] An **unbooked** gap does not reduce `expected_closing`; it surfaces at the next count.
- [x] `outstanding(salesman)` correct after a partial settlement.
- [x] Correct after a **reversed shortfall** and after a **reversed settlement** — the
      negative rows net out (§6.6's convention, reused).
- [x] A settlement larger than outstanding is **accepted**; the balance goes negative.
- [x] A **cash settlement increases expected cash** on the shift it arrived in.
- [x] No denormalised outstanding total exists anywhere —
      `grep -rn "outstanding" app/models/` → zero matches (§14).
- [x] A shortfall is **not** reachable through `credit_sales`: a test asserts no
      `credit_customer` row is created and the §14 guardrail comment is still present
      (extend `tests/test_credit_permissions.py:148`'s existing structural test).
- [x] A **surplus** (declared > accountable) is reported as a negative gap and is **not**
      auto-booked.

### E — non-fuel sales (D6)

- [x] A **cash** oil sale: expected cash rises by the amount.
- [x] A **card-paid** oil sale: expected cash is **unchanged**, and the salesman shows **no
      phantom surplus** — the test that proves the sales-side placement, D6's table as code.
- [x] Removing the non-fuel row makes the salesman look short by exactly that amount —
      the failure it prevents, asserted directly.
- [x] A non-fuel sale is reversible under §6.9 and the reversal nets out of the day.

### F — valuation without margin (D2)

- [x] A day containing **petrol with a price but no margin** reconciles end to end — no
      `NO_MARGIN_FOR_DATE` anywhere in the cash path. **This is the blocker test**: without
      it, this outlet cannot use Phase 10 at all.
- [x] A **missing price** still refuses (409 `NO_PRICE_FOR_DATE`) — a day valued at zero is a
      plausible number and completely wrong.
- [x] `GET /shifts/{id}/sales` **still** 409s on a missing margin —
      `tests/test_sales_valuation.py:293` unmodified.
- [x] `price_only=True` leaves `margin_per_unit` and `profit` as `None`, never `0`.
- [x] Sales are valued at the **historical** rate; a later price revision does not move a
      finalised day's `expected_closing`.

### G — permissions (§8)

- [x] Attendant records a non-fuel sale on **their own** open shift → 201; on another
      attendant's shift → 403 `NOT_YOUR_SHIFT`.
- [x] Attendant records a **bank deposit** → 403 (§8: manager floor).
- [x] Attendant reads `/shifts/{id}/cash-position` → 403 (M8: it is a report).
- [x] Attendant books a shortfall → 403; records a settlement → 403; reads the ledger → 403.
- [x] Manager creates and patches a daily summary → 200; manager **finalises** → 403
      (§8: admin only).
- [x] Manager **unfinalises** → 403; admin unfinalises with a reason → 200, audit-logged.
- [x] Admin at **another outlet** → 403 `NOT_A_MEMBER`, pinned by a test so a change is
      deliberate (Phase 9's D8 posture, applied consistently).
- [x] Attendant reads the signed URL for a deposit slip on their own shift → 200; another
      attendant's → 403 `NOT_YOUR_ATTACHMENT` (M15's `may_read()` extension).

### H — idempotency, reversals, immutability

- [x] Same `Idempotency-Key` twice on each of the four money POSTs → **one** row,
      byte-identical response both times.
- [x] Same key, **different** body → 422 `IDEMPOTENCY_KEY_REUSED`.
- [x] Missing key → 400 `IDEMPOTENCY_KEY_REQUIRED` on all four.
- [x] A **refusal** releases the key — the corrected retry with the same key succeeds.
- [x] `POST /daily-summaries` takes **no** key and a retry gets 409 `SUMMARY_ALREADY_EXISTS`
      (M7) — asserted, so the omission reads as a decision.
- [x] Reversal on each of the four tables: negative amount, `reverses_id` set, non-blank
      reason required by **both** Pydantic and the DB CHECK (assert the CHECK with raw SQL —
      the `0007` lesson).
- [x] Double reversal → 409 `ALREADY_REVERSED`; reversing a reversal → 409
      `CANNOT_REVERSE_A_REVERSAL`; `PATCH` on a reversal → 409 `CANNOT_EDIT_A_REVERSAL`.
- [x] Any write to a **locked** shift → 409 `SHIFT_LOCKED`; a reversal on a locked shift is
      admin-only.
- [x] A **finalised** summary refuses every `PATCH` → 409 `SUMMARY_FINALISED`.
- [x] `Decimal` end to end — a `test_decimal_roundtrip`-style assertion for all five tables.
- [x] `audit_logs` carries a row for every book, settle, reverse, finalise and unfinalise,
      with money **stringified**, not floated.

### I — the day's preconditions and §13.10 (D8, Step 9)

- [x] Creating a summary with an **open** shift on that date → 409 `DAY_HAS_OPEN_SHIFTS`.
- [x] Finalising with a merely **closed** shift → 409 `DAY_NOT_LOCKED`.
- [x] Finalising with every shift locked → 200, `finalised_by` / `finalised_at` set.
- [x] Reopening a shift beneath a finalised summary **flags** it (`requires_review = true`
      with a note naming the shift) and **does not recompute** `expected_closing` — §13.10's
      rule, asserted on the stored figure being byte-identical afterwards.
- [x] A `business_date` in the future → 422 `BUSINESS_DATE_IN_FUTURE`, evaluated in
      `TZ_DISPLAY` and not UTC (§6.1) — test it at 23:00 IST, which is the previous day in UTC.

### J — the checks no test replaces

- [ ] **One real day, end to end, against the paper register.** Open the shift, enter the real
      readings, the real UPI and card figures, the real udhaar, the real expenses, the real
      cash declaration. Read the cash position. Compare the derived cash figure to what the
      salesman actually handed over.
- [ ] **Confirm the shortfall figure with the owner before anything is booked against a real
      person's name.** This is the number with a human consequence, and §4.7's whole argument
      is that it must be a question before it is a debt.
- [ ] **Count the locker once, physically, and enter it.** Confirm the variance is a figure
      the owner recognises and can explain, and that the next day opens at the counted figure.
- [ ] **Run three consecutive days** — one uncounted, one counted, one uncounted — and confirm
      the opening balances chain the way D1 says they do.
- [ ] Confirm the owner agrees that `expected_closing` **should** drop when a shortfall is
      booked. If that reads wrong to them, D3 is wrong and the whole equation needs revisiting
      before real money touches it.

---

## 8. What actually shipped

**1,135 tests, up from 931.** 204 new across 7 files, plus the Step 0 fixes and five existing
files extended. **100% coverage on all ten Phase 10 modules.** `alembic` at `0013`, `check`
clean, `downgrade base` / `upgrade head` round-trips with zero leaked enums or tables, suite
green twice back to back against the same database.

| Step | Commit |
|---|---|
| 0 | `Phase 10 Step 0: fix the three Phase 9 defects found auditing credit` |
| 1 | `Spec: the locker, the shortfall record and non-fuel income, before Phase 10` |
| 2 | `Phase 10 Step 2: cash engine tables and models` |
| 3 | `Phase 10 Step 3: separate valuation from profit so a missing margin cannot block a day` |
| 4 | `Phase 10 Step 4: non-fuel sales -- the missing term in §6.4` |
| 5 (+7's `link()`) | `Phase 10 Step 5: bank deposits, receipt-linked` |
| 6 | `Phase 10 Step 6: §6.4's per-shift cash position` |
| 7 | `Phase 10 Step 7: salesman shortfalls -- booked by a human, settled in cash` |
| 8 | `Phase 10 Step 8: the daily cash summary and §6.5's rolling balance` |
| 9 | `Phase 10 Step 9: a reopened shift flags its day, it does not rewrite it` |
| 10 | `Phase 10 Step 10: structural permission tests for the cash engine` |
| 11 | this file and `docs/phase-10-notes.md` |

**Where it differs from the plan above.**

* **Step 0 found three defects, and the third was the dangerous one.** The plan expected a
  Phase 9 audit. Two findings were as anticipated (`may_read`'s joins never exercised as
  `True`; `cash_total` computed in a router). The third was not: `test_uploads_api.py` called
  `engine.connect()` twice without a context manager, leaving a connection **idle in
  transaction** holding a lock on `attachments`, so `test_migration_is_reversible`'s
  `DROP TABLE` blocked forever. Latent until two added tests shifted the ordering — and Phase
  10 adds five tables to that same mid-suite downgrade path, so it would have surfaced within
  days and looked like `0013`'s fault.
* **`cash_total` was not merely misplaced, it was wrong.** The plan said "move it into the
  service". It was summing over `rows[:_MAX_ROWS]`, so a shift with more than 100 repayments
  under-reported a term of §6.4. `total` had the same bug. Both fixed test-first.
* **`app/core/cash.py` did not land in Step 2 as planned.** `OpeningBalanceSource` had no
  importer until Step 8 — models use literal enum labels deliberately, to avoid phantom
  autogenerate drift from Python declaration order — and this repo deletes an uncovered
  helper rather than carrying it (§11). It landed with its first caller. The same call was
  made for a `_is_reversed` helper in Step 4.
* **§6.4's reversal shape is written once, not four more times.** The plan did not specify
  this. Phases 6–9 wrote `reverse` four times; Phase 10 adds four tables, and eight
  near-identical copies of the rule deciding whether a correction is honest is what `0012`
  paid for in miniature. `cash.append_reversal` takes an explicit `carry` list rather than
  copying every column, because the exceptions are the point.
* **The plan put the cash-position route in Step 6 without saying where.** It became its own
  module, `app/api/v1/cash_position.py`, rather than living inside `non_fuel_sales.py` or
  `shifts.py` — it is a report about a shift, not a child resource of one.
* **§13.16's "flag a finalised day" case turns out to be unreachable**, and the plan assumed
  it was not. Finalising requires every shift locked, and §6.8 makes `locked` terminal, so a
  shift beneath a frozen day cannot reach the reopen route at all. Two independent rules
  meeting; neither section mentions the other. Now pinned by a test that fails if either
  half changes.
* **Two of my own arithmetic errors were caught by the tests I wrote to check the code.**
  The full-equation docstring said ₹25,900 where the terms give ₹24,900, and a structural
  test tripped on a comment saying "No `salesman_id`". Both recorded in the notes.
* **`cash_expenses_total` landed in `services/expenses.py`**, not in `cash.py` — beside the
  rows it sums and next to the enum deciding which count, the placement `cash_repayments_
  total` already follows in `credit.py`.

---

---

## 9. Still owed by the owner

New, and load-bearing:

- **There is no way to write off a shortfall.** The D5 answer was cash repayment only, so a
  ₹20 gap nobody will ever chase stays on a salesman's outstanding balance forever, and that
  balance only ever grows. Recommend revisiting before the first quarter closes; it is a
  `mode` column and a filter, cheap while the table is small.
- **Petrol and diesel dealer margins are still not entered.** D2 means a missing margin no
  longer blocks reconciliation, so Phase 10 works without them — but **profit reporting still
  covers CBG only**, and Phase 13 is built on it.
- **The first opening balance must be seeded before anything can be finalised.** Someone has
  to count the locker once, on a known date, and an admin has to enter that figure.
- **Does a salesman hold a change float overnight, and is it counted separately?** §14's
  question, still open. If the float is inside the locker figure it is fine; if it is in his
  pocket, every count is short by it.
- **Is a surplus ever booked?** D4 reports one and refuses to book it. If a salesman
  consistently declares more than the meters imply, that is a signal too, and it currently has
  nowhere to go.

Carried forward:

- Phase 9's `credit_customers` tenancy posture (403 `NOT_A_MEMBER`, not §7.3's 404).
- The real credit customer list and their limits; whether a credit limit is used here at all.
- `EXPENSE_RECEIPT_THRESHOLD` = ₹5,000 is still a guess, live on real money.
- The real expense category list beyond the four seeded.
- CBG's real max flow rate in kg/min; whether testing quantities are recorded on paper, and in
  what unit.
