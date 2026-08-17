# Fuel Station — Daily Stock & Cash Flow Manager (V1)

> **Read this file completely before writing any code.** It encodes domain rules that
> are not obvious from the code and that are expensive to retrofit. Several rules here
> exist because a naive implementation produces *silently wrong money figures* rather
> than a crash. Wrong-but-plausible numbers are the primary failure mode of this project.

---

## 1. Project Overview

A daily stock and cash-flow management system for a single petrol pump (retail outlet)
in India. Attendants record shift readings, collections, credit sales, and expenses.
Managers close shifts and reconcile cash. Admins review flagged items and lock days.

**This is a cash-accounting system.** Correctness beats features. Auditability beats
convenience. If a rule is ambiguous, stop and ask — do not guess.

**Secondary goal:** the owner is learning backend development. Prefer clear, boring,
explicit code over clever abstractions. Explain non-obvious decisions in comments.

---

## 2. Stack

| Layer | Choice |
|---|---|
| Language | Python 3.12+ |
| Web framework | FastAPI |
| Validation | Pydantic v2 |
| ORM | SQLAlchemy 2.x (declarative, typed) |
| Migrations | Alembic |
| Database | PostgreSQL (Supabase free tier) |
| Object storage | Supabase Storage (bucket: `receipts`) |
| Auth | Supabase Auth (JWT verified server-side) |
| Frontend | Static HTML/CSS/vanilla JS, served by FastAPI `StaticFiles` |
| Tests | pytest + httpx `AsyncClient` |

**No frontend framework in V1.** No React/Vue/build step. Plain `fetch()` calls.
Do not introduce npm, Vite, Tailwind, or a bundler without being asked.

### Note on "decoupled" vs `StaticFiles`

FastAPI serving the HTML files is a *deployment convenience*, not a coupling.
The rules that make this decoupled are:

- The backend **never** renders HTML templates. It returns JSON only.
- No endpoint knows anything about tables, forms, or pages.
- Every rule is enforced server-side; the frontend is treated as untrusted.
- A mobile app must be able to do everything the web page can, hitting identical endpoints.

If you ever find yourself building HTML strings in Python, you have broken the architecture.

---

## 3. Non-Negotiable Rules

These are not preferences. Violating any of them is a bug, even if tests pass.

1. **Money is `NUMERIC(12,2)`. Never `FLOAT`, never `REAL`, never Python `float`.**
   Use `Decimal` in Python end-to-end. Configure SQLAlchemy to return `Decimal`.
   Pydantic fields for money use `condecimal(max_digits=12, decimal_places=2)`.
   `0.1 + 0.2 != 0.3` in binary floating point; in a cash system this compounds into
   unexplainable variance.

2. **Quantities are `NUMERIC(10,3)`** — litres for liquid fuel, kilograms for gas
   (millilitre / gram precision). Totalizer readings are `NUMERIC(12,2)`.
   **The unit is an attribute of the fuel type (`fuel_types.unit_of_measure`), never
   assumed.** See §4.5 — this outlet sells CBG by the kilogram.

3. **All endpoints live under `/api/v1/`.** No exceptions, not even health checks
   that "obviously won't change".

4. **Every timestamp is stored in UTC** (`TIMESTAMPTZ`). Display conversion to
   `Asia/Kolkata` happens in the frontend only.

5. **`business_date` is an explicit `DATE` column, never derived from a timestamp.**
   See §6.1 — the night shift crosses midnight and this rule exists because of it.

6. **No hard deletes on any financial table.** Corrections happen via reversal
   entries that reference the original row. See §6.9.

7. **Every write endpoint validates input via a Pydantic model.** Never accept a raw
   `dict`. Never trust a client-supplied `total`, `amount_due`, or any derived figure —
   recompute it server-side.

8. **Never store a computed sales figure that the client sent.** Sales are derived
   from nozzle readings and historical prices, server-side, always.

9. **Migrations only via Alembic.** Never `Base.metadata.create_all()` outside of
   test fixtures. Never edit the DB schema by hand.

10. **Errors use a consistent envelope:**
    `{"detail": "human message", "code": "MACHINE_READABLE_CODE"}`.
    Validation failures return HTTP 422 with field-level detail (FastAPI default shape
    is acceptable for 422; add `code` for all other errors).

---

## 4. Domain Background (verified — read this, it drives the schema)

These facts were confirmed against Indian oil-company Marketing Discipline Guidelines
and current pricing mechanics. They are the reason the schema looks the way it does.

### 4.1 Fuel prices revise daily at 06:00 IST

India has used dynamic daily pricing since June 2017. OMCs (IOCL/BPCL/HPCL) publish
revised rates every morning at 06:00 IST. In practice prices often stay flat for weeks
and then move — so this is a low-churn table, but it *must* be effective-dated.

**Consequence:** a price is not an attribute of a fuel. It is a dated record.
Storing "current price" as a mutable column silently corrupts every historical report
the moment the price changes.

**Consequence:** 06:00 can fall *inside* a morning shift. Price lookup is
per-transaction-time, not per-shift. See §6.3.

### 4.2 Calibration testing is mandatory and moves the totalizer

Dealers are required to keep a Weights & Measures–verified 5-litre standard measure
for exclusive pump-testing use. Delivery accuracy is checked against a tolerance band
expressed per 5 litres. This testing fuel is dispensed (totalizer increments) and
poured back into the tank. **It was never sold.**

**Consequence:** `quantity_sold = closing − opening − testing_quantity`.
Omitting `testing_quantity` produces a small, permanent, daily cash shortfall that is
extremely hard to diagnose. This is the single most common bug in home-grown pump
software.

**This is a liquid-fuel rule.** The 5-litre standard measure does not apply to gas.
CBG is not calibration-tested at this outlet, so `testing_quantity` is always 0 for it
(§4.5). The column and the `TESTING_EXCEEDS_THROUGHPUT` guard remain and still apply in
full to petrol and diesel — nothing is removed.

