# Phase 8 — Expense Categories & Attachments: Notes

> The phase where the system learns to hold a document, not just a number. Written as a
> learning reference, not a spec. For the authoritative rules, see `CLAUDE.md`.

---

## 1. The big picture

Phase 7 recorded what the pump paid out, against a fixed four-value category. Phase 8
started as "add receipts" and grew into three connected changes once the owner asked two
questions before any code existed: whether a receipt was ever mandatory on an expense (it
wasn't — only on a credit sale), and whether categories could be admin-managed the way
`fuel_types` already are (they should be, for exactly the reason §5.1 already gives).

### What shipped

| Piece | File(s) |
|---|---|
| Phase 7 audit + one fix (`uq_expenses_reverses_id` unmapped) | `app/core/errors.py`, `tests/test_errors.py` |
| Spec amendment (§5.1, §5.2, §5.3, §6.7, §6.11, §7, §8, §10, §14, §16) | `CLAUDE.md` |
| Migration `0010` — `expense_categories`, `expenses.category → category_id` | `alembic/versions/0010_expense_categories.py`, `app/models/expense_category.py`, `app/api/v1/expense_categories.py` |
| Migration `0011` — `attachments`, `expenses.attachment_id`/`receipt_required` | `alembic/versions/0011_attachments.py`, `app/models/attachment.py`, `app/models/expense.py` |
| Content sniffing, no I/O | `app/core/uploads.py` |
| Storage protocol, two implementations | `app/services/storage.py` |
| Attachment orchestration | `app/services/attachments.py` |
| Two routers | `app/api/v1/uploads.py`, `app/api/v1/attachments.py` |
| §6.11 wired into expenses | `app/api/v1/expenses.py`, `app/services/expenses.py` |
| Month-end summary | `GET /api/v1/expenses/summary` |
| Cleanup job | `app/jobs/cleanup_attachments.py` |
| 141 new tests across 9 new files, 5 existing files updated | see §6 |

741 tests total, 100% coverage on every Phase 8 module bar one uncovered `__main__` guard
line — the same one both pre-existing jobs already carry.

---

## 2. Auditing Phase 7 found one defect, and that itself is a data point

Phases 6 and 7 each found three real defects in the phase before them, every time in code
that had passed its own checklist. Phase 8's audit of Phase 7 found **one**:
`uq_expenses_reverses_id` — the unique constraint stopping a double reversal — had never
been added to `_CONSTRAINT_ERRORS`. Two managers reversing the same expense at the same
instant would both pass the service-level `ALREADY_REVERSED` check and race on the
database constraint; the loser got an opaque 500 with no way to tell whether their
reversal had landed. Phase 6 had fixed the identical bug on `collections`; Phase 7 copied
the reversal shape onto `expenses`, constraint included, but not the line that makes the
race readable.

Fixed test-first, and the fix was written to generalise rather than patch the one
instance: `test_every_reversal_unique_constraint_is_mapped_to_a_business_error` reads
`pg_constraint` directly for every `uq_%_reverses_id` in the live schema and asserts each
one is in the allowlist. Phase 9's `credit_sales` will pick this up automatically the day
its own migration lands, whether or not whoever writes it remembers this specific lesson.

That the audit found one defect instead of three is worth reading as a signal that the
Step-0-audit habit is paying off — Phase 7's own Step 0 caught three Phase 6 defects
before any Phase 7 code, and that discipline appears to be holding downstream.

---

## 3. A category is not a fuel type, but the argument is the same shape

§5.2 originally said "keep expense categories an enum, not free text." §5.1 already said
the opposite thing about `fuel_types`, one table up: an outlet sells products this
codebase cannot anticipate, and needing a migration to add one you already sell is wrong.
Once the owner asked for admin-managed categories, applying `fuel_types`'s exact shape —
immutable `code`, editable `display_name`, deactivate rather than delete — was not a new
decision, just recognising the same argument applied here too.

The part that needed care was what the enum was *actually* protecting against, which
turned out to be two different things wearing one rule:

1. **Free text**, where `Tea`/`tea`/`TEA` become three categories and §6.7's aggregate
   silently stops working. `ck_expense_categories_code_format`
   (`^[A-Z][A-Z0-9_]*$`) plus normalisation in the API keeps this closed — a code is
   still a controlled value, just not a fixed list of them.
2. **A specific, named exclusion**: `fuel_purchase` was removed from the Phase 7 enum
   because a tanker restock settles against the bank, never the drawer, and recording one
   as an expense would invent a cash shortfall §6.4 never actually had. The enum made this
   *structurally impossible*. An admin-managed table makes it merely *undocumented if
   nobody says otherwise* — so the guardrail moved from the schema to §14's prose and this
   phase's `create_expense_category` docstring, and two tests that used to prove
   impossibility now say plainly that they only check the seed. A green suite here is not
   the same safety net it used to be, and pretending otherwise would be the more dangerous
   mistake.

---

## 4. The collision between "one attachment, one row" and reversals

The plan flagged this during design, before any code: §6.9 corrects an expense by
appending a reversal and, optionally, a replacement — never by editing. If receipts follow
a naive "one attachment, one expense" rule, correcting a ₹5,000 receipt-required expense
down to ₹4,800 would leave the replacement with no receipt to point at, and the owner
would be asked to photograph the same piece of paper a second time for no reason.

The fix was already sitting in the codebase's own vocabulary. §5.2 defines a *live*
collection as one that is "not itself a reversal, and not referenced by one," specifically
so that `live_collection_for_mode` can tell a cancelled row from an active one without a
second column tracking it. The same definition, applied to attachments: `link()` checks
whether any *live* expense already claims an attachment, and by the time a reversal's
replacement is built, the reversal has already flushed — so the original is no longer
live, and the replacement's inherited `attachment_id` is never blocked by the very check
that stops a fresh expense from stealing someone else's receipt.

`receipt_required` on the replacement is still re-evaluated against its own amount, not
copied from the original. A correction that pushes a small, no-receipt-required expense
over the threshold, with nothing to inherit, is refused with the same
`EXPENSE_REQUIRES_RECEIPT` a `PATCH` would give — the rule does not get weaker just
because the write happens inside a reversal.

---

## 5. §7.3's own text overrode an existing, accepted pattern

Every outlet-scoped resource in this codebase resolves its outlet from the row and checks
membership through `require_role`, and a caller from another outlet gets 403
`NOT_A_MEMBER` — proven correct for shifts by `tests/test_shift_permissions.py::
test_an_admin_at_another_outlet_is_not_a_member_here`, which exists specifically to pin
that behaviour down.

Writing the equivalent cross-outlet test for `GET /attachments/{id}/url` failed against
exactly that expectation, and the reason was in the spec the whole time: §7.3 says an id
belonging to another outlet must return 404, not 403 — "existence is not leaked across
tenants." A 403 already confirms the id is real; it just tells the caller it belongs to
somewhere they cannot see. For a shift id that leak is harmless. For a receipt — which can
be a photograph of a cheque, an invoice with a phone number on it, anything a vendor
handed over — it is not, and §7.3 is explicit that this resource gets a stricter rule.

The fix was to stop composing the generic `require_role` for this one route.
`_attachment_access` in `app/api/v1/attachments.py` folds "no membership at this
attachment's outlet" into the same 404 as "this id does not exist at all," and a comment
at the point of divergence explains why this route does not look like every other
outlet-scoped route in the codebase — because it is not supposed to.

---

## 6. Two branches that coverage caught as dead, not two bugs

Both new routers — `uploads.py`'s form-based shift access and `attachments.py`'s
`_attachment_access` — originally carried an `INSUFFICIENT_ROLE` branch copied from the
`require_role` pattern they were standing in for. Coverage flagged both as unreachable:
`Role.attendant` is already `Role`'s floor (`app/core/roles.py`'s `_RANK` map), so any row
in `outlet_memberships` clears it by construction, and the branch could never fire.

