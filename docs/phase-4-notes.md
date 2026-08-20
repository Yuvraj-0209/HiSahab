# Phase 4 — Shifts: Notes

> The spine every later phase hangs off, and the phase where the spec turned out to
> describe a station this outlet does not run. Written as a learning reference, not a
> spec. For the authoritative rules, see `CLAUDE.md`.

---

## 1. The big picture

Phase 4 builds `shifts` — the row that readings, collections, expenses, credit sales and
deposits will all point at. Nothing else in the project has as many things depending on
its shape, which is why the planning took longer than the coding.

### What shipped

| Piece | File(s) |
|---|---|
| Migration `0004` — 3 tables, 2 enums, 1 trigger, 1 seeded row | `alembic/versions/0004_shifts_and_audit.py` |
| ORM models | `app/models/shift.py`, `app/models/audit.py` |
| Status + audit vocabularies | `app/core/shifts.py`, `app/core/audit.py` |
| Chain and clock helpers | `app/services/shifts.py` |
| The audit writer | `app/services/audit.py` |
| Ownership dependency (§8's second axis) | `app/api/deps.py` |
| Shift cursor | `app/api/cursor.py` |
| 2 routers, 10 routes | `app/api/v1/shifts.py`, `shift_templates.py` |
| 262 tests, 98% coverage | `tests/test_shifts.py`, `test_shift_permissions.py`, `test_audit.py`, `test_business_date.py`, `test_shift_templates.py` |

Phase 3 was reshaped mid-planning by CBG. Phase 4 was reshaped by something bigger: a
description of how the outlet actually operates, which did not match §5.2 at all.

---

## 2. The spec was wrong, and finding out cost nothing

`CLAUDE.md` §5.2 originally said:

```
- shift_type — enum: morning | night
- Unique constraint on (outlet_id, business_date, shift_type)
```

and §6.1 explained the whole business-date rule in terms of a night shift crossing
midnight. Both were written from a reasonable guess about how a petrol pump works.

What actually happens here:

- The station **trades 06:00 → 22:00** and is shut for eight hours. There is **one**
  accounting period a day, not two.
- The salesmen work a **09:00 → 09:00 roster**, but the 06:00–09:00 cash is handed to the
  incoming crew — so one crew ends up holding an entire trading day's takings.
- The sales register has **one line per day**.
- The whole day is **typed in after the fact**, in one sitting. Nobody uses the app while
  trading.

The roster is real, but it is a **labour arrangement, not an accounting boundary**. The
system does not model it, and that is the right call: modelling it would force the
salesmen to invent a 09:00 cash handover that they currently settle among themselves.

Had this been discovered in Phase 6, `shift_type` would already be on live rows and the
fix would be a data migration containing a guess. Discovered in Phase 4, it was a prose
edit. This is the same lesson as the CBG rename (commit `22e8339`) and it keeps recurring:
**the cheapest moment to fix a schema is before it holds data.**

---

## 3. Why the fix was not "one shift a day"

The obvious correction was to replace `morning | night` with one record per day. It was
rejected, because the owner also intends to sell this to stations that *do* run 24 hours on
three 8-hour shifts. Hardcoding one shift a day would be the same mistake in the opposite
direction.

The model that covers both is a **chain**:

- Shifts are **sequence-numbered per business date**. Any number of them. `shift_type` is
  deleted.
- A nozzle's opening reading is **carried forward from the most recent closing reading of
  that same nozzle**, across shifts and across days.
- The attendant only ever types a **closing** value.

This outlet runs one shift a day; a 24-hour customer runs three; an eight-hour overnight
closure and a 24-hour handover are the same thing — a gap between one shift's close and the
next one's open.

Written up as `CLAUDE.md` §4.7.

---

## 4. The most important decision in the phase: confirm, don't assume

The original sketch had the opening reading auto-filled and taken as true. That is very
good UX and it hides something serious.

An attendant currently makes **two independent observations** of each meter: one at the
start of a shift, one at the end. Auto-filling the opening deletes the first one.

Walk through what that costs. Suppose 10 litres are siphoned through a nozzle at 2am:

| | Reality | What the system believes |
|---|---|---|
| 22:00 close | 1000 | 1000 |
| overnight | 1000 → 1010 | *nothing happened* |
| 06:00 open | 1010 | 1000 (chained) |
| 22:00 close | 1200 | 1200 |
| quantity sold | 190 to customers | **200** |

The system computes 200 litres sold, but only 190 produced cash. The shift is short by ten
litres' worth. And at this outlet a shortfall is **booked as udhaar against the salesman's
own name**.

So an assumed opening reading silently converts overnight theft into a **debt owed by
someone who did nothing wrong**, and leaves nothing anywhere pointing at the gap. It is a
textbook plausible-wrong-number: no crash, no error, a number that looks exactly like data.

The resolution keeps nearly all the UX: **pre-filled, not typeable, but confirmed against
the physical meter**, with a "doesn't match" path that captures the real reading and raises
it for review before anyone is blamed. Zero typing on a normal day; the abnormal day
becomes visible instead of being reassigned to a person.

Phase 4 records the rule in §4.7 and builds the chain-ordering half. Phase 5 builds the
readings and the confirmation itself.

---

## 5. The chain, stated precisely

Four details that all matter, and each of which was a bug avoided:

**5.1 The lookup is per nozzle, not per shift.** "The previous shift's reading" breaks the
moment a nozzle is out of order for one shift, is installed mid-life, or a whole day is
skipped. "The most recent closing reading *for that nozzle*" handles all three.

**5.2 The chained value is stored on the row, not computed on read.** If it were computed,
correcting shift 1 would silently rewrite shift 2's history. `CLAUDE.md` §5.2 already keeps
`expected_closing` stored for exactly this reason: you must be able to see what the system
told the manager *on the day*.

**5.3 The chain needs an anchor.** The first shift ever, and every newly installed nozzle,
has no predecessor. Both need a one-time seeded starting reading — the same shape as §6.5's
seeded first opening balance.

**5.4 A meter reset breaks the chain on purpose.** §4.3: totalizers reset to zero when
repaired. An opening that could *never* be changed would leave the chain permanently wrong.
§6.2's admin-only override is the re-anchor.

**5.5 Only one shift may be open at a time.** With two open, "the most recent closing
reading" stops having a single answer. `SHIFT_ALREADY_OPEN`, and it is also why `POST
/shifts` refuses a `business_date` earlier than the chain's tip (`SHIFT_OUT_OF_SEQUENCE`) —
a shift spliced into the middle would leave the one after it with two possible predecessors.

---

## 6. What "sequence" replaced, and why the constraint still matters

```python
sa.UniqueConstraint("outlet_id", "business_date", "sequence",
                    name="uq_shifts_outlet_date_sequence")
```

The old constraint on `shift_type` was a duplicate guard: it stopped the same shift being
entered twice. The new one does the same job and carries slightly more weight, because
`sequence` is **assigned by the server** from a read of this same table:

```python
highest = db.execute(select(func.max(Shift.sequence)).where(...)).scalar_one_or_none()
return 1 if highest is None else int(highest) + 1
```

That read-then-write is racy in principle. Two concurrent opens could read the same maximum.
The constraint is what turns the loser into an `IntegrityError` instead of a duplicate row.
In practice `open_shift()` already refuses the second caller — so it is the second of two
locks on the same door, which is the §6.6 "belt and braces" habit again.

**The client never supplies `sequence`.** `ShiftCreate` omits it and uses
`extra="forbid"`, so sending it is a 422 rather than a silent no-op. A client-chosen
sequence could overwrite or skip a link in the chain.

---

## 7. Shift templates — a small table that prevents a big error

`outlet_shift_templates` holds "the shifts this outlet usually runs":

| sequence | label | starts_at_local | ends_at_local |
|---|---|---|---|
| 1 | Day | 06:00:00 | 22:00:00 |

### 7.1 Why it exists

Days are typed in after the fact. Without defaults, somebody types 06:00 and 22:00 every
morning forever — and `started_at` is the field §6.3 uses to pick **which day's fuel rate**
values the entire shift. A rushed retype is exactly where a wrong hour comes from, and a
wrong hour is invisible: no error, just a day priced off the wrong rate.

### 7.2 Why `TIME` is not a violation of "everything in UTC"

§3 rule 4 says every timestamp is stored in UTC. This table stores `TIME`, and that is not
a breach, because **the rule governs instants**. "06:00 local, every day" is a recurring
wall-clock time — a genuinely different type that cannot be stored as an instant without
inventing a date to attach it to.

`app/services/shifts.py::template_window` is where it *becomes* an instant, for one
specific date:

```python
start = local_time_on(business_date, starts, tz_name)
end_date = business_date if ends > starts else business_date + timedelta(days=1)
```

The wrap matters. A 24-hour outlet's night template reads 22:00 → 06:00; taken naively on
one date, the end lands *before* the start and `ck_shifts_ended_after_started` refuses it —
correctly, but at the wrong layer and with an unhelpful message. This outlet's template
never wraps; the branch exists so one of the future customers is not the one to find it, and
it is covered by a test rather than left to be discovered.

### 7.3 The rule that is easiest to break by accident

**The template is read at shift creation and never again.** Its values are materialised onto
the shift row.

If the times were read back through the template, editing it would restate what a past day's
fuel was worth — silently, with no error and no audit entry. There is a test named after
this, `test_editing_a_template_does_not_revalue_a_shift_that_already_traded`, and it exists
because that would be a very easy "optimisation" for a future reader to make.

### 7.4 Three sources for a shift's clock

`_resolve_window` tries them in descending order of authority:

1. **The payload** — whoever is entering the day says what actually happened.
2. **A template for this sequence** — the outlet's usual hours.
3. **The previous shift's end time** — §4.7's chain, applied to the clock rather than the
   totalizer.

Source 3 is what makes "add another shift" work at an outlet whose template describes only
its usual day. This outlet has one template; a relief shift tacked on after it starts when
shift 1 finished and needs no template of its own.

With none of the three, the request is **refused** (`SHIFT_START_TIME_REQUIRED`) rather than
defaulted to `now()`. Since days are typed in after the fact, `now()` is routinely the wrong
day entirely. A refusal is recoverable; a plausible wrong instant is not.

One subtlety found by a test: when the caller overrides the start, the template's end is
dropped rather than kept. A template's 22:00 end only means anything alongside its 06:00
start — once the station opened at 22:00 instead, that end is not an end at all.

---

## 8. Ownership — the second permission axis

§8 has always said this, and Phase 4 is where it lands:

> Role alone cannot express this, so `require_role` handles the role floor only, and the
> shift-scoped dependency (Phase 4) applies the ownership check **only when the actor's
> role is `attendant`**.

Two users can hold the identical role and get different answers, because the question is not
about the role at all:

```python
if actor.role is Role.attendant and shift.attendant_id != actor.user.id:
    raise AppError(403, "NOT_YOUR_SHIFT", ...)
```

The `is Role.attendant` condition is load-bearing. Written unconditionally it would lock
managers out of the very shifts they exist to close.

`require_shift_access(minimum, *, writable=False)` composes three checks — role floor,
ownership, writability — and Phases 5–10 are expected to depend on it rather than
re-implementing any part of it:

```python
@router.post("/shifts/{shift_id}/collections")
def add_collection(access: ShiftAccess = Depends(
    require_shift_access(Role.attendant, writable=True)
)): ...
```

`writable=True` means "about to change financial state" and enforces §6.9. Two distinct
codes come out of it, on purpose:

| Code | Meaning | Recoverable? |
|---|---|---|
| `SHIFT_NOT_OPEN` | closed | Yes — an admin can reopen it |
| `SHIFT_LOCKED` | locked | Never |

A client should be able to tell the user which without parsing prose.

---

## 9. The audit log arrived seven phases early

§11's build order put it at step 11, with a hedge:

> *(consider building this at step 4 instead if it feels cheap to do early; retrofitting is
> the usual regret)*

It was taken, because Phase 4 is the first phase that **cannot be correct without it**. §5.2
requires backwards status transitions to be audit-logged, and §6.8 lets an admin reopen a
shift. A reopen with nothing recording who did it and why is not a feature, it is a hole.

### 9.1 The caller commits

```python
def record(db, *, outlet_id, table_name, record_id, action, changed_by,
           old_values=None, new_values=None) -> AuditLog:
    ...
    db.add(entry)      # and nothing else
```

The audit row and the change it describes land in **one transaction or neither**. A
separately committed audit log can describe a change that was later rolled back — which is
worse than having no log, because it is a log that lies. There is a test for exactly this:
a refused transition writes no audit row.

### 9.2 Money in the audit log is a string, and that took two goes to get right

`json.dumps` refuses `Decimal`, and money fields are precisely what most needs auditing.
Phase 3 already hit this bug once, in the 422 handler, where it turned every validation
error on a money field into an opaque 500. So the values are run through
`jsonable_encoder` on the way in.

The first version stopped there — and was wrong, caught by the guardrail sweep at the end
of the phase. `jsonable_encoder`'s default for `Decimal` is **`float`**:

```python
>>> jsonable_encoder({"amount": Decimal("5000.00"), "small": Decimal("0.10")})
{'amount': 5000.0, 'small': 0.1}
```

Wrong twice over. §3 rule 1 says Decimal end-to-end and never float — and the scale is
silently dropped, so an audit row can no longer show that the stored value carried two
decimal places. For a table whose entire job is answering "what was this before", that is
the one thing it must not do. `0.1` is also the exact example §3 rule 1 opens with.

The fix is a custom encoder:

```python
_ENCODERS = {Decimal: str}
```

The string is what a consumer wants anyway: `Decimal(row["amount"])` round-trips exactly,
while `Decimal(5000.0)` would reintroduce binary floating point at the very last step.

Two tests pin it: `test_decimal_values_survive_serialisation` and
`test_sub_rupee_amounts_are_not_rounded_through_a_float`. Both were written before any
money reaches this helper, which happens in Phase 6.

### 9.3 `record_id` is deliberately not a foreign key

It points at rows in many different tables, and a real FK cannot express that. It also has
to survive its target being restructured — an audit trail that breaks when the schema moves
is useless exactly when you need it.

### 9.4 Append-only, reusing 0003's function

```sql
CREATE TRIGGER trg_audit_logs_append_only
BEFORE UPDATE OR DELETE ON audit_logs
FOR EACH ROW EXECUTE FUNCTION reject_modification();
```

`reject_modification()` was created by migration `0003` for `fuel_prices` and
`fuel_margins`. `0004` reuses it and — importantly — **does not drop it on downgrade**.
Rolling back one step must not silently disarm the append-only guard on the two price
tables. `test_zero_zero_zero_three_still_owns_reject_modification` asserts all three
triggers point at the one function.

### 9.5 `request_id` is text, not uuid

`RequestIdMiddleware` honours an inbound `X-Request-ID` header, and a caller can send
anything at all in it. Verified end-to-end: sending `X-Request-ID: e2e-trace-1` produced an
audit row carrying that exact string.

---

## 10. What §6.1 survived, and why

`business_date` is still an explicit column, still never derived from a timestamp. It
survived §4.7's rewrite for **two independent reasons**, and it is worth being clear which
is which, because the first one stops applying here:

1. A 24-hour outlet's night shift crosses midnight. *(Does not apply to this outlet.)*
2. **The whole day is typed in after the fact**, so `created_at` is frequently the
   *following* calendar day. *(Applies to every outlet, including this one.)*

Reason 2 is the one that would have been missed. Someone reading "we close at 22:00, nothing
crosses midnight" could reasonably conclude the rule is now dead weight and derive the date
from a timestamp — and be wrong on every single row.

`test_the_business_date_is_not_derived_from_when_it_was_typed_in` is the test that fails the
moment anyone tries.

A related guard: a `business_date` in the **future** is always a typo, because trading has
not happened yet. Evaluated in the outlet's timezone, never UTC — between 18:30 and 24:00
UTC the outlet's date is already tomorrow, so a UTC comparison would refuse legitimate
shifts for five and a half hours every night.

---

## 11. Two problems that quietly disappeared

Worth noticing, because they were real costs in the original design:

**§6.3's mid-shift price revision.** OMCs revise at 06:00 IST, and §6.3 values a whole
shift at the rate effective at `started_at` — a documented approximation. This outlet's day
*starts* at 06:00 IST, and `rate_at`'s comparison is `<=`, so the shift picks up the new
rate and one rate covers the entire day. **Exact, not approximate.**

That is a property of these particular trading hours, not a general guarantee. A 24-hour
outlet's 02:00–10:00 shift straddles 06:00 and the approximation applies in full — which is
why the warning stays in the spec.

**§6.1's midnight crossing.** Gone for this outlet, kept for the customers who need it.

---

## 12. The lifecycle, and the one reversal

```
open ──→ closed ──→ locked
  ↑         │
  └─────────┘
   admin + reason
```

Encoded as an explicit map rather than inferred from declaration order:

```python
_ALLOWED_TRANSITIONS = {
    ShiftStatus.open:   frozenset({ShiftStatus.closed}),
    ShiftStatus.closed: frozenset({ShiftStatus.open, ShiftStatus.locked}),
    ShiftStatus.locked: frozenset(),
}
```

Same reasoning as `app/core/roles.py`'s explicit rank map: reordering the enum members is a
harmless-looking edit that would otherwise silently change which transitions are legal.

`locked` is absent as a source. It is terminal — if locked could be reopened, locking would
guarantee nothing.

**The reason lives in the audit row, not on the shift.** A column would hold only the most
recent one, and a shift reopened three times is exactly the case somebody will need to
reconstruct.

### 12.1 The limitation that was chosen over a lie

Only the **most recent** shift can be reopened (`NOT_THE_LATEST_SHIFT`).

Reopening mid-chain would let a closing reading change while the shift after it still holds
the old value as its opening, with nothing to recompute it. Phase 5 owns that cascade. Until
it exists, refusing is the honest answer — and it is recorded as §13.10 rather than left for
someone to discover.

---

## 13. Preconditions that are *not* stubbed

§6.8 names three close preconditions and §6.7 one lock precondition. All four read tables
that do not exist yet. There is no empty registry waiting for them — §11 forbids scaffolding
ahead — only a comment at the exact line each belongs on:

```python
# §6.8's close preconditions belong HERE, each with the phase that owns it:
#   MISSING_NOZZLE_READINGS      -- Phase 5, needs `nozzle_readings`
#   MISSING_COLLECTIONS          -- Phase 6, needs `collections`
#   CREDIT_SALE_MISSING_RECEIPT  -- Phase 9, needs `credit_sales`
```

The reasoning is worth stating: **an empty check that always passes is indistinguishable
from a check that was forgotten.** A comment cannot be mistaken for a working guard.

---

## 14. A contradiction found while planning, recorded before it bites

§5.2 makes `credit_sales.attachment_id` `NOT NULL` **at the database level** — every udhaar
row must carry a receipt photo, enforced so a client cannot bypass it.

But this outlet books a salesman's cash shortfall as udhaar **against his own name**, and a
shortfall has no receipt. There is nothing to photograph.

So one of two things has to give, and they are not equivalent:

- Weaken the `NOT NULL` → silently removes the receipt control from genuine *customer*
  credit sales too.
- Give shortfalls their own table → keeps the control intact.

**Recommendation carried into Phase 9: a separate table.** A shortfall is a different
economic event — the outcome of a reconciliation, not a sale — and a customer's outstanding
balance should not be polluted by staff debts.

Nothing was built for it. It is on §14's open list, which is the point: it costs nothing now
and would be ugly once `credit_sales` holds rows.

---

## 15. Bugs the tests caught

**The fixture teardown order.** `make_user` deleted `user_profiles` while shifts still
referenced them. pytest tears fixtures down in reverse *setup* order, and a
`usefixtures`-declared fixture is set up first — so a `clean_shifts` cleanup would have run
*after* the foreign key already blew up. Fixed the way the existing fixtures do it:
children first, inside `make_user` itself.

**A fixture with a hardcoded start date.** `make_shift` derived `business_date` from its
argument but pinned `started_at` to a fixed instant, so any test passing a different date got
a shift whose start was months away from it — and tripped
`ck_shifts_ended_after_started` on close. The constraint caught a test bug, which is what
constraints are for.

**A template end outliving its start.** Overriding `started_at` while keeping the template's
`ended_at` produced "ends before it starts" — an error about a field the caller never sent.
Now the template's end is dropped when it no longer follows the start.

**Money serialised as a float in the audit log** (§9.2). Not caught by a test — every test
passed — but by the §14 guardrail sweep at the end of the phase, which is exactly the kind
of thing that checklist exists for. A green suite is not the same as a correct one when the
rule being broken is "never float" and nothing yet writes money through the path.

---

## 16. Verification

```
262 tests passing, 98% coverage (Phase 3 left it at 97%)
Every Phase 4 module at 100%
alembic downgrade -1 / upgrade head clean — the enum-leak trap avoided
alembic revision --autogenerate → empty upgrade(), no model/migration drift
```

Plus a full 14-step walkthrough against a real `uvicorn` server on a clean database: open,
one-at-a-time, future date, role floors on close and lock, locked-is-final, sequence 2
chaining from shift 1's end time, reopen with and without a reason, not-the-latest,
ownership, scoped listing, and backwards-date. The audit table afterwards held exactly nine
rows — **and none for any of the refused calls**.

---

## 17. Quick-reference glossary

| Term | Meaning |
|---|---|
| **The chain** | Each nozzle's opening reading carried forward from its own most recent closing reading, across shifts and days |
| **Chain tip** | The latest shift at an outlet, ordered by `(business_date, sequence)` |
| **Confirm, don't assume** | A chained value is pre-filled but must be checked against the physical meter, never taken as fact |
| **Sequence** | The shift's number within its business date, server-assigned; replaced `shift_type` |
| **Shift template** | An outlet's usual shift hours, supplying a default at creation and never read again |
| **Ownership axis** | The permission question role cannot answer: "is this the attendant's own shift" |
| **Terminal status** | `locked` — a state with no legal transition out of it |
| **Append-only** | A table only ever inserted into; enforced by trigger, not just by the absence of a route |
| **Materialised default** | A default copied onto a row at creation so that changing its source cannot rewrite history |

---

## 18. Open items, carried forward

Recorded in `CLAUDE.md` §14 so they are not lost:

- **The salesman-shortfall vs receipt contradiction** (§14 above) — decide before Phase 9.
- **Phase 10's locker model.** Cash is not counted at a fixed moment and the drawer is never
  emptied on a schedule, so §6.5's "opening balance = yesterday's actual counted" needs
  restating for a *running locker* rather than a daily drawer.
- Do salesmen hold a change float overnight, counted separately from the locker?
- The §6.4 / §5.2 `cash_expenses` contradiction, still open from Phase 3 — decide before
  Phase 7.
- CBG's real max flow rate (still the generous guess of 15 kg/min) — **Phase 5 consumes it
  next**, so this one is now urgent.
- Petrol and diesel dealer commissions — still nothing in `fuel_margins` for them.

### What Phase 5 inherits

- `latest_shift()` — the chain anchor to read opening readings from.
- `require_shift_access(..., writable=True)` — the guard every reading write hangs off.
- `audit.record()` — already wired; readings just call it.
- The reopen cascade to implement, which lifts §13.10's limitation.