### 4.3 Totalizers are per-nozzle, mechanical, and imperfect

- A **dispensing unit** has multiple **nozzles**. Each nozzle has its own totalizer
  and is wired to exactly one fuel type. Two nozzles can dispense the same fuel.
- Totalizers **roll over** like an odometer at their maximum value.
- Totalizers **reset to zero** when a meter is repaired or replaced (and are re-sealed
  by W&M).

**Consequence:** a closing reading lower than the opening reading is a legitimate
real-world event. Naive subtraction yields negative litres and negative sales.
See §6.2.

### 4.4 Not all cash comes from fuel

Cash arrives from: fuel sales, non-fuel sales (lubricants, coolant), and **udhaar
repayments** — a credit customer settling an old bill. Repayment cash has no
corresponding sale on the day it arrives.

**Consequence:** if repayments are not modelled, expected cash is wrong every time
someone settles up.

### 4.5 CBG (compressed bio gas) is sold here, and it is not measured in litres

This outlet sells CBG alongside petrol and diesel. It is dispensed through a nozzle with
a totalizer exactly like liquid fuel, but:

- It is **priced and metered per kilogram**, not per litre. The rate is ₹/kg and the
  totalizer counts kilograms.
- **No stock is held.** A cascade truck stands on site and is swapped out when the gas is
  nearly finished. There is no tank, so tank-dip reconciliation is not merely out of
  scope (§12) — for CBG it is meaningless.
- **It is not calibration-tested** at this outlet, so `testing_quantity` is always 0.
- The **dealer margin is a fixed ₹/kg** (currently ₹2.28). IOCL deducts
  `(retail_rate − margin) × kg` from a running ledger balance. See §4.6.
- IOCL bills fortnightly and **splits the invoice at each price revision** — one invoice
  per price period. That is real-world confirmation that prices must be effective-dated.

**Consequence:** a quantity in this system is a *measure*, not necessarily a *volume*.
Any code, column name, comment or variable that assumes litres is wrong. The unit lives on
`fuel_types.unit_of_measure` and must be read, never inferred.

**Consequence:** the §6.2 sanity ceiling cannot be one global litres-per-minute figure.
A CBG dispenser does single-digit kg/min while a petrol nozzle does ~60 L/min; one shared
number would either never fire or reject every real sale. The ceiling is per fuel type.

### 4.6 Dealer margin is constant; price changes pass straight through

The gap between what the dealer pays and what the dealer charges does **not** move when
the retail price moves. If retail rises ₹1/litre, the next tanker invoice rises ₹1/litre
too. For CBG the same holds by construction: IOCL deducts retail minus a fixed ₹2.28/kg.
The margin changes only when the OMC revises the commission itself, which is rare.

**Consequence:** margin is stored **directly and effective-dated** (`fuel_margins`), not
derived from purchase invoices. This is the entire reason V1 needs no purchase, tanker or
stock data in order to report fuel profit:

```
dealer_profit = quantity_sold × margin_at(fuel_type, at)
```

**Consequence:** this figure is *gross margin on quantity sold*. It deliberately excludes
stock revaluation — holding 12 kL when the price rises ₹1 is a real ₹12,000 gain that this
system will never show. See §13.7; that limitation must be stated wherever profit is
displayed, or the number is plausible and wrong.

---

## 5. Database Schema

Table names are `snake_case` plural. Every table has `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`,
`created_at TIMESTAMPTZ NOT NULL DEFAULT now()`, and `created_by UUID REFERENCES user_profiles(id)`
unless stated otherwise.

### 5.0 Multi-tenancy contract

V1 serves a single outlet. The **schema** is nonetheless multi-outlet ready, because
the columns and unique constraints below have no correct retrofit once real money data
exists. There is **no outlet-switching UI and no RLS in V1** — see §13.

**`outlets`** (created in Phase 1, migration `0001`)
- `id` — UUID PK
- `name` — text
- `is_active` — boolean, default true
- No `created_by`. The first outlet is seeded by the system before any user exists,
  and `user_profiles` does not exist until Phase 2. This is a deliberate, commented
  exception to the rule stated above.

Exactly one row is seeded, at the fixed id `00000000-0000-0000-0000-000000000001`
(config: `DEFAULT_OUTLET_ID`). Fixed rather than random so dev, test and production
agree and fixtures stay deterministic.

**The rule that decides which tables carry `outlet_id`:**

> A tenancy column must exist from birth if it is **not derivable**. If it **is**
> derivable from a parent row, it can be added later with a one-line `UPDATE … FROM`
> and should wait.

`shifts.outlet_id` is not derivable — nothing else in a shift row says which pump it
belonged to, so there is no correct backfill, only a guess. `collections.outlet_id`
*is* derivable via `collections.shift_id → shifts.outlet_id`, so `collections` does
not carry its own column. This also avoids denormalising a value that could drift,
consistent with §6.6.

Per-phase landing schedule:

| Phase | Table | Own `outlet_id`? |
|---|---|---|
| 2 | `user_profiles` | No — scoped via `outlet_memberships` |
| 2 | `outlet_memberships` | **Yes** |
| 3 | `fuel_types` | No — global reference data |
| 3 | `fuel_prices`, `fuel_margins`, `nozzles` | **Yes** |
| 4 | `shifts` | **Yes** |
| 5–7 | `nozzle_readings`, `collections`, `expenses` | No — derivable via `shift_id` |
| 8 | `attachments` | **Yes** — no parent shift |
| 9 | `credit_customers` | **Yes** |
| 9 | `credit_sales`, `credit_repayments` | No — derivable via `shift_id` |
| 10 | `bank_deposits` | No — derivable via `shift_id` |
| 10 | `daily_cash_summaries` | **Yes** |
| 11 | `audit_logs` | **Yes** — no parent |

