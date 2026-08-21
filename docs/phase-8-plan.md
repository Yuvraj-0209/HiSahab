# Phase 8 — Expense Categories & Attachments: Plan, Decisions, and Verification Checklist

> Finalised at implementation time, matching the `phase-5/6/7-plan.md` pattern. Written
> mostly before the code (Steps 0-1 land the audit and the spec); §8 records what actually
> shipped where it differs from the original plan.

---

## Context

Phase 7 shipped clean: 79 expense tests green, `app/models/expense.py` matched §5.2's
amended column list exactly, and `attachment_id` was deliberately not built because
`attachments` did not exist and §11 forbids scaffolding ahead of a table.

Phase 8 was scoped as attachments alone. Two owner questions reshaped it before any code
was written:

1. *"Wasn't the receipt requirement just for credit sales?"* — correct, and the schema
   already said so quietly: only `credit_sales.attachment_id` is `NOT NULL`. An expense
   receipt was always optional; nothing was going to force one.
2. *"Can an admin create their own expense categories, and mark which ones need a receipt?
   For tea it would be redundant."* — §5.1 already made this exact argument one table up,
   for `fuel_types`: adding a product you sell is data entry, not a migration. Every word
   transfers.

So Phase 8 became three things: `expenses.category` stops being an enum and becomes
`expense_categories`, admin-managed; receipts on expenses become conditional
(`category.requires_receipt OR amount > threshold`); and attachments — upload, validation,
signed download, cleanup — which is what makes the receipt rule enforceable at all.

Phase 8 is also a hard prerequisite for Phase 9: §5.2 makes `credit_sales.attachment_id`
**`NOT NULL` at the database level**, so credit cannot be built until there is a table for
that FK to point at.

---

## 1. Phase 7 audit — Step 0 verdict

**One defect, not the three each of the last two phases found.** Phase 7 was tighter than
its predecessors — its own Step 0 had already fixed three P6 defects test-first, and that
discipline held.

| Check | Result |
|---|---|
| `pytest` | 582 passed (after the fix below) |
| `alembic current` / `check` | `0009 (head)` / no drift |
| Coverage, Phase 7 modules | 100% on all four |
| P6-1 shape (PATCH after reversal) | Already covered |
| §6.7 aggregate spanning shifts | Already covered |
| `0009` description-trim boundary | Already covered |

**P7-1** — `uq_expenses_reverses_id` was never added to `_CONSTRAINT_ERRORS`. Phase 6
Step 0 fixed exactly this on `collections`; Phase 7 copied §6.9's reversal shape onto
`expenses` — unique constraint included — but not the allowlist entry. Two managers
reversing one expense at the same instant both pass the service-level `ALREADY_REVERSED`
check; the unique index refuses the second `INSERT`; the loser got an opaque 500. Fixed
test-first, with a structural test (`test_every_reversal_unique_constraint_is_mapped_to_a_
business_error`) that reads `pg_constraint` directly, so Phase 9's `credit_sales` inherits
the check automatically the day its migration lands.

---

## 2. Decisions

### D1 — `expense_categories` is an outlet-scoped table modelled on `fuel_types`

| Column | Notes |
|---|---|
| `id` | UUID PK |
| `outlet_id` | FK NOT NULL. Not derivable (§5.0); categories are genuinely per-outlet |
| `code` | text, immutable once created; `ck_expense_categories_code_format` (`^[A-Z][A-Z0-9_]*$`) stops `Tea`/`tea`/`TEA` becoming three rows |
| `display_name` | text, editable |
| `requires_receipt` | boolean, editable — the knob §6.11 gives the admin |
| `is_active` | boolean, default true. Deactivate, never delete (§3 rule 6) |
| | `UNIQUE (outlet_id, code)` |

Seeded (migration `0010`) with the four Phase 7 values — `SALARY`, `MAINTENANCE`,
`ELECTRICITY`, `OTHER` — with `OTHER` alone carrying `requires_receipt = true`, since it is
the unknown-spend bucket. `expenses.category` (enum) becomes `expenses.category_id` (FK),
backfilled via `shift_id → shifts.outlet_id`, then the enum type is dropped — the exact
trap `0008`'s own downgrade comment warned about (`DROP TABLE` does not drop an enum).

**Downgrade is lossy and says so loudly.** A custom category has no enum label to return
to; anything outside the original four collapses to `other` with a `RAISE WARNING` naming
the row count. Downgrade is a development operation, and pretending otherwise would be
worse than losing it visibly.

### D2 — the receipt rule is snapshotted onto the row, and enforced by a CHECK

```
receipt_required = category.requires_receipt OR amount > EXPENSE_RECEIPT_THRESHOLD
```

