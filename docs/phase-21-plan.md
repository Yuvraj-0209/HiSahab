# Phase 21 — The billing-period statement: Plan, Decisions, and Test Inventory

> Follows the `phase-5` … `phase-19-plan.md` pattern: written **before** the code, with the
> final section recording what actually shipped where it differs.
>
> No migration, no new table, no new column, no new error code. One read endpoint, one screen,
> and a print stylesheet.

---

## Context

This outlet bills every udhaar customer twice a month: on the **16th** for the 1st–15th, and on
the **1st** for the 16th to month end. To prepare and check those bills, the owner needs to see,
for any date range and for every customer:

- what they owed going in;
- the udhaar issued and the repayments received inside the range;
- what the bill should say;
- what has been paid since the bill went out;
- what they owe today;
- and a total repaid across everyone.

Until now the Credit tab could answer only two questions. *"What does each customer owe right
now"* (`GET /credit-customers/outstanding`, `GET /credit-opening-balances`). *"Every line of one
customer's account"* (`GET /credit-customers/{id}/ledger`). Nothing filtered by date, and
`credit.outstanding()` had no as-of-date form. §11 lists that form as a missing piece of the
profit bridge, which is why this phase is numbered **21** and the profit bridge moves to **22**.

## Phase 20 audit

- **Suite green**: 1,703 passed at the start of this phase.
- **Phase 20 has no `docs/phase-20-plan.md` or `docs/phase-20-notes.md`.** The phase workflow
  requires both. Still owed; recorded here so it is not lost.
- **Coverage below 100% on Phase 20 modules**:
  - `api/v1/bank_statements.py` is at 87%;
  - `services/bank.py` misses 4 lines, one of them the stored-link branch of
    `credit_reviews`, which this phase's tests now reach;
  - Phase 16's `credit_opening_balances.py` (88%) and `credit_repayments.py` (92%) have gaps
    too.

  This phase covers what it touches. The rest is still owed.
- Structural tests (`test_audit_coverage`, `test_errors`, `test_routes`,
  `test_frontend_assets`) pass.

## Decisions

Each decision below was put to the owner, with the concept explained first.

### D1 — A full four-figure statement per customer

**Owed before · Udhaar in range · Repaid in range · Billed.** "Billed" is the figure the paper
bill should carry, and every row's arithmetic can be checked by eye.

### D2 — "Paid since" and "Owes today" beside it, with no allocation to bills

The owner wants to see payments made *after* the bill went out. Putting them into "Repaid in
range" would break the statement's arithmetic. So they get their own column: **Paid since** is
every repayment dated after `to`, up to today. **Owes today** is §6.6's `outstanding`, unchanged.

**No repayment is matched to a specific bill.** One payment covering parts of three bills has no
honest per-bill answer, the same reason Phase 9 deleted `credit_sales.is_settled`. A FIFO
allocation was offered and declined: it would be a new business rule this system has never had.
Comparing Billed with Paid since is a human judgement, and §13 records it.

**Addition beyond the six columns: "Udhaar since".** `owes_today ≠ billed − paid_since` whenever
udhaar was issued after `to`, which is the normal case on the 23rd. The expanded panel therefore
shows sales dated after `to` as one figure, and the arithmetic
`owes_today = billed + udhaar_since − paid_since` stays traceable.

### D3 — One row per customer, expandable to every slip and payment

The detail lists each udhaar slip with its date, fuel, quantity and unit, vehicle and amount.
It lists each payment with its date, mode, bank reference and amount. Lines are split into
"in the period" and "since the bill".

### D4 — Listed if there is a balance or any activity

A customer appears if any of owed-before, billed, paid-since or owes-today is non-zero, **or** if
they have any row dated in `[from, today]`. The row test matters because a sale plus its reversal
nets to zero but is still activity somebody may be asked about.

### D5 — Two date boxes, no period buttons

The screen opens on the most recently completed half-month (1–15 or 16–end), then leaves the
dates entirely to the user.

### D6 — Reversals shown and tagged as cancelled; totals net them

This is §6.9's rule: both rows stay visible. Every sum includes every row, the same convention
§6.6 uses for `outstanding`.

*Corrected while amending the spec.* This section first said a reversal is dated by its own
shift. It is not: `reverse_sale` and `reverse_repayment` copy the original's `shift_id` and
`business_date`, so a pair always nets inside one period. The cross-period effects are two
different ones, and §13.41 records both:
- the corrected **replacement** slip needs an open shift, so it lands in a later period;
- a reversal entered after a bill went out changes that **past** period's figures.

### D7 — Print-friendly layout, for the whole period or one customer

A `@media print` block, `window.print()`, and nothing else. There is no PDF library and no
WhatsApp or SMS, which §12 keeps out of V1. A print stylesheet is a *medium*, not a second
palette, so §12's one-palette rule is untouched.

### D8 — The bank tick is the stored link **or** the Bank screen's own live match

Only a repayment *created from* a statement line is permanently linked to it. A hand-typed
bank-transfer repayment is verified only by `_verify_against_recorded`'s live `(date, amount)`
match, which the Bank screen recomputes every time. That function's candidate query is split
out so this screen can reuse it **unchanged**, and the two screens can therefore never disagree.

