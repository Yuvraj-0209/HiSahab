# Phase 27 — Voiding an empty shift: Notes

> A learning reference for why a one-line problem — "delete the shift opened by mistake" —
> became the route it did. The plan and the "What shipped" record are in
> `docs/phase-27-plan.md`.

---

## What was built

- **`PATCH /shifts/{id}/void`**: admin only, open shifts only, a reason of 3–500 characters
  after stripping. It deletes the shift and writes one `status_change` audit row, in the same
  transaction, carrying the shift's date, sequence, attendant, times and the reason.
- **`shift_service.tables_holding`**: asks `pg_constraint` which tables point at `shifts`, then
  asks each whether it points at *this* shift. Any answer refuses the void with 409
  `SHIFT_NOT_EMPTY`, naming the tables.
- **A "Void shift" button for admins** on Today (and `#/shifts/{id}`) and on the Cash tab's
  open-shift card, behind a sheet that takes the reason.
- **`Sheet` gains an optional `onExited`**, so a screen that is about to be replaced can wait for
  its sheet to leave first.
- No migration, table, column or npm dependency. One new error code, `SHIFT_NOT_EMPTY`.
- **Tests:** pytest 1,740 (+14), Vitest 86 (unchanged), Playwright 174 (+10).

## Every rule was right, and together they were a trap

The 5 October shift is a good example of a bug that is not in any one place. Three rules met:

1. **One open shift per outlet** (§5.2). With two open, "the most recent closing reading" has no
   single answer and §4.7's chain breaks.
2. **A new shift only extends the chain** (§4.7). Inserting 2 October *before* 5 October would
   leave 5 October with two possible predecessors.
3. **A shift cannot close with an unread meter** (§6.8). An unread nozzle is fuel that left the
   tank with no sale against it.

So with 5 October open, 2 October was `SHIFT_ALREADY_OPEN`; had 5 October been closed, 2 October
would have been `SHIFT_OUT_OF_SEQUENCE`; and 5 October could not be closed, because it had no
readings. None of these should be relaxed. Relaxing rule 3 ("close an empty shift") would leave a
closed 5 October on the chain, and rule 2 would still refuse 2 October.

What was missing was not a looser rule but a **backwards move for an open shift**. The
lifecycle already had one for a closed shift (reopen). Void is its sibling, and it is narrow on
purpose: it only reaches a shift that recorded nothing.

## Why the row is deleted, when §3 rule 6 says never delete

The obvious way to keep §3 rule 6 is a fourth status, `void`. It was the first design considered
and it was rejected because of what it does to everyone *else* who reads `shifts`.

Walk through the 5 October case with a `void` status:

- The voided row still holds `(5 Oct, sequence 1)`.
- When 5 October is entered for real, `next_sequence` says **2**.
- `_resolve_window` looks for a template for sequence 2. This outlet has one template, for
  sequence 1 (06:00–22:00). So it falls back to "start where the previous shift ended" —
  4 October, 22:00.
- §6.3 values the whole shift at the price effective at `started_at`. If IOCL revised the price at
  06:00 on 5 October, the day is valued at the *old* rate. The figure is plausible, nothing
  errors, and the cash position is off by the revision times the litres.

And that is only one reader. `latest_shift`, the one-open-shift check, §6.5's "a day traded if it
has a shift", the Cash worklist and every report's default window would each need a
`WHERE status <> 'void'`. The one that forgot would be wrong silently.

Deleting the empty row avoids all of it, because nothing is left for anyone to filter. The
exception to §3 rule 6 follows the shape §7.4 already set for unlinked attachments. The rule
protects money rows, and this row provably has none: no row in any table points at it. The
`status_change` audit row is the record, and it outlives the shift because
`audit_logs.record_id` was deliberately never a foreign key (§5.3).

**The lesson that generalises:** a "soft delete" flag is not free. It is a filter that every
future query must remember. Where the thing being removed is genuinely empty, a hard delete with
an audit row can be the *safer* choice.

## "Empty" is a question for the database

The brief said: refuse if any table with a foreign key to `shifts` has a row, and ask
`pg_constraint` rather than writing a list. The reason is history repeating. §6.9 records
`_CONSTRAINT_ERRORS` being forgotten twice, because a list in code has to be updated by someone
who remembers it exists. The nine tables today would be ten the day a later phase adds one, and
a list would not notice.

The catalogue query does three things worth understanding:

```sql
FROM pg_constraint c
CROSS JOIN LATERAL unnest(c.conkey, c.confkey) WITH ORDINALITY AS k(child_attnum, parent_attnum, ord)
WHERE c.contype = 'f' AND c.confrelid = 'shifts'::regclass
```

- `contype = 'f'` picks foreign keys, and `confrelid` is the table they *point at*.
- `conkey` and `confkey` are arrays of column numbers: the child's columns and the parent's.
  `unnest` with two arrays walks them **in step**, so a composite key comes back as pairs. Every
  key today is a single `shift_id`, but §5.0 already sketches `(shift_id, outlet_id)` for later,
  and a query that assumed the column name would be a hand-written list in a smaller font.
