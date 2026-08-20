# Phase 3 — Reference Data: Notes

> Fuel types, units, prices, margins, nozzles — what was built, and why every piece
> is shaped the way it is. Written as a learning reference, not a spec. For the
> authoritative rules, see `CLAUDE.md`.

---

## 1. The big picture

Phase 3 answers three questions the rest of the system needs before anything else can
work: **what do we sell, through which meter, and at what price / margin?**

Everything from Phase 5 onward — sales math, cash reconciliation, profit reporting —
reads from what got built here. That is why so much care went into it. A mistake in
this layer does not crash the app. It produces a *plausible wrong number*, which is
the exact failure mode `CLAUDE.md`'s preamble warns about:

> Wrong-but-plausible numbers are the primary failure mode of this project.

### What shipped

| Piece | File(s) |
|---|---|
| Migration `0003` — 4 new tables, 1 enum, 1 trigger, seed data | `alembic/versions/0003_reference_data.py` |
| ORM models | `app/models/fuel.py`, `app/models/nozzle.py` |
| Unit-of-measure vocabulary | `app/core/units.py` |
| The rate/margin lookup service | `app/services/pricing.py` |
| Cursor pagination helper | `app/api/cursor.py` |
| 4 API routers, 10 endpoints | `app/api/v1/fuel_types.py`, `nozzles.py`, `fuel_prices.py`, `fuel_margins.py` |
| 182 tests, 97% coverage | `tests/test_pricing.py`, `test_migrations.py`, `test_fuel_types.py`, `test_nozzles.py`, `test_fuel_prices.py`, `test_fuel_margins.py` |

A mid-planning discovery reshaped this phase: **the outlet also sells CBG (compressed
bio gas)**, priced and metered **per kilogram**, not per litre. `CLAUDE.md` originally
hardcoded "litre" into column names, formulas, and config. That got fixed at the spec
level *before* any code was written — see §2 below.

---

## 2. Why CBG changed the whole schema

The original spec assumed every fuel is liquid and every quantity is a litre:
`rate_per_litre`, `litres_sold`, `testing_litres`. CBG is sold by weight, so those
names would have been **actively wrong** for it, not just imprecise.

Two consequences fell out of this:

1. **A quantity needs a unit that lives on the fuel type**, never assumed from a
   column name. See §5.
2. **Dealer margin turned out to be the stable number, not price.** IOCL deducts
   `(retail_rate − 2.28) × kg` for CBG. When petrol/diesel retail rises ₹1/litre, the
   next tanker invoice rises ₹1/litre too — the *gap* never moves. That single fact
   is why `profit = quantity_sold × margin` can be computed straight from the nozzle
   totalizer, with zero purchase data, zero stock tracking, zero IOCL integration.
   See §8.

---

## 3. The migration (`0003_reference_data.py`)

### 3.1 What a migration actually is

A migration is a **numbered, chained Python file** describing one schema change.
`0001` created `outlets`, `0002` added `user_profiles` / `outlet_memberships`, and
`0003` is this phase. Each file declares which one comes before it:

```python
revision = "0003"
down_revision = "0002"
```

That `down_revision` line is what turns a pile of independent files into a **chain**
— Alembic reads it and knows `0003` only makes sense once `0002` has already run.

Alembic tracks "where the database currently is" in one tiny table:

```sql
SELECT version_num FROM alembic_version;
-- ('0003',)
```

One row, one value: the ID of the last migration applied.

### 3.2 `upgrade()` and `downgrade()`

Every migration file has exactly two functions:

- **`upgrade()`** — instructions to move the schema *forward* one step
- **`downgrade()`** — instructions to move it *backward* one step, undoing `upgrade()`

```bash
alembic upgrade head      # run every not-yet-applied upgrade(), in order
alembic downgrade -1      # run the current migration's downgrade(), go back one step
```

This is not a one-way ratchet. `tests/conftest.py` actually exercises the *reverse*
direction on every single test run — it downgrades the test database all the way to
`base` and re-upgrades to `head` before the suite starts, specifically so the tests
prove the migrations themselves work, not just whatever schema happened to already be
sitting in the database.

**Verified live**, not just trusted:

```
downgrade -1  →  fuel_types, nozzles, fuel_prices, fuel_margins: gone
              →  fuel_type_unit_of_measure enum: gone (0 rows in pg_type)
              →  reject_modification() trigger function: gone
upgrade head  →  all four tables back, seed data restored exactly
```

Zero leftovers in either direction — a genuine round trip, not just "the file has a
downgrade function."

### 3.3 `upgrade()` step by step

1. Create the `fuel_type_unit_of_measure` enum type in Postgres
2. Create `fuel_types`
3. Create `nozzles`
4. Create `fuel_prices` and `fuel_margins` (in a loop — the two tables are structurally
   identical, so one loop builds both instead of duplicating the `create_table` call)
5. Create the `reject_modification()` trigger function, attach it to both money tables
6. Insert the 4 seed rows into `fuel_types`

### 3.4 `downgrade()` — the exact reverse, in the exact reverse order

```python
def downgrade() -> None:
    op.drop_table("fuel_margins")
    op.drop_table("fuel_prices")
    op.execute("DROP FUNCTION IF EXISTS reject_modification()")
    op.drop_table("nozzles")
    op.drop_table("fuel_types")
    unit_of_measure_enum.drop(op.get_bind(), checkfirst=True)
```