**Unique constraints are the expensive part**, not the columns. Each one below is
cheap now and horrible to change once there is data. They are written into §5.1–§5.3
in their outlet-scoped form.

*If you want direct `outlet_id` on the derivable tables later* (RLS policies are
simpler without a join), add it with a **composite** foreign key —
`collections(shift_id, outlet_id) REFERENCES shifts(id, outlet_id)` — which
denormalises the value while making drift structurally impossible. Not built in V1.

### 5.1 Reference tables (slow-changing)

**`user_profiles`**
Supabase Auth owns `auth.users`. This table holds application concerns.
- `id` — UUID, matches `auth.users.id`
- `full_name` — text
- `is_active` — boolean, default true
- `phone` — text, nullable
- **No `role` and no `outlet_id`** — a user's role is per-outlet, held in
  `outlet_memberships`. See §5.0.

**`outlet_memberships`** — which users may act at which outlet, and in what capacity
- `user_id` — FK to user_profiles
- `outlet_id` — FK to outlets
- `role` — enum: `admin` | `manager` | `attendant`
- `is_active` — boolean, default true
- Unique constraint on `(user_id, outlet_id)`

> Role lives here rather than on `user_profiles` because someone can legitimately be
> a manager at one outlet and an attendant at another. Putting `outlet_id` directly
> on `user_profiles` would hardcode one-user-one-outlet — the same retrofit this
> section exists to avoid, merely moved to a different table.

**`fuel_types`** — global reference data, **admin-managed**
- `code` — text unique (`PETROL`, `DIESEL`, `PREMIUM_PETROL`, `CBG`)
- `display_name` — text
- `unit_of_measure` — enum: `litre` | `kilogram` (§4.5)
- `max_flow_rate_per_minute` — NUMERIC(10,3), the §6.2 sanity ceiling **for this fuel**
- `is_active` — boolean

> Admins add fuel types through the API, not through a migration. Outlets sell products
> this list does not anticipate (XP-95, Extra Green, and whatever comes next); adding a
> product you sell is data entry, and requiring a schema change for it would be wrong.
>
> **`code` and `unit_of_measure` are immutable once created.** Changing a unit would
> retroactively reinterpret every quantity ever recorded against that fuel — litres read
> as kilograms, and every historical sale value silently wrong. A fuel that is genuinely
> different is a new row. Deactivate rather than delete (§3 rule 6); `display_name`,
> `max_flow_rate_per_minute` and `is_active` remain editable.

**`fuel_prices`** — **append-only, never updated, never deleted**
- `outlet_id` — FK to outlets, NOT NULL
- `fuel_type_id` — FK
- `rate_per_unit` — NUMERIC(12,2)
- `effective_from` — TIMESTAMPTZ (the moment this rate became live)
- `entered_by` — FK to user_profiles
- Unique constraint on `(outlet_id, fuel_type_id, effective_from)`
- **Lookup pattern:** the applicable rate for a fuel at an outlet at time T is the row
  with the greatest `effective_from <= T`. Implement this as one shared helper
  function taking `(outlet_id, fuel_type_id, at)` (DRY — this logic appears in sales,
  reporting, and reconciliation).

> Prices are outlet-scoped, not national: dealers are supplied by different OMCs
> (IOCL / BPCL / HPCL) and rates vary by state and district. A global price table is
> wrong the moment a second outlet exists.

**`fuel_margins`** — **append-only, never updated, never deleted**
- `outlet_id` — FK to outlets, NOT NULL
- `fuel_type_id` — FK
- `margin_per_unit` — NUMERIC(12,2) — ₹ per litre or per kg, per §4.6
- `effective_from` — TIMESTAMPTZ (the moment this margin became live)
- `entered_by` — FK to user_profiles
- Unique constraint on `(outlet_id, fuel_type_id, effective_from)`
- **Lookup pattern:** identical to `fuel_prices` — greatest `effective_from <= T`. Shares
  the same helper shape, `margin_at(outlet_id, fuel_type_id, at)`.

> **Why a separate table rather than a column on `fuel_prices`:** the two revise on
> different schedules. Price moves often (§4.1); margin almost never (§4.6). Sharing a row
> would force re-entry of an unchanged margin on every single price revision — and the
> first time someone forgets, that period's profit is null or wrong with no error raised.
> Independent revision cadences need independent effective-dating.

**`nozzles`**
Dispensers are deliberately *not* a separate table in V1 (YAGNI — a label suffices).
- `outlet_id` — FK to outlets, NOT NULL
- `label` — text, e.g. `DU-1/N-2`; unique per outlet — `(outlet_id, label)`
- `dispenser_label` — text, e.g. `DU-1` (for grouping in reports)
- `fuel_type_id` — FK
- `totalizer_max_value` — NUMERIC(12,2), the rollover ceiling for this meter
  (e.g. `999999.99`). Required to compute rollover correctly — see §6.2.
- `meter_installed_at` — TIMESTAMPTZ
- `is_active` — boolean

**`credit_customers`**
- `outlet_id` — FK to outlets, NOT NULL
- `name` — text
- `phone` — text
- `vehicle_numbers` — text[] (nullable)
- `credit_limit` — NUMERIC(12,2), nullable (null = no limit)
- `is_active` — boolean

### 5.2 Transactional tables

**`shifts`** — the spine. Everything hangs off this.
- `outlet_id` — FK to outlets, NOT NULL
- `business_date` — DATE, **explicit, not derived**
- `shift_type` — enum: `morning` | `night`
- `started_at`, `ended_at` — TIMESTAMPTZ nullable
- `attendant_id` — FK to user_profiles
- `status` — enum: `open` | `closed` | `locked`
- `closed_by`, `closed_at` — nullable
- `locked_by`, `locked_at` — nullable
- Unique constraint on `(outlet_id, business_date, shift_type)`

