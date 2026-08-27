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

**Where the files live, and what "no build step" means concretely (Phase 12).** The assets sit
in `app/static/` — inside the package `pyproject.toml` already installs, so deployment ships one
artefact rather than two that can drift — and are mounted **after** `include_router`, which is
what stops the mount shadowing `/api/v1`. The mount is a Starlette `Mount` rather than an
`APIRoute`, so `tests/test_routes.py` neither sees it nor is broken by it.

No bundler, no transpiler, no `package.json`: the browser loads ES modules natively via
`<script type="module">`, and routing is hash-based (`#/shifts/{id}/readings`) so a deep link
never reaches the server and needs no SPA rewrite. The mount serving `index.html` is still not
rendering: FastAPI hands over a file it did not generate, which is the distinction this section
is about.

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

### 4.7 The shift chain (verified against how this outlet actually runs)

The original spec assumed exactly two shifts a day, `morning | night`, with the night
shift crossing midnight. **That is not this outlet, and it is not general enough to be
any outlet.** What is actually true:

- This station **trades 06:00 → 22:00** and is shut for eight hours. Nothing is dispensed
  overnight. It has **one** accounting period per day, not two.
- The salesman roster runs **09:00 → 09:00**, but the 06:00–09:00 cash is handed to the
  incoming crew, so one crew ends up holding an entire trading day's takings. **The roster
  is a labour arrangement, not an accounting boundary**, and the system does not model it.
- The sales register has **one line per day**. The whole day is typed in **after the
  fact**, in one sitting — nobody operates the app while trading.
- Other outlets this software will serve **do** run 24 hours on three 8-hour shifts.

So the number of shifts in a day is data, not schema. Shifts are **sequence-numbered per
business date**, any number of them, and `shift_type` does not exist.

**The chain.** A nozzle's opening reading is not entered. It is carried forward from the
**most recent closing reading of that same nozzle**, across shifts and across days. An
attendant only ever types a closing value. An overnight closure and a 24-hour handover are
then the same thing: a gap between one shift's close and the next one's open.

The lookup is "the most recent closing reading **for that nozzle**", not "the previous
shift's reading". A nozzle out of order for one shift, a nozzle installed mid-life, or a
whole skipped day would all snap a chain built on the latter.

**The chained value is confirmed, never assumed.** This is the rule that matters most, and
it is not a formality. Auto-filling the opening deletes one of the two independent meter
observations an attendant makes. Fuel siphoned through a nozzle between shifts still moves
the totalizer — but an assumed opening says it did not, so the missing quantity is
absorbed into the next shift as sales that produced no cash. The shift then comes up
short, and this outlet books a shortfall as **udhaar against the salesman's own name**.
An assumed opening therefore converts theft into a debt owed by someone who did nothing
wrong, and leaves no trace pointing at the gap.

So: pre-filled, not typeable, and **confirmed against the physical meter**, with a mismatch
path that captures the real reading and raises it for review before anybody is blamed.
Zero typing on a normal day; the abnormal day becomes visible instead of reassigned.

**The chained opening is stored on the row, not computed on read.** Computing it would
mean that correcting one shift silently rewrites the next shift's history — destroying
exactly the record §5.2 keeps `expected_closing` for.

**Anchoring.** The first shift ever, and every newly installed nozzle, has no predecessor
and needs a one-time seeded starting reading (admin-only), the same shape as §6.5's seeded
first opening balance. A meter reset (§4.3) breaks the chain deliberately and re-anchors
it through §6.2's admin-only override path.

**The anchor is the first reading itself, not a separate record.** When the chain lookup
finds no predecessor for a nozzle, `opening_reading` becomes a *required* payload field and
the caller must be an admin (403 `ANCHOR_REQUIRES_ADMIN` otherwise); the row stores
`chained_opening_reading = NULL` to mark it as anchored rather than carried. A column on
`nozzles` or a separate anchor table would both duplicate a value that already exists on
that first row, and the two copies would eventually disagree about where a meter started.

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
| 4 | `shifts`, `outlet_shift_templates` | **Yes** |
| 4 | `audit_logs` | **Yes** — no parent (moved from Phase 11, see §11) |
| 5–7 | `nozzle_readings`, `collections`, `expenses` | No — derivable via `shift_id` |
| 6 | `idempotency_keys` | No — infrastructure, not a business row (§5.3) |
| 8 | `expense_categories` | **Yes** — reference data, but per-outlet (§5.1) |
| 8 | `attachments` | **Yes** — no parent shift |
| 9 | `credit_customers` | **Yes** |
| 9 | `credit_sales`, `credit_repayments` | No — derivable via `shift_id` |
| 10 | `non_fuel_sales`, `bank_deposits` | No — derivable via `shift_id` |
| 10 | `salesman_shortfalls`, `salesman_shortfall_settlements` | No — derivable via `shift_id` |
| 10 | `daily_cash_summaries` | **Yes** |
| ~~11~~ | ~~`audit_logs`~~ | Built in Phase 4 instead — see the row above |

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
- `id` — UUID, matches `auth.users.id`. **Immutable, and never generated here** — see below
- `full_name` — text
- `is_active` — boolean, default true. **Written only by `provision_user.py`** — see
  `outlet_memberships` below for why the API does not touch it
- `phone` — text, nullable
- **No `role` and no `outlet_id`** — a user's role is per-outlet, held in
  `outlet_memberships`. See §5.0.
- **No `email` and no password.** Supabase owns the credential and this table deliberately
  does not mirror it — one source of truth. **Phase 14 amendment:** this is why
  `POST /users` cannot pre-check a duplicate the way every other create does, and why an
  email can never be changed through this API

> **`id` is immutable for the reason `fuel_types.code` is, and then one reason more.**
> §5.1 already refuses to let a code change because it "relabels every expense ever filed
> under it"; here roughly fifteen tables hold a foreign key to this column — `created_by`,
> `entered_by`, `reviewed_by`, `attendant_id`, `closed_by`, `locked_by`, `uploaded_by`,
> `changed_by`, `salesman_id`, `finalised_by` — and none of them cascade. But the sharper
> reason is that this value *is* the JWT's `sub` claim. Change it and the person
> authenticates successfully and is refused forever with `PROFILE_NOT_PROVISIONED`, which
> is the exact failure `app/jobs/provision_user.py` warns about when it says the command
> "cannot invent it".
>
> **`created_by` distinguishes the two ways a user arrives.** `NULL` means the CLI
> provisioned them — a system action, and for the very first admin there is no user to
> credit. A populated value means an admin created them through `POST /users`, by name.
> **Phase 14 amendment**; before it, every row was NULL because the CLI was the only path.

**`outlet_memberships`** — which users may act at which outlet, and in what capacity
- `user_id` — FK to user_profiles
- `outlet_id` — FK to outlets
- `role` — enum: `admin` | `manager` | `attendant`
- `is_active` — boolean, default true. **This is how a person is retired** — see below
- Unique constraint on `(user_id, outlet_id)`

> Role lives here rather than on `user_profiles` because someone can legitimately be
> a manager at one outlet and an attendant at another. Putting `outlet_id` directly
> on `user_profiles` would hardcode one-user-one-outlet — the same retrofit this
> section exists to avoid, merely moved to a different table.

> **Retirement is a membership act, not a profile act. Phase 14 amendment.** There are two
> `is_active` flags and they mean different things, which `app/api/deps.py` already makes
> visible by giving them different error codes: a false membership flag is 403
> `MEMBERSHIP_INACTIVE`, *"Your access to this outlet has been revoked"*; a false profile
> flag is 403 `PROFILE_INACTIVE`, *"This account has been deactivated."*
>
> The API writes **only the membership flag**. In V1 that is already a complete lockout,
> because every protected route resolves through `require_role` and therefore through the
> membership. `user_profiles.is_active` means "gone from every outlet" — a sentence V1 has
> no way to mean, since there is one outlet — so exposing both would be two switches doing
> one job today and diverging confusingly the day there are two. It keeps its single
> writer, `provision_user.py`.
>
> **There is no delete, and could not be.** §3 rule 6 forbids it as policy; the fifteen
> non-cascading foreign keys above forbid it as physics. A user who has done anything is
> undeletable twice over.

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

**`outlet_shift_templates`** — the shifts an outlet *usually* runs
- `outlet_id` — FK to outlets, NOT NULL
- `sequence` — SMALLINT, matches `shifts.sequence`
- `label` — text, e.g. `Day`, `Night` (display only; nothing keys off it)
- `starts_at_local`, `ends_at_local` — TIME
- `is_active` — boolean
- Unique constraint on `(outlet_id, sequence)`

> Supplies the default `started_at` / `ended_at` when a shift is opened, because days are
> typed in after the fact and nobody should retype 06:00 and 22:00 every morning. The
> defaults are *materialised onto the shift row*, never read back through the template —
> editing a template must not revalue a shift that already happened (§6.3 prices a shift
> from its own `started_at`).
>
> `TIME`, not `TIMESTAMPTZ`, and this is not a breach of §3 rule 4. That rule governs
> *instants*. "06:00 local, every day" is a recurring wall-clock time, a genuinely
> different type that cannot be stored as an instant without inventing a date.
>
> This outlet has one row: sequence 1, 06:00 → 22:00. A 24-hour outlet has three.

**`credit_customers`** — who may take udhaar, **admin-managed**. **Phase 9 amendment.**
- `outlet_id` — FK to outlets, NOT NULL
- `name` — text, NOT NULL, non-blank
- `phone` — text, **NOT NULL**
- `vehicle_numbers` — text[] (nullable), normalised upper-case
- `credit_limit` — NUMERIC(12,2), nullable (null = no limit, **never read as zero**)
- `is_active` — boolean
- Unique constraint on `(outlet_id, phone)`

> **Why `phone` is the natural key.** §6.7's argument about `Tea`/`tea`/`chai ` transfers
> intact: two rows for one person split one real balance across two ledgers, and §6.6's
> credit limit then never fires against either. Names genuinely collide — a pump has three
> customers called Ramesh — and phone numbers do not, which is why the constraint is on the
> phone rather than the name. This is the same middle ground `expense_categories.code`
> occupies: open to new rows, closed to accidental duplicates.
>
> No `code` column, unlike `fuel_types` and `expense_categories`. A customer is not a label
> an aggregate is grouped by, so `^[A-Z][A-Z0-9_]*$` has nothing to protect here.
>
> **Deactivation is asymmetric, and deliberately so.** A deactivated customer refuses a new
> **credit sale** (409 `CREDIT_CUSTOMER_INACTIVE`) but still **accepts a repayment**. You
> deactivate somebody precisely to stop the debt growing while they pay off what they owe;
> refusing their money would be backwards, and would leave a balance nothing can ever clear.
> Historical rows keep reading and reporting either way (§3 rule 6).

**`expense_categories`** — what an expense can be filed under, **admin-managed**.
**Phase 8 amendment** — replaces the `expense_category` enum.
- `outlet_id` — FK to outlets, NOT NULL
- `code` — text, e.g. `TEA`, `ELECTRICITY`; **immutable once created**
- `display_name` — text
- `requires_receipt` — boolean, NOT NULL, default false — see §6.11
- `is_active` — boolean
- Unique constraint on `(outlet_id, code)`

> **Why this stopped being an enum.** §5.2 originally said "keep it an enum, not free text",
> and Phase 7 shipped `salary | maintenance | electricity | other` as a Postgres enum. But
> §5.1 already rejects that shape one table up, for `fuel_types`: *"Admins add fuel types
> through the API, not through a migration… adding a product you sell is data entry, and
> requiring a schema change for it would be wrong."* Every word of that transfers. An outlet
> buys tea, or diesel-exhaust fluid, or pays a borewell bill, and needing a migration to say
> so is the same mistake in a different table.
>
> **This is not a retreat to free text**, which is what the original rule was guarding
> against. Free text lets `Tea`, `tea` and `chai ` become three categories, and §6.7's
> aggregate rule then never fires — ₹600 under one label plus ₹600 under another never sums
> to ₹1,200, and the control silently becomes theatre. A controlled, admin-managed reference
> table is the same middle ground `fuel_types` occupies: open to new rows, closed to typos.
>
> **`code` is immutable** for the reason `fuel_types.code` is: changing it retroactively
> relabels every expense ever filed under it, and history stops meaning what it said.
> Deactivate rather than delete (§3 rule 6); `display_name`, `requires_receipt` and
> `is_active` stay editable. A deactivated category refuses **new** expenses (409
> `CATEGORY_INACTIVE`) while historical rows keep pointing at it and keep reporting.
>
> **Outlet-scoped, unlike `fuel_types`.** `PETROL` means the same thing at every outlet;
> "tea" does not. One pump's category list is not another's, and `requires_receipt` is a
> control decision an outlet's own admin makes. Not derivable from anything, so by §5.0's
> rule the column exists from birth.

### 5.2 Transactional tables

**`shifts`** — the spine. Everything hangs off this. See §4.7 for the chain model.
- `outlet_id` — FK to outlets, NOT NULL
- `business_date` — DATE, **explicit, not derived**
- `sequence` — SMALLINT, **server-assigned**, 1 for the first shift of that business
  date, 2 for the next, and so on. There is deliberately **no `shift_type`** — see §4.7
- `started_at` — TIMESTAMPTZ **NOT NULL**. A shift with no start cannot be priced (§6.3)
- `ended_at` — TIMESTAMPTZ, nullable until close
- `attendant_id` — FK to user_profiles, NOT NULL — the one person accountable for this
  shift's cash. Other staff may be on duty; exactly one name carries the drawer
- `status` — enum: `open` | `closed` | `locked`
- `closed_by`, `closed_at` — nullable
- `locked_by`, `locked_at` — nullable
- Unique constraint on `(outlet_id, business_date, sequence)`

Status transitions: `open → closed → locked`. Never backwards without an admin action
that is itself audit-logged. Nothing referencing a `locked` shift may be modified.

**Only one shift per outlet may be `open` at a time.** This is what makes §4.7's chain
unambiguous: there is always exactly one closing reading to carry forward.

**`nozzle_readings`** — one row per nozzle per shift. The source of truth for sales.
- `shift_id` — FK
- `nozzle_id` — FK
- `opening_reading` — NUMERIC(12,2) — the **confirmed** physical value (§4.7)
- `chained_opening_reading` — NUMERIC(12,2) nullable — what the chain predicted.
  `NULL` means this row anchored the chain (no predecessor). See below
- `opening_variance_reason` — text, nullable, **required** when `opening_reading`
  differs from `chained_opening_reading`
- `closing_reading` — NUMERIC(12,2), nullable until shift close
- `testing_quantity` — NUMERIC(10,3), NOT NULL DEFAULT 0
- `rollover_occurred` — boolean, default false
- `meter_reset_occurred` — boolean, default false
- `manual_quantity_override` — NUMERIC(10,3) nullable (admin-only, see §6.2)
- `override_reason` — text, nullable, required if override is set
- `requires_review` — boolean, default false (§4.7's mismatch path, and §13.10's
  downstream flag; same shape as `expenses.requires_review`)
- `reviewed_by`, `reviewed_at` — nullable
- `review_note` — text nullable
- Unique constraint on `(shift_id, nozzle_id)`
- No `outlet_id` — derivable via `shift_id`, per §5.0's rule

> **Why `chained_opening_reading` exists as its own column.** §4.7 requires the carried
> opening to be *confirmed against the physical meter*, not assumed. Without somewhere to
> keep what the chain predicted, a confirmed reading and an assumed one are indistinguish-
> able the moment they are written, and a mismatch — the signal that fuel moved between
> shifts — leaves no trace at all. Storing both makes "the meter did not say what we
> expected" a fact on the row rather than an event nobody recorded.
>
> The three review columns mirror `expenses` deliberately. A flag with no way to clear it
> is a flag nobody looks at twice.

**`collections`** — money received during a shift, tagged by how it arrived.
- `shift_id` — FK
- `mode` — enum: `cash` | `card` | `upi` | `wallet`
- `amount` — NUMERIC(12,2); `>= 0` on an ordinary row, `<= 0` only on a reversal
- `reference` — text nullable (settlement/batch reference)
- `reverses_id` — FK to `collections.id`, nullable, **unique** — §6.9's correction path
- `reversal_reason` — text, nullable, **required** when `reverses_id` is set
- No `outlet_id` — derivable via `shift_id`, per §5.0's rule

