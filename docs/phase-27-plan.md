# Phase 27 — Voiding an empty shift: Plan

> Written before the code on 5 October, from the owner's brief; "What shipped" is filled in as
> commits land. No migration, no table, no npm dependency. One route, one button, one exception
> to §3 rule 6.

## Context

On 5 October a manager opened that day's shift (`ba5f6c17-3fa0-49fe-b459-956521d0a917`) before
2 October had been entered. The outlet then could not move:

| Wanted | Refused with | Why the refusal is correct |
|---|---|---|
| Open 2 October while 5 October is open | 409 `SHIFT_ALREADY_OPEN` | §5.2: one open shift per outlet keeps §4.7's chain unambiguous |
| Open 2 October once 5 October is not open | 409 `SHIFT_OUT_OF_SEQUENCE` | §4.7: a new shift extends the chain; it is never spliced in before the tip |
| Close 5 October to get it out of the way | 409 `MISSING_NOZZLE_READINGS` | §6.8: an unread meter is fuel with no sale against it |

Every rule did its job. Together they left one way out — deleting the row in the production
database by hand — and that writes no audit row and skips every check. The auto-mode classifier
refused it, correctly. The owner's read-only check on 5 October found **zero rows** attached to
the shift; the latest shift before it is 1 October, closed. So once 5 October is gone, 2 October
opens as sequence 1 with no other change.

## Previous-phase audit

Phase 26 is merged and deployed (`d49afe9`, Railway SUCCESS, no migration). Baseline on the
Phase 27 branch before any change: **1,726 backend tests passed in 71.5 s**; the frontend
Vitest suite is recorded under "What shipped". Nothing in Phase 26 touched the shift lifecycle,
so the audit for this phase is of the lifecycle itself, and it found the gap above: a lifecycle
with a backwards move for a *closed* shift (reopen) and none at all for an *open* one.

The catalogue was read before the design was settled:

```
nozzle_readings, collections, expenses, credit_sales, credit_repayments, non_fuel_sales,
bank_deposits, salesman_shortfalls, salesman_shortfall_settlements
```

Nine tables hold a foreign key to `shifts`, every one on `shift_id`, every one `NO ACTION`
(`confdeltype = 'a'`). Nothing in `app/models` or `alembic/versions` says `ondelete`.

## Decisions

### D1 — The empty row is deleted, not marked `void`

§3 rule 6 forbids hard deletes on financial tables, and the obvious way to honour it is a fourth
status. It was rejected because of what it costs every reader of `shifts`:

- `latest_shift` would have to skip it, or 2 October would still be `SHIFT_OUT_OF_SEQUENCE`.
- `next_sequence` would count it, so the real 5 October would be **sequence 2**. This outlet has
  one shift template, for sequence 1, so `_resolve_window` would fall back to the previous
  shift's end — 4 October at 22:00 — as the start. §6.3 prices the whole shift from
  `started_at`, so a 06:00 revision on 5 October would value the day at the old rate, with no
  error anywhere.
