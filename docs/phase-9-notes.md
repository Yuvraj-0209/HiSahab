# Phase 9 — Credit (Udhaar): Notes

> The phase where the system learns who owes it money. Written as a learning reference, not a
> spec. For the authoritative rules, see `CLAUDE.md`.

---

## 1. The big picture

Phases 6 and 7 recorded money that arrived and money that left. Neither explained the largest
gap on a real day at this pump: fuel that went through a nozzle against a customer's name with
no cash behind it. §6.4 derives expected cash by subtracting `credit_sales_amount` from metered
sales, and until this phase there was nothing to subtract — which is precisely why §6.8 refuses
to block a shift close when collections do not match sales.

### What shipped

| Piece | File(s) |
|---|---|
| Phase 8 audit + the check-then-insert race family | `app/core/errors.py`, `tests/test_errors.py` |
| Spec amendment (§5.1, §5.2, §6.6, §6.9, §8, §10, §13, §14) | `CLAUDE.md` |
| Migration `0012` — three tables + `credit_repayment_mode` | `alembic/versions/0012_credit.py`, `app/models/credit.py`, `app/core/credit.py` |
| Outstanding, limits, reversals | `app/services/credit.py` |
| Three routers | `app/api/v1/credit_customers.py`, `credit_sales.py`, `credit_repayments.py` |
| `link()` and `may_read()` extended | `app/services/attachments.py` |
| §6.8's last close precondition | `app/api/v1/shifts.py` |
| The customer ledger and outstanding report | `GET /credit-customers/{id}/ledger`, `GET /credit-customers/outstanding` |
| 175 new tests across 8 files, 3 existing files updated | see §7 |

931 tests total, 100% coverage on every Phase 9 module.

---

## 2. Auditing Phase 8 found the same bug Phase 8 had just fixed

Phase 8's Step 0 found that `uq_expenses_reverses_id` was missing from `_CONSTRAINT_ERRORS`,
fixed it, and — commendably — generalised the fix into a structural test reading
`pg_constraint`, so the *next* table to grow a reversal would be covered automatically.

Phase 9's audit found `uq_expense_categories_outlet_code` unmapped. Phase 8 had added that
constraint itself, in the same phase, while paying attention to this exact class of bug.

The lesson had been drawn one size too small. A reversal race is one instance of a much broader
shape this codebase uses everywhere:

```
SELECT to see whether a row exists  →  INSERT
```

Two callers pass the `SELECT` together, the unique index refuses the second `INSERT`, and the
loser gets an opaque 500 — with no way to tell whether their write landed, which is the
dangerous half. Fuel types, nozzles, prices, margins, readings, shifts, shift templates and
expense categories all had it. Eight constraints, eight 500s.