Order matters here, and it is not arbitrary — it is the same rule as unstacking
plates from the top:

- `nozzles.fuel_type_id` is a **foreign key** into `fuel_types`. Postgres will refuse
  to drop `fuel_types` while a table still points into it. So `nozzles` must be
  dropped *before* `fuel_types`.
- `fuel_types.unit_of_measure` *uses* the enum type. The enum can't be dropped while
  a column still uses it. So the enum is dropped **last**, after every table that
  references it is already gone.

> **Rule of thumb:** drop things in the reverse order you created them. Whatever a
> thing depends on must still exist while you're removing it, and whatever depends on
> a thing must be removed before it.

---

## 4. Migrations vs. models — the layer that confuses everyone at first

This is the single most common point of confusion when learning a framework like
this, so it's worth its own section. Both a *migration* and a *model* describe the
same table (say, `fuel_types`) — but they run at completely different times, for
completely different purposes.

**The mental model that untangles it:** a migration is a one-time construction crew.
A model is the map you use every single day after the building already exists.

| | Migration (`alembic/versions/0003_reference_data.py`) | Model (`app/models/fuel.py`) |
|---|---|---|
| **When does it run** | Once, when you run `alembic upgrade head` | Every single time the app handles a request |
| **What it talks to** | The database *schema* itself (the empty table structure) | *Rows* of actual data inside a table that already exists |
| **Its job** | *Build* the table | *Read and write* data in a table already built |
| **Written in** | Alembic's `op.create_table(...)` commands | SQLAlchemy's `class FuelType(Base): ...` |
| **Analogy** | The crew that builds a house, once | The floor plan you consult every day to find a room |

Concretely: when the running app handles "give me all fuel types," it never touches
the migration file at all. It runs code like:

```python
db.execute(select(FuelType))
```

`FuelType` here is an ordinary **Python class**, and SQLAlchemy translates that class
into the real SQL query (`SELECT * FROM fuel_types`) behind the scenes. The migration
did its job the moment the table was built; from then on, the *model* is what every
request actually uses to talk to that table.

### 4.1 Why two files, if they describe "the same thing"?

Because they answer two different questions:

- The migration answers: *"How do I change the database from state A to state B?"*
  — a set of imperative instructions ("do this, then this").
- The model answers: *"What does one row of this table look like, in Python?"*
  — a declarative description ("here's the shape").

```python
class FuelType(Base):
    __tablename__ = "fuel_types"
    __table_args__ = (
        sa.UniqueConstraint("code", name="uq_fuel_types_code"),
        sa.CheckConstraint("max_flow_rate_per_minute > 0", ...),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(), primary_key=True, ...)
    code: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    display_name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    unit_of_measure: Mapped[str] = mapped_column(_unit_of_measure_enum, nullable=False)
    max_flow_rate_per_minute: Mapped[Decimal] = mapped_column(sa.Numeric(10, 3), ...)
```

Each attribute here is called a **mapped column** — it maps a Python attribute
(`code`) to a real database column (`code`). This class creates nothing by itself; it
only *describes* a shape SQLAlchemy uses to translate Python into SQL.

Here's the class actually being used, inside the router that handles
`POST /fuel-types`:

```python
row = FuelType(
    code=code,
    display_name=payload.display_name.strip(),
    unit_of_measure=payload.unit_of_measure.value,
    max_flow_rate_per_minute=payload.max_flow_rate_per_minute,
    created_by=actor.user.id,
)
db.add(row)
db.commit()   # <- THIS is the moment the real INSERT SQL is sent to Postgres
```

`FuelType(code="XP95", ...)` builds an ordinary Python object in memory, exactly like
constructing any other class. `db.add(row)` stages it. `db.commit()` is what finally
sends `INSERT INTO fuel_types (...) VALUES (...)` to the database. The model is the
*translator* between "a Python object with these attributes" and "a row with these
column values" — in both directions (reading rows back out works the same way, in
reverse).

### 4.2 Why the two descriptions have to agree exactly, and how that was checked

Since the migration builds the table's *real* shape and the model describes what
shape SQLAlchemy *thinks* the table has, the two must match perfectly. If the
migration created a column called `unit_of_measure` but the model spelled it
`unitOfMeasure`, every query built from the model would fail or silently target the
wrong column.

This was verified independently, not just assumed. Alembic's **autogenerate**
feature inspects the *live database* and compares it against what the models claim,
then reports the diff:

```
alembic revision --autogenerate -m "drift check"
```
```python
def upgrade() -> None:
    """Upgrade schema."""
    pass          # <- empty. Zero differences found between models and database.
```

An empty `upgrade()` is the proof — not a hope — that `app/models/fuel.py` and
`app/models/nozzle.py` describe *exactly* what migration `0003` actually built.

### 4.3 The four model classes

- **`FuelType`** (in `fuel.py`) — one row per product sold (PETROL, DIESEL, CBG, …)
- **`FuelPrice`** (in `fuel.py`) — one row per price *revision*, ever
- **`FuelMargin`** (in `fuel.py`) — one row per margin *revision*, ever
- **`Nozzle`** (in `nozzle.py`) — one row per physical meter