Removed rather than kept "for completeness" — §14 says not to write dead code, and a
comment now sits where each branch used to, naming why it is gone rather than leaving a
future reader to wonder if it was forgotten.

---

## 7. What coverage forced, that HTTP-level testing could not reach

Two paths needed a different testing shape entirely:

**`attachment_service.create()`'s commit-failure branch (M4).** The row flushes, the
storage upload succeeds, and *then* `db.commit()` fails — the one window where a real
object exists in the bucket with nothing in the database pointing at it. No amount of HTTP
setup can make a database fail at that exact instant on demand.
`tests/test_attachments_service.py` calls `create()` directly against a `MagicMock`
session whose `commit()` raises, and asserts the `orphan_storage_object` log line actually
fires with the right bucket and path — proving the safety net exists, not assuming it.

**`services/attachments.py::orphans()`**, the sweep predicate, sat genuinely unused until
Step 10 wrote the job that calls it. Nothing wrong here — Step 3 built it ahead of its one
consumer because the function belonged with the model it queries, and it reached 100%
coverage the moment its actual caller existed, exactly as planned.

---

## 8. Verification

```
741 passed
100% coverage on every Phase 8 module except cleanup_attachments.py's __main__
    guard (97%) -- the identical single line both pre-existing jobs carry uncovered
alembic check                          ->  no new upgrade operations, head 0011
alembic downgrade base && upgrade head ->  clean
```

