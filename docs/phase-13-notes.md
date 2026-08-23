# Phase 13 — Reporting: Notes

> The narrative counterpart to `phase-13-plan.md`. The plan says what was built; this says
> **why**, and records what went wrong on the way. Written to be read six months from now by
> somebody trying to understand a decision, not as a changelog.

---

## 1. The question this phase is actually about

§11 describes Phase 13 in six words. The build was not hard; twelve phases had already
computed every figure a report needs, and `pricing.py`'s docstring had been naming this phase
as a future caller since Phase 3.

What took the thinking was a question the spec had never had to answer, because nothing had
ever read across days: **which figures is a report allowed to compute?**

§5.2 is emphatic that `expected_closing` and its eleven components are stored so a reader can
see the figure *as it stood*, and §14 forbids recomputing a finalised day. Taken alone, that
says a report reads and never calculates.

But a business date nobody reconciled has no stored row at all. Under §6.5's locker model —
*"there is no fixed counting moment"* — that is not an edge case, it is most days. A 7-day view
that showed five days and silently omitted two would be a report lying by omission, and §4.7's
whole argument is that the abnormal day should become **visible** rather than absorbed.

So §13.20: read the snapshot where one exists, compute live where none does, and **say which**.
`source` is part of the contract rather than a hint. A snapshot is a record of what somebody
was told; a computed day is an estimate of a day still in motion. They are different kinds of
claim, and a reader who cannot tell them apart will average them.

## 2. The thing nobody would have predicted

`daily_cash_summaries` stores `metered_fuel_sales` as **one number**. No per-fuel split, no
margin, no rate.

That means the fuel breakdown on a report is *necessarily* computed live — including on a day
that was finalised months ago. There is nothing else it could be.

And that has a consequence the spec had never stated: **the breakdown is priced today while
the total it sits beside was frozen then.** If somebody entered a backdated `effective_from`,
those two disagree. §11 already names that exact hazard as the reason `fuel_prices` needed an
audit trail — *"a backdated `effective_from` … can revalue a closed shift with nothing
recording who entered it"* — but nothing in the system had ever noticed it happening.

The report does now. `breakdown_reconciles` compares the two and shows **both figures** when
they differ. It does not pick a winner, and that restraint is the point: §5.2 warns that when
*"the total and its own explanation disagree"*, the explanation is the half a human can
actually check. Resolving it silently would throw away the useful one.

This is the only genuinely new control in the phase, and it fell out of a storage decision made
three phases earlier rather than out of anything §11 asked for.

*Lesson: "what is not stored" is a design question with the same weight as "what is". The
per-fuel split's absence from the snapshot was invisible until something tried to read it back.*

## 3. The oldest trap in this codebase, for the fourth time

`test_the_snapshot_branch_of_a_report_never_recomputes` reads `_from_snapshot`'s source and
asserts it contains no `day_totals`.

It failed on its first run — against `_from_snapshot`'s **own docstring**, which explains that
the function must not call `day_totals`. The prose describing the rule tripped the check
enforcing it.

This codebase has now recorded four versions of the same shape:

- **Phase 10**: a structural test tripped over the comment documenting the rule it checked, and
  concluded *"left as a text search, it would have taught the next person to delete the
  comment."*
- **Phase 11**: the same, in `_CONSTRAINT_ERRORS`.
- **Phase 12**: the worse variant — a comment **silently satisfied** a search instead of
  breaking it, so the test sat green while the audit-log screen was deliberately broken.
- **Phase 13**: this one, back to the noisy version.

The noisy version is the good one. It fails immediately and the fix is obvious. Phase 12's is
the one that guards nothing forever.

The rule those four arrived at, and it is now applied consistently: **the prose lives in the
test's docstring, and the scanner reads only executable source.** Here that meant stripping the
docstring node before serialising the function body. Otherwise the fix a future reader reaches
for is deleting the explanation, which is exactly backwards.

## 4. A structural test decided the shape of the phase

The plan had Steps 4, 5, 6 and 7 as four commits: three endpoints, then the screens.

The moment `app/api/v1/reports.py` existed, `tests/test_frontend_assets.py` went red —
*"new router(s) with no entry in this test's prefix map: ['reports']"*. Adding the entry then
fails the second assertion until a screen actually calls the path.

That test is Phase 12's Step 13, and its docstring says exactly what it is for: *"a Phase 13
router that ships with no way to reach it fails the suite rather than the review."* It was
written for a hypothetical future router, and the first real one it met was this.

The consequence is that **a router cannot land green without a screen**, so the phase's real
unit of work is endpoint-plus-screen, not endpoint-then-screen. Steps 4–7 became one commit.

That is the best outcome a structural test can have — not catching a bug, but making the wrong
shape of work impossible to commit. Phase 11's notes framed the goal as *"the gap survived
seven phases precisely because nothing failed when it was missing"*; this is the inverse
working as designed.

## 5. Two places the schema answered a question the plan had gotten wrong

**A zero margin cannot exist.** The plan called for a test proving a ₹0.00 commission is a real
margin rather than an absence — the guard against implementing "no margin" as `if not margin`,
since `Decimal("0.00")` is falsy. The test could not be written: `ck_fuel_margins_margin_positive`
refuses the row.

So it became a test asserting the **constraint**, with the reasoning attached, so that anybody
relaxing it lands on the explanation rather than discovering the truthiness bug through a wrong
report. The service still uses `is None` regardless — correctness that depends on a CHECK
constraint two tables away is correctness waiting to expire.

**A fixture date in the future is refused.** Other cash-engine test modules use 2027 dates
freely, because `cash-position` has no opinion about them. These endpoints enforce §6.1, so
`DAY = date(2027, 5, 12)` produced sixteen failures that were entirely the endpoint being
right. Cost one debugging round; the constant now carries a note saying why it is in the past.