evaluated once, at insert (migration `0011`), stored in `expenses.receipt_required`.
Enforced by `ck_expenses_receipt_required_has_attachment`:
`reverses_id IS NOT NULL OR receipt_required = false OR attachment_id IS NOT NULL` — belt
and braces (§6.6), and the one CHECK this phase adds that a client can genuinely reach
(the three on `attachments` itself are unreachable — upload validation refuses every case
first), so it earns a `_CONSTRAINT_ERRORS` entry.

**A `PATCH` that raises the amount re-evaluates the rule** — reading the category's
*current* `requires_receipt`, not a stored snapshot of it at insert. This looks like it
contradicts "never recomputed" and does not: that rule protects a row nobody is touching.
A `PATCH` is an active edit happening now, and deriving the rule from current inputs is
what makes the threshold-defeat check (enter low, edit up) actually mean something.

`EXPENSE_RECEIPT_THRESHOLD` defaults to `5000.00` — above §6.7's `1000.00` review
threshold on purpose, since "a manager should look" and "this needs paper" are different
questions. The default is a guess (§8, still owed by the owner).

### D3 — one attachment, one *live* row; `attachment_id` is immutable once set

A second claim on a *live* attachment is refused with 409 `ATTACHMENT_ALREADY_LINKED`.
"Live" is §5.2's own vocabulary for collections, reused: not itself a reversal, and not
referenced by one. That is what resolves the collision with §6.9: a correction creates a
reversal plus a replacement, and the replacement — a real expense, potentially still
receipt-required — inherits the original's `attachment_id` directly (not via a second
`link()` call, since by the time the replacement is built the original is no longer live
and re-checking would only re-verify what inheritance already guarantees).

`expenses.attachment_id` may be set at create, or by a `PATCH` **only while `NULL`**;
swapping is 409 `ATTACHMENT_ALREADY_SET`, corrected via §6.9's reversal like everything
else here.

### D4 — `shift_id` as a required upload form field, not a path segment

§7.2's path needs a `business_date`, but at upload time no expense or credit sale exists
yet. "Today" is wrong specifically for this outlet: §4.7 says the day is typed in *after
the fact*, so uploads routinely land on the following calendar day. `POST
/api/v1/uploads/receipt` takes `shift_id` in the multipart form body; the server derives
`business_date`, `outlet_id`, and the §8 ownership check from the shift itself.

This forced its own shift-access dependency (`_shift_access_from_upload_form` in
`uploads.py`) rather than reusing `require_shift_access`: FastAPI resolves a dependency
parameter's source (path/query/form) from its own declaration, and only one function in a
request's graph may declare `shift_id` without ambiguity. Spelled out explicitly rather
than forked with a flag — §14 prefers exactly that trade.

### D5 — `storage_path` excludes the bucket name

`bucket = "receipts"` (config), `storage_path = "{outlet_id}/{YYYY}/{MM}/{DD}/{uuid4}.
{ext}"`, `UNIQUE`. The attachment's own id doubles as the path's uuid4 — generated in
Python before the row is written, since the path has to be known before the insert, not
after.

### D6 — a `StorageBackend` protocol: `SupabaseStorage` and `LocalStorage`

Three methods (`upload`, `signed_url`, `delete`) behind a `Protocol`. `SupabaseStorage` is
plain `httpx` against the Storage REST API — no SDK, per §14's ask-before-adding-a-
dependency and §16's stated preference. Tested against `httpx.MockTransport`, never a real
network call. `LocalStorage` is a directory on disk: the dev fallback when Supabase Storage
is unconfigured, and what every test in the suite actually runs against, via a
`get_storage` override baked into the `client` fixture itself (not a fixture every upload
test has to remember to request).