Structural assertions no value test could make:

- The `expense_category` enum type is gone from `pg_type` after `0010` — the exact trap
  `0008`'s own downgrade comment warned about (`DROP TABLE` does not drop an enum).
- `attachments` carries its own `outlet_id`, unlike everything Phases 5-7 built.
- `float(` / `sa.Float`, hardcoded `5242880`/`300`/`5000.00`/`"receipts"` — zero matches
  across every Phase 8 module.
- `git grep "CREDIT_SALE_MISSING_RECEIPT\|credit_sales"` finds only named comments and
  spec prose — Phase 8 did not scaffold Phase 9's table.

---

## 9. Open items, carried forward

Nothing new falls due before Phase 9's first table. Still open, unchanged in substance:

- **§5.2 vs §4.7's salesman-shortfall contradiction — decide before Phase 9.** This is
  the last phase before it. `credit_sales.attachment_id` is `NOT NULL`; a cash shortfall
  booked as udhaar against the salesman has no receipt to photograph. §14's standing
  recommendation, carried since Phase 4: keep `credit_sales` receipt-mandatory, give
  shortfalls their own table.
- `EXPENSE_RECEIPT_THRESHOLD` = ₹5,000 is a guess, now live on real money.
- The real expense category list, beyond the four seeded, and which of them genuinely
  need a receipt.
- CBG's real max flow rate in kg/min; petrol/diesel dealer commissions; whether testing
  quantities are recorded on paper.
- The locker model's effect on §6.5's rolling balance — decide before Phase 10.

### The check no test replaces

**Photograph one real receipt on a phone, upload it against a real shift, attach it to
that day's `OTHER` expense, close and lock the shift, then open the signed URL and confirm
the photo that comes back is the one taken.** Do it once from an iPhone on default camera
settings specifically — that is the HEIC path, and the entire point of §7.2's special-
cased message is that a real user hits it. Phase 7 owed an end-to-end day with expenses;
Phase 8 is the first phase where that day can carry a document.

### What Phase 9 inherits

- **`attachment_service.link()`, unchanged, ready for `credit_sales.attachment_id`.** The
  one-attachment-one-live-row rule already generalises past `expenses` — `live_expense_
  for_attachment` in `services/expenses.py` is the only piece that is table-specific, and
  its own docstring says so: Phase 9 adds a matching check for `credit_sales` and `link()`
  itself does not change.
- **The `Idempotency-Key` + reversal shape, now proven on four tables** (`nozzle_readings`
  needed neither; `collections`; `expenses`; and the upload endpoint's own reasoned
  *absence* of one) — ready to copy onto `credit_sales`.
- **The 403-vs-404 tenancy question, decided once for attachments, worth re-asking for
  credit customers.** §7.3 set a stricter rule than the shift/nozzle pattern for a
  specific reason — sensitive content. `credit_customers` carries phone numbers and
  vehicle registrations; Phase 9 should ask explicitly whether the same stricter rule
  applies there too, rather than defaulting to the older pattern by habit.