`FuelPrice` and `FuelMargin` share a file because they're structurally identical (see
§8) — it made sense to keep them next to each other. Every time you see
`db.get(FuelType, id)` or `select(Nozzle).where(...)` anywhere in the routers, that's
one of these four classes being used to build a real SQL query, with nobody writing
raw SQL by hand.

---

## 5. Enums — what they are and why one exists here

### 5.1 The plain-English version

An **enum** (enumeration) is a fixed, closed list of allowed values — not "any text
you want," only these specific options. A dropdown menu, not a free-text box.

`unit_of_measure` only ever makes sense as `litre` or `kilogram`. Without an enum,
this could be a plain text column, and nothing would stop `"litres"` (typo, plural),
`"kg"` (abbreviation), or `"LITRE"` (wrong case) from being inserted. Now the same
concept is spelled three different ways across the data, and any query filtering
`WHERE unit_of_measure = 'litre'` silently misses the rows spelled differently.

An enum stops this **at the database level** — Postgres itself refuses anything that
isn't exactly `litre` or `kilogram`:

```python
def test_unit_of_measure_rejects_an_unknown_value(engine):
    with pytest.raises(DBAPIError):
        connection.execute(text("SELECT CAST('gallon' AS fuel_type_unit_of_measure)"))
```

`'gallon'` gets rejected outright by the database, not by application code that could
be bypassed or forgotten.

### 5.2 Why there's a separate Python variable for it

```python
unit_of_measure_enum = postgresql.ENUM(
    "litre", "kilogram", name="fuel_type_unit_of_measure", create_type=False
)
```

This line alone creates nothing. It's a Python object *describing* a Postgres type,
reused in three places:

| Where | What it does |
|---|---|
| `unit_of_measure_enum.create(op.get_bind(), checkfirst=True)` | Runs `CREATE TYPE` — makes the type exist |
| `sa.Column("unit_of_measure", unit_of_measure_enum, nullable=False)` | Uses it as a column type, same as `sa.Text()` or `sa.Boolean()` |
| `unit_of_measure_enum.drop(op.get_bind(), checkfirst=True)` | Runs `DROP TYPE` on the way down |

### 5.3 Why not let SQLAlchemy create the type automatically?

If the column were written inline —
`sa.Column("unit_of_measure", postgresql.ENUM("litre", "kilogram", name="..."))` —
SQLAlchemy would "helpfully" auto-run `CREATE TYPE` as a side effect of
`create_table()`. Convenient, until `downgrade()`: `op.drop_table("fuel_types")` only
drops the *table*, never the type that column secretly created. Run
`upgrade → downgrade → upgrade` and the second `upgrade()` tries `CREATE TYPE` again
on a type that's still there from before — Postgres throws `type already exists`, and
the migration chain is permanently broken.

`create_type=False`, plus the explicit `.create()` / `.drop()` calls, sidestep this
entirely. This is the exact same pattern `0002` already used for `membership_role`
— nothing new was invented here, the convention was just followed.

### 5.4 The Python-side twin: `app/core/units.py`

```python
class UnitOfMeasure(StrEnum):
    litre = "litre"
    kilogram = "kilogram"
```

`StrEnum` means the member compares equal to its own string value —
`UnitOfMeasure.litre == "litre"` is `True`. That keeps the Python code, the JSON API,
and the Postgres enum all speaking one vocabulary with no translation layer between
them. Every API response that involves a quantity echoes this value back, so a
client never has to *assume* litres — it reads `unit_of_measure` and knows for
certain.

---

## 6. Database schema — the four tables

Every table follows the project's baseline (`id UUID PRIMARY KEY`, `created_at`,
`created_by`) unless documented otherwise.

### `fuel_types` — the product catalogue

```
id, code (unique), display_name, unit_of_measure (enum), max_flow_rate_per_minute,
is_active, created_at, created_by
```

**No `outlet_id`.** A litre of petrol is a litre of petrol at every outlet — this is
global reference data, not tenant-scoped, per `CLAUDE.md` §5.0's landing schedule.

**`code` and `unit_of_measure` are frozen after creation.** Enforced at the API layer
(see §11 below) — changing a unit would retroactively reinterpret every quantity ever
recorded against that fuel.

Seeded with 4 rows: `PETROL`, `DIESEL`, `PREMIUM_PETROL` (all `litre`), `CBG`
(`kilogram`). Nothing else is seeded — see §7.

### `nozzles` — the physical meters

```
id, outlet_id, label, dispenser_label, fuel_type_id, totalizer_max_value,
meter_installed_at, is_active, created_at, created_by
```

**Carries its own `outlet_id`** — not derivable from anything else in the row.
`totalizer_max_value` is the mechanical rollover ceiling (`CLAUDE.md` §4.3: totalizers
wrap like an odometer). Required at creation, because Phase 5's sales math cannot
compute a rollover without knowing where the meter wraps.

**Unique per outlet, not globally**: `UNIQUE (outlet_id, label)`. `"DU-1/N-1"` is a
label a second outlet would also use.

Seeded with **zero rows** — see §7.

### `fuel_prices` and `fuel_margins` — structurally identical, both append-only

```
id, outlet_id, fuel_type_id, rate_per_unit / margin_per_unit,
effective_from, entered_by, created_at, created_by
```