- "Traded means has a shift" (§6.5's `EARLIER_DAY_NOT_RECONCILED`), the Cash worklist, each
  report's default window (§13.30) and `DAY_HAS_OPEN_SHIFTS` would all need a filter.

Each of those is a place to forget, and forgetting is a plausible wrong figure. Deleting a row
that nothing references leaves nothing to forget. The record is the `status_change` audit row,
whose `record_id` is deliberately not a foreign key (§5.3) and so outlives the shift. §3 rule 6
gains one exception, in the shape §7.4 already uses for unlinked attachments: the rule protects
money, and an empty shift holds none.

### D2 — "Empty" is read from `pg_constraint`

`shift_service.tables_holding(db, shift_id)` asks the catalogue for every foreign key whose
referenced table is `shifts`, then asks each referencing table whether it has a row for this
shift, and returns the names of those that do. The route refuses with 409 `SHIFT_NOT_EMPTY` and
names them.

The existence test is written as a join on the constraint's own column pairs
(`conkey`/`confkey`), not as `WHERE shift_id = :id`. Every key today is one column called
`shift_id`, but §5.0 already sketches a composite `(shift_id, outlet_id)` key for later, and a
query that assumed the column name would be the hand-written list again in a smaller font.

Identifiers come from the catalogue and are quoted with the dialect's preparer before they reach
SQL; the shift id is always a bound parameter.

### D3 — The race loses at the database, with the same answer

Between the check and the delete, an attendant on the same shift can save a reading. Every key
is `NO ACTION`, so the `DELETE` then fails on the foreign key rather than cascading. The route
catches that `IntegrityError` (SQLSTATE `23503`, foreign-key violation) around the flush and
returns the same 409 `SHIFT_NOT_EMPTY`; the audit row was added in the same transaction and
rolls back with it. Any other integrity error is re-raised, so it stays a loud 500 (§3 rule 10's
allowlist reasoning in `app/core/errors.py`).

A test pins the second half: no foreign key to `shifts` is `ON DELETE CASCADE`. Without that, a
later migration could turn this lock into a trapdoor.

### D4 — Open shifts only; not restricted to the chain's tip

`closed` → 409 `SHIFT_NOT_OPEN`, `locked` → 409 `SHIFT_LOCKED`, through the existing
`_guard_transition` vocabulary. A closed shift is a manager's statement about a day, and the
audited way back is reopen; an admin who genuinely wants a closed empty shift gone reopens it
first, and both acts are on the record.

The tip is not required. The only open shift that is not the tip is a reopened mid-chain one; if
it holds nothing, nothing chains through it, and the sequence gap it leaves is a label (§13.45).

### D5 — The response describes what was removed

`200` with `VoidedShiftResponse { id, business_date, sequence }`. Returning `ShiftResponse` would
describe a row that no longer exists, with a `status` of `open`. `204` would leave the client
nothing to say in its toast.

### D6 — The audit row

`action = status_change` — a shift lifecycle move, which is exactly what §14 reserves that label
for. `old_values` is the existing `_audit_snapshot` plus the identity fields (`business_date`,
`sequence`, `started_at`, `attendant_id`), so the row alone reconstructs the shift.
`new_values = {"status": "voided", "reason": …}`. The reason lives in the audit row, as reopen's
does.

### D7 — The button

- **Today / `#/shifts/{id}`** (`ShiftHeader`): admins see "Void shift" on an *open* shift, beside
  Close. It opens a sheet with the reason field — `ReopenForm`'s shape — and a sentence saying
  what voiding is and is not. On success the shift no longer exists, so the screen goes to
  `#/today` and the shift queries are refreshed.
- **Cash tab**, open-shift card: the same sheet, for admins, because that card is where the
  manager who made the mistake was looking.
- No client-side pre-check of emptiness. §8: a hidden control is not a permission check, and the
  server's 409 names the tables in words a person can act on.

### D8 — Used in production through the route, by a person

The 5 October shift is voided through `PATCH /shifts/{id}/void` with an admin's own session, so
`changed_by` names a real admin. No token is minted from the JWT secret to do it: an audit row
that says the owner did something he did not do is the kind of record this phase exists to keep
honest.

### Mechanical decisions

| # | Decision |
|---|---|
| M1 | Route in `app/api/v1/shifts.py`, after `reopen_shift`; same `require_shift_access(Role.admin)` floor |
| M2 | Request model `ShiftVoid { reason: str (3–500) }`, `extra="forbid"`, separate from `ShiftReopen` so the OpenAPI document names it |
| M3 | Helper `tables_holding` in `app/services/shifts.py`, beside `latest_shift` and `next_sequence` |
| M4 | New error code `SHIFT_NOT_EMPTY` (409). Not added to the client's `FRIENDLY` map: the server's detail names the tables, which is the useful part |
| M5 | Tests in `tests/test_shift_void.py`; the probe table is created and dropped by its own fixture in committed transactions, never left idle in one (Phase 10's hang) |
| M6 | `frontend/src/api/openapi.json` and `schema.d.ts` regenerated in the same commit as the route, so every commit is green |

## Build order

1. `CLAUDE.md: Phase 27` — §3 rule 6, §5.2, §6.8, §8, §9, §10, §11, §13.45, §14 (`29940e5`).
2. `Phase 27: void an empty shift` — failing tests first, then the helper, the route, the
   OpenAPI snapshot.
3. `Phase 27: the Void shift button` — `ShiftHeader` and the Cash card, Vitest first.
4. `Phase 27 notes` — this plan's "What shipped" and `docs/phase-27-notes.md`.

Then merge to `main`, push (Railway deploys `main`; no migration, so `preDeploy` is a no-op),
and void `ba5f6c17-…` in production.

## Error codes introduced

| Code | Status | When |
|---|---|---|
| `SHIFT_NOT_EMPTY` | 409 | A void, when any table with a foreign key to `shifts` has a row for the shift — found by the check or by the foreign key itself |

Reused: `SHIFT_NOT_OPEN`, `SHIFT_LOCKED`, `INSUFFICIENT_ROLE`, `SHIFT_NOT_FOUND`.

## Verification checklist

- [x] An admin voids an empty open shift: 200, the row is gone, one `status_change` audit row
      carries the shift's identity and the reason
- [x] Manager and attendant → 403; missing and blank reason → 422
- [x] Closed → 409 `SHIFT_NOT_OPEN`; locked → 409 `SHIFT_LOCKED`
- [x] A reading, a collection, an expense, a shift-bound credit repayment each → 409
      `SHIFT_NOT_EMPTY` naming the table; the shift survives and no audit row is written
- [x] A probe table with a foreign key to `shifts`, created in the test, blocks the void
- [x] No foreign key to `shifts` is `ON DELETE CASCADE`
- [x] A row slipped past the check (helper patched to say "empty") is refused by the foreign key
      with the same 409, and nothing is deleted or audited
- [x] The 2 October case end to end: open 5 October, void it, open 2 October → sequence 1 with
      the template's 06:00 start
- [x] `test_audit_coverage`, `test_routes`, `test_openapi_snapshot`, `test_errors` green
- [x] Full backend suite and Vitest green; `npm run build` and `npm run budget` pass
- [x] The button: admin sees it on an open shift, manager does not; the sheet requires a reason
- [ ] Production: the deploy succeeds, the 5 October shift is voided through the route, and the
      latest shift is then 1 October

## What shipped

| Commit | What |
|---|---|
| `29940e5` CLAUDE.md: Phase 27 | §3 rule 6's exception, §5.2, §6.8 (why, the catalogue, the second lock, delete-not-status), §8 row, §9, §10 list, §11 entry, §13.45, four §14 guardrails |
| `3a04299` Phase 27: void an empty shift | `tables_holding` in `app/services/shifts.py`; `ShiftVoid`, `VoidedShiftResponse` and `void_shift` in `app/api/v1/shifts.py`; `tests/test_shift_void.py` (14); OpenAPI snapshot |
| `f9c5f8d` Phase 27: the Void shift button | `VoidShiftForm` and `useForgetShift` in `today.tsx`; the button on `ShiftHeader` and on the Cash card; `Sheet` gains `onExited`; five Playwright tests |

**Counts.** pytest 1,740 (+14), 100% coverage on both changed modules; Vitest 86 (unchanged —
screen behaviour lives in Playwright); Playwright 174 (+10); front-door budget 65.6 KB of 70.

**Deviations from the plan.**

- **The audit `INSERT` is flushed on its own, before the `DELETE`** (D3). The plan caught the
  `IntegrityError` around one flush containing both and checked SQLSTATE `23503` to tell them
  apart; the non-`23503` branch could not be reached by any test. Splitting the flush makes every
  `IntegrityError` on the guarded flush a foreign-key violation by construction, and leaves an
  audit failure a loud 500 — same behaviour, no dead branch.
- **`Sheet` gained an optional `onExited`** (D7). Frames 40 ms after pressing Void showed Today's
  sheet cut mid-exit: the refresh replaced the shift screen that owned it. Today now refreshes
  once the sheet has left; the Cash tab's sheet already sat at screen level and refreshes at once.
  `VoidShiftForm` therefore reports success and leaves the refresh to its caller.
- **The reason uses `StringConstraints(strip_whitespace=True, …)`**, collections.py's rule,
  rather than reopen's bare `Field(min_length=3)`, so six spaces are a 422.

## Still owed by the owner

- Open 2 October and enter it; then 3 and 4 October; then 5 October again, which will be
  sequence 1 with the 06:00 template start.