> **One *live* row per mode, enforced in the service layer — not by a unique constraint.**
> This outlet has one card machine and one UPI QR and the register writes one lumped figure
> per mode, so a second live `cash` row is a mistake and `POST` refuses it with 409
> `COLLECTION_ALREADY_EXISTS`; the client `PATCH`es instead. *Live* means: not itself a
> reversal, and not referenced by one.
>
> `UNIQUE (shift_id, mode)` would be the obvious way to say that and **cannot be used**,
> because it is incompatible with §6.9. A reversed row stays in the table forever, so its
> replacement collides with it on `(shift_id, mode)`. Every partial-index variant fails the
> same way — the replacement carries `reverses_id IS NULL`, exactly like the original — and
> the only escape is a `reversed_at` marker stamped onto the original, which is an `UPDATE`
> on a financial row in a closed shift and therefore the very thing §6.9 forbids.
>
> Retry safety therefore comes from `idempotency_keys` (§5.3, §6.10), which a unique
> constraint would not have provided for the reversal endpoint anyway.

> **`mode = cash` is a *declaration*, not an input to §6.4.** Look at the cash equation:
> it derives cash as the residual — `total_sales − card − upi − wallet − credit_sales` —
> and never reads a cash collection row. That is deliberate. The `cash` row is the
> **independent observation** the derived figure gets checked against:
>
> * **derived cash** — what the meters say the salesman should be holding
> * **declared cash** — the `cash` collection row: what he says he counted into the locker
>
> and the gap between them is the shortfall this outlet books as udhaar against his own
> name (§14). Same shape as §4.7's chain — the system predicts, a human confirms, **both
> values are stored**, and a disagreement leaves a trace instead of being absorbed.
>
> **Never sum the `cash` row together with derived `cash_sales`.** Doing so double-counts
> the entire day's cash, and the result is plausible.

**`credit_sales`** (udhaar issued)
- `shift_id` — FK
- `credit_customer_id` — FK
- `fuel_type_id` — FK, nullable (null = non-fuel credit sale)
- `quantity` — NUMERIC(10,3), nullable (litres or kg, per the fuel's unit — §4.5)
- `amount` — NUMERIC(12,2)
- `vehicle_number` — text nullable
- **`attachment_id` — FK to `attachments`, `NOT NULL`** ← the receipt constraint,
  enforced at the database level, not in application code and not in JavaScript
- `limit_override_reason` — text, nullable. **Phase 9 amendment** — §6.6's admin override,
  required (and non-blank) whenever the sale was allowed past `credit_limit`
- `reverses_id` — FK to `credit_sales.id`, nullable, **unique**. **Phase 9 amendment**
- `reversal_reason` — text, nullable, required (and non-blank) when `reverses_id` is set.
  **Phase 9 amendment**
- ~~`is_settled`~~ — **removed in Phase 9.** See below

> **Why `is_settled` is gone.** It was described here as a "derived convenience flag" while
> §6.6, two sections down, says to compute outstanding from repayments and explicitly warns
> against a denormalised running total because *"it will drift"*. Those two sentences could
> not both be obeyed. Worse, the flag has no non-arbitrary value: a customer with three open
> bills who pays a third of the total has settled *which* rows? Any answer is an invention,
> and every answer is rewritten the moment a reversal lands. "Fully settled" is
> `outstanding == 0`, derived, and that is the only form of the question with an answer.

> **The `NOT NULL` on `attachment_id` survives §6.9 intact, via inheritance.** A reversal is
> a new row, so a bare `NOT NULL` would appear to demand a receipt for a cancellation — the
> exact thing §6.11 refuses to do for expenses, since a cancellation is not a spend and there
> is nothing to photograph. `expenses` escapes through a CHECK that exempts reversals; **that
> escape is deliberately not copied here.** This column is stated as `NOT NULL` *at the
> database level* in two places, as the belt to the API's braces, and weakening it to a CHECK
> would take the receipt control off genuine customer credit sales to solve a problem
> inheritance already solves.
>
> So a `credit_sales` reversal — and its replacement, if any — **carries the original's
> `attachment_id`**, exactly as an expense's replacement already does (§5.3). §5.3's
> one-attachment-one-*live*-row rule still holds throughout: the original is reversed and so
> not live, the reversal is itself a reversal and so not live, and the replacement is the
> single live claimant. `link()` is not called again for either.

**`credit_repayments`**
- `credit_customer_id` — FK
- `shift_id` — FK (the shift during which the money physically arrived)
- `amount` — NUMERIC(12,2)
- `mode` — enum: `cash` | `card` | `upi` | `bank_transfer`
- `attachment_id` — FK nullable
- `reverses_id` — FK to `credit_repayments.id`, nullable, **unique**. **Phase 9 amendment**
- `reversal_reason` — text, nullable, required (and non-blank) when `reverses_id` is set.
  **Phase 9 amendment**

> **`mode` is its own Postgres type, `credit_repayment_mode`** — not `collection_mode`
> (which has `wallet` and no `bank_transfer`) and not `expense_mode`, whose labels happen to
> match today. Sharing a type would force a later phase to alter a live enum or carry a value
> meaningless to one of its users; §5.2 already makes this argument for `collections`.
>
> **Only `mode = cash` repayments enter §6.4's equation.** A customer settling by bank
> transfer moves no money through the drawer, and adding it to expected cash would invent a
> shortfall on the day they pay.

> **Both credit tables carry §6.9's reversal shape** (`reverses_id` unique, a mandatory
> non-blank reason, the strict sign rule `(reverses_id IS NULL AND amount > 0) OR
> (reverses_id IS NOT NULL AND amount < 0)`), for the reason §6.9 gives generally and one
> specific to this outlet: §4.7 says the whole day is typed in **after the fact**, so a
> mistyped udhaar discovered once the shift is closed is the *normal* case here, not an edge
> one. Strict `>` / `<` rather than `>=` / `<=`, matching `expenses`: a ₹0 udhaar records
> nothing and has no reason to exist.

**`expenses`** (renamed from `cash_flows` — the old name was ambiguous, since
collections and deposits are also cash flows)
- `shift_id` — FK
- `category_id` — FK to `expense_categories`, NOT NULL. **Phase 8 amendment** — replaces the
  `category` enum; see §5.1 for why
- `mode` — enum: `cash` | `card` | `upi` | `bank_transfer`. **Phase 7 amendment.**
- `amount` — NUMERIC(12,2). **Sign rule, not a bare `CHECK > 0`** — see the note below
- `description` — text, NOT NULL, 3–500 chars
- `paid_to` — text nullable
- `attachment_id` — FK to `attachments`, nullable. **Lands in Phase 8.** Optional by design —
  only `credit_sales.attachment_id` is `NOT NULL` (§6.6). When it *is* required, §6.11 decides
- `receipt_required` — boolean, NOT NULL. **Phase 8 amendment** — §6.11's answer,
  **snapshotted at insert**, not read back off the category. See §6.11
- `reverses_id` — FK to `expenses.id`, nullable, unique. **Phase 7 amendment**
- `reversal_reason` — text, nullable, required (and non-blank) when `reverses_id` is set.
  **Phase 7 amendment**
- `requires_review` — boolean, default false
- `reviewed_by`, `reviewed_at` — nullable
- `review_note` — text nullable

> **Why `attachment_id` is immutable once set.** It may be supplied at create, or by a
> `PATCH` **while it is still `NULL`**. Swapping one receipt for another is refused with 409
> `ATTACHMENT_ALREADY_SET`, and the correction path is §6.9's reversal like everything else
> here. The alternative is worse in both directions: allowing a swap either strands the old
> receipt as linked-forever garbage §7.4's sweep can never reclaim, or unlinks it and lets
> the sweep delete evidence for an expense that still exists.

> **Why `fuel_purchase` was removed from the category enum.** Restocking the tank is paid
> from the bank account and settles against the IOCL ledger — it never touches the drawer,
> so it is not a cash expense in any sense §6.4 or §6.7 can reason about. It belongs to the
> post-V1 bank/PAD module §12 already scopes out (see the §14 note below). Leaving the label
> in the category dropdown invited exactly the mistake §14 forbids: recording a lakh-rupee
> bank settlement as a drawer expense. `misc` was folded into `other` at the same time —
> two synonymous categories can split one real expense across both labels, silently
> defeating §6.7's per-category daily aggregate (₹600 under `misc` plus ₹600 under `other`
> never sums to ₹1,200).
>
> **That exclusion outlived the enum, and got sharper.** Phase 8 turned the category list
> into admin-managed data (§5.1), so nobody has to edit a migration to add a category — and
> nobody is stopped by one either. **An admin must not create a fuel-purchase, tanker,
> IOCL, or PAD-settlement category.** The reasoning is unchanged and is now the *only* thing
> enforcing it: that money leaves the bank, never the drawer, and §6.4 would invent a daily
> cash shortage that never happened. Removing a label from an enum was a schema act; not
> creating one is a discipline, so it is restated in §14.
>
> **Why `mode` was added.** §6.4 subtracts `cash_expenses`, which only makes sense if some
> expenses are *not* cash — but the original column list had no way to say that. Without
> `mode`, Phase 10 would have had to treat every expense as a drawer movement: a ₹40,000
> electricity bill paid online would then read as a ₹40,000 cash shortfall, and §14 already
> records that this outlet books a shortfall as udhaar against the salesman's own name. `mode`
> is NOT NULL with no default — an answer, never an omission, exactly as §6.8 requires an
> explicit ₹0 cash declaration rather than accepting silence. Phase 10 filters `mode == cash`
> when assembling §6.4's equation; a `card`/`upi`/`bank_transfer` expense is on the record but
> never subtracted from the drawer.
>
> **Why the sign rule replaces `CHECK > 0`.** §6.9 corrections are negative rows, which a
> bare `CHECK > 0` makes impossible the moment the first expense needs reversing. The rule
> is the same shape as `collections`: `(reverses_id IS NULL AND amount > 0) OR (reverses_id
> IS NOT NULL AND amount < 0)`. Strict, not `>=`/`<=` — unlike a cash collection, a ₹0
> expense records nothing and has no reason to exist.

**`non_fuel_sales`** — lubricants, coolant, and anything else sold that no meter counts.
**Phase 10 amendment** — §6.4 named `other_cash_income` from the beginning and §5.2 never
gave it a column. This is that column, and it is per shift.
- `shift_id` — FK
- `amount` — NUMERIC(12,2). §6.9's sign rule, as below
- `description` — text nullable
- `reverses_id` — FK to `non_fuel_sales.id`, nullable, **unique**
- `reversal_reason` — text, nullable, required (and non-blank) when `reverses_id` is set
- No `outlet_id` — derivable via `shift_id`, per §5.0's rule

> **Per shift, not per day**, even though §13.2 calls this "a manual entry field". A ₹500
> bottle of oil is in the salesman's hand and **not** in the meter-derived figure, but it *is*
> in the cash he counts into the locker. Compare derived fuel cash against his declaration
> without it and he shows a ₹500 **surplus** every day he sells one — a phantom in his name.
> The figure has to sit beside the declaration it is checked against, which is the shift.
>
> **It is added to `total_sales`, never to the cash side** — see §6.4's worked example. A
> card-paid oil sale is already inside the card collections total, so putting it on the cash
> side understates derived cash by exactly its amount.
>
> **Still not an itemised sales module (§12, §13.2).** An amount and an optional note. No
> product catalogue, no stock, no unit price. It is a table rather than a column on `shifts`
> only because it is a money row, and §6.9 says a money row is corrected by a reversal — and
> you cannot reverse a column.

**`bank_deposits`**
- `shift_id` — FK
- `business_date` — DATE. **Set server-side from the shift**, never accepted from a client
- `amount` — NUMERIC(12,2). §6.9's sign rule, as below
- `bank_reference` — text nullable
- `attachment_id` — FK nullable (deposit slip)
- `reverses_id` — FK to `bank_deposits.id`, nullable, **unique**. **Phase 10 amendment**
- `reversal_reason` — text, nullable, required (and non-blank) when `reverses_id` is set.
  **Phase 10 amendment**
- No `outlet_id` — derivable via `shift_id`, per §5.0's rule

> **`business_date` is derivable from the shift and is written anyway**, which reads like a
> breach of §5.0's own rule until you notice §5.0 is about *tenancy* columns with no correct
> backfill. This one has a correct backfill, so it is kept for the reason §5.2 keeps
> `expected_closing`: a deposit is filed against a trading day, and reading that through a
> join every time makes the most-queried column on the table the one you cannot index
> directly. **§3 rule 7 still applies** — the server recomputes it from
> `shifts.business_date` and refuses to take the client's word for it, so the two cannot
> drift.

**`salesman_shortfalls`** — what a salesman owes because the drawer came up short.
**Phase 10.** See §13.14 for why this is not a `credit_sale`.
- `shift_id` — FK — the shift whose reconciliation produced it
- `salesman_id` — FK to **`user_profiles`**, NOT NULL. **Never a `credit_customer_id`**
- `amount` — NUMERIC(12,2). §6.9's sign rule, as below
- `computed_gap` — NUMERIC(12,2), NOT NULL — what the system calculated at the moment of
  booking, stored beside what the human actually booked
- `reason` — text, NOT NULL, non-blank
- `reverses_id` / `reversal_reason` — §6.9's shape
- No `outlet_id` — derivable via `shift_id`

> **`salesman_id` is not a payload field.** It is read from `shifts.attendant_id`, which §5.2
> already defines as *"the one person accountable for this shift's cash… exactly one name
> carries the drawer, and a shortfall is booked against it."* Accepting it from a client would
> let a typo put a debt on the wrong person's name, and there is no second source of truth to
> catch that.
>
> **A shortfall is booked by a human, never automatically.** The system computes the gap and
> shows it; a manager books it with a mandatory reason. §4.7's argument applies with more
> force here than anywhere else in this document — *"an assumed opening converts theft into a
> debt owed by someone who did nothing wrong"* — because here the debt is explicit and
> carries a name. A ₹500 gap is more often a mistyped reading, a forgotten UPI figure or an
> unrecorded udhaar slip than it is theft, and the software must not be the thing that
> decides.
>
> **`computed_gap` and `amount` are both stored, and may differ.** A manager may know part of
> the gap is a slip he has already corrected. A divergence **logs a warning and writes** — it
> never refuses, for the reason §6.8 gives about close preconditions: refusing a human's
> judgement sends the correction outside the system, where nothing can see it. Storing both
> is §4.7's predict-and-confirm shape again: the system's figure and the human's, side by
> side, with the disagreement legible.

**`salesman_shortfall_settlements`** — a salesman paying back what he owed. **Phase 10.**
- `shift_id` — FK — the shift during which the money physically arrived
- `salesman_id` — FK to `user_profiles`, NOT NULL
- `amount` — NUMERIC(12,2). §6.9's sign rule, as below
- `reverses_id` / `reversal_reason` — §6.9's shape
- No `outlet_id` — derivable via `shift_id`