Two real bugs surfaced writing `SupabaseStorage`'s own tests, both fixed before any
endpoint used it: `signed_url`'s fragment-join produced a double slash (`httpx.URL.
__str__` always ends in `/`), caught by asserting the exact returned URL rather than its
shape; and auth headers baked into client-level defaults meant an injected test client
(built with no defaults) silently sent none, fixed by moving them to per-request headers.

### D7 — no `Idempotency-Key` on the upload endpoint

An attachment is not a money record (§6.10). A retried upload creates a second row the
client simply does not use, reclaimed within 24 hours. The step that would actually
duplicate money — linking, inside `POST /shifts/{id}/expenses` — has carried a key since
Phase 7.

### Mechanical decisions

| # | Decision | Reason |
|---|---|---|
| M1 | HEIC checked only after JPEG and PNG both fail to match | Otherwise §7.2's actionable "Most Compatible" message could misroute; in practice the magic bytes never collide, but the ordering is the safe direction |
| M2 | Client `Content-Type` and filename extension both ignored entirely | Both are the thing being verified; trusting either defeats the sniff |
| M3 | Row `INSERT` + `flush()` before the storage write; `db.rollback()` if it fails | A failed upload leaves nothing; the reverse order risks a row pointing at an object that was never written |
| M4 | A commit failure *after* a successful upload logs `orphan_storage_object` with the full path | The one remaining window — narrow, loud, reclaimable by hand. Exercised directly against the service function with a stubbed session, since no HTTP-level test can provoke a commit failure on demand |
| M5 | Upload reads bounded chunks (`max_bytes + 1`), never trusts `Content-Length` | A client can lie about the header; the actual read is what is bounded |
| M6 | `attachments` DB CHECKs (`size_bytes > 0`, checksum format, mime allowlist) are unreachable through the API | Upload validation refuses every case first — belt and braces, not a live safety net |
| M7 | `uq_attachments_storage_path` → 409 `ATTACHMENT_PATH_COLLISION` in `_CONSTRAINT_ERRORS` | A uuid4 collision is probabilistically, not structurally, unreachable — a retry beats an opaque 500 |
| M8 | Partial index `WHERE linked_at IS NULL` on `attachments` | The one query §7.4's sweep runs |
| M9 | `ORPHAN_TTL` defined once in `services/attachments.py`, imported by the job | So the sweep predicate and the job's own cutoff computation can never disagree about "expired" |
| M10 | Storage `delete()` treats a 404 as success | The row-delete can fail independently of the object-delete; the next run must not wedge |
| M11 | Storage failure → 502 `STORAGE_UNAVAILABLE`, never 500 | A dependency being down is not a bug |
| M12 | The cleanup job hard-deletes; not a §3 rule 6 breach | `attachments` is not a financial table; `orphans()` only returns unlinked rows by construction |
| M13 | `GET /attachments/{id}/url` does **not** compose the generic `require_role` | §7.3 demands 404 (not 403) for a cross-outlet id; `require_role`'s ordinary `NOT_A_MEMBER` 403 leaks "this id is real, just not yours" — a real conflict with the existing, accepted shift pattern, resolved in §7.3's favour since a receipt can carry real financial/personal information |
| M14 | `INSUFFICIENT_ROLE` branches removed from both new auth paths | `attendant` is already `Role`'s floor; every valid membership clears it. Coverage flagged both as dead; §14 says not to write dead code |
| M15 | `GET /expenses/summary` capped at 366 days | An unbounded range on a growing table is a slow query waiting to happen |
| M16 | `PATCH` cannot change `category_id` (carried from Phase 7, restated) | Moving an expense out of its aggregate group is a different row, not a correction |

---

## 3. Build order (as it actually shipped)

| Step | What | Commit |
|---|---|---|
| 0 | Phase 7 audit + P7-1 fix, test-first | `Phase 8 Step 0` |
| 1 | `CLAUDE.md` amendment (+260 lines) | `Spec: expense categories become data, receipts become conditional, before Phase 8` |
| 2 | Migration `0010`, `expense_categories` table + admin CRUD, `expenses.category → category_id` | `Phase 8 Step 2` |
| 3 | Migration `0011`, `attachments` table + `expenses.attachment_id`/`receipt_required` | folded into Steps 3-7 |
| 4 | `app/core/uploads.py` — pure content sniffing | folded into Steps 3-7 |
| 5 | `app/services/storage.py` — the protocol, both implementations, `get_storage()` | folded into Steps 3-7 |
| 6 | `app/services/attachments.py` — create/link/may_read/orphans | folded into Steps 3-7 |
| 7 | `app/api/v1/uploads.py`, `app/api/v1/attachments.py` | `Phase 8 Steps 3-7` |
| 8 | Wire §6.11 into `expenses.py` (create, PATCH, reverse) | `Phase 8 Step 8` |
| 9 | `GET /expenses/summary` | `Phase 8 Step 9` |
| 10 | `app/jobs/cleanup_attachments.py` | `Phase 8 Step 10` |
| 11 | Tests | distributed across every step above, not a separate pass |
| 12 | This file and `docs/phase-8-notes.md` | final commit |

Steps 3-7 landed as one commit rather than five: the migration, the pure validator, the
storage protocol and the two routers only became independently testable once the whole
vertical slice existed end to end, and splitting them would have meant committing code
with nothing yet exercising it.

---

## 4. Error codes introduced

| Code | Status | Meaning |
|---|---|---|
| `CATEGORY_NOT_FOUND` | 404 | |
| `CATEGORY_CODE_INVALID` | 422 | Fails `^[A-Z][A-Z0-9_]*$` after normalisation |
| `CATEGORY_CODE_EXISTS` | 409 | `(outlet_id, code)` unique |
| `CATEGORY_INACTIVE` | 409 | A retired category used on a new expense |
| `EXPENSE_REQUIRES_RECEIPT` | 422 | §6.11 — no attachment where the rule demands one |
| `FILE_TOO_LARGE` | 413 | |
| `EMPTY_FILE` | 422 | |
| `UNSUPPORTED_FILE_TYPE` | 422 | |
| `HEIC_NOT_SUPPORTED` | 422 | The specific, actionable message |
| `ATTACHMENT_NOT_FOUND` | 404 | Also across outlets — existence not leaked |
| `ATTACHMENT_ALREADY_LINKED` | 409 | D3 |
| `ATTACHMENT_ALREADY_SET` | 409 | D3, a `PATCH` swapping a receipt |
| `ATTACHMENT_PATH_COLLISION` | 409 | M7 |
| `NOT_YOUR_ATTACHMENT` | 403 | §7.3's ownership axis |
| `STORAGE_UNAVAILABLE` | 502 | M11 |
| `INVALID_DATE_RANGE` | 422 | `from > to`, or over 366 days |

Reused unchanged: `SHIFT_NOT_FOUND`, `SHIFT_NOT_OPEN`, `SHIFT_LOCKED`, `NOT_YOUR_SHIFT`,
`NOT_A_MEMBER`, `MEMBERSHIP_INACTIVE`, `NOT_AUTHENTICATED`, `INSUFFICIENT_ROLE`,
`VALIDATION_ERROR`, `NO_FIELDS_TO_UPDATE`, `ALREADY_REVERSED`.

---

## 5. Verification checklist

- [x] `pytest` fully green — 741 passed.
- [x] `alembic upgrade head` → `0011`; `alembic check` clean; `downgrade base` /
      `upgrade head` round-trips.
- [x] 100% coverage on every Phase 8 module except `cleanup_attachments.py`'s `__main__`
      guard (97%) — the same single line both existing jobs already carry uncovered.
- [x] The `expense_category` enum type is gone from `pg_type` after `0010`.
- [x] `attachments` has its own `outlet_id NOT NULL`; no `created_by` (has `uploaded_by`).
- [x] Every constraint added is unreachable through the API or in `_CONSTRAINT_ERRORS`.
- [x] §10's four named upload tests, against real magic bytes, not guesses.
- [x] The receipt-rule boundary: ₹5000.00 does not require one, ₹5000.01 does.
- [x] A reversal needs no receipt; a replacement inherits the original's attachment.
- [x] Flipping a category's `requires_receipt` does not touch historical rows.
- [x] `float(` / `sa.Float` — zero matches across every Phase 8 module.
- [x] No hardcoded `5242880`, `300`, `5000.00`, or `"receipts"` literal anywhere touched.
- [x] `git grep "CREDIT_SALE_MISSING_RECEIPT\|credit_sales"` finds only named comments and
      prose — Phase 8 did not scaffold Phase 9.
- [x] `CLAUDE.md` amended in its own commit, before any Phase 8 code.
- [x] One real day, end to end: a `TEA` category with no receipt required, an `OTHER`
      expense with a real uploaded photo, close and lock the shift, read the signed URL
      back. Owed since Phase 5; this is the first phase where that day carries a document.

---

## 6. Not in Phase 8

`credit_sales` / `credit_repayments` and the receipt-mandatory credit sale, including
`CREDIT_SALE_MISSING_RECEIPT` (Phase 9); `bank_deposits.attachment_id` (Phase 10); OCR
(§12); presigned direct upload (V2, §7.1); a job scheduler (§7.4, §12); virus scanning;
image resizing or thumbnails; per-category spending limits; any frontend (Phase 12);
§6.4's cash equation (Phase 10).

---

## 7. Still owed by the owner

- **`EXPENSE_RECEIPT_THRESHOLD` = ₹5,000 is a guess**, live on real money since Step 8.
- **The real category list**, beyond the four seeded. Which genuinely need a receipt?
- **§5.2 vs §4.7 — the salesman shortfall, decide before Phase 9.** This outlet books a
  cash shortfall as udhaar against the salesman's own name, but `credit_sales.
  attachment_id` is `NOT NULL` and a shortfall has no receipt to photograph. Carried
  unchanged since Phase 4; §14's standing recommendation is a separate table for
  shortfalls, keeping `credit_sales` receipt-mandatory for genuine customer sales.
- CBG's real max flow rate in kg/min; petrol/diesel confirmation of 60 L/min.
- Whether salesmen record testing quantities on paper, and in what unit.
- Petrol and diesel dealer commissions.
- The locker model's effect on §6.5's rolling balance — decide before Phase 10.