**Append-only** means: never `UPDATE`, never `DELETE`. Every price or margin change
is a brand-new row with a later `effective_from`. This is the single most important
idea in the whole migration.

**Why:** if there were one mutable `current_price` column, the moment it changed,
every past report calculated from it would start returning wrong numbers for old
dates too — there would be no way to reconstruct what the price *used to be* on any
given day. Effective-dating turns "what was the price on March 3rd" into an
answerable question forever, not just until the next revision.

**The unique constraint does double duty**:
```sql
UNIQUE (outlet_id, fuel_type_id, effective_from)
```
It stops two prices for the same fuel becoming effective at the exact same instant,
*and* its backing index is exactly the access pattern the lookup function needs (see
§9). No extra index required.

---

## 7. Seeding — what it is, and what got seeded

**Seeding** = pre-populating a table with rows as part of a migration, instead of
requiring a human to enter them through the app afterward.

Only `fuel_types` got seeded. The reasoning splits data into two kinds:

| Kind | Example | Seeded? |
|---|---|---|
| Facts about the world that don't need confirming | "we sell PETROL, DIESEL, PREMIUM_PETROL, and CBG today" | ✅ Yes |
| Real-world figures nobody has supplied yet | a specific meter's rollover ceiling, today's actual rate, the actual dealer commission | ❌ No |

Fuel types needed seeding because Phase 5's sales math needs *some* fuel type to
attach a nozzle reading to — it can't wait for someone to manually POST these first.

Nozzles, prices, and margins were left **empty on purpose**. A guessed rollover
ceiling or a guessed CBG rate would sit in the database looking exactly like real,
trustworthy data — until someone tries to reconcile against the physical meter and
can't explain the mismatch. An empty table honestly says "nobody has entered this
yet." A guessed one lies by omission. This is directly tested:

```python
def test_nothing_unverified_is_seeded(engine):
    """A plausible guess in a money table looks exactly like data, which is worse
    than an empty table -- an attendant could enter readings against a nozzle that
    does not exist."""
    assert counts == {"nozzles": 0, "fuel_prices": 0, "fuel_margins": 0}
```

**One honest exception:** CBG's flow-rate ceiling (`15` kg/min) *is* a guess, and it's
flagged as one directly in the code — a real CBG dispenser does 3–8 kg/min, and 15 is
set deliberately high. The difference from a nozzle or price guess: this number is a
*safety ceiling that fails safe when wrong* (too generous just misses a typo; too
tight would reject real sales), not a figure that directly corrupts a money total.
It's on `CLAUDE.md` §14's open-questions list to confirm before Phase 5 uses it.

---

## 8. Dealer margin — the newest domain concept

You supplied two facts that reshaped this phase:

1. **CBG**: IOCL deducts `(retail_rate − 2.28) × kg` from a running ledger balance.
   The ₹2.28 is fixed.
2. **Petrol/diesel**: when retail rises ₹1/litre, the next tanker invoice you pay
   also rises ₹1/litre. The *gap* — the margin — doesn't move.

Put together: **margin is the stable number, price is not.** That means:

```
dealer_profit = quantity_sold × margin_at(fuel_type, at)
```

...is computable straight from the nozzle totalizer, with **zero** purchase
invoices, **zero** stock tracking, **zero** IOCL integration.

**Why `fuel_margins` is its own table, not a column on `fuel_prices`:** price revises
often (daily, per `CLAUDE.md` §4.1), margin revises almost never (only when the OMC
changes the commission). Sharing one row would force re-entering an unchanged margin
on every price update — and the first time someone forgot, that period's profit
would silently be null or wrong. Independent revision schedules need independent
effective-dating.

**Pinned directly with a test**: enter a margin once, revise the price *twice*
afterward, and confirm the margin lookup still returns the original figure at every
point in time along the way. If this test ever fails, storing margin directly
(instead of deriving it from purchase invoices) was the wrong call.

**What this figure deliberately excludes**: stock revaluation. Holding 12 kL of
petrol when the price rises ₹1/litre is a real gain this system will never see,
because nothing moved through a nozzle. `CLAUDE.md` §13 documents this explicitly —
"profit" here means *gross fuel margin on quantity sold*, not full business profit.

### 8.1 Any fuel's margin can be entered — not just CBG's

`POST /api/v1/fuel-margins` takes `fuel_type_id` as an ordinary part of the request
body:

```python
class FuelMarginCreate(BaseModel):
    fuel_type_id: UUID
    margin_per_unit: condecimal(max_digits=12, decimal_places=2, gt=0)
    effective_from: datetime
```

CBG's ₹2.28 was just the one concrete example used while building and testing this.
Nothing about the endpoint is CBG-specific — an admin can enter petrol's margin,
diesel's, premium petrol's, or any fuel type added later, through this exact same
route.

### 8.2 "Changing" a margin later means adding a row, never editing the old one

This is worth being very precise about, because it's the whole point of the table's
design. Say diesel's margin today is ₹3.20/litre, and six months from now the OMC
revises the commission to ₹3.50/litre. The system does **not**:

```sql
UPDATE fuel_margins SET margin_per_unit = 3.50 WHERE fuel_type_id = diesel
```