> **The shape is `credit_sales` / `credit_repayments`, deliberately.** A shortfall is a debt
> and a settlement pays it down, which is the same question udhaar already answers, so:
>
> ```
> outstanding(salesman) = SUM(salesman_shortfalls.amount)
>                       − SUM(salesman_shortfall_settlements.amount)
> ```
>
> Summed over **every** row, reversals included — they carry negative amounts and net out.
> **Computed, never stored.** §6.6's rule and the reasoning that deleted
> `credit_sales.is_settled` both transfer verbatim, and §14's guardrail against a
> denormalised running total covers this table too.
>
> A settlement points at the **salesman**, not at a particular shortfall — §5.2's reason for
> `credit_repayments`: one payment covering part of three debts has no honest per-row answer.
>
> **No `mode` column, and every settlement is cash.** The owner's answer was that a shortfall
> is repaid in cash — not written off, not deducted from wages. A `mode` column added later
> backfills to `'cash'` for every existing row *correctly*, because every existing row
> genuinely is cash, so by §5.0's own derivability rule it can wait rather than being guessed
> at now (§11's rule against scaffolding ahead). **The consequence is recorded in §13.15**:
> until that column exists there is no way to close out a ₹20 gap nobody will ever chase.

**`daily_cash_summaries`** — one row per outlet per `business_date`
- `outlet_id` — FK to outlets, NOT NULL
- `business_date` — DATE; unique per outlet — `(outlet_id, business_date)`
- `opening_balance` — NUMERIC(12,2)
- `opening_balance_source` — enum: `seeded` | `counted` | `carried` (§6.5). **Phase 10**
- `expected_closing` — NUMERIC(12,2) — **the figure the system computed and showed**
- `actual_counted` — NUMERIC(12,2), nullable
- `variance` — NUMERIC(12,2), generated as `actual_counted - expected_closing`
- `is_finalised` — boolean
- `finalised_by`, `finalised_at` — nullable. **Phase 10**, mirroring `shifts.closed_by`
- `requires_review`, `review_note` — §13.10's flag, for a shift reopened beneath a finalised
  day. **Phase 10**
- `notes` — text nullable
- **The component snapshot** — `metered_fuel_sales`, `non_fuel_sales_total`, `card_total`,
  `upi_total`, `wallet_total`, `credit_sales_total`, `cash_credit_repayments`,
  `cash_shortfall_settlements`, `cash_expenses`, `bank_deposits_total`, `shortfalls_booked`,
  each NUMERIC(12,2) NOT NULL. **Phase 10**

> **Why store `expected_closing` rather than always recomputing?**
> If a calculation bug is fixed six months from now, you still need to know what the
> system told the manager *on that day*. A recomputed-on-read figure destroys that record.
> This is deliberate, not redundant storage.

> **And why store every term, not just the total.** Every word of the paragraph above applies
> to the components. A manager looking at a ₹300 variance needs the breakdown **as it stood**,
> not as recomputed six months later after a reversal landed underneath it — otherwise the
> total and its own explanation disagree, and the explanation is the part he can check. It is
> also the only way to answer *which* term moved when two days are compared.

> **Finalising is terminal, and requires every shift on the date to be `locked`.** Creating
> the row requires them all `closed` or `locked` (409 `DAY_HAS_OPEN_SHIFTS`); setting
> `is_finalised` requires `locked` (409 `DAY_NOT_LOCKED`). §6.5 chains days together, so a
> stale `expected_closing` does not stay local — it propagates into every opening balance
> after it. §5.2 already says nothing referencing a `locked` shift may be modified and §6.8
> makes `locked` terminal, so that is the only state in which the snapshot is guaranteed to
> stay true. It inherits §6.7's quality gate for free: a shift cannot lock while a flagged
> expense is unreviewed.
>
> An admin may **unfinalise** with a mandatory reason, audit-logged — §6.8's shift-reopen
> shape. A finalised summary is otherwise immutable (409 `SUMMARY_FINALISED`).

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
- **No `created_by`**, and this is a deliberate exception to §5's preamble, in the same
  shape as `outlets`'. `uploaded_by` *is* the creator, under the name §7.2 already uses.
  Carrying both would mean two columns holding one value until the day they disagree

> **`storage_path` does not include the bucket.** §7.2 writes the example path as
> `receipts/{outlet_id}/…`, where `receipts` is the bucket — which object storage takes as a
> separate argument, not as a path prefix. Storing it in both places puts the object at
> `receipts/receipts/…` the first time anything concatenates them. So `bucket` holds
> `receipts` and `storage_path` holds `{outlet_id}/{YYYY}/{MM}/{DD}/{uuid4}.{ext}`.

> **One attachment belongs to one *live* business row.** A second row claiming the same
> attachment is refused with 409 `ATTACHMENT_ALREADY_LINKED`. Without that rule one
> photograph can justify ten expenses, which is the abuse the receipt requirement exists to
> catch, and no flag would ever fire.
>
> *Live* means what it means for `collections` in §5.2: **not itself a reversal, and not
> referenced by one.** That precision is load-bearing rather than pedantic. Correcting a
> receipt-required expense under §6.9 creates a reversal *plus* a replacement row, and the
> replacement is a real expense that needs the receipt — so **the replacement inherits the
> original's `attachment_id`**. The original is dead, exactly one live row holds the
> attachment throughout, and nobody has to photograph the same piece of paper twice.

**`idempotency_keys`** — §6.10's replay store. Infrastructure, not a business record.
- `idempotency_key` — text, client-supplied
- `endpoint` — text, the route template (e.g. `POST /shifts/{shift_id}/collections`)
- `user_id` — FK to user_profiles
- `request_fingerprint` — text, SHA-256 of the path params and canonical JSON body
- `response_status` — smallint nullable (`NULL` = in flight)
- `response_body` — JSONB nullable
- Unique constraint on `(idempotency_key, endpoint, user_id)` — §6.10's exact tuple
- **No `outlet_id`**, and this is not a breach of §5.0. That rule protects tenancy on rows
  with no correct backfill; these rows are keyed by user and endpoint, carry no business
  meaning, and are deleted after 24 hours — there is never a migration to get wrong,
  because the data does not survive to be migrated.

> A repeat with the same key and the **same** body replays the stored response and creates
> nothing. The same key with a **different** body is a client bug, not a retry, and is
> refused with 422 `IDEMPOTENCY_KEY_REUSED` rather than silently returning someone else's
> answer.

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

> **`outlet_id` on a row describing *global* reference data means the outlet whose admin made
> the change.** Phase 11 amendment. `fuel_types` is the one audited table with no `outlet_id`
> of its own (§5.1 — a litre is a litre at every outlet), while this column is `NOT NULL`. The
> audit row therefore carries `actor.outlet_id`: *who did this, acting where*, not *which
> outlet owns the row*. In V1 there is one outlet and the distinction is invisible. The day
> there are two, an admin at outlet B adding XP-95 writes a row stamped B for a fuel that
> belongs to everyone — and a reader filtering by outlet A would conclude, from an entirely
> correct query, that it never happened.
>
> Making the column nullable was rejected: it would weaken §5.0's tenancy guarantee on the one
> table that has no other tenancy signal, to accommodate a single case.

> **`record_id` is deliberately not a foreign key** (it points at rows in many tables, and it
> has to survive its target being restructured). The consequence is a read-side one, and it is
> a decision rather than an oversight: a query for an id that never existed returns an **empty
> page, not a 404**. Nothing can tell the difference between "no such row" and "that row was
> never changed", and inventing a 404 would claim knowledge the table does not have.

### 5.4 Relationship summary in plain English

- A **shift** has many nozzle readings, collections, credit sales, expenses, deposits,
  non-fuel sales and shortfalls.
- A **nozzle reading** belongs to exactly one shift and one nozzle.
- A **credit sale** belongs to one shift, one customer, and **must** have one attachment.
- A **credit repayment** belongs to one customer and the shift in which cash arrived.
- An **expense** belongs to one shift and may have one attachment.
- A **non-fuel sale** belongs to one shift and nothing else — it has no product record (§13.2).
- A **salesman shortfall** belongs to one shift and one `user_profiles` row — **never** a
  credit customer (§13.14). A **settlement** belongs to one salesman and the shift in which
  the cash arrived.
- A **daily cash summary** aggregates one business date across **every** shift on it — one at
  this outlet, three at a 24-hour one. (This line used to say "both shifts"; §4.7 removed the
  two-shift assumption and the sentence survived it.)
- Everything financial points to the user who created it and appears in `audit_logs`.

---

## 6. Business Logic

### 6.1 Business date vs timestamps

A night shift runs (say) 20:00 to 08:00. It belongs to **one** `business_date` but its
timestamps span two calendar dates. Never do `date(created_at)` to determine the business
day. Always read `shifts.business_date`.

The business date is chosen when the shift is opened and is immutable thereafter.

This rule **stops mattering for the outlet described in §4.7**, whose single 06:00–22:00
shift never crosses midnight. It is kept in full because a 24-hour outlet's night shift
still does, and because `date(created_at)` is wrong for a further reason here: the whole
day is typed in **after the fact**, so `created_at` is frequently the *following* day.

A `business_date` in the future is always a data-entry error — trading has not happened
yet — and is rejected with 422 `BUSINESS_DATE_IN_FUTURE`. "Future" is evaluated in the
outlet's local timezone (`TZ_DISPLAY`), not UTC.

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

**It happens to be exact for the outlet in §4.7.** Its single shift starts at 06:00 IST,
which is the revision instant itself, and `rate_at`'s comparison is `<=` — so the shift
picks up the new rate and one rate covers the whole day with nothing to apportion. That is
a property of these particular trading hours, not a general guarantee: a 24-hour outlet's
02:00–10:00 shift straddles 06:00 and the approximation applies in full. Do not delete the
warning on the strength of the local case.

**Valuation and profit are separable, and §6.4 needs only the first. Phase 10 amendment.**
`sale_value` needs `rate_at`; `dealer_profit` needs `margin_at`. They are different questions
with different reference data behind them, and a caller that wants one **must not be refused
because the other is missing**.

This is not hypothetical. Petrol and diesel dealer commissions have never been entered at this
outlet (§14's open questions), so `margin_at` raises 409 `NO_MARGIN_FOR_DATE` for both. A cash
engine that priced a shift through a single function computing both figures would refuse to
reconcile **every petrol day**, on day one, because of a reference-data gap that has nothing
to do with cash.

§6.8 already settled this shape for close preconditions — *"a close precondition that
inherited that would make every petrol shift unclosable because of a reference-data gap, which
is a very confusing way to be told about a missing margin."* Reconciling the drawer is the
same argument, one phase later. So the shift valuation takes a flag for whether profit is
wanted; when it is not, no margin is looked up at all, and `margin_per_unit` / `dealer_profit`
come back as `None` — **never `0`**, which would be a plausible-looking figure and completely
wrong (§4.6, §13.7).

**A missing *price* still refuses.** A day valued at zero reconciles to a cash surplus nobody
can explain, and §5.1's helpers are right to raise rather than return null or zero.

### 6.4 Cash flow engine

```
total_sales       = metered_fuel_sales + non_fuel_sales

cash_sales        = total_sales − card_collections − upi_collections
                                 − wallet_collections − credit_sales_amount

expected_closing  = opening_balance
                  + cash_sales
                  + cash_credit_repayments      ← settlements received in cash
                  + cash_shortfall_settlements  ← a salesman paying back what he owed
                  − cash_expenses
                  − bank_deposits
                  − shortfalls_booked           ← what a salesman owes instead of holding

variance          = actual_counted − expected_closing
```

**Rules:**
- Variance is **recorded, never auto-corrected**. Do not "fix" the closing balance to
  make it match. The variance *is* the signal.
- Only `mode = cash` repayments enter this equation. UPI/bank repayments do not touch
  the drawer.
- `total_sales` is derived from nozzle readings, never entered.
- **`cash_expenses` means `expenses` rows with `mode = cash`, and only those.** §5.2 gives
  `expenses` a `mode` column precisely so this line is answerable — before Phase 7, every
  expense was implicitly cash because there was nowhere to record otherwise, and that read
  a bank-paid bill as a drawer withdrawal that never happened. A `card` / `upi` /
  `bank_transfer` expense is on the record for reporting but contributes nothing to this
  equation, the same way a `card`/`upi`/`wallet` collection contributes nothing to
  `cash_sales` except through the subtraction already shown.

> **`other_cash_income` was renamed and moved, Phase 10.** It read
> `+ other_cash_income ← non-fuel sales (V1: manual entry)`, on the *cash* side. That is
> only correct when every non-fuel sale is paid in cash, and it is silently wrong otherwise.
>
> Take a ₹500 bottle of oil paid by **card**, on a day of ₹95,000 metered fuel, ₹20,500 on
> the card machine (₹20,000 fuel + the oil), ₹10,000 UPI and ₹5,000 udhaar. The salesman
> actually holds ₹60,000. The old wording gives
> `95,000 − 20,500 − 10,000 − 5,000 = 59,500`, plus ₹0 of *cash* oil income — **₹59,500,
> understated by exactly the card-paid oil**. Adding it to `total_sales` instead gives
> `(95,000 + 500) − 20,500 − 10,000 − 5,000 = ₹60,000`.
>
> The sales-side form is also right in the all-cash case —
> `(95,000 + 500) − 20,000 − 10,000 − 5,000 = ₹60,500`, which is what he holds. **It is
> correct regardless of how the non-fuel sale was paid**, which is why it needs no mode
> column of its own: the collections rows already record how the money arrived.

> **Why `shortfalls_booked` is subtracted, Phase 10.** Without it the same money is an asset
> twice. Monday's meters imply Ramesh should hand over ₹50,000; he declares ₹49,500; a
> manager books ₹500 as udhaar against his own name (§14). The locker physically gains
> ₹49,500 — but this equation adds the **derived** ₹50,000, so `expected_closing` says
> ₹50,000 and Tuesday opens ₹500 rich. That ₹500 is now both Ramesh's debt *and* cash that is
> not in the locker, and every count from then on is off by it with nothing to explain why.
>
> `derived − shortfall` is algebraically identical to the declared figure, which is the
> reassurance that this is arithmetic rather than a fudge. It is written as a **subtraction**
> deliberately: §14 forbids summing the `cash` collection row into a derived figure, and this
> form means the equation **never reads that row at all**. The `cash` row stays what §5.2
> says it is — the independent observation the derived figure is checked against.
>
> **When no shortfall is booked, nothing is subtracted.** The gap then resurfaces at the next
> physical count as a variance with nobody's name on it. That is the correct outcome of a
> manager choosing not to book, not a hole — §4.7's principle that the abnormal day becomes
> visible rather than reassigned.

### 6.5 Rolling balance

`opening_balance` for day N = `expected_closing`... **no.**

`opening_balance` for day N = **`actual_counted`** of day N−1 — **whenever day N−1 was
actually counted.**

This is important and easy to get wrong. The physical cash actually in the drawer is
what carries forward, not the theoretical figure. Otherwise a ₹200 shortage on Monday
silently disappears instead of being visible in Monday's variance and absent from
Tuesday's opening.

**But this outlet has a locker, not a drawer, and nobody counts it nightly.** §14 records the
answer to "who physically counts the cash, and at what time?": *there is no fixed counting
moment.* The salesman reconciles his own shift, puts the cash in the locker, and the locker
carries a running balance that rolls forward on any day with no bank deposit. So on most days
`actual_counted` is null — and the original rule, which refused to finalise a day whose
predecessor had no count, would have blocked **every day forever**.

The rule is therefore stated in full as:

```
opening_balance(day N) = actual_counted(day N−1)      if day N−1 was counted
                       = expected_closing(day N−1)    if it was not
                       = <admin-seeded figure>        if there is no day N−1 at all
```

This is **§4.7's chain applied to money**: the system predicts, a human occasionally
confirms, **both values are stored**, and a disagreement is recorded rather than absorbed.
The paragraph above survives intact, because on every day a physical figure exists it is
still the one that wins. A physical count is an occasional **audit that re-anchors the
chain**, exactly as a confirmed meter reading re-anchors §4.7's.

- **`PRIOR_DAY_NOT_RECONCILED` (409) keeps its code and narrows its meaning**: day N cannot
  be finalised while day N−1 exists and is not finalised. It no longer fires merely because
  nobody counted.
- The very first day requires a manually seeded opening balance (admin-only, one-time).
  **The anchor is that first summary row itself, not a separate record** — §4.7's argument
  transplanted. When no prior summary exists, `opening_balance` becomes a *required* payload
  field and the caller must be an admin (403 `OPENING_BALANCE_REQUIRES_ADMIN` otherwise).
  Supplying one when a prior day *does* exist is refused with 409
  `OPENING_BALANCE_IS_CHAINED` — the figure is derived, not typed. A separate seed table
  would duplicate a value that already lives on that first row, and the two copies would
  eventually disagree about where the locker started.
- `daily_cash_summaries.opening_balance_source` records which of the three branches produced
  the figure, so a reader never has to infer it from the previous row.
- **Days must be reconciled oldest first.** A business date is refused with 409
  `EARLIER_DAY_NOT_RECONCILED` while any earlier date that traded has no summary row.
  **Phase 15 amendment.**

> **Why this is a rule and not merely good practice.** `cash.py::previous_summary` is *the
> most recent summary before this date* — deliberately, since §4.7's argument about a nozzle
> applies here too and "literally yesterday" would snap the chain on a day the outlet was
> shut. But the same looseness means reconciling 3 July before 2 July chains 3 July's opening
> from **1 July**, quietly skipping a whole day's cash. Nothing recomputes it afterwards:
> §5.2 stores `expected_closing` precisely so that a later correction cannot rewrite it, and
> §13.16 says a reopened shift *flags* the summary rather than moving it. So the wrong
> opening is permanent, it propagates into every day after it (§6.5 chains), and it is
> invisible — the figure is plausible and the arithmetic is internally consistent.
>
> Note it is **`EARLIER_DAY_NOT_RECONCILED`, not `PRIOR_DAY_NOT_RECONCILED`.** The two govern
> different acts and must stay distinguishable: the older code refuses to *finalise* day N
> while day N−1 is not finalised; this one refuses to *create* day N's summary at all while
> an earlier trading day has none. Reusing the code would make a 409 unanswerable — a caller
> could not tell which step to go and do.
>
> "Traded" means *has at least one shift*. A date the outlet was shut has no shift and is
> therefore no obstacle, which is the same distinction §13.20's `no_trading` source draws.

### 6.6 Udhaar (credit) rules

- **A credit sale cannot be created without a valid `attachment_id`.** Enforced by
  `NOT NULL` FK at the DB level *and* validated at the API level (the attachment must
  exist and have been uploaded by an authenticated user). Belt and braces: a client
  can bypass JavaScript, but not a database constraint.
- Outstanding balance for a customer = `SUM(credit_sales.amount) − SUM(credit_repayments.amount)`.
  Compute it; do not maintain a denormalised running total in V1 (it will drift).
  **Sum every row, reversals included** — they carry negative amounts and net out on their
  own. Do not filter to "live" rows here: a reversal that has not yet been replaced must show
  as the reduction it is, which is the same convention `totals_by_category_range` follows.
- **Outstanding may legitimately be negative.** A customer who pays in advance, or overpays
  a bill by rounding up, is owed money by the pump. A repayment larger than the outstanding
  balance is **accepted**, not refused. Recorded here so it reads as a decision rather than
  an oversight the first time someone sees a minus sign.
- **The credit limit check, stated unambiguously** (the original wording — "a new sale would
  exceed outstanding + amount" — inverted the comparison):

  ```
  if credit_limit is not None and outstanding + amount > credit_limit:
      -> 409 CREDIT_LIMIT_EXCEEDED
  ```

  **Strictly `>`**, matching §6.7's and §6.11's boundary convention: landing exactly on the
  limit is allowed, one paisa over is not. **`credit_limit IS NULL` means no limit** and must
  never be coerced to `0` — that would refuse every sale to an unlimited customer.
- **An admin may override the limit**, and the mandatory reason is stored on the row
  (`credit_sales.limit_override_reason`) **as well as** being audit-logged. A non-admin
  supplying one is refused with 403 `LIMIT_OVERRIDE_REQUIRES_ADMIN`.

  > Stored on the row, not audit-only. §14 already settled this argument for the analogous
  > manual-quantity override: *"`override_reason` is mandatory in the database, so it can
  > never be an unexplained number."* Nothing reads `audit_logs` at report time; a column is
  > visible next to the figure it explains.
- **The limit check is not serialised.** Two sales issued in the same instant can both read
  the same outstanding balance and both pass. See §13.13 — this is a known approximation with
  a stated reason, not an omission.

### 6.7 Expense review flagging

- Threshold is a **config value** (`EXPENSE_REVIEW_THRESHOLD`, default `1000.00`),
  not a hardcoded literal. Changing it must not require a deploy.
- Flag when `amount > threshold` → `requires_review = true`.
- **Also flag** when the sum of a single category for one `business_date` exceeds
  the threshold. A single ₹1,000 rule is trivially defeated by two ₹600 entries;
  without this, the control is theatre.
- **The aggregate rule flags every live, unreviewed row in that
  `(business_date, category_id)` group, not only the row that crossed the line.** (Grouped
  on `category_id` since Phase 8 made categories a table — §5.1. The rule is unchanged; only
  what identifies a category is.) Three maintenance entries of ₹400, ₹400
  and ₹500 cross ₹1,000 together; flagging only the ₹500 row shows a manager a trivial
  amount and hides the ₹1,300 pattern the rule exists to surface. "Live" excludes a
  reversed row and the reversal that cancels it — §6.9's correction, not a fourth expense.
- **The aggregate check re-runs whenever a row in the group changes** — on create, on a
  `PATCH` to the amount, and on a reversal — not only at insert. Otherwise two ₹300
  entries (₹600, under the line) followed by an edit of one to ₹800 (₹1,100) never trips
  it.
- **Flags are never auto-cleared**, including when a reversal drops a group back under the
  threshold. Auto-clearing would erase a control signal silently; a human clears a flag
  through the review route, the same shape as §13.10's downstream reading flag.
- **A reversed expense does not block a lock.** It is money a manager formally cancelled;
  both rows stay in the audit trail, but blocking a lock on cancelled money is friction with
  no control value.
- A shift **cannot be locked** while it has unreviewed flagged expenses.
  Return 409, code `UNREVIEWED_EXPENSES_EXIST`.

### 6.8 Shift closing preconditions

Reject shift close (409) if any of:
- Any active nozzle lacks a `closing_reading` → `MISSING_NOZZLE_READINGS`
- Any credit sale lacks a confirmed attachment → `CREDIT_SALE_MISSING_RECEIPT`
- Fuel moved and **no `cash` collection has been declared** → `MISSING_COLLECTIONS`

> **This fires on absence, never on a mismatch.** A shift whose collections total ₹40,000
> against ₹95,000 of metered sales closes normally: udhaar issued during the shift accounts
> for part of that gap and the rest is the variance §6.4 exists to record. Refusing to close
> until the numbers agree would leave the salesman in front of a form with exactly one
> freely adjustable field, and he will type whatever balances it. The system would then be
> perfectly reconciled and worthless.
>
> An **explicit ₹0** satisfies the check. On a day that genuinely took no cash the salesman
> declares zero, and that is a different fact from having entered nothing — the same
> distinction §4.7 draws about an assumed opening reading. Zero as an answer, never zero as
> an omission.
>
> "Fuel moved" is read from `nozzle_readings` alone, never by pricing the shift. §6.3's
> valuation refuses with `NO_PRICE_FOR_DATE` / `NO_MARGIN_FOR_DATE` when a rate is missing,
> and a close precondition that inherits that would make a shift unclosable because of a
> reference-data gap.

Locking (admin-only) additionally requires `status == closed` (otherwise 409
`SHIFT_NOT_CLOSED`) and all flagged expenses reviewed.

All three close preconditions read tables that do not exist until later phases —
`nozzle_readings` (5), `collections` (6), `credit_sales` (9) — and the lock precondition
reads `expenses` (7). Each lands **with its own phase**. Phase 4 builds the lifecycle and
leaves a named comment at the insertion point rather than an empty stub, per §11's rule
against scaffolding ahead.

**Reopening.** An admin may move `closed → open` with a mandatory reason, audit-logged
(§5.2). `locked` is terminal and is refused with 409 `SHIFT_LOCKED` — otherwise locking
guarantees nothing.

**Any** closed shift may be reopened, including one in the middle of the chain. Phase 4
refused a mid-chain reopen (409 `NOT_THE_LATEST_SHIFT`) because the following shift's
chained opening (§4.7) would be left stale; Phase 5 lifted that by **flagging** the stale
reading for review rather than recomputing it, which would have been the silent rewrite
§4.7 exists to prevent. `NOT_THE_LATEST_SHIFT` no longer exists. See §13.10.

### 6.9 Corrections after close

No `UPDATE` and no `DELETE` on financial rows in a `closed` or `locked` shift.
Corrections create a **reversal entry**: a new row with the negated amount, a
`reverses_id` FK to the original, and a mandatory reason. Both rows remain visible.

This is how double-entry accounting has worked for 600 years and it is the only way
to answer "who changed this, when, and what was it before".

**Tables carrying this shape**, one more per phase: `collections` (6), `expenses` (7),
`credit_sales` and `credit_repayments` (9). Every one of them brings a
`uq_<table>_reverses_id` whose only job is to lose a concurrent double-reversal loudly —
**and every one of them must have an entry in `app/core/errors.py::_CONSTRAINT_ERRORS`**, or
the losing caller gets an opaque 500 and cannot tell whether their reversal landed. This has
now been forgotten twice (Phase 6 on `collections`, Phase 7 on `expenses`), which is why
`tests/test_errors.py` asserts it structurally against `pg_constraint` rather than trusting
anyone to remember. Phase 9 widened that test past reversals to **every** check-then-insert
unique constraint, which is the real bug class.

**A `credit_sales` reversal inherits the original's `attachment_id`** rather than needing a
receipt of its own — see §5.2. A cancellation is not a spend, and §6.11 already refuses to
demand a photograph for one.

### 6.10 Idempotency

Every `POST` that creates a money record accepts an `Idempotency-Key` header.
Store `(key, endpoint, user_id) → response` for 24 hours. A repeat with the same key
returns the original response without creating a second row.

**This is not optional.** Attendants use phones on patchy rural connectivity. A retry
after a timeout must not create a duplicate ₹5,000 expense.

**Built in Phase 6, with `collections` — the first table that needs it.** A nozzle reading
is already idempotent by construction: `UNIQUE (shift_id, nozzle_id)` means a retried POST
cannot create a second row, it returns 409 `READING_ALREADY_EXISTS` and the client PATCHes
instead. A collection has no such natural key — two ₹5,000 cash collections in one shift
are both legitimate — so a timed-out retry genuinely does duplicate money there. Building
the store in Phase 5 for a POST that cannot duplicate would be scaffolding ahead (§11).

**The upload endpoint (§7.2) deliberately takes no `Idempotency-Key`.** An attachment is not
a money record. A retried upload creates a second row the client simply does not use, and
§7.4's sweep reclaims it within 24 hours — whereas fingerprinting a 5 MB multipart body to
prove two uploads are "the same request" is worse than the problem. The step where a retry
*would* duplicate money is the linking one, inside `POST /shifts/{id}/expenses`, and that
has carried a key since Phase 7.

### 6.11 When an expense needs a receipt

**Phase 8.** A receipt has never been mandatory on an expense — §5.2 makes
`expenses.attachment_id` nullable, and only `credit_sales.attachment_id` is `NOT NULL`
(§6.6). This is the rule that decides when one *is* required:

```
receipt_required = category.requires_receipt  OR  amount > EXPENSE_RECEIPT_THRESHOLD
```

- `EXPENSE_RECEIPT_THRESHOLD` is a **config value** (default `5000.00`), never a literal —
  same rule as §6.7's threshold.
- It is deliberately **a different number from `EXPENSE_REVIEW_THRESHOLD`**. "A manager
  should look at this" and "this needs paper proof" are different questions and deserve
  independent dials; sharing one would weld them together forever.
- The comparison is strictly `>`, matching §6.7. Exactly at the threshold does not require a
  receipt; one paisa over does.
- Refuse with 422 `EXPENSE_REQUIRES_RECEIPT` when the rule demands an attachment and none is
  supplied.

**Why the category flag alone is not enough.** Tea must be frictionless — demanding a
photograph for a ₹20 chai run is friction that teaches staff to fake it, which is the failure
mode this whole document exists to prevent. But a category marked "no receipt" then becomes
the obvious place to file something large. The threshold closes that: a ₹50,000 anything
needs paper, whatever it was filed under.

**The answer is stored on the row, not recomputed.** `expenses.receipt_required` is evaluated
**once, at insert**, and saved. It is never read back off `expense_categories` when validating
or reporting on an existing expense.

> This is the same reasoning §5.2 gives for storing `expected_closing`. `requires_receipt` is
> editable — that is the point of §5.1's table. The day an admin flips `MAINTENANCE` to
> receipt-required, a recomputed rule would retroactively declare every historical
> maintenance expense non-compliant, and the system would start reporting a control failure
> that never happened. Store what the rule was that day.

**Re-evaluated on a `PATCH` that changes the amount**, for the same reason §6.7's aggregate
re-runs there: entering ₹900 and editing up to ₹9,000 must be able to start demanding a
receipt, or the threshold is defeated by a two-step entry. If the row has no attachment, the
`PATCH` is refused.

**A reversal never needs a receipt.** A reversal row (§6.9) is a cancellation, not a spend;
there is nothing to photograph. Its *replacement* does need one — and inherits the original's
attachment rather than demanding a second photo of the same paper (§5.3).

Enforced at the database as well as in the application (§6.6, belt and braces):

```sql
CHECK (reverses_id IS NOT NULL OR receipt_required = false OR attachment_id IS NOT NULL)
```

---

## 7. File Upload Flow

### 7.1 Pattern choice for V1: proxy through FastAPI

The file passes through the API server on its way to Supabase Storage.

**Why not presigned URLs?** Presigned direct-to-storage upload is the better pattern
at scale and should be V2. For one pump, images under 5 MB, and a developer learning
backend basics, proxying is materially simpler, synchronous, and easier to reason
about and test. KISS. Document this as a known trade-off, not an oversight.

### 7.2 Sequence

1. Client `POST`s `multipart/form-data` to `/api/v1/uploads/receipt`, carrying the file and
   a **required `shift_id` form field**.
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

> **Where `shift_id` comes in, and why it is not a column.** Step 4's path needs a
> `business_date`, but at upload time no expense or credit sale exists yet to read one off.
> The three candidates were a client-supplied date, "today", and the shift.
>
> **"Today" is wrong here specifically.** §4.7 says the whole day is typed in *after the
> fact*, so an upload routinely happens on the following calendar day and the receipt would
> file under a date the register never mentions — the same trap §6.1 exists to name. A
> client-supplied date is a derived value the client chose, which §3 rule 7 says to recompute
> server-side.
>
> So the upload takes `shift_id` and the server reads three things off that shift: its
> already-validated, immutable `business_date`, its `outlet_id`, and §8's ownership check —
> by reusing the shift-scoped write dependency unchanged, so an attendant can upload only
> against their own open shift and a closed or locked shift refuses uploads for free.
>
> **`shift_id` is not stored on the `attachments` row.** §5.3 has no such column and does not
> gain one. It is an input that resolves three values, not a fact about the file — and a
> receipt is not owned by a shift, it is owned by the business row that eventually links it.

> **The upload takes no `Idempotency-Key`.** See §6.10's closing note for why.

### 7.3 Reading files back

The bucket is **private**. Never make it public.
`GET /api/v1/attachments/{id}/url` checks the caller's permission and returns a
short-lived signed URL (5 minutes). Expired links are useless if leaked.

**The permission rule**, following §8's two axes — role first, then ownership, with ownership
applied only when the caller is an `attendant`:

- **Manager or admin** at the attachment's outlet → allowed, any attachment.
- **Attendant** → allowed only if they uploaded it, **or** it is linked to a business row on
  a shift whose `attendant_id` is theirs. Otherwise 403 `NOT_YOUR_ATTACHMENT`.
- An id belonging to **another outlet** returns **404, not 403** — existence is not leaked
  across tenants.

An attendant needs the uploaded-it case as well as the linked-to-their-shift one, because
between §7.2's steps 5 and 6 the attachment is linked to nothing at all, and they must still
be able to see what they just uploaded.

### 7.4 Housekeeping

A scheduled job deletes `attachments` rows (and their storage objects) where
`linked_at IS NULL AND created_at < now() - interval '24 hours'`.
These are abandoned uploads. V1: a management command run manually or via cron.
Do not build a job scheduler.

> **This hard-deletes, and that is not a breach of §3 rule 6.** That rule protects *financial*
> tables. `attachments` is not one, and an unlinked row is by definition referenced by no
> financial row — that is exactly what `linked_at IS NULL` means. **A linked attachment is
> never deleted, at any age**, including one whose expense was later reversed: §6.9 keeps
> both rows, so the receipt stays evidence.
>
> **Delete the storage object first, then the row.** If the object delete fails, the row
> survives and the next run retries it. If the row delete fails after the object is gone, the
> next run finds a row whose object no longer exists — so **the object delete must treat a
> 404 as success**, or the sweep wedges permanently on one bad row.

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
| Create readings/collections/expenses/credit sales/repayments on an open shift | own only | any | any |
| Record a non-fuel sale on an open shift (§6.4) | own only | any | any |
| Upload a receipt against an open shift (§7.2) | own only | any | any |
| Read a receipt's signed URL (§7.3) | own only | any | any |
| Read own shift | ✅ | ✅ | ✅ |
| Read all shifts / reports | ❌ | ✅ | ✅ |
| Read the month-end expense summary | ❌ | ✅ | ✅ |
| Read the daily report (§13.20) | ❌ | ✅ | ✅ |
| Read the rolling range report | ❌ | ✅ | ✅ |
| Read the variance alerts (§13.23) | ❌ | ✅ | ✅ |
| List expense categories (to fill a dropdown) | ✅ | ✅ | ✅ |
| List credit customers (to fill a dropdown — **name and vehicles only**, §9) | ✅ | ✅ | ✅ |
| Read one customer's detail, outstanding balance, or ledger | ❌ | ✅ | ✅ |
| List the outlet roster (to fill a picker — **name and role only**, §13.26) | ❌ | ✅ | ✅ |
| Read one user's detail, including their phone | ❌ | ❌ | ✅ |
| Close a shift | ❌ | ✅ | ✅ |
| Record bank deposits | ❌ | ✅ | ✅ |
| Review flagged expenses | ❌ | ✅ | ✅ |
| Read a shift's cash position (§6.4) — it is a report, not a data-entry sheet | ❌ | ✅ | ✅ |
| Book a salesman shortfall, or record a settlement (§13.14) | ❌ | ✅ | ✅ |
| Read the shortfall ledger / who owes what | ❌ | ✅ | ✅ |
| Create or update a daily cash summary, incl. `actual_counted` | ❌ | ✅ | ✅ |
| Lock a shift / finalise a day | ❌ | ❌ | ✅ |
| Unfinalise a day (mandatory reason, audit-logged) | ❌ | ❌ | ✅ |
| **Read the audit log (§5.3)** | ❌ | ❌ | ✅ |
| Enter fuel prices and margins | ❌ | ❌ | ✅ |
| Manage fuel types (add a new product, e.g. XP-95) | ❌ | ❌ | ✅ |
| Manage expense categories, incl. `requires_receipt` (§5.1, §6.11) | ❌ | ❌ | ✅ |
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

> **Why reading the audit log is admin-only, not manager.** Phase 11 amendment. Every other
> read at the manager floor is a *report* — shifts, the cash position, the month-end expense
> summary. §5.3 frames `audit_logs` as the **control record** instead, and it is partly the
> record of what managers did.
>
> It is also a leak boundary, which is the half that is not a matter of taste. `old_values` /
> `new_values` on a `credit_customers` row contain `phone` and `credit_limit` — precisely the
> two fields the table above forbids an attendant from seeing, and which §9 restricts to
> manager-and-above only for the customer's *own* detail route. A manager floor here would
> expose one table's restricted columns through a different endpoint, so admin-only is what
> keeps §8 and §9 consistent rather than merely cautious.

> **Why the roster sits at the manager floor and not the attendant floor.** Phase 14
> amendment. It reads like an inconsistency next to the credit-customer dropdown one row
> above, which every role may list — but the two dropdowns have different callers.
>
> An attendant has **nothing to pick a colleague for**. They cannot read another attendant's
> shift (`NOT_YOUR_SHIFT`), so no name ever needs resolving on their screen, and
> `POST /shifts` refuses them a foreign `attendant_id` outright, so the picker would offer
> them a list of choices the server would reject. The only caller who needs the roster is
> the manager opening a shift in a salesman's name. Nobody below that floor has a use for
> it, and staff detail defaults above the attendant floor for the same reason the
> customer's phone does.

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

> **The audit log's sort key is `(changed_at DESC, id DESC)`** — Phase 11 amendment — and it
> reuses `encode_cursor` / `decode_cursor` **unchanged**. `app/api/cursor.py` already states
> that the pair "serves any `(TIMESTAMPTZ, UUID)` sort key, not only `effective_from`", and
> Phase 7 reused it for `created_at`. A third encoder would be exactly the drift that module
> exists to prevent: a cursor issued by one copy and parsed by a divergent one is a genuinely
> nasty bug to find.

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
- Cash udhaar repayment increases expected cash; UPI repayment does not; `bank_transfer`
  does not
- A `cash` expense reduces expected cash; a `card` / `upi` / `bank_transfer` expense does not
- Rolling balance uses prior day's **actual counted**, not expected, **whenever a count
  exists** (§6.5)
- With no prior count, the opening carries from the prior day's `expected_closing` and
  `opening_balance_source = carried`
- A count that disagrees **re-anchors**: the next day opens at the counted figure and
  `opening_balance_source = counted`
- The very first day requires an admin-seeded opening; a manager is refused 403
  `OPENING_BALANCE_REQUIRES_ADMIN`; supplying one when a prior day exists → 409
- Day N cannot finalise if day N−1 is unreconciled → 409
- `variance` is NULL when nothing was counted, and is never auto-corrected into
  `expected_closing`
- **A ₹200 shortage on Monday appears in Monday's variance and is absent from Tuesday's
  opening** — §6.5's worked example, as a test
- Creating a summary with an open shift on that date → 409; finalising with a merely *closed*
  shift → 409 (§5.2)
- A shift reopened beneath a finalised day **flags** it and leaves `expected_closing`
  byte-identical (§13.10)
- A day containing petrol with a price but **no margin** reconciles end to end — no
  `NO_MARGIN_FOR_DATE` anywhere in the cash path (§6.3)
- A **missing price** still refuses; `price_only` leaves profit as `None`, never `0`

*Non-fuel sales*
- A **cash** non-fuel sale raises expected cash by its amount
- A **card-paid** non-fuel sale leaves expected cash unchanged, and the salesman shows **no
  phantom surplus** (§6.4's worked example)
- Removing the non-fuel row makes the salesman look short by exactly that amount
- A non-fuel sale is reversible under §6.9 and the reversal nets out of the day

*Shortfalls*
- The cash position endpoint returns every term and the gap, and **writes nothing**
- A shortfall is attributed to `shifts.attendant_id`; a client-supplied salesman is refused
- Booking an amount different from `computed_gap` succeeds and **logs a warning**
- **A booked shortfall reduces `expected_closing` by exactly its amount** (§6.4)
- An **unbooked** gap does not reduce it, and resurfaces at the next count
- `outstanding(salesman)` correct after a partial settlement, a reversed shortfall, and a
  reversed settlement
- A settlement larger than outstanding is accepted; the balance goes negative
- A cash settlement increases expected cash on the shift it arrived in
- **A shortfall is never a `credit_sale`** — no `credit_customer` row is created, and §14's
  guardrail comment is still present in the credit router

*Credit*
- Credit sale with no `attachment_id` → rejected
- Credit sale with a non-existent `attachment_id` → rejected, and **no row written**
- Credit sale with an attachment already claimed by a live expense → 409
  `ATTACHMENT_ALREADY_LINKED`, and the same in the other direction
- Credit sale with an attachment from another outlet → 404, not 403
- Credit sale exceeding credit limit → 409; admin override succeeds, the reason lands on the
  row, and an `audit_logs` entry carries it
- A non-admin supplying `limit_override_reason` → 403 `LIMIT_OVERRIDE_REQUIRES_ADMIN`
- **Boundary:** outstanding + amount exactly equal to the limit is accepted; one paisa over
  is refused. `credit_limit IS NULL` is unlimited
- Outstanding balance correct after a partial repayment
- Outstanding balance correct after a **reversed sale** and after a **reversed repayment** —
  the negative rows net out (§6.6)
- A repayment larger than outstanding is accepted; the balance goes negative
- A deactivated customer refuses a new sale but **accepts a repayment**; historical rows
  still read and report
- A `credit_sales` reversal carries the original's `attachment_id`; no re-upload is demanded
- A quantity with no `fuel_type_id` is refused — a measure with no unit is meaningless (§4.5)
- A CBG credit sale's quantity resolves as `kilogram`; nothing in the credit path assumes
  litres
- Two customers cannot share a phone at one outlet; the same phone at a *different* outlet is
  fine (the constraint is outlet-scoped)
- An attendant listing customers sees no `phone`, no `credit_limit` and no balance

*Expenses*
- Boundary: ₹999.99 not flagged, ₹1000.00 not flagged, ₹1000.01 flagged
  (confirm the intended comparison is strictly `>`)
- Two ₹600 same-category same-day expenses → category aggregate flag fires
- Shift lock blocked while an unreviewed flagged expense exists

*Expense categories and receipts (§5.1, §6.11)*
- A category's `code` cannot be changed after creation; `display_name`,
  `requires_receipt` and `is_active` can
- A deactivated category refuses a **new** expense but historical rows still read and report
- Non-admins cannot create or edit a category; every role can list them
- `requires_receipt = false` and under threshold, no attachment → accepted
- `requires_receipt = true` and no attachment → 422
- `requires_receipt = false` but amount over `EXPENSE_RECEIPT_THRESHOLD` → 422
- Boundary: exactly at the threshold does not require a receipt; one paisa over does
- A `PATCH` raising the amount past the threshold starts requiring one
- **Flipping a category to `requires_receipt = true` does not invalidate historical rows** —
  the snapshot holds
- A reversal needs no receipt; its replacement inherits the original's attachment

*Auth*
- Attendant writing to another attendant's shift → 403
- Attendant closing a shift → 403
- Manager locking a shift → 403

*Uploads*
- Oversized file → 413
- PDF renamed to `.jpg` → rejected by content sniffing
- HEIC → specific, actionable error message
- Unlinked attachment older than 24h is cleaned up
- **A linked attachment is never cleaned up, at any age**
- The client-supplied filename appears nowhere in the storage path — test one containing
  `../` and one containing spaces
- The stored path's date segments are the **shift's `business_date`**, not today
- A second business row claiming one attachment → 409
- Attendant reading another attendant's receipt → 403; an id from another outlet → 404
- Storage failing mid-upload → 502, and no `attachments` row is left behind

*Idempotency*
- Same `Idempotency-Key` twice → one row, identical response both times

*Immutability*
- Any write to a `locked` shift → 409

*Audit trail (§5.3, Phase 11)*
- Every admin write to reference data records exactly one `audit_logs` row — asserted per
  endpoint, never as one loop over a list (a loop that silently skips is the failure mode)
- A create records `action = insert` with `old_values` NULL; a `PATCH` records
  `action = update` with **both** sides populated and genuinely different
- **A refused write records nothing** — a 409 duplicate, a 422 immutable field and a 403
  non-admin each leave `count(*) == 0`. This is what proves the audit row and the change share
  one transaction
- No reference-data write records `status_change` — that label means a *shift* lifecycle move
- `changed_by` is the **acting** admin, not the row's `created_by`; test the case where they
  differ
- Money inside `old_values` / `new_values` round-trips as a **string**, never a float, and a
  null `credit_limit` stays null rather than becoming `0`
- The read endpoint is **admin-only** and **outlet-scoped**; a row from another outlet is
  absent, asserted by inserting one rather than by inferring from an empty page
- An unknown `record_id` returns an empty page, not a 404; an invalid `action` returns 422,
  not an empty page
- Paging with a row inserted mid-walk neither repeats nor skips — the failure `OFFSET` causes
- **A structural test fails when any new `@router.post` / `@router.patch` lacks an
  `audit.record` call**, parsed with `ast`, with a short explicit exemption list

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
4. **Shifts** — open/close/lock/reopen lifecycle and status guards, `outlet_shift_templates`,
   the shift-scoped ownership dependency (§8), and **`audit_logs`** (moved up from 11)
5. **Nozzle readings & sales math** — the whole of §6.2 and §6.3, heavily tested,
   plus §4.7's chain: the carried-forward opening and its confirm-don't-assume guard.
   Also lands §6.8's `MISSING_NOZZLE_READINGS` close precondition and §13.10's
   flag-don't-recompute resolution
6. **Collections**
7. **Expenses** + flagging rules
8. **Expense categories & attachments** — `expense_categories` replaces the category enum
   (§5.1); upload, validation, signed download and cleanup (§7); §6.11's conditional receipt
   rule, which is what the first two exist to make enforceable; and a month-end
   category-wise expense summary. Scope grew from "attachments" because §6.11 needs both
   halves, and splitting them would mean writing `expenses.py` twice
9. **Credit** — customers, sales (receipt-enforced), repayments, outstanding balance
10. **Cash engine** — daily summary, expected vs actual, rolling balance
11. **Audit retrofit & the audit trail endpoint** — the *table* was built in Phase 4, not
    here. The invitation above was taken: §5.2 requires backwards status transitions to be
    audit-logged and §6.8 lets an admin reopen a shift, so Phase 4 is the first phase that
    cannot be correct without it. **This slot is what was left behind**, and it is two things:

    **(a) Retrofit audit writes onto every admin-managed reference-data endpoint.** This
    line used to name four — fuel types, nozzles, prices, margins — and call it "genuinely
    optional". **Both halves of that were wrong**, and the sentence is older than the gap it
    describes: it was written in Phase 1, before `outlet_shift_templates` (4),
    `expense_categories` (8) and `credit_customers` (9) existed. All seven have the same
    defect — **zero** `audit.record` calls across twelve write endpoints — and three of them
    gate money rules directly. `expense_categories.requires_receipt` is the knob §6.11 exists
    to give the admin; `credit_customers.credit_limit` decides what §6.6 refuses, so raising
    it is how an over-limit sale becomes a legal one; `outlet_shift_templates.starts_at_local`
    supplies the instant §6.3 prices a whole shift from. It is not optional, because §5.3 says
    a cash system requires an audit trail and `created_by` is not one.

    The append-only tables are the sharpest case: `fuel_prices` and `fuel_margins` exist in
    that shape *because* §4.1 says a mutable price column "silently corrupts every historical
    report" — yet a **backdated** `effective_from`, which the router permits and merely warns
    about, can revalue a closed shift with nothing recording who entered it.

    **(b) `GET /audit-logs`, admin-only (§8, §9).** Nothing could read the table back. Every
    phase since 4 has written to a trail whose only reader was a raw SQL prompt, and a trail
    nobody can read is not one.

    What makes this durable is neither of the above but a **structural test**: an `ast` walk
    asserting every `@router.post` / `@router.patch` in `app/api/v1/` calls `audit.record`.
    The gap survived seven phases because nothing failed when it was missing — the same reason
    §6.9 gives for `_CONSTRAINT_ERRORS` being forgotten twice, and the same fix.
12. **Frontend** — static HTML/CSS/vanilla JS, served by `StaticFiles`, **no build step**.
    This line used to read "minimal HTML/CSS/JS forms and tables", which is still the scope:
    forms and tables over the endpoints eleven phases have already built, adding no business
    rule of its own. What it understated is that **this is the first phase a human being
    touches.** Every rule in this document has been verified against a test client and never
    against a salesman with a phone, and §4.7's own argument — the day is typed in after the
    fact, in one sitting, by somebody who would rather be elsewhere — makes the interface a
    control rather than a decoration. §6.8 says as much in the other direction: a form that
    fights its user teaches that user to type figures that balance.

    **Coverage is every router, not the easy ones.** Nineteen screens across four role-gated
    tabs; the admin reference data and the audit log included. A structural test asserts that
    every module in `app/api/v1/` is named by a screen, discovered by directory listing, so a
    Phase 13 router shipping with no way to reach it fails the suite rather than the review —
    the same construction, and the same reasoning, as `tests/test_audit_coverage.py`.

    **Two read-only config endpoints land here**, and they exist to stop config being copied
    into JavaScript. `GET /auth-config` is unauthenticated and returns the Supabase URL and
    anon key, because a login screen cannot authenticate without them (both are public by
    design; `SUPABASE_SERVICE_KEY` never leaves the server). `GET /client-config` sits at the
    attendant floor and returns `TZ_DISPLAY` and the two expense thresholds — §6.7 and §6.11
    both say *"changing it must not require a deploy"*, and a threshold hardcoded in the client
    to warn before the server refuses would desynchronise the moment it moved.

    The motion and material vocabulary is hand-written — §14 forbids npm, so the spring,
    momentum projection and rubber-banding are about 200 lines of vanilla JS over
    `requestAnimationFrame`. See `docs/phase-12-plan.md` for the decisions and §13.18–19 for
    what this phase deliberately does not test.
13. **Reporting** — daily summary, 7-day rolling view, variance alerts. Three manager-floor
    read endpoints under `/api/v1/reports/`, three screens on the Cash tab, **no migration and
    no new table**: every figure already exists, and this phase is about *presenting* it.

    The scope is unchanged from the line above. What needed writing down is **which figures a
    report may compute**, because that is the question a reporting layer gets wrong:

    **(a) A snapshot is read, never recomputed.** §5.2 stores `expected_closing` *and every
    component* so a reader can see what the manager was told on the day. A report that
    recomputed would destroy exactly that record, and §6.5 chains days so the damage would not
    stay local. This is now pinned structurally, the same way §13.10's rule is.

    **(b) But a day nobody reconciled has no snapshot at all**, and a 7-day view that silently
    drops yesterday because nobody created a summary is a report lying by omission — the
    opposite of §4.7's *"the abnormal day becomes visible instead of reassigned."* So an
    unreconciled day **is** computed live, and every row says which it was. `source` is not
    decoration; it is the difference between *what we were told* and *what is true now*.

    **(c) The fuel breakdown is always live, even on a finalised day**, because there is
    nothing else it could be — `daily_cash_summaries` stores `metered_fuel_sales` as one
    number with no per-fuel split and no margin. That has a consequence worth stating: the
    breakdown is computed *today* beside a total frozen *then*, and a backdated price revision
    makes them disagree. §13.22 is what this phase does about it.

    Two read-only screens' worth of hand-written SVG lands here, and §14 gains a guardrail
    about it: **the server sends bar heights as CSS percentage strings**, because
    `value / max` is arithmetic on money and §3 rule 1 does not stop at the API boundary.
14. **Users** — `POST`/`GET`/`PATCH` under `/api/v1/users`, an admin Users screen, and the
    roster picker two existing screens have been missing. **No migration and no new table**,
    like Phase 13: `user_profiles` and `outlet_memberships` have existed since migration
    `0002` and this phase is about *reaching* them.

    §8's permission table has said *"Manage users, nozzles, customers — admin only"* since
    Phase 2. Nozzles landed a router in Phase 3 and customers in Phase 9; **users never did.**
    Adding a manager or an attendant still means creating them by hand in the Supabase
    dashboard, copying the UUID, and running `app/jobs/provision_user.py` from a shell.

    **The command stays**, and its own reasoning says why: *"the very first admin has no admin
    to create them. A command breaks that cycle, because anyone with shell access to the
    server is already more privileged than any API role."* That argument covers the bootstrap
    and nothing beyond it. Every *subsequent* user going through the same three-system dance
    is the gap, and it costs exactly what that file warns about — *"a mismatch produces a user
    who authenticates successfully and is then refused with `PROFILE_NOT_PROVISIONED`
    forever."*

    **(a) A user is created in two systems, and Supabase is written first**, because
    `user_profiles.id` must equal `auth.users.id` and only Supabase can say what that is. The
    identity provider therefore arrives as an `AuthBackend` protocol with a `SupabaseAuth`
    implementation and a `LocalAuth` double, structurally identical to §7.1's
    `StorageBackend` and hand-rolled for the same reason §16 gives — *"three REST calls behind
    a small interface is more boring and more testable than a client library"*. The window
    between the two writes is not transactional; §13.25 is what this phase does about it.

    **(b) The last active admin cannot be demoted or deactivated** (§13.27). The CLI guards
    this with `--force`; an HTTP endpoint has no `--force` because there is no caller left who
    could send it, so it refuses outright and names the CLI as the repair.

    **(c) Two screens stop lying.** `today.js` renders the literal string *"another
    attendant"* because nothing could turn a `user_id` into a name, and the shift-open sheet
    offers no attendant picker even though `POST /shifts` has accepted `attendant_id` since
    Phase 4 — so through the app, a manager cannot open a shift in a salesman's name at all.
    Both are consequences of the missing router rather than separate bugs, and both are fixed
    by the roster read the same endpoint provides.

    This is **not** the bank/IOCL module. §12 scopes that as one post-V1 module built together,
    after V1 is hand-tested and deployed.
15. **The Cash tab as a worklist** — no migration, no new endpoint, and **one new business
    rule** (§6.5's `EARLIER_DAY_NOT_RECONCILED`). Phase 12 built nineteen screens and Phase 13
    added three more; this is the first phase written after somebody entered a real trading
    day through them, and what it fixes is not a broken endpoint but a tab that could not be
    used to do the thing it is named after.

    The report of it was concrete. A day was entered, closed and locked, and then could not be
    found. Three defects had stacked:

    **(a) A read-only report was labelled with a write verb.** `cash.js` offered *"Reconcile
    this shift"*, whose entire action was to navigate to `GET /shifts/{id}/cash-position` —
    which §8 requires to write nothing, because it is a report. Doing exactly what the button
    said left no summary row. §14 gains a guardrail about this, because the failure is not a
    typo: it taught a manager that a day was settled when nothing had been written.

    **(b) A day that traded and was never reconciled was structurally invisible.**
    `GET /daily-summaries` is a single-table read of `daily_cash_summaries` with no join to
    `shifts`. That is correct for what it is, and it means the one list on the Cash tab could
    not show the one day that needed attention. **The fix is a client-side merge of
    `GET /shifts` with `GET /daily-summaries`, not a new endpoint** — both already exist, both
    are already manager-floor, and the join is a presentation concern.

    **(c) Reports were anchored to the calendar rather than to the trading.** See §13.30.

    So the tab stops being a menu of six reports and becomes a worklist: **the days that need
    you, oldest first**, each showing where it stands in the lifecycle and offering the one
    act that would move it on. §4.7's principle at the interface — the abnormal day becomes
    visible instead of being reassigned to nobody's attention.

    **One day, one screen.** `#/daily-summaries/{date}` and `#/reports/{date}` described the
    same business date from two different tables and never linked to each other. They merge
    into `#/days/{date}`; the old routes redirect. §13.20's distinction is untouched and stays
    on the merged screen — a `computed` day is an estimate and a `snapshot` is a record, and
    the screen must go on saying which.

    **The lifecycle is drawn, not assumed.** Entered → Closed → Locked → Reconciled →
    Finalised, as five dots on every day row. Two of those steps are per *shift* and two are
    per *day*, which is the whole reason the sequence is confusing to a newcomer, and the only
    durable fix is to show it rather than to document it. `actual_counted` is deliberately not
    a step: §6.5 says most days are never counted under the locker model, so making it one
    would mark every normal day incomplete.

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
- **Credential management of any kind: password reset, email change, invite emails, SMTP,
  login history, session listing.** Phase 14 clarification. Supabase owns the credential
  (§5.1) and V1 does not build a second place to manage it — an admin uses the Supabase
  dashboard. Phase 14 builds *authorisation* (who may act, and as what), which is this
  application's business, and deliberately stops at the boundary. Note the one deliberate
  exception: `POST /users` *sets* an initial password, because it must supply one to create
  the account at all, and it never reads one back
- Real-time updates, websockets, push notifications
- Per-transaction (per-fill) data capture
- Role-based UI theming, **a dark/light mode toggle**, i18n. **Phase 12 clarification:** all
  three of these are *per-user configurability* features — a theme that varies by role, a
  switch between two palettes, a language picker — and each costs a second code path that has
  to be maintained and tested forever. None of them describes **shipping one palette that
  happens to be dark**, which is what Phase 12 does: no toggle, no second palette, no
  persistence, no setting. It is the app's look, not a mode.

  What the single palette *does* honour, because these are accessibility signals rather than
  user preferences: `prefers-reduced-motion` (springs become cross-fades, overshoot removed,
  gesture tracking retained — reduced motion means gentler, not dead),
  `prefers-reduced-transparency` (materials go solid, `backdrop-filter` dropped) and
  `prefers-contrast: more`. Refusing those would not be scope discipline, it would be a bug
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
10. **A mid-chain reopen flags the next shift for review; it does not recompute it.**
    Phase 4 refused a mid-chain reopen outright (409 `NOT_THE_LATEST_SHIFT`) and this
    section previously said Phase 5 would lift that by implementing a recomputing cascade.
    **It does not, because it cannot:** §4.7 stores the chained opening on the row
    precisely so that "correcting one shift silently rewrites the next shift's history"
    is impossible, and a recomputing cascade is that rewrite.

    So Phase 5 lifts the restriction the other way. A reopened shift's closing reading may
    change; the following shift's stored `opening_reading` is **left exactly as it was**
    and marked `requires_review = true` with a note naming the shift that moved beneath it.
    A human reconciles the two readings and clears the flag. Nothing is recomputed, nothing
    is silently rewritten, and the discrepancy surfaces the same way §4.7 wants every
    opening mismatch to surface — as a question, before anybody is blamed. §4.7, §6.8
11. **Outlet timezone comes from the global `TZ_DISPLAY`, not a column on `outlets`.**
    Used to decide whether a `business_date` is in the future and to resolve a shift
    template's local times to instants. Unlike `outlet_id` this *is* derivable later —
    every existing outlet backfills to `Asia/Kolkata` correctly — so by §5.0's own rule it
    can wait. §5.0, §6.1
12. **An outlet has one cash chain and one drawer.** Two crews working simultaneously on
    separate drawers is not modelled, which is precisely why only one shift may be `open`
    at a time (§5.2). An outlet that needs it will need a drawer concept, not a second
    open shift.
13. **§6.6's credit limit check is not serialised.** It reads the outstanding balance, then
    inserts; two sales issued in the same instant both read the pre-sale figure and both
    pass, so a customer can end up marginally over their limit. Not fixed with
    `SELECT … FOR UPDATE` because this codebase holds **no** row locks anywhere, and §13.12
    above means one outlet has one open shift and therefore effectively one person writing.
    The failure is a limit exceeded by one sale, not money lost or double-counted, and it is
    visible in the very next balance read. Revisit if an outlet ever runs concurrent
    drawers — the same change that needs a drawer concept needs this lock.
14. **Salesman cash shortfalls are their own record type, never credit sales.**
    This outlet books a shortfall as udhaar against the salesman's own name (§14), but a
    shortfall is the *outcome of a reconciliation*, not a sale: it has no receipt to satisfy
    `credit_sales.attachment_id`, and putting it there would pollute a real customer's
    outstanding balance with staff debt — nobody could answer "what does this customer owe
    me" again. **Phase 10 builds `salesman_shortfalls` and
    `salesman_shortfall_settlements`** (§5.2), pointing at `user_profiles`, in the
    `credit_sales`/`credit_repayments` shape. Phase 9 deliberately built neither, because
    §6.4's reconciliation is the only thing that produces one and it did not exist yet (§11's
    rule against scaffolding ahead). §5.2, §6.4, §12

15. **A shortfall cannot be written off in V1, only repaid in cash.** The owner's answer was
    that a salesman repays a shortfall in cash — not deducted from wages, not written off. So
    `salesman_shortfall_settlements` has no `mode` column and every row reaches §6.4's drawer.
    **The consequence: a ₹20 gap nobody will ever chase stays on that salesman's outstanding
    balance permanently, and the balance only ever grows.** Recorded here as a known
    limitation rather than discovered later as a puzzle. The fix is a `mode` column and a
    filter, and it is cheap while the table is small. §5.2

16. **The daily summary is a snapshot, and a reopened shift flags it rather than moving it.**
    §13.10's rule, one table further on. An admin reopening a shift beneath a finalised day
    marks the summary `requires_review` with a note naming the shift; `expected_closing` and
    every stored component are left exactly as they were. Recomputing would be the silent
    rewrite §5.2 stores `expected_closing` to prevent, and §6.5 chains days, so the rewrite
    would not stay local. §5.2, §6.5, §13.10

17. **The audit log has no retention policy, no archival and no partitioning.** Phase 11. It
    is append-only by design (§5.3) and is the fastest-growing table in the schema — every
    financial write and, from Phase 11, every reference-data write adds a row that is never
    removed. `0014`'s index keeps *reads* fast at any size; **nothing keeps the table small.**

    Recorded here as a decision rather than discovered later as a disk alert. A policy needs a
    real row count behind it to be anything but a guess, and there is none yet — so the honest
    move is to name the gap and revisit it with a week of production data. Note that any future
    policy has to answer a question this project has already answered elsewhere: §7.4 permits
    hard-deleting *unlinked attachments* only because they are not a financial record, and
    §3 rule 6's no-hard-deletes rule would apply in full to anything that could be reconstructed
    from an audit row. §5.3, §7.4

18. **The frontend has no automated behavioural tests.** Phase 12. Everything below the browser
    is tested to 100%; the JavaScript itself is verified by hand against the checklist in
    `docs/phase-12-plan.md` §7.

    This is a consequence of §14's no-npm rule, not an oversight: Jest and Vitest are npm
    dependencies, and a headless-browser runner is a build step. What *is* automated is
    everything Python can reach — that the mount does not shadow the API, that no asset
    references an external host, that no money path calls `parseFloat`, that every router has a
    screen. Those are **structural** guarantees, and they are the half that rots silently. The
    behavioural half is checked by a person, because a person is the only thing that can tell
    whether a sheet *feels* right.

    The consequence to be honest about: a refactor of `api.js` can break a form without failing
    the suite. Revisit if the frontend grows past what one person can re-check in an afternoon.

19. **The access token lives in the browser, not in an httpOnly cookie.** Phase 12. Supabase
    issues a bearer token to the client and §8 verifies it server-side on every request, so the
    token has to be readable by JavaScript to be sent at all. The access token is held **in
    memory** and only the refresh token reaches `sessionStorage`, which limits the window but
    does not close it: script injected into this origin could read either.

    The mitigations are therefore structural rather than incidental — a strict
    `Content-Security-Policy` of `default-src 'self'`, no third-party script of any kind, and
    no `innerHTML` on a server-derived value anywhere in the app. Recorded here so the next
    person to reach for a CDN convenience knows what it costs. §7.3, §8

20. **A report reads the snapshot where one exists and computes live where none does — and
    always says which.** Phase 13. §5.2 keeps `expected_closing` and its eleven components so a
    reader can see the figure *as it stood*; §14 forbids recomputing a finalised day. But a
    business date nobody reconciled has no stored row at all, and omitting it from a 7-day view
    would make the report quietly wrong about the week.

    So each day carries a `source`: `snapshot` (read verbatim), `computed` (live, because there
    was nothing to read), `no_trading` (no shifts and no summary), or `unavailable` (live
    computation refused — see §13.21's sibling case, a missing price). **The two are never
    mixed within one day**, and the field is part of the contract rather than a hint: a
    computed figure is an estimate of a day still in motion, a snapshot is a record. Reading
    them as the same number is the mistake this field exists to prevent. §5.2, §6.5, §13.16

21. **Profit in a report is per fuel type, and the combined total is withheld when any fuel
    with sales has no margin.** Phase 13. §6.3 already established that valuation and profit
    are separable and that the cash path must not be refused for a missing margin; a report
    wants the profit but must not be refused either, since petrol and diesel margins have never
    been entered at this outlet (§14's open questions). So reporting calls `shift_sales` with
    `price_only=True` and looks up `margin_at` per fuel inside a `try/except`, rather than
    relaxing `pricing.py` — the raise is correct and stays correct; the reporting layer is the
    one with a reason to tolerate a gap.

    A fuel with no margin reports `null` and a reason code, **never `0`**. The combined total is
    `null` unless coverage is complete, alongside a list naming the fuels excluded. A partial
    total presented as a total is exactly the plausible-but-wrong number this document opens by
    warning about. §4.6, §6.3, §13.7

22. **The fuel breakdown on a snapshotted day is computed live, and may legitimately disagree
    with the stored `metered_fuel_sales`.** Phase 13, and it follows from §13.20 rather than
    contradicting it: there is no stored per-fuel split to read, so the breakdown has no choice
    but to be live.

    Which means a backdated `effective_from` — which §11 already names as the reason
    `fuel_prices` needed an audit trail, since it *"can revalue a closed shift"* — makes the
    breakdown and the total disagree. §5.2 warns about precisely this shape: *"the total and
    its own explanation disagree, and the explanation is the part he can check."*

    **The report detects it and shows both figures**, rather than picking a winner or hiding
    the difference. Nothing else in the system notices that a closed day has been revalued.
    §5.2, §11, §13.20

23. **Variance alerts are derived on every read and cannot be individually dismissed.** Phase
    13. Every signal an alert reports is already stored — `daily_cash_summaries.variance` and
    `.requires_review`, `expenses.requires_review`, `nozzle_readings.requires_review`,
    `shifts.status` — so an alerts table would be a second copy of facts that already exist,
    free to drift from them. Clearing an alert means reviewing the row it points at, through
    the review route that row already has.

    Two consequences to be honest about. **Alerts are windowed**, so a flag older than the
    window is not surfaced here (the dedicated queues, `/expenses/flagged` among them, remain
    the complete view). And **there is no "seen it, it's fine" state** — a variance a manager
    has consciously accepted keeps appearing while it is in range. Revisit if the list starts
    being ignored, which is the failure mode §5.2 names for a flag nobody can clear. §6.7,
    §13.10, §13.16

24. **The range report is O(days × shifts) for unreconciled days, and is capped at 31.** Phase
    13. A `snapshot` day is one row read; a `computed` day is a full §6.4 pass — `day_totals`
    loops the date's shifts and each shift costs roughly eight aggregates plus a valuation.
    Seven reconciled days is trivial; thirty-one unreconciled ones is a few hundred queries.

    The cap is deliberately tighter than `/expenses/summary`'s 366 days, because that endpoint
    reads rows and this one may compute. Recorded as a decision rather than discovered as a
    slow page: 31 covers both §11's 7-day view and a calendar month, and the honest move is to
    name the cost and revisit it with a real query count rather than optimise on a guess —
    the same reasoning §13.17 applies to audit-log growth. §6.4, §6.5

25. **Creating a user writes two systems, and the window between them is not transactional.**
    Phase 14. Supabase Auth is written first — it has to be, because `user_profiles.id` must
    equal `auth.users.id` and only Supabase can say what that is — and the two database rows
    follow inside one transaction. If that transaction fails, the endpoint issues a
    **best-effort compensating delete** against the auth user and re-raises.

    Everything before that is ordinary. What must be written down is the case where the
    compensation *also* fails: an auth user then exists with no profile behind it. **That fails
    closed** — they can obtain a valid token and are refused at every endpoint with 403
    `PROFILE_NOT_PROVISIONED`, which is exactly the state §5.1 describes and `deps.py` already
    handles. It is logged at `error` with the id, and the repair is `provision_user.py`, whose
    entire purpose is "an auth user exists, give it a profile here".

    Two alternatives were rejected. Generating the UUID locally and passing it to Supabase
    would let the database be written first and make the whole thing rollback-safe — but it
    depends on the GoTrue admin API accepting a caller-supplied `id`, which is version-specific,
    and a silent change there would put us back here without a test failing. A two-phase
    "reserve then confirm" protocol is a distributed transaction for a form an admin fills in
    once a month. The honest move is one ordered pair of writes, a compensation, and this
    paragraph. §5.1, §7.1

26. **Email and password are not in this database, and cannot be read, changed or reset
    through this API.** Phase 14, and it is a restatement of §5.1 rather than a new rule —
    but it is the first phase where the consequences are user-visible, so they belong here.

    Three of them. **The Users screen cannot show an email**, so an admin identifies people by
    name and phone and goes to the Supabase dashboard when they need the address. **There is no
    password reset**, and a person who forgets theirs needs an admin in that dashboard or
    Supabase's own recovery flow — this application has nowhere to put the operation.
    And **`POST /users` cannot pre-check a duplicate**, unlike every other create in this
    codebase, which pre-checks its unique key and raises a specific 409 before inserting. The
    unique key here is the email, Supabase is its only authority, and so a duplicate surfaces
    as 409 `AUTH_USER_EXISTS` *from the provider* — whose detail names `provision_user.py`,
    because an admin who hits it is otherwise stuck with a real account they cannot attach.

    Mirroring the email locally would fix all three and is refused: §5.1's "one source of truth
    for a credential" is worth more than the convenience, and a mirrored email is wrong from
    the first time somebody changes it in the dashboard. §5.1, §8

27. **The last active admin at an outlet cannot be demoted or deactivated through the API.**
    Phase 14. `app/jobs/provision_user.py` already guards this for the CLI and states the
    stake: *"a careless re-run with the wrong `--role` would otherwise silently demote the only
    admin and lock everyone out of §8's admin-only actions."* There it is recoverable, because
    `--force` exists and a shell operator is more privileged than any API role.

    **An HTTP endpoint has no `--force`, because there is no caller left who could send it.**
    So it refuses with 409 `LAST_ADMIN_AT_OUTLET` and the detail names the command that can
    undo it. The count joins `user_profiles.is_active` as well as the membership flag — an
    admin who cannot sign in is not a second admin, and counting them would strand the outlet
    on the strength of an entirely correct query.

    **Self-demotion is allowed when another admin exists**, and takes effect on the caller's
    very next request. That is startling and it is correct: the rule protects the outlet from
    having no admin, not an individual from their own decision, and a second rule guarding the
    second case would be guarding nothing this one does not already cover. §5.1, §8

28. **The login screen ships binary assets: one self-hosted webfont and one licensed
    photograph.** This supersedes `docs/phase-12-plan.md` M5 — *"System font stack, no
    webfont"* — and app.css's header comment was updated to match rather than left to
    contradict the file it heads.

    **M5's reasoning was about dependencies, and it still holds.** What it forbade in
    practice was a *CDN* font: §14 forbids the npm dependency, a CDN is the same dependency
    with worse failure modes, and §13.19 makes any third-party asset able to read the session
    token. **None of that applies to a file served from this origin.** The CSP already said
    so before the font existed — `font-src 'self'` permits our file and refuses Google's —
    and `test_no_asset_references_an_external_host` refuses the CDN at commit time.

    So the rule is narrowed, not dropped: **no font, image or media may be referenced from an
    external host; a self-hosted asset in `app/static/` is permitted.** The face is
    Instrument Serif (SIL OFL 1.1, `app/static/fonts/OFL.txt`) and it dresses **the wordmark
    and the two scroll headlines only** — every other surface in the application keeps the
    system stack, so the exception cannot spread by habit. The photograph is Adobe Stock
    free-tier, licensed to the owner's account, recorded in `app/static/img/CREDITS.txt`.

    **Every such asset must be listed in `pyproject.toml`'s `package-data`.** This is the
    failure mode worth writing down, because it is silent in the direction that matters: the
    files sit on disk in development and the app looks correct, then a built wheel omits them
    and production loses its wordmark and its backdrop with nothing failing. §2's
    one-artefact rule is what makes this the only place the list can live. §7.1, §13.19, §14

29. **The login backdrop is a photograph, and nothing about it is recomputed.** Two earlier
    versions of this screen stuttered, both for the same reason, and the reason generalises
    past this screen.

    The first repainted a full-viewport canvas every frame — five `drawImage` blits, three
    radial gradients and ~46 colour strings per tick — underneath a `backdrop-filter` on the
    sign-in card, so the browser re-ran a 30px gaussian over changing pixels sixty times a
    second. The second moved the drawing to CSS transforms and was smooth, but was vector
    line art where the brief wanted photography.

    What is left is one `<img>` under a CSS Ken Burns keyframe, a parallax `translate3d`
    written once per changed scroll position, and a colour grade made of **stacked gradients
    rather than a CSS `filter`**. The per-frame budget is one `style.transform` write.

    **The rule this leaves behind:** anything that moves continuously moves under `transform`
    or `opacity`, and is never redrawn to say so. §13.18, §14

30. **A report's default window ends on the outlet's most recent trading day, not on today.**
    Phase 15. `reports.py::_resolve_window` used to default `to` to `outlet_today()` and
    `from` to six days before it, and the docstring defended the *timezone* half of that
    carefully — at 23:00 IST the UTC date is still yesterday, so a UTC default would drop the
    current trading day. That reasoning is untouched and still correct. What it never
    questioned was the anchor itself.

    §4.7 says the whole day is typed in **after the fact**, in one sitting, often days later.
    An outlet catching up on July in late August therefore opened both reporting screens onto
    seven days of `no_trading` — and the `day_not_reconciled` alert, which exists for exactly
    the day they were looking for, is windowed (§13.23) and so was never shown either. A
    report about a week in which nothing happened is a report about nothing.

    So the default `to` is now the most recent `business_date` that has a shift, falling back
    to `outlet_today()` for an outlet that has never traded, and clamped so it can never
    exceed today. **For a live outlet this changes nothing**, because the most recent trading
    day *is* today; it only differs for an outlet that is behind, which is the one this
    software was written for.

    Two consequences to be honest about. The window is now **data-dependent**, so two managers
    opening "this week" on different days can see the same seven dates — which is correct, and
    is why the resolved `from`/`to` were already returned in the response and rendered in the
    screen's subtitle. And a **single backdated shift moves the window**, since the anchor is
    a maximum; that is the intended behaviour for a back-entered day, and §13.24's 31-day cap
    is unchanged, so the cost is bounded. §4.7, §13.23, §13.24

---

## 14. Guardrails for Claude Code

Read this section before generating anything. These are the failure modes most likely
to occur on this specific project.

**Do not:**
- Use `float` for money anywhere, including in a "quick" test fixture or a docstring example
- Compute sales from a client-supplied total
- Store fuel price as a mutable single-value column
- Attach totalizer fields directly to `shifts` (they belong on `nozzle_readings`)
- Reintroduce `shift_type`, or assume a day has exactly two shifts (§4.7)
- Let a client supply `shifts.sequence` — it is server-assigned, always
- Treat a carried-forward opening reading as verified fact. It is pre-filled and must be
  **confirmed**; an assumed opening turns overnight theft into a salesman's debt (§4.7)
- Compute a shift's opening reading on read instead of storing it (§4.7)
- Read `started_at` / `ended_at` back through `outlet_shift_templates` — the template
  supplies a default at creation and is never consulted again (§5.1)
- Skip `testing_quantity` because it seems like a rounding detail — it is not
- Assume a quantity is in litres — read `fuel_types.unit_of_measure` (§4.5)
- Hardcode the ₹2.28 CBG margin, or any margin — it is effective-dated data, not a constant
- Derive margin from a purchase price — V1 stores margin directly and holds no purchase
  data at all (§4.6)
- Read `MAX_FLOW_RATE_LPM` in the §6.2 guard — it only seeds `fuel_types`
- Record an IOCL / PAD payment as an expense — it is a bank movement, not a drawer
  movement, and §6.4 would invent a cash shortage (§12)
- **Record a fuel restock / tanker settlement as an expense, in any `mode`.** This is why
  `fuel_purchase` was removed from `expenses.category` in Phase 7 — the payment settles
  against the IOCL ledger, not the drawer, and §12 already puts tanker delivery entirely out
  of V1. Adding it back as a category, in any mode, reopens exactly the trap the line above
  already forbids for the IOCL/PAD case
- **Create a fuel-purchase / tanker / IOCL / PAD expense *category*.** Phase 8 made
  categories admin-managed data (§5.1), so the enum that used to make this impossible is
  gone and only discipline is left. Same money, same wrong drawer, same phantom shortfall
- Read `expense_categories.requires_receipt` when validating or reporting on an **existing**
  expense — read `expenses.receipt_required`, the snapshot taken at insert. The live flag is
  editable, and reading it back retroactively rewrites whether history complied (§6.11)
- Make expense categories free text, or let one be renamed by changing its `code` — the
  first defeats §6.7's aggregate, the second relabels every expense ever filed under it (§5.1)
- Demand a receipt on a **reversal** row — a cancellation is not a spend and there is nothing
  to photograph (§6.11)
- Make one attachment serve two live business rows, or force a re-upload of the same paper
  for a §6.9 replacement — the replacement inherits the original's attachment (§5.3)
- Put the client-supplied filename, or the bucket name, in `storage_path` (§5.3, §7.2)
- Trust a client-declared `Content-Type` or a file extension — sniff the magic bytes (§7.2)
- Delete a **linked** attachment, at any age, including one whose expense was reversed (§7.4)
- Take the upload's `business_date` from "today" — the day is typed in after the fact and
  the receipt would file under a date the register never mentions (§7.2, §4.7)
- Block a shift close because collections do not equal sales — that gap is §6.4's variance
  and §6.6's udhaar, and blocking on it teaches staff to type figures that balance (§6.8)
- Sum the `cash` collection row together with §6.4's derived `cash_sales` — the cash row is
  a declaration to check that figure against, not a term in it (§5.2)
- **Book a salesman's cash shortfall as a `credit_sale`.** It is a reconciliation outcome,
  not a sale; it has no receipt to satisfy that table's `NOT NULL`; and it would mix staff
  debt into a real customer's outstanding balance, so nobody could answer "what does this
  customer owe me" again. Shortfalls have their own record type, `salesman_shortfalls`,
  pointing at `user_profiles` (§5.2, §13.14)
- **Book a shortfall automatically at shift close.** The system computes the gap and shows
  it; a *human* books it, with a reason. §4.7's argument applies with more force here than
  anywhere else in this document, because here the debt is explicit and carries a name: a
  ₹500 gap is more often a mistyped reading, a forgotten UPI figure or an unrecorded udhaar
  slip than it is theft, and the software must not be the thing that decides (§5.2)
- **Take `salesman_id` from the client.** It is read from `shifts.attendant_id` — the one
  name §5.2 says carries the drawer. A client-supplied value lets a typo put a debt on the
  wrong person, with no second source of truth to catch it (§5.2)
- **Forget §6.4's `− shortfalls_booked` term.** Without it a booked shortfall is an asset
  twice over — the salesman's debt *and* cash the locker does not hold — and every count
  after it is wrong by that amount with nothing to explain why (§6.4)
- **Add non-fuel income to the cash side of §6.4.** It belongs in `total_sales`. On the cash
  side, a card-paid oil sale understates derived cash by exactly its amount, because the
  collections row already counted it (§6.4)
- **Carry day N's opening from `expected_closing` when day N−1 was actually counted.** The
  count wins whenever there is one; §6.5 exists precisely so a ₹200 shortage does not vanish
  into the next day's opening. Carrying the arithmetic forward is the fallback for the days
  nobody counted, never the default (§6.5)
- **Recompute a finalised day's `expected_closing`** when a shift beneath it is reopened.
  Flag it (§13.16). Recomputing is the silent rewrite §5.2 stores the figure to prevent, and
  §6.5 chains days so it would not stay local
- **Read `margin_at` on the cash path.** §6.4 needs the price, not the margin, and petrol and
  diesel margins have never been entered here — a cash engine that looked one up would refuse
  to reconcile every petrol day (§6.3)
- **Maintain a denormalised outstanding balance**, on the customer row, a salesman row, or
  anywhere else.
  §6.6 says compute it, and Phase 9 deleted `credit_sales.is_settled` for exactly this
  reason — a stored total drifts, and a stored per-row flag has no honest value once one
  repayment covers part of three bills (§5.2)
- **Weaken `credit_sales.attachment_id` from `NOT NULL` to a CHECK** to make room for a
  reversal. The reversal inherits the original's attachment instead; the `NOT NULL` is the
  receipt control the whole of §6.6 rests on (§5.2)
- **Read `credit_limit IS NULL` as zero.** Null means *no limit*; coercing it refuses every
  sale to the customers who are trusted most (§6.6)
- Filter reversals out when computing an outstanding balance — they are negative rows that
  net out, and dropping them makes a cancelled udhaar reappear as debt (§6.6)
- Use offset pagination
- Set `allow_origins=["*"]`
- Add a frontend framework, bundler, or npm dependency
- Hardcode `1000` for the expense review threshold, or `5000` for the receipt threshold —
  both are config, and they are deliberately **separate dials** (§6.11)
- Create scaffolding for out-of-scope features
- Create a table listed in §5.0's schedule **without** its `outlet_id`, or with a
  unique constraint that is not outlet-scoped where §5.1–§5.3 says it should be
- Remove an `outlet_id` column because §12 says multi-outlet is out of scope — the
  *features* are out of scope, the schema is not. See §5.0
- **Add an admin write endpoint without an `audit.record` call in the same transaction.**
  §5.3 requires the trail and `created_by` is not one — it says who last touched a row, never
  what it was before. This is pinned structurally by an `ast` test over every
  `@router.post` / `@router.patch` in `app/api/v1/`, so a new router **fails the suite** rather
  than the review. That is deliberate: the gap survived seven phases precisely because nothing
  failed when it was missing. If an endpoint genuinely should not be audited, add it to that
  test's exemption list **with a reason**, the way `uploads.py` is (an attachment is not a
  business row, and §7.4 sweeps unlinked ones) — never by deleting the assertion (§5.3, §11)
- **Call `audit.record` after `db.commit()`, or commit it separately.** `services/audit.py`
  is explicit that the caller commits, so the audit row and the change it describes land in one
  transaction or neither. A separately committed audit row can describe a change that was then
  rolled back, which is worse than no log at all: it is a log that lies (§5.3)
- **Use `AuditAction.status_change` for a reference-data deactivation.** That label means a
  *shift* lifecycle move — §5.2 singles out backwards transitions as the thing that must be
  traceable. Because §3 rule 6 forbids hard deletes, `is_active` is how **every** reference
  table retires a row, so admitting those would make the label mean "a shift moved, or anything
  at all was deactivated" and nobody could query for lifecycle events again. A deactivation is
  an ordinary `update`, already fully legible in `old_values` / `new_values` (§5.3)
- **Do arithmetic on a money value in JavaScript.** JS has no decimal type and
  `0.1 + 0.2 !== 0.3` there exactly as it does in Python, so §3 rule 1 does not stop at the API
  boundary. Every figure the frontend shows — totals, gaps, outstanding balances, variances —
  is already computed server-side and is rendered **as received, as a string**. `parseFloat` on
  a money field is the same bug as `float` in a fixture, one language further out (§3 rule 1,
  §13.18)
- **Coalesce a `null` money value to zero.** `?? 0` and `|| 0` are the most dangerous two
  characters this project can write in a client. `declared_cash: null` means *nobody declared*
  and `"0.00"` means *they counted zero* — §6.8's "zero as an answer, never zero as an
  omission", and the same distinction carries `gap`, `variance`, `actual_counted` and
  `credit_limit`, where null means **no limit** and zero would refuse every sale (§5.2, §6.6)
- **Mint a fresh `Idempotency-Key` on a retry.** The key belongs to the *submission*, not to
  the `fetch` call: minted once when a form is first submitted and reused by every retry until
  it succeeds. A key per call reintroduces, in the client, the exact duplicate-₹5,000-expense
  §6.10 was built to prevent — and the network it was built for is the one that makes retries
  routine (§6.10)
- **Pre-confirm a chained opening reading, or default `testing_quantity` to 0, in the UI.**
  `opening_confirmed` is a required boolean with no server-side default precisely so that "I
  checked the meter" cannot be what happens when nobody looked. A pre-ticked box in the client
  defeats that as completely as a default in the schema would, and §4.7 spells out the cost: an
  assumed opening converts theft into a debt owed by someone who did nothing wrong (§4.2, §4.7)
- **Label a read-only screen with a verb that implies a write.** Phase 15's report was that a
  primary button reading *"Reconcile this shift"* only navigated to `GET /cash-position`,
  which §8 requires to write nothing. The user pressed it, believed the day was settled, and
  no `daily_cash_summaries` row was ever created — so §6.5's chain never advanced and the day
  vanished from every list. **A verb on a button is a promise about what the server will be
  asked to do.** If the screen behind it only reads, name what it shows ("Cash position"),
  not what the reader wishes it did (§13.20, §11 phase 15)
- **Show a list of *records* where the user is looking for a list of *days*.**
  `GET /daily-summaries` reads one table and cannot surface a business date that traded and
  was never reconciled — which is precisely the date somebody needs to act on. Merge it with
  `GET /shifts` client-side; do not add an endpoint, and do not let the absence of a row read
  as the absence of a day (§11 phase 15)
- **Offer "reconcile" on any day but the oldest unreconciled one.** §6.5's opening balance
  chains from the most recent *summary*, not from yesterday, so reconciling out of order
  skips a day's cash permanently and invisibly. The server refuses with 409
  `EARLIER_DAY_NOT_RECONCILED`; the client should not need to be refused, and sorts its
  worklist oldest-first so the right day is the one with the button (§6.5)
- **Treat a hidden control as a permission check.** §8 already says hiding a button is UX, not
  a control. The corollary for Phase 12: every screen still handles a 403 as a real outcome,
  and no client-side rule exists that the server does not also enforce (§8)
- **Ship a font, image or media file without adding it to `pyproject.toml`'s
  `package-data`.** It will work perfectly in development, where the source tree is on disk,
  and be missing from the built wheel. Nothing fails; the wordmark and the backdrop simply do
  not arrive in production. §2 ships one artefact precisely so there is one list to keep
  right (§13.28)
- **Put a CSS `filter` or `backdrop-filter` on an element that also carries an animated
  `transform`** — or on one sitting over animating content. The filtered result has to be
  re-rasterised every frame, and this has already cost this project one visibly janky login
  screen. Grade with stacked gradients instead; they cost nothing and are tunable without
  re-exporting an asset (§13.29)
- **Redraw anything to make it move.** Continuous motion belongs to `transform` and
  `opacity`, on the compositor. A canvas that repaints per frame to shift some rectangles is
  the shape of the bug §13.29 records, and it looks like a slow device rather than like a
  mistake (§13.29)
- **Add a `<script src>`, stylesheet, or font from an external host.** §14 forbids the npm
  dependency and a CDN is the same dependency with worse failure modes — plus §13.19 makes any
  third-party script able to read the session token. The CSP refuses it and a structural test
  refuses it; do not weaken either (§13.19)
- **Recompute a snapshotted day's cash figures in a report.** Read the stored row. §5.2 keeps
  `expected_closing` and its eleven components so a reader can see what the manager was told on
  the day, and a report is the one thing whose whole job is to show that. §6.5 chains days, so a
  recomputed figure would not even stay local to the day it got wrong. Compute live **only**
  where there is no snapshot, and say so with `source` (§13.20)
- **Sum a partial profit into a total.** A fuel with no margin makes the combined figure
  *unknowable*, not smaller — so the total is `null` and the excluded fuels are named. Petrol
  and diesel margins have never been entered here, so a naive `sum()` over per-fuel profit is
  wrong on the very first day it runs, and wrong in the direction that looks plausible
  (§4.6, §13.7, §13.21)
- **Sum quantities across units of measure.** Litres of petrol and kilograms of CBG do not add,
  and a `total_quantity` field is a number with no meaning. Report `quantity_by_unit`, the shape
  `ShiftSales` already uses (§4.5)
- **Divide a money value in JavaScript to size a chart bar.** `value / max` is arithmetic on
  money one language further out, and it is the kind that looks harmless because the output is a
  pixel rather than a rupee. The server computes bar heights in `Decimal` and sends a CSS
  percentage string the client can only assign — which also makes the chart provably consistent
  with the table beneath it, since both come from one pass (§3 rule 1, §13.18)
- **Accept `user_profiles.id` from a client, or generate one.** It must equal `auth.users.id`
  — it *is* the JWT's `sub` claim — so it is copied from whatever the identity provider
  returned, never invented. A wrong value produces a person who authenticates successfully and
  is refused forever with `PROFILE_NOT_PROVISIONED`, which looks like a permissions bug and is
  not (§5.1, §13.26)
- **Store, log, or audit an email or a password.** Neither is a column in this database and
  neither may become one: §5.1 refuses to mirror the credential, and one source of truth is the
  whole point. `_audit_snapshot` covers real columns only, so nothing from the create payload
  can reach `audit_logs` by accident — keep it that way (§5.1, §13.26)
- **Let the API demote or deactivate the last active admin at an outlet.** There is no
  privileged caller left to undo it, so this is a one-way door into an outlet nobody can
  administer. Refuse with 409 and name `provision_user.py --force`, which is the only tool that
  can still get in. Count only admins who could actually sign in — join `user_profiles.is_active`
  as well as the membership flag (§13.27)
- **Take `outlet_id` from a user-management payload.** It comes from `actor.outlet_id`, like
  every other create. A client-supplied outlet is how somebody grants themselves a role at a
  pump they do not work at (§5.0, §8)
- **Retire a person by writing `user_profiles.is_active` from the API.** That flag means "gone
  from every outlet" and V1 has no way to mean it. Deactivate the *membership* — different
  flag, different error code, and the one §8's checks actually resolve against (§5.1, §13.26)
- **Treat the Supabase-then-database create as atomic, or pretend it is.** Two systems, one
  ordered pair of writes, a best-effort compensating delete, and a loud log if that fails. An
  orphaned auth user fails closed and is repaired with the CLI; a comment claiming the pair is
  transactional is worse than the orphan, because it stops the next person looking (§13.25)
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
- ~~Are there exactly two shifts a day, always? Any third/relief shift?~~ **Answered:**
  one 06:00–22:00 shift a day here; other outlets run three. Shifts are now
  sequence-numbered with no fixed count — see §4.7
- ~~Who physically counts the cash, and at what time?~~ **Answered:** there is no fixed
  counting moment. The salesman reconciles his own shift (sales vs UPI, card, credit) and
  puts the cash in the locker; a shortfall is booked as udhaar **against his own name**.
  The locker carries a running balance that rolls forward on any day with no bank deposit.
  Consequences for §6.4/§6.5 are on the Phase 10 list below
- Is non-fuel (lubricant) sales volume significant enough to itemise in V2?
- ~~Does the pump currently record testing litres on paper? In what unit?~~ **Restated
  above as urgent** — Phase 5 consumes it.
- **URGENT — What is the real maximum flow rate of the CBG dispenser, in kg/min?**
  Migration `0003` seeds a deliberately generous 15. **Phase 5 now reads that column in
  §6.2's guard, so it is live on real money.** Too high and it never fires; too low and it
  refuses genuine sales on a busy day. The same applies to the 60 L/min seeded for petrol
  and diesel — the column is authoritative now, and `MAX_FLOW_RATE_LPM` is only its seed.
- **Do the salesmen record testing quantities on paper today, and in what unit?** §4.2's
  `testing_quantity` defaults to 0 and Phase 5 requires it to be an *answer*, not an
  omission. If nothing is recorded on paper, every row will carry 0 and §4.2's small,
  permanent, daily cash shortfall reappears with the field looking correctly filled in.
- What are the petrol and diesel dealer commissions per litre? Needed to enter
  `fuel_margins` rows for them; CBG's ₹2.28 is known. Until entered, profit reporting
  covers CBG only.
- ~~**§5.2 vs §4.7 contradiction, decide before Phase 9 — the salesman shortfall.**~~
  **Answered before Phase 9, as recommended.** `credit_sales` stays receipt-mandatory —
  the `NOT NULL` is untouched, and §6.9's reversal reaches it through inheritance rather
  than by weakening it (§5.2). Shortfalls become **their own record type, built in Phase
  10**, where §6.4's reconciliation is what actually produces one; building the table in
  Phase 9 would have been scaffolding ahead of its only producer (§11). Until then a
  shortfall shows up as §6.4's variance and nothing else. See §13.14 and §14's guardrail.
  **Still to decide before Phase 10:** does a shortfall row point at a `user_profiles.id`
  rather than a `credit_customer_id`, and is it repaid, written off, or deducted from wages?
- **Phase 10 consequences of the locker model (§14 above), decide before Phase 10:**
  cash is not counted at a fixed moment and the drawer is never emptied on a schedule, so
  §6.5's "opening_balance for day N = actual_counted of day N−1" needs restating for a
  *running locker* rather than a daily drawer. Also decide whether an occasional full
  physical locker count is recorded as an audit against the arithmetic balance.
- Do salesmen hold a change float overnight, and is it counted separately from the locker?
- ~~§6.4 vs §5.2 contradiction, decide before Phase 7~~ **Answered in Phase 7:**
  `expenses` gained a `mode` column (§5.2), and §6.4 now states that `cash_expenses` means
  `mode = cash` rows only.
- ~~Should every expense carry a receipt?~~ **Answered in Phase 8: no, and it never did.**
  Only `credit_sales.attachment_id` was ever `NOT NULL`. §6.11 now decides per expense, from
  an admin-managed per-category flag OR an amount threshold — so tea stays frictionless and
  a ₹50,000 anything still needs paper.
- **`EXPENSE_RECEIPT_THRESHOLD` is defaulted to ₹5,000 and that figure is a guess.** Set
  above §6.7's ₹1,000 review line because "a manager should look" and "this needs paper" are
  different questions, but the owner has not confirmed it. It is live on real money from the
  moment §6.11 lands.
- **What is the real expense category list, and which entries genuinely need a receipt?**
  Phase 8 seeds `SALARY`, `MAINTENANCE`, `ELECTRICITY` and `OTHER`, with `OTHER` alone
  requiring one, and everything else is now data entry (§5.1) — but seeding the real list
  means the app matches the paper register from day one instead of after a round of typing.
- **`VARIANCE_ALERT_THRESHOLD` is defaulted to ₹100 and that figure is a guess** — the same
  admission `EXPENSE_RECEIPT_THRESHOLD` carries, and the same risk. Too low and every day is
  flagged, which trains a manager to dismiss the list without reading it; too high and the
  ₹500 gap §5.2 describes — the one that gets booked as udhaar against a salesman's own name —
  never surfaces at all. Needs a week of real variances behind it to be anything but arbitrary.
- **Are §13.23's six alert kinds the right list?** Variance over threshold, a day never
  reconciled, a summary flagged by a reopened shift, unreviewed flagged expenses, a reading
  flagged for review, and a shift still open on a past date. Each is derived from a signal the
  system already stores, so adding or removing one is cheap — but a list that reports things
  the owner does not act on is a list that gets ignored, and then the ones that matter are
  ignored with it.
- **Who actually works at this pump, and in what capacity?** Phase 14 makes staff data entry
  rather than a shell command, which means the real list can be typed in before the first day
  of trading is entered — the same argument this section already makes about the real expense
  category list. Until it is, `shifts.attendant_id` carries one name (the owner's) on every
  shift, and §5.2's *"exactly one name carries the drawer"* is technically satisfied and
  practically meaningless.
- **Should an admin be able to see a person's email inside the app?** Today they cannot, by
  design (§5.1, §13.26) — identification is by name and phone, and the email lives in the
  Supabase dashboard. That is correct and mildly inconvenient, and it is worth confirming the
  inconvenience is acceptable before somebody proposes mirroring the column to fix it.

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
SUPABASE_SERVICE_KEY          # server-side only, never exposed to frontend. Phase 14:
                              # REQUIRED when ENV=prod, which it was not before. Two callers
                              # now -- Storage (§7.1) and Auth admin (§11 phase 14) -- and its
                              # absence used to be silent: build_storage() falls back to a
                              # local temp directory, so a misconfigured production wrote
                              # receipts to /tmp with nothing complaining. Failing at startup
                              # is the only safe direction, as it already is for the three
                              # keys below.
SUPABASE_ANON_KEY             # Phase 12. PUBLIC BY DESIGN -- it is meant to ship in a
                              # browser, and GET /api/v1/auth-config serves it unauthenticated
                              # so the login screen can reach Supabase at all. Note the
                              # contrast with the line above: same provider, opposite rule.
                              # Confusing the two hands a client full database access.
SUPABASE_JWT_SECRET
SUPABASE_STORAGE_BUCKET=receipts
EXPENSE_REVIEW_THRESHOLD=1000.00   # §6.7 -- flag for a manager's eyes
EXPENSE_RECEIPT_THRESHOLD=5000.00  # §6.11 -- demand a receipt. A DIFFERENT dial from the
                              # line above on purpose: "look at this" and "prove this"
                              # are different questions. Never fold them into one value.
VARIANCE_ALERT_THRESHOLD=100.00    # §13.23 (Phase 13) -- a THIRD dial, and separate for the
                              # same reason the two above are separate from each other. Those
                              # two ask about one expense; this asks whether a whole day's
                              # cash reconciled. Served by GET /client-config so the screen
                              # and the server agree on which days are flagged -- a copy
                              # hardcoded in JavaScript desynchronises the moment it moves.
                              # THE FIGURE IS A GUESS, exactly as the 5000.00 above is, and
                              # it is live on real money from the day reporting ships.
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

**Runtime dependencies added in Phase 8**, both forced by §7.1's proxy-upload choice:
`python-multipart` (FastAPI cannot parse `multipart/form-data` without it) and `httpx`,
promoted from a dev-only dependency because the Supabase Storage REST API is called
directly. **No Supabase SDK** — three REST calls behind a small `StorageBackend` protocol is
more boring and more testable than a client library, and it keeps the test suite offline.
**Content sniffing is hand-rolled**, not `python-magic`: about twenty lines for three
signatures, nothing to install, and §7.2's HEIC message falls out of it naturally.