The generalisation now covers **every** `uq_*` in the schema, minus two exclusions justified in
writing at the point of exclusion: `uq_idempotency_keys_key_endpoint_user` (whose race is
§6.10's mechanism, caught inside `idempotency.begin`) and `uq_outlet_memberships_user_outlet`
(reachable only from a single-operator CLI, so mapping it would be an entry no request can
produce).

**The transferable lesson is about the shape of a generalisation, not about constraints.** When
a fix is generalised, the question worth asking is *what is the actual class of this bug* — not
*what is the class of the thing I happened to be looking at*. Phase 8 generalised along
"reversal", which was the noun in front of it. The bug was never about reversals.

`uq_credit_customers_outlet_phone` is the first constraint in this codebase to be mapped on the
day its migration landed rather than a phase later.

---

## 3. A NOT NULL receipt collides with reversals, and inheritance is the way out

§6.6 states twice, in two sections, that `credit_sales.attachment_id` is `NOT NULL` *at the
database level* — the belt to the API's braces, so a client that bypasses JavaScript still
cannot record udhaar without evidence.

§6.9 corrects a financial row by appending a reversal: a new row, negated. And a new row on
that table needs an `attachment_id`, because the column is `NOT NULL`. So the correction path
appears to demand a photograph of a cancellation — which §6.11 already refuses to do for
expenses, on the grounds that a cancellation is not a spend and there is nothing to photograph.

`expenses` escapes through a CHECK that exempts reversals:
`reverses_id IS NOT NULL OR receipt_required = false OR attachment_id IS NOT NULL`. Copying
that shape here would have meant making `attachment_id` nullable, which takes the receipt
control off *genuine customer sales* to solve a problem that only exists for cancellations.

The resolution was already in the codebase's vocabulary, and Phase 8 had used it for a
different purpose: **the reversal inherits the original's `attachment_id`**. The same
photograph, the same piece of evidence. §5.3's one-attachment-one-*live*-row rule holds
throughout — the original is reversed and so not live, the reversal is itself a reversal and so
not live, and the replacement is the single live claimant — so nothing has to be relaxed.

For expenses, inheritance was a convenience that spared somebody photographing one slip twice.
Here it is the only thing that makes the correction path exist at all. §14 now forbids the
weakening by name, and a structural test asserts that **no CHECK on `credit_sales` even
mentions `attachment_id`**, so the exemption cannot be reintroduced quietly.

---

## 4. A constraint written without thinking about reversals, and the test that caught it

Step 2 gave `credit_sales` a straightforward-looking CHECK:

```sql
quantity IS NULL OR quantity > 0
```

A quantity should be positive. Obviously.

Except §6.9's reversal negates the quantity alongside the amount — deliberately, so that a
per-fuel udhaar report nets to zero the same way the money does. Leave the quantity positive on
the reversal and the report shows 40 litres sold on credit with no money owed against them.

So every reversal of a **fuel** credit sale hit that CHECK and returned a 500. Steps 2, 3 and 4
all passed, because nothing in them reversed a fuel sale. Step 5's
`test_the_quantity_is_negated_alongside_the_amount` was the first thing to try it.

Fixed to mirror the amount rule it should have mirrored from the start:

```sql
quantity IS NULL
OR (reverses_id IS NULL AND quantity > 0)
OR (reverses_id IS NOT NULL AND quantity < 0)
```

**Two things are worth taking from this.** First, `ck_credit_sales_amount_sign` was sitting
four lines above, already sign-aware, and the quantity constraint was still written as though
reversals did not exist — proximity to the right answer is not the same as noticing it. Second,
the bug was invisible to every test that did not combine *two* features (a fuel sale *and* a
reversal). A test matrix that exercises features one at a time will not find this class of
thing.

`0012` was amended in place rather than corrected by an `0013`, because it has never been
applied outside development — unlike `0007` and `0009`, which had to correct migrations that
had already shipped.

---

## 5. Phase 8's null-handling was right for Phase 8 and wrong here

`expense_categories.py`'s PATCH skips every explicit null:

```python
for field, value in changes.items():
    if value is None:
        continue
```

with `exclude_unset` keeping "not mentioned" distinct from "explicitly set to null". That is
correct for that table: every editable column is `NOT NULL`, so an explicit null is always a
client bug, and applying it would reach the database as an `IntegrityError` and surface as an
opaque 500.

Copying it here would have made a credit limit, once set, impossible to remove. `credit_limit`
is nullable and its null is *meaningful* — §6.6's "no limit" — and lifting a cap from a
customer who has earned unlimited trust is a real operation an admin must be able to perform.

So `_NULLABLE_FIELDS` names the two columns where an explicit null applies (`credit_limit`,
`vehicle_numbers`) and everything else keeps Phase 8's skip. **The general point: a pattern
copied from a sibling module inherits that module's assumptions along with its shape.** Here
the assumption was "all editable columns are NOT NULL", which was true there and false here,
and nothing about the code's appearance said so.

---

## 6. §6.8's third precondition cannot fire, and is kept anyway

`CREDIT_SALE_MISSING_RECEIPT` had a named comment waiting for it in `shifts.py` since Phase 5.
Wiring it up revealed that **no HTTP request can make it fail**: `attachment_id` is `NOT NULL`
and every write path calls `attachment_service.link()`, which stamps `linked_at`.

This looked like exactly the dead code Phase 8 deleted — two `INSUFFICIENT_ROLE` branches that
coverage flagged as unreachable — and §14 says not to write dead code. The distinction that
justifies keeping it:

* Those branches could not be reached by **any caller, through any path**, because `attendant`
  is already the role floor. There was no execution in which they ran.
* This one has a caller; it just is not an HTTP request. A row written outside the API — a
  fixture, a data migration, the bulk import of the paper register this outlet will eventually
  want — can carry an attachment that was never linked.

That is the same argument `_CONSTRAINT_ERRORS` makes for constraints the API refuses first, and
the tests provoke it the only way it can be provoked: by writing the row directly. The comment
at the check says all of this plainly, rather than implying a live safety net.

One sub-decision worth recording: **this check counts reversed sales**, unlike §6.7's lock
precondition which excludes a reversed expense. The two ask different questions. §6.7 asks
*does a human still need to look at this?* — and a cancelled expense needs nobody. This asks
*is there evidence for every udhaar line on this day?* — and §6.9 keeps both rows precisely so
the record is complete, which makes an unlinked receipt as much a gap on the cancelled row as
on the live one.

---

## 7. A guard test that had to be inverted rather than deleted

`tests/test_collections_permissions.py` carried this, from Phase 6:

```python
def test_the_credit_sale_precondition_is_still_only_a_comment() -> None:
    ...
    assert "CREDIT_SALE_MISSING_RECEIPT" not in literals
```

It was §11's no-scaffolding-ahead rule enforced mechanically, and it did its job for three
phases. Phase 9 built the table, so it went red — correctly.

Deleting it would have been the obvious move and the wrong one. The failure it was really
guarding against is *the precondition being forgotten*, and while `credit_sales` did not exist
that meant "must not be implemented early". Once the table exists, the same concern points the
other way: it must not be skipped on the way past. So the assertion is inverted and now proves
all three preconditions landed. Same line, same purpose, opposite polarity.

**Tests written to enforce a temporary state should be re-aimed when the state changes, not
removed.** A deleted guard leaves nothing behind; an inverted one keeps the intent.

---

## 8. Where the tests went, and why two files came early

| File | Tests | Covers |
|---|---|---|
| `tests/test_credit_customers.py` | 46 | Admin CRUD, the split response shape, phone uniqueness, null semantics, the ledger |
| `tests/test_credit_sales_api.py` | 38 | Issue, correct, list; receipts; limits over HTTP; idempotency; the rate sanity check |
| `tests/test_credit_repayments.py` | 30 | Modes, the deactivation asymmetry, §6.4's `cash_total`, reversals |
| `tests/test_credit_reversals.py` | 18 | §6.9 on sales, and the attachment inheritance the `NOT NULL` depends on |
| `tests/test_credit_permissions.py` | 16 | §8's matrix plus six source-reading structural checks |
| `tests/test_credit_outstanding.py` | 12 | §6.6's arithmetic through reversals, against a real database |
| `tests/test_credit_limits.py` | 9 | The boundary, the null case, the accumulating balance |
| `tests/test_shift_close_credit.py` | 6 | §6.8's third precondition, provoked directly |

`test_credit_outstanding.py` and `test_credit_limits.py` landed with the **service** in Step 3
rather than waiting for a router, which is a departure from how Phases 6-8 sequenced things.
The reason: outstanding is the number §14 predicts the owner will check first, and a
plausible-but-wrong balance is this project's stated primary failure mode. It deserved tests
against the definition, not only against whatever an endpoint chooses to return — so the
arithmetic is pinned underneath the API rather than through it.

The ledger's own test earns the endpoint: it asserts the per-line `balance_delta` values sum
**exactly** to the figure on the customer's detail page, over a history containing a reversed
sale and a reversed repayment together. A balance nobody can take apart is one the owner has to
trust rather than verify.

---

## 9. Verification

```
931 passed  (741 at the end of Phase 8)
100% coverage on every Phase 9 module
alembic check                          ->  no new upgrade operations, head 0012
alembic downgrade base && upgrade head ->  clean
pytest twice back to back, same DB     ->  931 both times
```

Structural assertions no value test could make:

- `credit_customers` carries its own `outlet_id`; `credit_sales` and `credit_repayments`
  deliberately do not (§5.0), asserted in both directions.
- `credit_sales.attachment_id` is `NOT NULL` in `information_schema`, and **no CHECK on that
  table mentions `attachment_id`** — the reversal exemption §14 forbids cannot creep back.
- `credit_repayment_mode` exists as its own type with exactly four labels, and is neither
  `collection_mode` nor `expense_mode`.
- `downgrade()` leaves zero rows in `pg_type` for it — the trap `0008` warned about and `0010`
  was caught by.
- `is_settled` appears in no table in the schema.
- `float(` / `sa.Float` / `.offset(` / `@router.delete` / `relationship(` — zero matches across
  every Phase 9 module.
- The outstanding sum does not filter reversals, asserted per-function via `ast` so that
  `live_credit_sale_for_attachment` — which legitimately *does* filter — is not confused for it.

---

## 10. Open items, carried forward

- **The tenancy posture for `credit_customers` is the one loose end.** It returns 403
  `NOT_A_MEMBER` across outlets, not §7.3's stricter 404, on the reasoning that attendants
  cannot read customer detail at all so the sensitive fields are already role-gated. Pinned by
  a test whose docstring explains the divergence, so changing it is a decision rather than a
  drift. If a phone number and vehicle registrations deserve a receipt photo's protection, say
  so and it becomes a bespoke resolver like `attachments.py`'s.
- **D9 was never answered by the owner.** Both credit tables got §6.9's reversal columns on the
  recommendation. Cheap to change only before there are rows.
- **The real customer list and their credit limits.** Until entered, every customer is
  unlimited and `CREDIT_LIMIT_EXCEEDED` never fires — so the override path is untested against
  reality.
- **Is a credit limit used at this outlet at all?** If nobody sets one, an entire control and
  its admin-override path are dead weight that looks alive.
- **Phase 10 must decide the shortfall record's shape**: does it point at a
  `user_profiles.id` rather than a `credit_customer_id`, and is it repaid, written off, or
  deducted from wages?
- `EXPENSE_RECEIPT_THRESHOLD` = ₹5,000 is still a guess, live on real money.
- The real expense category list beyond the four seeded.
- CBG's real max flow rate in kg/min; petrol and diesel dealer commissions; whether testing
  quantities are recorded on paper.
- **The locker model's effect on §6.5's rolling balance — now due. Phase 10 is next.**

### The check no test replaces

**Take one real udhaar slip, photograph it on the phone that actually gets used, and enter the
day.** Upload the slip against a real shift, record the credit sale against a real customer,
record a part repayment in cash on a later day, then open the customer's ledger and confirm
every line matches the paper register — and that the outstanding figure is the one the owner
recognises.

That last clause is the whole point. The arithmetic is tested to the paisa and the coverage is
100%, and neither fact says the number means what the owner thinks it means. A customer's
outstanding balance is the first figure they will check and the one they already know by heart;
if it disagrees with what is in their head, the disagreement is worth more than any test in
this repository.

### What Phase 10 inherits

- **`credit_service.credit_sales_total(shift_id)`**, written and tested but with no caller
  until §6.4's equation exists — the same shape `attachments.orphans()` had between Phase 8's
  Steps 3 and 10. It is the `credit_sales_amount` term.
- **`cash_total` on the repayments list**, already filtered to `mode = cash`, so Phase 10 does
  not have to re-derive which modes touch the drawer.
- **The check-then-insert lesson from Step 0.** `daily_cash_summaries` has a
  `UNIQUE (outlet_id, business_date)` in its future, and the widened structural test will
  demand its `_CONSTRAINT_ERRORS` entry on the day that migration lands.