- Joining `pg_attribute` turns the column numbers into names.

Then, for each key, one `EXISTS` joins the child to `shifts` on those column pairs. The names come
from the catalogue, not from a user, and are still quoted by SQLAlchemy's identifier preparer
before they are put into SQL. The shift id is always a bound parameter.

The test that proves the design is `test_a_table_added_later_blocks_the_void_without_touching_the_route`.
It creates a table nobody ever wrote, `zz_void_probe`, with a foreign key to `shifts`, inserts a row,
and watches the void refuse **naming that table**. A list-based implementation cannot pass it.

## Two locks on the same door

Between `tables_holding` answering "empty" and the `DELETE`, an attendant on that open shift could
save a reading. Two things make that safe:

1. **Every foreign key to `shifts` is `NO ACTION`.** Postgres refuses to delete a parent while a
   child points at it, so the `DELETE` fails. Under `ON DELETE CASCADE` it would instead have
   *deleted the reading*. `test_no_foreign_key_to_shifts_cascades` reads `confdeltype` from the
   catalogue so that a future migration cannot quietly change this.
2. **The route turns that failure into the same 409.** The test makes the race deterministic by
   patching `tables_holding` to say "empty" while a collection exists.

One detail made this cleaner. The audit row and the `DELETE` were first flushed together, so an
`IntegrityError` could have come from either, and the code had to check the SQLSTATE (`23503`,
foreign-key violation) to tell them apart. The branch for "some other integrity error" could not
be reached by any test. So the audit `INSERT` is now flushed **first, on its own**, and the guarded
flush contains only the `DELETE`. A `DELETE` can violate exactly one kind of constraint, a foreign
key pointing at the row, so every `IntegrityError` there is the race. The unreachable branch is
gone, and a failure writing the audit row still surfaces as a loud 500.

The rollback matters too. The audit row was staged in the same transaction, so when the delete
fails, `db.rollback()` takes the audit row with it. There is never a record of a void that did
not happen. The tests assert this: every refusal leaves zero `status_change` rows.

## Why PATCH, and what it returns

§9 says `DELETE` is unused, and the brief chose `PATCH /void`. That is consistent rather than
evasive: `/close`, `/lock` and `/reopen` are all `PATCH` actions on the lifecycle with a body, and
void is one more with a mandatory reason. HTTP `DELETE` with a body is poorly supported.

It returns `{id, business_date, sequence}`, not a `ShiftResponse`. A `ShiftResponse` would describe
a row that no longer exists, with `status: "open"`. These three fields are what the screen needs
for its toast: "Shift 1 on 5 Oct 2026 voided."

## The sheet that was cut off, found by looking

The first version passed every test and still looked wrong. Frames captured 40 ms after pressing
"Void shift" showed:

- **On the Cash tab**, the sheet sliding down and the scrim fading, correctly.
- **On Today**, at 40 ms, no sheet and no scrim at all. It had been cut mid-exit.

The cause was structural. `Sheet` stays mounted for its exit animation, but only while its
*parent* is mounted. On Today the parent is the shift screen. The void's refresh made
`/shifts/current` answer `NO_OPEN_SHIFT`, Today replaced the shift screen with "No shift is open",
and the sheet went with it. On Cash the sheet already lived at screen level (the reconcile sheet's
pattern), so the card could change behind it.

The fix is in the order of events. `Sheet` gains an optional `onExited`, which it already knew
internally and now reports. On Today the refresh waits for it. Frames after the fix:

- **40 ms:** the sheet is sliding away.
- **220 ms:** the sheet has gone. The shift is still shown with its buttons disabled, because it is
  dead.
- **800 ms:** "No shift is open".

Dismiss, then change. Reduced motion still works, because the spring "arrives immediately" and
still calls `onRest`, which is what fires `onExited`.

Two smaller choices came out of this:

- **The form reports success and leaves refreshing to its caller**, because *when* to refresh is the
  one thing the two screens disagree about.
- **The deleted shift's cached reads are removed, not refetched.** Refetching
  `/shifts/{id}/sales` for a shift that no longer exists would only produce 404s in the console.

## Things that went wrong in the process

- **The baseline did not run the first time.** macOS has no `timeout` command, the backgrounded
  pipeline exited 127, and the "completed" notification looked like success. Read the output, not
  the exit notification.
- **A test helper bound a UUID as a string**, and psycopg sent it as `VARCHAR`
  (`operator does not exist: uuid = character varying`). The route was already working; the log
  line "shift voided" in the failure output was the clue.
- **`npm run gen:api` failed from `frontend/`**, because `scripts/dump_openapi.py` reads `.env`
  from the working directory. Run it from the repo root, then `npm run api:types`.
  `schema.d.ts` is gitignored; only `openapi.json` is committed.

## Using it in production

The route exists so that production is never edited by hand. The 5 October shift was voided through
it, as an admin, with a reason. That is recorded in the plan's "What shipped".

## Still owed by the owner

- Open and enter 2 October, then 3 and 4 October, then 5 October again. 5 October will be
  sequence 1, with the template's 06:00 start.