## 6. Why the chart shaped the API

§12 deferred charts to this phase: *"Phase 13 decides what is worth plotting before anything
plots it."* The answer was two shapes — a magnitude over time, and a signed deviation — because
those are the two questions a rolling view exists to answer.

The interesting part was not what to plot but a rule collision. **A bar chart is
`value / max × height`, which is arithmetic on money**, and §14 forbids that in JavaScript
because JS has no decimal type.

There was no way to satisfy both by writing better client code, so the API changed:
`bar_height_pct` arrives as a ready-made CSS percentage string computed server-side in
`Decimal`, and the client *assigns* it. The client divides nothing and parses nothing.

The side effect turned out better than the rule demanded. Because the bars and the table
beneath them come from **one** server-side pass, the chart cannot disagree with its own
numbers. A client-side division could have drifted from the figures printed next to it, and
nobody would ever have noticed — which is precisely this project's stated failure mode.

*Transferable: when a house rule makes the obvious client implementation illegal, that is often
the API telling you it is missing a field.*

## 7. One quiet DOM trap

`dom.js` needed an SVG twin of `el()`, and it has to be a separate function rather than a flag.

`document.createElement("rect")` **does not fail**. It returns an HTML unknown element, which
lays out as nothing inside an `<svg>` and renders a blank box with no error in the console.
That is §13.18's exact failure shape — invisible in the Python suite, total on screen — and it
is the sort of thing that would have been found by opening the browser, which is how Phase 12
found its blank-page bug.

Second trap in the same function: `className` on an SVG element is a read-only
`SVGAnimatedString`, so `node.className = "chart-bar"` silently does nothing. It must be set as
an attribute. Both are noted in the code rather than left to be rediscovered.

## 8. Verification

```
pytest                                 ->  1403 passed  (1302 before Phase 13)
pytest, second run, same database      ->  1403 passed
alembic current                        ->  0014 (head) -- this phase adds NO migration
alembic check                          ->  no new upgrade operations
100% coverage on app/services/reporting.py and app/api/v1/reports.py
```

Structural assertions no value test makes:

- **`_from_snapshot` contains no `day_totals`, no `expected_closing`, no `shift_sales`, and no
  `BinOp` at all** — every value must be a column read, because a `+` there means a figure is
  being derived rather than reported
- The reporting layer contains no `db.add` / `commit` / `flush` / `delete` / `audit.record`
- The router declares no write verb, so `test_audit_coverage.py`'s silence about it is
  deliberate rather than accidental
- **Every `except` in `reporting.py` inspects `exc.code` and re-raises what it does not
  recognise** — a broad catch would report a real failure as "no commission entered"
- All three endpoints are called **by name** from the screen (`test_frontend_assets.py` only
  proves the string `/reports` appears somewhere, and would pass with two of three unbuilt)

**All five were deliberately broken once**, in executable code rather than in a comment, and
confirmed to fail naming the right thing. Phase 12's notes are explicit that breaking a
structural test in the wrong place is how one stays green while guarding nothing.

## 9. Open items, carried forward

**New:**

- **`VARIANCE_ALERT_THRESHOLD` = ₹100 is a guess**, exactly as `EXPENSE_RECEIPT_THRESHOLD`'s
  ₹5,000 is, and it is live on real money from the day this ships. Too low and every day is
  flagged, which teaches a manager to dismiss the list unread; too high and the ₹500 gap §5.2
  describes — the one booked as udhaar against a salesman's own name — never surfaces.
- **Are §13.23's six alert kinds the right list?** Each is derived from a signal already
  stored, so adding or removing one is cheap. A list that reports things the owner does not act
  on is a list that gets ignored, and the ones that matter get ignored with it.
- **§13.23 has no "seen it" state.** A variance a manager has consciously accepted keeps
  appearing while it is in the window. Recorded as a limitation rather than discovered as an
  annoyance; the fix is a stored acknowledgement, and it should wait for evidence that the list
  is being ignored.
- **§13.24's query cost has never been measured.** A 31-day window of entirely unreconciled
  days runs a full §6.4 pass per day. Correct, and bounded, but the bound was chosen by
  reasoning rather than by timing anything.
- **The frontend is hand-verified only** (§13.18, unchanged). The three new screens have
  structural coverage and no behavioural coverage.

**Carried forward, all still open, all still load-bearing:**

- **Petrol and diesel dealer margins have never been entered.** Phase 13 makes this visible in
  the product for the first time — `fuels_missing_margin` names them on every day report — but
  it does not fix it. Until they are entered, profit reporting covers CBG only.
- The first opening balance must be seeded before any day can be finalised
- No way to write off a shortfall (§13.15)
- Audit trail retention (§13.17)
- CBG's real max flow rate in kg/min; whether testing quantities are recorded on paper and in
  what unit; `EXPENSE_RECEIPT_THRESHOLD` = ₹5,000 is still a guess; the real expense-category
  and credit-customer lists
- Does a salesman hold a change float overnight? Is a surplus ever booked?

## 10. The check no test replaces

Reconcile one real week against the paper register, on the phone it will be used on.

Phase 10's notes said it, Phase 12's repeated it, and this phase adds a version of its own that
is specific to reporting: **every screen here invites a comparison between two days.** That is
what a rolling view is for. The suite proves each day's figures are right individually; it
cannot prove that putting a `snapshot` day next to a `computed` one in the same column of the
same table leads a reader to the right conclusion about the week.

The `source` pill is this phase's attempt at that, and it is the part most worth watching a
real person use. If a manager reads seven bars and never notices that two of them are
estimates, the label has failed regardless of how correct the arithmetic underneath it is.
