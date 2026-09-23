# Phase 21 — The billing-period statement: Notes

> A learning reference, not a changelog: *why* the statement is built the way it is, and what
> went wrong on the way. The plan is `docs/phase-21-plan.md`; the rules live in `CLAUDE.md`
> §6.6, §8, §11 (phase 21), §13.40–42 and §14.

---

## What was built

- `GET /api/v1/credit-customers/statement?from=&to=`, at the manager floor. For every customer
  it returns six figures, the lines behind them, and the column totals.
- `#/credit/statement` on the Credit tab: two date boxes, one card per customer, expandable
  entries, and print.
- No migration, no new table, no new error code. 1,734 tests pass (31 new), and every new line
  is covered.

## A bill is §6.6's sum cut at two dates

The whole feature is one idea: the outstanding balance `outstanding()` has always computed,
evaluated with a date condition on each row. Three grouped queries (openings, sales, repayments)
each produce `SUM(CASE WHEN date … THEN amount ELSE 0 END)` buckets for *before*, *in range*
and *since*. Python then merges them per customer. That is the shape
`outstanding_by_customer` already had, and it was chosen for the same reason: a customer may
have rows in any subset of the three tables.

`owes_today` deliberately comes **from `outstanding_by_customer`, not from the buckets**. The
buckets could produce it too, but then there would be two implementations of one definition.
Instead the tests assert the identity `owes_today = billed + opening_since + udhaar_since −
paid_since` against the definition. If either side ever drifts, a test fails; nobody has to
remember that the two must agree.

### Why there is an `opening_since` field nobody asked for

The owner asked for six columns. For `owes_today` to be *exactly* reconcilable from them, the
"since" figures needed one more term. A customer whose opening balance is dated *after* the
window (here, several were dated late July) owes it today but owed nothing on the bill. Leave it
out and the identity holds for most customers and silently fails for a few. It is shown only in
the expanded panel, and only when non-zero.

The same reasoning made the "since" buckets **unbounded above** rather than capped at today.
§6.1 refuses a future business date on every write path, so the two are the same rows; the
unbounded form is what makes the identity exact rather than true by coincidence.

## The spec amendment contained a false claim, caught before any code

The first draft of §6.6 and §13.41 said a reversal is dated by its *own* shift, so a slip
cancelled in a later period nets out in that later period. That is wrong. `reverse_sale` and
`reverse_repayment` both copy the original's `shift_id` and `business_date` (§6.9), so a pair
**always nets inside one period**.

Checking the service before committing the spec turned up the two cross-period effects that
really exist. The corrected §13.41 records both:

1. The corrected **replacement** slip needs an open shift, so it lands in a later period.
2. A reversal entered after a bill went out changes that **past** period's figures. The
   statement is recomputed on every read, so the 1–15 bill that was printed on the 16th no
   longer matches the screen.

This is the §14 lesson from Phase 20 ("a spec sentence is a claim about the world") one level
down: **a sentence about what the code does is a claim about the code, and the code is one
grep away.**

## The bank tick: reuse by splitting, not by copying

Phase 20's `_verify_against_recorded` answered one question per statement line: *which
repayment is this, or is it a tie?* The statement asks the reverse, per repayment. A tie also
has to mark **every** candidate `ambiguous`, and the old function returned only
`(None, True)`, which does not say who the candidates were.

Copying its query into the credit module would have meant two matchers that could drift. So
the function was split three ways, without changing its logic:

- `recorded_candidates(row)`: the query;
- `resolve_candidates(candidates, row)`: pure, with no queries;
- `verify_against_recorded(row)`: the two composed, keeping the old public behaviour.

The Bank screen and the statement now run the same code. The previously uncovered
bank-reference tie-break line is exercised by a new test, because the statement now depends
on it.

`no_statement` is separate from `not_on_statement` for the reason §6.8 separates null from
zero. "The bank did not see it" and "nobody has uploaded that month" are different facts, and
merging them would make an un-uploaded month look like a list of missing money.

## Route order

`/credit-customers/statement` is registered **before** `/credit-customers/{customer_id}`.
Otherwise FastAPI parses "statement" as a UUID and returns a 422 that looks like a validation
bug. A test pins it.

## Window validation, shared

The three refusals (from after to; longer than the cap; a future `to`) moved from
`reports._resolve_window` into `app/api/window.py`. The *defaults* stayed in reports, because
§13.30's trading-anchored default answers a different question from a bill, where both dates
are required.

`expenses.py` and `bank_statements.py` still carry their own older copies of the same checks.
Converging them was not this phase's job, and it is recorded here so it is not forgotten.

## Checked against the real July data

The endpoint was called in-process against the local development database. The token was
minted locally, the way `make_token` does in the tests, and only GETs were made.

- **1–15 July and 16–31 July both reconcile for all customers.** Every `billed` equals the
  ledger's own `balance_after` on the last day of the window, and both identities hold.
- **All 17 repayments on record are cash entered on a shift.** No bank transfer has been
  recorded, so no bank tick appears anywhere yet.
- **19 bank statement credits are classified `udhaar_repayment` and none has been confirmed.**
  Until they are confirmed on the Bank screen they are in no ledger, so **the statement does
  not include them.** This is the most important thing for the owner to know before sending a
  bill from this screen.
- **A shift in 16–31 July is still open**, so that period shows the open-shift warning.

## What was not verified

The screen has not been driven in a browser. Login goes through Supabase, and this session had
no credentials. §13.18 already says the frontend's behaviour is checked by hand, and the
structural tests pass (no `parseFloat`, no markup strings, every import resolves). The default
half-month logic was exercised in Node across month ends, January and a leap-year February.

Still to check by hand:

- the layout of six figures on a phone;
- expand and collapse;
- print preview for the whole period and for one customer.