Instead, `POST` a brand-new row: `{fuel_type_id: diesel, margin_per_unit: "3.50",
effective_from: "<six months from now>"}`. The old ₹3.20 row **stays in the table
forever, untouched**.

Why this matters: if someone asks *"what was our diesel profit in March?"* — whether
that question is asked in March, or a year later during an audit — the answer must
come out the same both times. That's only possible if March's margin row is still
sitting there exactly as it was, even after five more margin changes have happened
since. `margin_at(diesel, at=<a day in March>)` always finds the ₹3.20 row for that
date, no matter how many newer rows exist above it, because the lookup is "the
greatest `effective_from` that is still `<=` the date asked about" — a *query*, not
an *overwrite*.

**This isn't just a promise in the API code — it's enforced at two independent
levels, proven live:**

```
1) No PATCH/PUT/DELETE route exists for fuel_margins at all -- the API only
   registers GET and POST. You can't even ask the app to edit an old margin.

2) Even bypassing the API entirely with raw SQL, the database itself refuses:

   UPDATE fuel_margins SET margin_per_unit = 999 WHERE fuel_type_id = diesel;

   ProgrammingError: table fuel_margins is append-only (CLAUDE.md 5.1); insert
   a new effective-dated row instead of modifying UPDATE
   CONTEXT: PL/pgSQL function reject_modification() line 3 at RAISE
```

That second error is the `reject_modification()` trigger from migration `0003`
actually firing on a real row. This is the "belt and braces" principle from
`CLAUDE.md` §6.6: *a client can bypass your API's validation, but it can never bypass
a database constraint.* The missing PATCH endpoint is a nice UX signal; the trigger
is the actual guarantee.

**The full lifecycle, going forward:**

1. Enter CBG's margin today: `POST` with `fuel_type_id=CBG`, `effective_from=today`.
2. Whenever diesel's commission is known: `POST` again with `fuel_type_id=diesel` —
   a completely separate row, doesn't touch CBG's row at all.
3. Whenever the OMC revises CBG's commission, months later: `POST` a *third* row —
   same `fuel_type_id` (CBG), new `margin_per_unit`, `effective_from` set to whenever
   the new commission actually takes effect.
4. Every query about margin at any point in time — past, present, or future-dated —
   is answered by `margin_at(fuel_type_id, at)` picking whichever row was in effect
   *at that moment*. Every row before it stays permanently correct for its own
   window of time.

> **In one sentence: updating means adding, never overwriting.**

---

## 9. `app/services/pricing.py` — the most important file in the phase

Exactly two functions:

```python
def rate_at(db, *, outlet_id, fuel_type_id, at) -> Decimal
def margin_at(db, *, outlet_id, fuel_type_id, at) -> Decimal
```

### 9.1 The lookup rule

Both answer the same shape of question: *"what was the value in effect for this
fuel, at this outlet, at this exact instant?"* The rule (`CLAUDE.md` §5.1): the row
with the **greatest `effective_from` that is still `<= at`.**

```python
select(model)
.where(model.outlet_id == outlet_id, model.fuel_type_id == fuel_type_id,
       model.effective_from <= at)
.order_by(model.effective_from.desc())
.limit(1)
```

Written **once**, in a private helper `_effective_row_at`, and called by both
`rate_at` and `margin_at` with a different model class. This is the DRY rule
`CLAUDE.md` §5.1 explicitly demands — this exact query shape will be needed again in
sales (Phase 5), reporting (Phase 13), and reconciliation, and three independent
copies of it would eventually drift apart.

### 9.2 Why `<=` and not `<`

A price stamped effective at exactly 06:00:00 must be "live" starting at that exact
instant, not one microsecond later. This is a one-character difference that would
otherwise misprice the first transaction of every price-revision day — OMC revisions
land exactly on the hour. There's a dedicated test pinning this boundary.

### 9.3 Why it raises instead of returning `None`

```python
if row is None:
    raise AppError(status_code=409, code="NO_PRICE_FOR_DATE", ...)
```

This is the single most important design decision in the file. If this returned
`None`, every future caller (Phase 5's sales math, Phase 10's cash engine, Phase 13's
profit report) would need to remember a null-check. The first caller that forgets it
would multiply a real quantity by `None` — or, worse, if someone defaulted it to `0`,
silently value an entire shift's fuel sales at zero rupees. No crash, no log entry,
just a wrong number that looks completely plausible. Raising makes a missing price
**impossible to ignore**.

### 9.4 Why no extra index was needed

The lookup filters on `outlet_id`, `fuel_type_id`, `effective_from` — exactly the
three columns in the migration's `UNIQUE` constraint. Postgres already has an index
serving this query. Called out explicitly in both the migration and the service's
docstring, so nobody adds a redundant one later.

---

## 10. Cursor pagination (`app/api/cursor.py`)

### 10.1 The problem with `OFFSET`

`CLAUDE.md` forbids offset pagination (`?page=3`). The reason: if a row is inserted
while someone is paging through results, everything after that point shifts by one
position, so the reader either sees a duplicate row or skips one entirely.

### 10.2 Keyset pagination instead

Rather than "give me page 3," a cursor asks "give me the rows that sort *after this
specific row I already saw*." That's stable no matter what gets inserted elsewhere.

The cursor itself is base64 text: `"<effective_from>|<row id>"`. Not encrypted — it
doesn't need to be, it only encodes a *position*, and every row it points to is one
the caller was already authorized to see.