Each payment gets one of four statuses, for `mode = bank_transfer` only:
- `verified`;
- `ambiguous`: it is one of several identical candidates, which §13.37 refuses to guess between;
- `not_on_statement`: an uploaded statement covers that date, but no matching line was found;
- `no_statement`: no uploaded statement covers that date.

Cash, card and UPI repayments get no status. Card and UPI settle through Paytm in aggregate, so
no single line can verify them.

### D9 — Udhaar on a still-open shift counts, with a warning

### D10 — An opening balance dated inside the range goes into "Owed before"

It is debt from before the app existed. Its line still appears in the expanded view.

### Mechanical decisions

| # | Decision |
|---|---|
| M1 | `GET /api/v1/credit-customers/statement?from=&to=`, manager floor (§8). **Registered before `/{customer_id}`**, or "statement" parses as a UUID and returns 422 |
| M2 | Both dates are required. The three window refusals are shared with `reports._resolve_window` through one helper, so the wording cannot drift. The maximum span is 366 days (rows only, no cash pass) |
| M3 | The error codes are reused: `INVALID_DATE_RANGE` and `BUSINESS_DATE_IN_FUTURE` |
| M4 | Totals come from grouped `SUM(CASE …)` aggregates, the shape of `outstanding_by_customer`, which also supplies `owes_today` so the two cannot disagree |
| M5 | Lines are capped at 5,000 with `lines_truncated`. **Totals never depend on lines** |
| M6 | The grand totals row is computed server-side. The client does no money arithmetic (§14) |
| M7 | `opening_balance_entered` per customer. A missing opening balance means "unknown", never ₹0 (§6.8, §14) |
| M8 | Sales are dated by `shifts.business_date`, the ledger's join (§6.1). Repayments are dated by their own `business_date` |

## The arithmetic

For window `[F, T]`, outlet today `D`:

```
owed_before  = Σ opening_balances(as_of_date ≤ T)
             + Σ sales(date < F) − Σ repayments(date < F)
udhaar_in    = Σ sales(F ≤ date ≤ T)
repaid_in    = Σ repayments(F ≤ date ≤ T)
billed       = owed_before + udhaar_in − repaid_in       (= outstanding as of T)
udhaar_since = Σ sales(T < date ≤ D)
paid_since   = Σ repayments(T < date ≤ D)
owes_today   = outstanding(c)                            (§6.6, unchanged)
```

## Build order

1. `CLAUDE.md` amendments, committed on their own before any code: §6.6, §8, §11 (21 is the
   statement, 22 the profit bridge), §13, §14.
2. Shared window validation, the service, the bank status, the endpoint and the tests:
   *"Phase 21: the billing-period statement endpoint"*.
3. The screen, the route, the hub button and the print CSS: *"Phase 21: the Billing statement
   screen"*.
4. Notes: *"Phase 21 notes"*.

## Error codes introduced

None.

## Test inventory

`tests/test_credit_statement.py`:

- every column correct with all terms non-zero; billed equals outstanding as of T; owes_today
  equals `outstanding()`;
- boundaries at F−1, F, T and T+1;
- a sale is dated by its shift, not by its `created_at`;
- an opening balance inside the range lands in owed_before and appears as a line;
- a reversal nets inside its original's period even when entered weeks later, with both lines
  tagged;
- bank and shift repayments both count, and totals agree with the ledger's `balance_after`;
- inclusion: zero and inactive is hidden, a net-zero sale plus reversal is listed, and a customer
  with only paid-since is listed;
- an open shift in range gives a warning, and its udhaar still counts;
- `opening_balance_entered` false vs a ₹0 row;
- bank status in all four states, plus none for cash;
- the line cap triggers truncation while totals stay exact;
- validation: 422 for from > to, for more than 366 days, for a future date and for missing
  parameters;
- an attendant gets 403; another outlet's customers are absent;
- the route-order check;
- money serialises as a string.

## Verification

- `pytest` green, including the structural tests, with 100% coverage on touched modules.
- By hand, as a manager: pick 1–15 of a real month and check Billed against the paper bill and
  against the ledger's `balance_after` on the 15th. Then expand a customer, print the whole
  period and one customer, and confirm an attendant is refused.

## What shipped

Shipped as planned, in four commits (spec, endpoint, screen, notes), with these differences:

- **`opening_since` added.** The approved six columns could not make `owes_today` exactly
  reconcilable for a customer whose opening balance is dated after the window. It is shown in
  the expanded panel only when non-zero. See the notes.
- **D6 corrected.** A reversal nets inside its original's period. The cross-period effects are
  replacements and after-the-fact corrections, recorded in §13.41.
- **Bank matcher split into three functions** rather than only being renamed:
  `recorded_candidates`, `resolve_candidates` and `verify_against_recorded`. A tie had to name
  its candidates.
- 1,734 tests pass (31 new). Every line of new code is covered.
- **Checked against the real July data:** all customers reconcile with their ledgers. The
  screen itself has not been checked in a browser (see the notes).

## Still owed by the owner

- Veto or keep "Udhaar since" and the half-month default.
- **Confirm the 19 unconfirmed bank udhaar credits on the Bank screen** before sending bills.
  Until then the statement does not include them.
- **Close the open shift in 16–31 July.**
- Check the screen by hand: the layout on a phone, and print preview.
- Phase 20's plan and notes documents (from the audit).