Status transitions: `open → closed → locked`. Never backwards without an admin action
that is itself audit-logged. Nothing referencing a `locked` shift may be modified.

**`nozzle_readings`** — one row per nozzle per shift. The source of truth for sales.
- `shift_id` — FK
- `nozzle_id` — FK
- `opening_reading` — NUMERIC(12,2)
- `closing_reading` — NUMERIC(12,2), nullable until shift close
- `testing_quantity` — NUMERIC(10,3), NOT NULL DEFAULT 0
- `rollover_occurred` — boolean, default false
- `meter_reset_occurred` — boolean, default false
- `manual_quantity_override` — NUMERIC(10,3) nullable (admin-only, see §6.2)
- `override_reason` — text, nullable, required if override is set
- Unique constraint on `(shift_id, nozzle_id)`

**`collections`** — money received during a shift, one row per payment mode used.
- `shift_id` — FK
- `mode` — enum: `cash` | `card` | `upi` | `wallet`
- `amount` — NUMERIC(12,2)
- `reference` — text nullable (settlement/batch reference)

**`credit_sales`** (udhaar issued)
- `shift_id` — FK
- `credit_customer_id` — FK
- `fuel_type_id` — FK, nullable (null = non-fuel credit sale)
- `quantity` — NUMERIC(10,3), nullable (litres or kg, per the fuel's unit — §4.5)
- `amount` — NUMERIC(12,2)
- `vehicle_number` — text nullable
- **`attachment_id` — FK to `attachments`, `NOT NULL`** ← the receipt constraint,
  enforced at the database level, not in application code and not in JavaScript
- `is_settled` — boolean, default false (derived convenience flag; the authoritative
  outstanding figure comes from repayments — see §6.6)

**`credit_repayments`**
- `credit_customer_id` — FK
- `shift_id` — FK (the shift during which the money physically arrived)
- `amount` — NUMERIC(12,2)
- `mode` — enum: `cash` | `card` | `upi` | `bank_transfer`
- `attachment_id` — FK nullable

**`expenses`** (renamed from `cash_flows` — the old name was ambiguous, since
collections and deposits are also cash flows)
- `shift_id` — FK
- `category` — enum: `salary` | `maintenance` | `electricity` | `fuel_purchase` |
  `misc` | `other` (extend as needed; keep it an enum, not free text)
- `amount` — NUMERIC(12,2), CHECK > 0
- `description` — text, max 500 chars
- `paid_to` — text nullable
- `attachment_id` — FK nullable
- `requires_review` — boolean, default false
- `reviewed_by`, `reviewed_at` — nullable
- `review_note` — text nullable

**`bank_deposits`**
- `shift_id` — FK
- `business_date` — DATE
- `amount` — NUMERIC(12,2)
- `bank_reference` — text nullable
- `attachment_id` — FK nullable (deposit slip)

**`daily_cash_summaries`** — one row per outlet per `business_date`
- `outlet_id` — FK to outlets, NOT NULL
- `business_date` — DATE; unique per outlet — `(outlet_id, business_date)`
- `opening_balance` — NUMERIC(12,2)
- `expected_closing` — NUMERIC(12,2) — **the figure the system computed and showed**
- `actual_counted` — NUMERIC(12,2), nullable
- `variance` — NUMERIC(12,2), generated as `actual_counted - expected_closing`
- `is_finalised` — boolean
- `notes` — text nullable

> **Why store `expected_closing` rather than always recomputing?**
> If a calculation bug is fixed six months from now, you still need to know what the
> system told the manager *on that day*. A recomputed-on-read figure destroys that record.
> This is deliberate, not redundant storage.

### 5.3 Supporting tables

**`attachments`** — one row per uploaded file. All other tables reference *this*,
never a raw URL string. Single authoritative representation of file knowledge (DRY).
- `outlet_id` — FK to outlets, NOT NULL
- `bucket` — text (e.g. `receipts`)
- `storage_path` — text unique (see §7 for the naming rule)
- `original_filename` — text (display label only)
- `mime_type` — text
- `size_bytes` — integer
- `checksum_sha256` — text
- `uploaded_by` — FK
- `linked_at` — TIMESTAMPTZ nullable (set when referenced by a business row;
  unlinked rows older than 24h are garbage — see §7.4)

**`audit_logs`** — append-only. No updates. No deletes. Ever.
- `outlet_id` — FK to outlets, NOT NULL
- `table_name` — text
- `record_id` — UUID
- `action` — enum: `insert` | `update` | `reversal` | `status_change`
- `changed_by` — FK
- `changed_at` — TIMESTAMPTZ
- `old_values` — JSONB nullable
- `new_values` — JSONB nullable
- `request_id` — text (correlate with application logs)

> `created_by` / `updated_at` columns are **change tracking**, not an audit trail.
> They tell you who last touched a row, not what it was before or how many times it
> changed. For a cash system, `audit_logs` is required in addition to those columns.

### 5.4 Relationship summary in plain English

- A **shift** has many nozzle readings, collections, credit sales, expenses, deposits.
- A **nozzle reading** belongs to exactly one shift and one nozzle.
- A **credit sale** belongs to one shift, one customer, and **must** have one attachment.
- A **credit repayment** belongs to one customer and the shift in which cash arrived.
- An **expense** belongs to one shift and may have one attachment.
- A **daily cash summary** aggregates one business date across both shifts.
- Everything financial points to the user who created it and appears in `audit_logs`.

---

## 6. Business Logic

### 6.1 Business date vs timestamps

A night shift runs (say) 20:00 to 08:00. It belongs to **one** `business_date` but its
timestamps span two calendar dates. Never do `date(created_at)` to determine the business
day. Always read `shifts.business_date`.

The business date is chosen when the shift is opened and is immutable thereafter.

### 6.2 Quantity sold, including the ugly cases

Normal case:

```
quantity_sold = closing_reading − opening_reading − testing_quantity
```

**Rollover case** (`closing < opening` and `rollover_occurred = true`):

```
quantity_sold = (nozzle.totalizer_max_value − opening_reading) + closing_reading − testing_quantity
```

**Meter reset case** (`meter_reset_occurred = true`): the reading pair is meaningless.
Require `manual_quantity_override` plus `override_reason`, settable by admin only, and
audit-log it. Do not attempt to infer the split automatically.

**Guards that must exist:**
- If `closing < opening` and neither `rollover_occurred` nor `meter_reset_occurred`
  is set → reject with 422, code `TOTALIZER_DECREASED`. Never compute a negative.
- If `testing_quantity > (closing − opening)` → reject, code `TESTING_EXCEEDS_THROUGHPUT`.
- Sanity ceiling: reject if the implied quantity exceeds
  `fuel_types.max_flow_rate_per_minute × shift duration in minutes`. Catches a mistyped
  extra digit, which is the most common data-entry error.
  **The ceiling is per fuel type, not one global constant** — a petrol nozzle does ~60
  L/min while a CBG dispenser does single-digit kg/min (§4.5). `MAX_FLOW_RATE_LPM` in
  §16 only *seeds* the column for litre fuels; do not read it here.
- `quantity_sold` must never be negative. If a code path can produce one, that path is wrong.

### 6.3 Valuing sales

```
sale_value = quantity_sold × rate_at(fuel_type, transaction_time)
```

The rate comes from `fuel_prices` via the shared lookup helper (§5.1), never from a
"current price" column.

Profit uses the same shape against `fuel_margins` (§4.6):

```
dealer_profit = quantity_sold × margin_at(fuel_type, transaction_time)
```

Both figures carry the same shift-start approximation described below, and both exclude
stock revaluation — see §13.7.

**Mid-shift price change:** because revisions happen at 06:00 IST, a morning shift
starting at, say, 05:30 spans a price change. V1 handling: **value the whole shift's
litres at the rate effective at the shift's `started_at`**, and log a warning if a
price change occurred during the shift window.

This is a deliberate, documented approximation — the pump has no per-transaction data
in V1, so exact apportionment is impossible. Write it in a comment where it happens.
Do not silently pretend it is exact. Revisit when per-transaction data exists.

### 6.4 Cash flow engine

```
cash_sales        = total_sales − card_collections − upi_collections
                                 − wallet_collections − credit_sales_amount

expected_closing  = opening_balance
                  + cash_sales
                  + cash_credit_repayments      ← settlements received in cash
                  + other_cash_income           ← non-fuel sales (V1: manual entry)
                  − cash_expenses
                  − bank_deposits

variance          = actual_counted − expected_closing
```

**Rules:**
- Variance is **recorded, never auto-corrected**. Do not "fix" the closing balance to
  make it match. The variance *is* the signal.
- Only `mode = cash` repayments enter this equation. UPI/bank repayments do not touch
  the drawer.
- `total_sales` is derived from nozzle readings, never entered.

### 6.5 Rolling balance

`opening_balance` for day N = `expected_closing`... **no.**

`opening_balance` for day N = **`actual_counted`** of day N−1.

This is important and easy to get wrong. The physical cash actually in the drawer is
what carries forward, not the theoretical figure. Otherwise a ₹200 shortage on Monday
silently disappears instead of being visible in Monday's variance and absent from
Tuesday's opening.

- If day N−1 has no `actual_counted`, day N cannot be finalised. Return 409 with
  code `PRIOR_DAY_NOT_RECONCILED`.
- The very first day requires a manually seeded opening balance (admin-only, one-time).

### 6.6 Udhaar (credit) rules

- **A credit sale cannot be created without a valid `attachment_id`.** Enforced by
  `NOT NULL` FK at the DB level *and* validated at the API level (the attachment must
  exist and have been uploaded by an authenticated user). Belt and braces: a client
  can bypass JavaScript, but not a database constraint.
- Outstanding balance for a customer = `SUM(credit_sales.amount) − SUM(credit_repayments.amount)`.
  Compute it; do not maintain a denormalised running total in V1 (it will drift).
- If `credit_limit` is set and a new sale would exceed outstanding + amount, reject with
  409, code `CREDIT_LIMIT_EXCEEDED`. Admin may override; the override is audit-logged
  with a mandatory reason.

### 6.7 Expense review flagging

- Threshold is a **config value** (`EXPENSE_REVIEW_THRESHOLD`, default `1000.00`),
  not a hardcoded literal. Changing it must not require a deploy.
- Flag when `amount > threshold` → `requires_review = true`.
- **Also flag** when the sum of a single category for one `business_date` exceeds
  the threshold. A single ₹1,000 rule is trivially defeated by two ₹600 entries;
  without this, the control is theatre.
- A shift **cannot be locked** while it has unreviewed flagged expenses.
  Return 409, code `UNREVIEWED_EXPENSES_EXIST`.

### 6.8 Shift closing preconditions

Reject shift close (409) if any of:
- Any active nozzle lacks a `closing_reading` → `MISSING_NOZZLE_READINGS`
- Any credit sale lacks a confirmed attachment → `CREDIT_SALE_MISSING_RECEIPT`
- Cash collections are absent while `quantity_sold > 0` → `MISSING_COLLECTIONS`

Locking (admin-only) additionally requires all flagged expenses reviewed.

### 6.9 Corrections after close

No `UPDATE` and no `DELETE` on financial rows in a `closed` or `locked` shift.
Corrections create a **reversal entry**: a new row with the negated amount, a
`reverses_id` FK to the original, and a mandatory reason. Both rows remain visible.

This is how double-entry accounting has worked for 600 years and it is the only way
to answer "who changed this, when, and what was it before".

### 6.10 Idempotency

Every `POST` that creates a money record accepts an `Idempotency-Key` header.
Store `(key, endpoint, user_id) → response` for 24 hours. A repeat with the same key
returns the original response without creating a second row.

**This is not optional.** Attendants use phones on patchy rural connectivity. A retry
after a timeout must not create a duplicate ₹5,000 expense.

---

## 7. File Upload Flow

### 7.1 Pattern choice for V1: proxy through FastAPI

The file passes through the API server on its way to Supabase Storage.

**Why not presigned URLs?** Presigned direct-to-storage upload is the better pattern
at scale and should be V2. For one pump, images under 5 MB, and a developer learning
backend basics, proxying is materially simpler, synchronous, and easier to reason
about and test. KISS. Document this as a known trade-off, not an oversight.

### 7.2 Sequence

1. Client `POST`s `multipart/form-data` to `/api/v1/uploads/receipt`.
2. Server validates **before** touching storage:
   - Size ≤ 5 MB (reject with 413, code `FILE_TOO_LARGE`)
   - MIME type by **content sniffing** (magic bytes), *not* file extension —
     an attacker renames `evil.exe` to `receipt.jpg` in two seconds
   - Allowed: `image/jpeg`, `image/png`
   - **HEIC/HEIF must return a clear, specific error** telling the user to change
     their iPhone camera setting to "Most Compatible". iPhones shoot HEIC by default
     and this *will* be the number one support complaint. A generic "invalid file
     type" message will waste hours.
3. Server computes SHA-256 checksum.
4. Server uploads to Supabase Storage at:
   `receipts/{outlet_id}/{business_date:YYYY}/{MM}/{DD}/{uuid4}.{ext}`
   - **Never use the client-supplied filename in the storage path.** Path traversal
     and collision risk. Keep the original only as a display label.
5. Server inserts an `attachments` row and returns `{"attachment_id": "..."}`.
6. Client submits the credit sale / expense referencing that `attachment_id`.
7. Server sets `attachments.linked_at` when the reference is created.

### 7.3 Reading files back

The bucket is **private**. Never make it public.
`GET /api/v1/attachments/{id}/url` checks the caller's permission and returns a
short-lived signed URL (5 minutes). Expired links are useless if leaked.

### 7.4 Housekeeping

A scheduled job deletes `attachments` rows (and their storage objects) where
`linked_at IS NULL AND created_at < now() - interval '24 hours'`.
These are abandoned uploads. V1: a management command run manually or via cron.
Do not build a job scheduler.

---

## 8. Auth & Permissions

JWT from Supabase Auth, verified server-side on every request. Never trust a
client-supplied role claim without verification against `outlet_memberships`.

**Roles are per-outlet.** The permission check is always "does this user hold role R
**at the outlet that owns this row**", never "is this user an admin". In V1 there is
one outlet (`DEFAULT_OUTLET_ID`) so every check resolves against it, but the role
dependency takes the outlet as a parameter from the start — that is the direct cost of
the §5.0 decision, and retrofitting it into every endpoint later would be worse.

| Action | attendant | manager | admin |
|---|:--:|:--:|:--:|
| Create readings/collections/expenses/credit sales on an open shift | own only | any | any |
| Read own shift | ✅ | ✅ | ✅ |
| Read all shifts / reports | ❌ | ✅ | ✅ |
| Close a shift | ❌ | ✅ | ✅ |
| Record bank deposits | ❌ | ✅ | ✅ |
| Review flagged expenses | ❌ | ✅ | ✅ |
| Lock a shift / finalise a day | ❌ | ❌ | ✅ |
| Enter fuel prices and margins | ❌ | ❌ | ✅ |
| Manage fuel types (add a new product, e.g. XP-95) | ❌ | ❌ | ✅ |
| Manage users, nozzles, customers | ❌ | ❌ | ✅ |
| Override credit limit / manual litres | ❌ | ❌ | ✅ |
| Seed initial opening balance | ❌ | ❌ | ✅ |

**Ownership is a separate axis from role.** The first row is constrained by *ownership* as
well as role: an attendant may write only to a shift whose `attendant_id` is their own user
id. Managers and admins may write to any open shift at their outlet. Role alone cannot
express this, so `require_role` handles the role floor only, and the shift-scoped dependency
(Phase 4) applies the ownership check **only when the actor's role is `attendant`**.

**Roles are hierarchical:** `attendant < manager < admin`. A manager can do everything an
attendant can, and an admin everything a manager can, so every check is a minimum-role
comparison (`app/core/roles.py::satisfies`), never exact matching.

**Enforced server-side on every endpoint.** Hiding a button is UX, not a control.
Write a permission test for the attendant-touching-another-shift case specifically.

---

## 9. API Conventions

- Base: `/api/v1`
- Resources are plural nouns: `/shifts`, `/shifts/{id}/expenses`
- Methods: `GET` read, `POST` create, `PATCH` partial update, `DELETE` unused (§6.9)
- Status codes: `200` ok, `201` created, `400` malformed, `401` unauthenticated,
  `403` unauthorised, `404` missing, `409` business-rule conflict, `422` validation,
  `413` payload too large
- **Cursor pagination on all list endpoints.** Not offset — inserts during scroll
  cause duplicates and skips with `OFFSET`.
- CORS: explicit origin allowlist from config. **Never `allow_origins=["*"]`.**
- Every request gets a `request_id`, logged and returned in error responses.

---

## 10. Testing Policy

**When a bug is reported: do not fix it first. Write a failing test that reproduces it,
then fix it, then show the test passing.** This applies without exception.

Tests use a real Postgres instance (Docker or a Supabase branch), not SQLite —
SQLite does not enforce the constraints this project depends on, so a passing SQLite
test suite would give false confidence about exactly the rules that matter most.

**Required test cases before any module is considered done:**

*Sales math*
- Normal reading pair
- Rollover: `closing < opening` with flag set → correct positive litres
- `closing < opening` with no flag → 422, no row written
- `testing_quantity` correctly subtracted
- `testing_quantity` exceeding throughput → 422
- Implied flow rate above sanity ceiling → 422
- Meter reset requires override + reason, admin only

*Pricing and margin*
- Sale valued at historical rate, not current rate
- Price change after a shift does not alter that shift's recorded sales
- Warning logged when a revision falls inside the shift window
- `at` exactly equal to an `effective_from` resolves to that row (the boundary is `<=`)
- A lookup with no prior row raises, rather than returning null or zero
- Prices and margins are isolated by outlet and by fuel type
- **Margin is unchanged by price revisions** (§4.6) — enter a margin, then revise the
  price twice; `margin_at` still returns the original figure
- Neither table can be `UPDATE`d or `DELETE`d

*Units*
- CBG resolves as `kilogram` while petrol resolves as `litre`
- The sanity ceiling used is the fuel's own, not a global constant
- A fuel type's `unit_of_measure` cannot be changed after creation

*Business date*
- Night shift spanning midnight assigned to a single correct `business_date`

*Cash*
- Full expected-cash calculation with every term non-zero
- Cash udhaar repayment increases expected cash; UPI repayment does not
- Rolling balance uses prior day's **actual counted**, not expected
- Day N cannot finalise if day N−1 is unreconciled → 409

*Credit*
- Credit sale with no `attachment_id` → rejected
- Credit sale with a non-existent `attachment_id` → rejected
- Credit sale exceeding credit limit → 409; admin override succeeds and is logged
- Outstanding balance correct after a partial repayment

*Expenses*
- Boundary: ₹999.99 not flagged, ₹1000.00 not flagged, ₹1000.01 flagged
  (confirm the intended comparison is strictly `>`)
- Two ₹600 same-category same-day expenses → category aggregate flag fires
- Shift lock blocked while an unreviewed flagged expense exists

*Auth*
- Attendant writing to another attendant's shift → 403
- Attendant closing a shift → 403
- Manager locking a shift → 403

*Uploads*
- Oversized file → 413
- PDF renamed to `.jpg` → rejected by content sniffing
- HEIC → specific, actionable error message
- Unlinked attachment older than 24h is cleaned up

*Idempotency*
- Same `Idempotency-Key` twice → one row, identical response both times

*Immutability*
- Any write to a `locked` shift → 409

---

## 11. Build Order

Build, test, and understand each phase before starting the next. Do not scaffold
ahead — no empty modules for later phases.

1. **Foundation** — project layout, config via env vars, Alembic set up, structured
   logging with `request_id`, health endpoint, one migration proving the pipeline
   works, and the `outlets` table with its single seeded row (§5.0)
2. **Auth** — Supabase JWT verification, `user_profiles`, `outlet_memberships`,
   per-outlet role dependency, permission tests
3. **Reference data** — `fuel_types` (admin-managed, unit-aware), `nozzles`,
   `fuel_prices` and `fuel_margins` (both append-only) + the shared `rate_at` /
   `margin_at` lookup helpers
4. **Shifts** — open/close/lock lifecycle and status guards
5. **Nozzle readings & sales math** — the whole of §6.2 and §6.3, heavily tested
6. **Collections**
7. **Expenses** + flagging rules
8. **Attachments** — upload, validation, signed download, cleanup
9. **Credit** — customers, sales (receipt-enforced), repayments, outstanding balance
10. **Cash engine** — daily summary, expected vs actual, rolling balance
11. **Audit log** — retrofit across all financial writes *(consider building this at
    step 4 instead if it feels cheap to do early; retrofitting is the usual regret)*
12. **Frontend** — minimal HTML/CSS/JS forms and tables
13. **Reporting** — daily summary, 7-day rolling view, variance alerts

---

## 12. Explicitly Out of Scope for V1

Do not build these. Do not add columns "ready for" them. If one seems necessary, stop
and ask.

- OCR / automatic reading of receipt images
- Tank dip readings and stock reconciliation (litres in tank vs litres sold).
  **Stock stays out of V1 entirely**, including revaluation when prices move — see §13.7.
  Note this is not needed for profit: margin is constant, so profit comes from the
  totalizer alone (§4.6)
- Fuel purchase / tanker delivery intake, including per-delivery invoice rates
- **Bank balances, the IOCL virtual account / PAD statement ledger, and net-position
  ("where is my money") reporting.** The PAD statement is **bank-only — it never touches
  the cash drawer**, so an IOCL payment must *never* be recorded as an expense (§6.4
  would invent a daily cash shortage that never happened). The ledger balance is
  meaningless until bank balances exist, so these three are **one post-V1 module, built
  together**. Recorded here rather than forgotten: the balance can be positive
  (prepayment, because restocks are paid in rounded amounts) or negative (payable)
- GST, invoicing, statutory reporting
- Payroll, attendance
- Multi-outlet *features* — switching UI, cross-outlet reporting, RLS policies.
  The **schema** is already outlet-ready; see §5.0. Do not build the features, and
  do not remove the `outlet_id` columns.
- Mobile app (the API must *permit* one; V1 does not *build* one)
- Real-time updates, websockets, push notifications
- Per-transaction (per-fill) data capture
- Role-based UI theming, dark mode, i18n
- Any frontend framework or build step

---

## 13. Known Approximations in V1

State these in code comments where they occur. They are decisions, not bugs — but a
future reader must be able to tell the difference.

1. Shift sales valued at a single rate (shift start), not apportioned across a
   mid-shift price revision. §6.3
2. Non-fuel cash income is a manual entry field, not an itemised sales module.
3. Meter reset requires manual admin entry rather than automated inference.
4. Upload proxies through the API rather than using presigned direct upload.
5. Cleanup of orphaned attachments is a manual/cron command, not a job queue.
6. Multi-outlet is **schema-only**. One outlet is seeded at a fixed id and every
   scoped row references it. There is no outlet-switching UI, no cross-outlet
   reporting, and no RLS. The columns exist so that adding a second outlet is a
   feature change rather than a data migration with no correct answer. §5.0
7. **Profit excludes stock revaluation.** V1 reports `quantity_sold × margin`. Holding
   12 kL of petrol when the rate rises ₹1 is a real ₹12,000 gain that this system will
   never see, because nothing moved through a nozzle. Reported profit is therefore
   *gross fuel margin on quantity sold*, not business profit, and will be understated or
   overstated whenever prices move against held stock. The owner monitors this outside
   the system. **Label it accordingly wherever it is displayed** — an unlabelled "profit"
   figure here is exactly the plausible-but-wrong number this document exists to prevent.
   §4.6, §12
8. **CBG revenue will not tie exactly to IOCL's invoice.** IOCL splits its fortnightly
   billing at the exact moment of a price revision; §6.3 values a whole shift at the rate
   effective at `started_at`. On revision days the two figures differ slightly. §4.5
9. Profit reporting is **fuel-margin only** — it excludes the IOCL ledger balance and any
   non-fuel income. §12

---

## 14. Guardrails for Claude Code

Read this section before generating anything. These are the failure modes most likely
to occur on this specific project.

**Do not:**
- Use `float` for money anywhere, including in a "quick" test fixture or a docstring example
- Compute sales from a client-supplied total
- Store fuel price as a mutable single-value column
- Attach totalizer fields directly to `shifts` (they belong on `nozzle_readings`)
- Skip `testing_quantity` because it seems like a rounding detail — it is not
- Assume a quantity is in litres — read `fuel_types.unit_of_measure` (§4.5)
- Hardcode the ₹2.28 CBG margin, or any margin — it is effective-dated data, not a constant
- Derive margin from a purchase price — V1 stores margin directly and holds no purchase
  data at all (§4.6)
- Read `MAX_FLOW_RATE_LPM` in the §6.2 guard — it only seeds `fuel_types`
- Record an IOCL / PAD payment as an expense — it is a bank movement, not a drawer
  movement, and §6.4 would invent a cash shortage (§12)
- Use offset pagination
- Set `allow_origins=["*"]`
- Add a frontend framework, bundler, or npm dependency
- Hardcode `1000` for the expense threshold
- Create scaffolding for out-of-scope features
- Create a table listed in §5.0's schedule **without** its `outlet_id`, or with a
  unique constraint that is not outlet-scoped where §5.1–§5.3 says it should be
- Remove an `outlet_id` column because §12 says multi-outlet is out of scope — the
  *features* are out of scope, the schema is not. See §5.0
- "Improve" the schema mid-implementation without flagging it first

**Do:**
- Ask before adding any table, dependency, or column not listed here
- Write the failing test first when a bug is reported
- Recompute derived values server-side, always
- Add a code comment wherever §13 approximations appear
- Prefer explicit, verbose, obvious code over clever abstractions
- Flag it loudly if a rule in this file appears to contradict another rule

**Open questions to raise with the owner before the relevant phase:**
- How many nozzles, and what is each meter's rollover ceiling?
- Are there exactly two shifts a day, always? Any third/relief shift?
- Who physically counts the cash, and at what time?
- Is non-fuel (lubricant) sales volume significant enough to itemise in V2?
- Does the pump currently record testing litres on paper? In what unit?
- What is the real maximum flow rate of the CBG dispenser, in kg/min? Migration `0003`
  seeds a deliberately generous 15; confirm before Phase 5 consumes it in §6.2.
- What are the petrol and diesel dealer commissions per litre? Needed to enter
  `fuel_margins` rows for them; CBG's ₹2.28 is known. Until entered, profit reporting
  covers CBG only.
- **§6.4 vs §5.2 contradiction, decide before Phase 7:** §6.4 subtracts `cash_expenses`
  from expected drawer cash, implying some expenses are not cash — but `expenses` has no
  payment-mode column, so every expense is implicitly cash. Either `expenses` gains a
  `mode`, or §6.4 must say expenses are cash-only. Cheap now, ugly once rows exist.

---

## 15. Commands

```bash
# Start the local Postgres (required before tests or the dev server)
docker compose up -d db

# Run dev server
uvicorn app.main:app --reload

# Tests
pytest
pytest -k "rollover"          # single concern
pytest --cov=app              # coverage

# Migrations
alembic revision --autogenerate -m "description"
alembic upgrade head
alembic downgrade -1

# Cleanup orphaned attachments
python -m app.jobs.cleanup_attachments
```

---

## 16. Config (environment variables)

```
DATABASE_URL
SUPABASE_URL
SUPABASE_SERVICE_KEY          # server-side only, never exposed to frontend
SUPABASE_JWT_SECRET
SUPABASE_STORAGE_BUCKET=receipts
EXPENSE_REVIEW_THRESHOLD=1000.00
MAX_UPLOAD_BYTES=5242880
MAX_FLOW_RATE_LPM=60           # seeds fuel_types.max_flow_rate_per_minute for litre
                              # fuels in migration 0003 ONLY. The §6.2 guard reads the
                              # per-fuel column, never this. See §4.5.
SIGNED_URL_TTL_SECONDS=300
CORS_ALLOWED_ORIGINS            # comma-separated, never "*"
TZ_DISPLAY=Asia/Kolkata
```

No secrets in the repository. `.env` is gitignored; `.env.example` is committed with
placeholder values.