### 10.3 Why the tiebreaker matters

Sorting by `effective_from` alone isn't enough — two rows *can* share the exact same
timestamp (a 06:00 revision typically moves petrol and diesel simultaneously). The
actual sort key is `(effective_from DESC, id DESC)`, with the row's UUID breaking
ties, making the ordering **total** — there's always exactly one correct "next" row.

```python
tuple_(FuelPrice.effective_from, FuelPrice.id) < tuple_(last_effective_from, last_id)
```

Proven with a test that inserts 5 rows, walks through them 2-at-a-time via the
cursor, and asserts the *set* of ids returned exactly equals the set inserted — no
duplicates, no gaps.

### 10.4 Where it's used, and where it deliberately isn't

Only `/fuel-prices` and `/fuel-margins` — the two tables that grow without bound over
the outlet's lifetime. `/fuel-types` and `/nozzles` return plain capped lists (max
500 rows); they're small, bounded reference data, so a cursor would be pure
ceremony. This is a *documented, considered exception* to the §9 rule, not an
oversight — flagged directly in the code.

---

## 11. The API — four routers, ten routes

### 11.1 Counting the routes precisely

`CLAUDE.md`'s permission table and `app.openapi()` agree on exactly **10 distinct URL
paths** across the *whole app* — 8 new in this phase, plus `/health` and `/me`
carried over from Phases 1–2:

| Router | Paths it owns | Methods on each |
|---|---|---|
| `fuel_types.py` | `/fuel-types`, `/fuel-types/{fuel_type_id}` | GET+POST, PATCH |
| `nozzles.py` | `/nozzles`, `/nozzles/{nozzle_id}` | GET+POST, PATCH |
| `fuel_prices.py` | `/fuel-prices`, `/fuel-prices/current` | GET+POST, GET |
| `fuel_margins.py` | `/fuel-margins`, `/fuel-margins/current` | GET+POST, GET |
| *(earlier phases)* | `/health`, `/me` | GET, GET |

Each router contributes exactly 2 URL paths — "one path for the collection, one path
for a single item or a special view" is a consistent pattern across all four.

### 11.2 Path parameters vs. query parameters — two different mechanisms

Both showed up in this phase, and they're easy to conflate, but they do genuinely
different jobs.

A **path parameter** — `{fuel_type_id}` in `/fuel-types/{fuel_type_id}` — is *not*
"one route per id value." It's a single route *pattern* with a placeholder. One
route definition matches *any* id:

```python
@router.patch("/nozzles/{nozzle_id}")
def update_nozzle(nozzle_id: UUID, ...):
    # nozzle_id is pulled straight out of whatever was in that slot of the real URL
```

A **query parameter** — `?fuel_type_id=...` on `GET /fuel-prices` — is an optional
*filter* tacked onto the collection URL with a `?`. It doesn't create a new route at
all; it's still the exact same `/fuel-prices` path, just narrowed down:

```python
fuel_type_id: UUID | None = Query(default=None)
```

| | Path parameter | Query parameter |
|---|---|---|
| Looks like | `/nozzles/{nozzle_id}` | `/fuel-prices?fuel_type_id=...` |
| Purpose | *Identifies* one specific resource | *Filters* a list of results |
| Required? | Always (part of the URL's structure) | Usually optional |
| Example here | "PATCH the nozzle with **this exact id**" | "GET prices, but only for **this fuel**" |

### 11.3 `fuel_types.py` — `GET/POST /fuel-types`, `PATCH /fuel-types/{id}`

**Admins can add new fuel types at runtime.** Originally this table was going to be
read-only and fully seeded. But the outlet may add products later (XP-95, Extra
Green), and requiring a code deploy + migration just to add a product already sold
would be the wrong tradeoff. So `POST` exists — proven with a test that creates a
brand-new fuel type and immediately attaches a nozzle to it, with zero code changes.

**`code` and `unit_of_measure` are permanently frozen.** In Pydantic, this is done by
simply not including those fields on the update schema:

```python
class FuelTypeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = None
    max_flow_rate_per_minute: ... | None = None
    is_active: bool | None = None
```

`extra="forbid"` means a client sending `{"unit_of_measure": "kilogram"}` gets a
**422 rejection**, not a silent no-op. That distinction matters enormously: a silent
ignore means an admin walks away believing the change worked, when actually every
historical sale for that fuel is now being misinterpreted. An explicit rejection is
loud and safe; a silent ignore is quiet and dangerous.

**No delete, only deactivate.** `CLAUDE.md` §3 rule 6 forbids hard deletes. Instead:
`PATCH /fuel-types/{id}` with `{"is_active": false}`. A deactivated fuel disappears
from the default `GET /fuel-types` list, but the row and everything historical
referencing it stays fully intact.

### 11.4 `nozzles.py` — `GET/POST /nozzles`, `PATCH /nozzles/{id}`

Same immutability pattern for `fuel_type_id` and `totalizer_max_value` — changing
either would retroactively reinterpret every historical reading taken through that
nozzle. Stated directly in the code: **a rewired or re-metered nozzle is a new row**,
not an edit.

**Introduces the row-scoped permission resolver.** Every earlier permission check
resolved "which outlet" from config (`DEFAULT_OUTLET_ID`) — fine for *creating*
something new, since nothing exists yet to check against. Editing an *existing*
nozzle needs to check permission against the outlet **that nozzle actually belongs
to**:

```python
def resolve_outlet_from_nozzle(nozzle_id, db) -> UUID:
    outlet_id = db.execute(select(Nozzle.outlet_id).where(Nozzle.id == nozzle_id))...
    if outlet_id is None:
        raise AppError(404, "NOZZLE_NOT_FOUND", ...)
    return outlet_id
```

In V1 with one outlet this always matches config anyway, but the code is written so
that the day a second outlet exists, this fails safely instead of silently
authorizing someone against the wrong outlet's data.

### 11.5 `fuel_prices.py` and `fuel_margins.py` — deliberately near-identical

Both support `POST` (admin only), `GET` (cursor-paginated), and `GET /current`
(today's rate/margin for every active fuel, via the same `rate_at` / `margin_at`
functions used everywhere else — so the endpoint and the lookup can never disagree).

**Timezone strictness.** `effective_from` must include a UTC offset:

```python
if value.tzinfo is None or value.utcoffset() is None:
    raise ValueError("effective_from must include a timezone offset...")
```

06:00 IST and 06:00 UTC are 5.5 hours apart, and OMC price revisions happen exactly
at 06:00 IST. A silent wrong-timezone guess would misprice five and a half hours of
a shift and nobody would notice — the number would still look plausible.

**Backdating is allowed, on purpose, but never silently.**

```python
is_backdated = payload.effective_from < datetime.now(tz=timezone.utc)
if is_backdated:
    logger.warning("backdated fuel price entered", extra={...})
```

Forbidding backdating outright would mean a rate someone forgot to enter for two
days has no legal way to ever be corrected — those days stay permanently mispriced.
So it's allowed, but flagged in the response (`"is_backdated": true`) and logged at
WARNING — visible, not hidden.

---

## 12. Anatomy of one request, start to finish

There are, confusingly, **two completely different kinds of class** in this codebase
that both get casually called "models." Untangling which is which resolves most of
the remaining confusion about how a request actually gets handled:

| | SQLAlchemy ORM models (`app/models/`) | Pydantic schemas (defined in router files) |
|---|---|---|
| Examples | `FuelType`, `Nozzle`, `FuelPrice` | `FuelTypeCreate`, `NozzleUpdate`, `FuelPricePage` |
| Describes | A row in the **database** | A JSON shape in an HTTP **request or response** |
| Used for | Building SQL queries — `select(FuelType)...` | Validating input, shaping output |
| Involved when you write `fuel_type_id: UUID \| None = Query(...)` | **Never** | **This is it** |

`Query(default=None)` on a function signature is pure Pydantic/FastAPI validation —
the same mechanism as validating a request body, just sourced from the URL's
`?key=value` part instead. It has **zero** connection to the `FuelType` / `FuelPrice`
ORM classes in `app/models/`.

### 12.1 What actually happens for `GET /api/v1/fuel-prices?fuel_type_id=...`

Three independent stages run, strictly in order:

**Stage 1 — Pydantic validates the *shape*, before your function body runs at all.**
FastAPI reads the function signature:

```python
def list_fuel_prices(
    fuel_type_id: UUID | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
    actor: Actor = Depends(require_role(Role.attendant)),
    db: Session = Depends(get_db),
) -> FuelPricePage:
```

and checks: is `fuel_type_id` present? If so, does it parse as a UUID? If it's
malformed, the request is rejected immediately — the function body below never
executes, and the database is never touched. Verified with a real request:

```
GET /api/v1/fuel-prices?fuel_type_id=not-a-real-uuid-at-all

422 {
  "detail": [{"type": "uuid_parsing", "loc": ["query", "fuel_type_id"],
              "msg": "Input should be a valid UUID, invalid character..."}],
  "code": "VALIDATION_ERROR"
}
```

**Stage 2 — your own plain Python code decides what to *do* with the validated
value.** This is not automatic model behavior — it's an ordinary `if` statement,
written by hand:

```python
statement = (
    select(FuelPrice)
    .where(FuelPrice.outlet_id == actor.outlet_id)
    .order_by(FuelPrice.effective_from.desc(), FuelPrice.id.desc())
)
if fuel_type_id is not None:
    statement = statement.where(FuelPrice.fuel_type_id == fuel_type_id)
```

Two requests prove the branch matters:

```
GET /api/v1/fuel-prices?fuel_type_id=<a real, well-formed UUID>
  -> 200 {"items": [], "next_cursor": None}
  -- Pydantic accepted the SHAPE. The `if` block added a WHERE clause.
     The query DID run; it found zero matching rows because nothing has
     that fuel_type_id.

GET /api/v1/fuel-prices                       (fuel_type_id omitted)
  -> 200 {"items": [], "next_cursor": None}
  -- fuel_type_id defaulted to None. The `if` block was skipped entirely,
     so the query ran WITHOUT any fuel_type_id filter at all.
```

**Stage 3 — SQLAlchemy translates whatever query your code built into real SQL, and
sends it.** SQLAlchemy has no opinion of its own here — it does exactly what
`select(...)` / `.where(...)` calls your Python code actually made, nothing more and
nothing automatic.

### 12.2 The full pipeline, in one line

```
HTTP request
  → Pydantic/FastAPI validates & parses input shapes (Query, Body, path params)
  → dependencies run (auth check, role check, DB session opened)
  → YOUR function body runs, with already-validated Python values
  → your code decides which SQLAlchemy filters to build (plain `if` statements)
  → SQLAlchemy translates the resulting query object into real SQL
  → the database executes it, rows come back
  → SQLAlchemy converts rows back into ORM model objects (e.g. FuelPrice)
  → your code converts those into a Pydantic response schema
  → FastAPI serializes that to JSON and sends the HTTP response
```

Nothing in this pipeline is magic — every arrow is either a well-known library
(Pydantic, SQLAlchemy) doing exactly one job, or plain Python code you can read
top to bottom.

---

## 13. Two real bugs the tests exposed

Writing thorough tests surfaced two latent bugs in **Phase 1 code**, neither related
to CBG — they just hadn't been exercised until this phase.

### Bug 1 — the 422 error handler crashed on `Decimal` input

When Pydantic rejects a field (say, a negative price), FastAPI's
`RequestValidationError.errors()` echoes the bad value back in the error detail. For
a `condecimal` field, that echoed value is a Python `Decimal` — and `json.dumps()`
doesn't know how to serialize one. The error-handling code itself crashed, so the
client got an opaque `500` instead of a helpful "your rate must be positive"
message — precisely on money fields, where clear errors matter most.

**Fix:** wrap the error payload with FastAPI's `jsonable_encoder()`. A regression
test now pins this permanently in `tests/test_errors.py`.

### Bug 2 — Alembic was silently disabling application logging

Alembic's `fileConfig()` defaults to `disable_existing_loggers=True` — a Python
logging quirk that disables every logger already created at the time it's called,
including every `app.*` logger. Since the test suite imports the app *and* runs
migrations in the same process, `logger.warning(...)` calls throughout the app were
silently going nowhere, depending purely on import order.

**Fix:** `fileConfig(config.config_file_name, disable_existing_loggers=False)` in
`alembic/env.py`, with a comment explaining exactly why the default is dangerous
here.

> Both bugs were found the way `CLAUDE.md` §10 requires: write the failing test
> first, watch it fail, then fix it, then show the test passing.

---

## 14. Testing summary

**182 tests, 97% line coverage.**

| File | Covers |
|---|---|
| `test_pricing.py` | `rate_at` / `margin_at` directly — no HTTP involved |
| `test_migrations.py` (+30 tests) | Schema shape, constraints, triggers, seed data |
| `test_fuel_types.py` | Fuel type CRUD, immutability, admin-only writes |
| `test_nozzles.py` | Nozzle CRUD, CBG unit end-to-end, row-scoped permissions |
| `test_fuel_prices.py` / `test_fuel_margins.py` | Full HTTP flow, backdating, pagination walks |

All tests hit a **real PostgreSQL instance**, never SQLite — SQLite doesn't enforce
foreign keys, CHECK constraints, or triggers the way Postgres does, so a green
SQLite suite would give false confidence about exactly the rules that matter most
here (the append-only trigger, the positive-value CHECKs, and so on).

---

## 15. Quick-reference glossary

| Term | Meaning |
|---|---|
| **Migration** | A numbered, chained file describing one schema change |
| **`upgrade()` / `downgrade()`** | Move the schema forward / backward one version |
| **`alembic_version` table** | Tracks which migration the database is currently at |
| **ORM model** (SQLAlchemy) | A Python class mapping to a database table, used to build queries against a table that already exists |
| **Mapped column** | One attribute on an ORM model that maps to one real database column |
| **Pydantic schema** | A Python class describing an HTTP request/response JSON shape — a different thing from an ORM model, despite both being called "models" colloquially |
| **Enum** | A closed list of allowed values, enforced by the database |
| **Seeding** | Pre-populating a table with rows as part of a migration |
| **Append-only** | A table that is only ever inserted into, never updated or deleted |
| **Effective-dated** | A row that becomes "live" from a timestamp onward, rather than overwriting the previous value |
| **Path parameter** | A placeholder inside a URL pattern (`/nozzles/{id}`) that identifies one specific resource |
| **Query parameter** | An optional `?key=value` filter on a URL, not a separate route |
| **Keyset / cursor pagination** | Paging by "rows after this specific row," not by page number — stable under concurrent inserts |
| **Row-scoped resolver** | A permission check that reads "which outlet" from the row being acted on, not from config |
| **`extra="forbid"`** | A Pydantic setting that rejects unexpected fields with a 422, instead of silently ignoring them |

---

## 16. Open items, carried forward

Recorded in `CLAUDE.md` §14 so they aren't lost:

- CBG's real max flow rate (currently a deliberately generous guess of 15 kg/min) —
  confirm before Phase 5 consumes it.
- Petrol/diesel dealer commission figures — `fuel_margins` currently only has CBG's
  ₹2.28 as a worked example in the tests; nothing is seeded for the liquid fuels.
- The §6.4 / §5.2 contradiction: `CLAUDE.md` subtracts `cash_expenses` from expected
  drawer cash, but `expenses` has no payment-mode column, so every expense is
  currently implicitly cash. Needs a decision before Phase 7.
