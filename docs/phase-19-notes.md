# Phase 19 — Notes

> The learning reference, not a changelog. What the building of the Summary tab taught, and
> the two things it got wrong on the way.

---

## 1. The constraint that decided the whole shape, found in the first ten minutes

The request was "a Summary tab with a date selector, charts, trends, from and to dates". The
obvious plan is a SQL aggregate over a date range. **It cannot be written**, and finding that
out early is what stopped a day being spent on it.

§6.3 values a shift at `rate_at(fuel_type, shift.started_at)`. §4.1 keeps that rate in an
effective-dated table rather than in a column on the reading. So there is no `price` for a
`SUM(quantity * price)` to multiply by, and the only honest way to total petrol across ninety
days is to walk the shifts and price each one at its own instant.

The tell was in `readings.py:343` — the rate lookup sits *inside* the per-nozzle loop, taking
`at=shift.started_at`. Anything that looked like a one-pass aggregate would have had to find a
current-price column somewhere, and §4.1 exists precisely because that column does not exist.

**The generalisable bit:** when a value is effective-dated, aggregation over time is a walk,
not a sum. The cost lands in the range cap, and that is why `/reports/range` had a 31-day cap
already — it was the same discovery, two phases earlier, recorded as §13.24.

## 2. Two caps, and why they must not become one

The instinct on seeing `_MAX_REPORT_RANGE_DAYS = 31` and `_MAX_SUMMARY_RANGE_DAYS = 366` is to
unify them. They bound different work:

* `/reports/range` re-runs a **full §6.4 cash pass** per unreconciled day.
* `/reports/summary` walks **readings** and memoises the price lookup.

`_resolve_window` was parameterised rather than copied, so the shared parts — §13.30's
trading-day anchor, the three refusals — stay in one place, and a test asserts the range
endpoint still refuses 32 days after the summary endpoint learned to accept 366.

## 3. The cache, and the version of it that was nearly written

`shift_sales` looks a rate up per nozzle per shift and caches nothing. Over a year that is a
few thousand repeats of one indexed query, because a price is constant within a trading day.

The first idea was to monkeypatch `pricing.rate_at` for the duration of the walk. That is a lie
to every other caller sharing the module, and under concurrency it would let one request read a
cache built for a different window. What landed instead is `pricing.LookupCache`: an **optional
parameter**, defaulting to `None`, threaded from the reporting layer through `shift_sales` into
`_effective_row_at` — the one function both lookups already share.

Three properties worth keeping:

* **The key is the exact tuple the lookup is a function of** — `(model, outlet, fuel, instant)`.
  A looser key is how a backdated revision (§11 phase 11) becomes invisible, which is the
  failure §4.1 calls "silently corrupts every historical report". There is a test for it.
* **A miss is cached too**, and still raises. Not caching the miss would leave the refusal path
  as the only uncached case, which is backwards.
* **Scope is one call.** A module-level cache would outlive the append that invalidates it.

While in there, `_warn_on_mid_shift_revision` stopped firing once per *nozzle* — the
approximation is a property of `(shift, fuel)`, so two petrol nozzles were repeating both the
query and the log line. §6.3 requires that warning and it still fires, once per thing it
actually describes.

## 4. Sharing an accumulator rather than writing a second one

`fuel_breakdown` (one day) and `fuel_breakdown_range` (a window) differ only in which shifts
they are handed. The body was extracted to `_accumulate_fuel` rather than copied, because §6.4
already records what a second copy costs: *"two implementations of one equation is the shape
that drifts"* — and the one that drifts would be the one the cash engine depends on.

The dividend was immediate: every rule the day version already enforced — a `None` quantity
marks the window incomplete rather than counting as zero, a rate label survives only if every
shift agreed, §13.21's withhold-the-margin-total — applied to the window for free, and could
not be forgotten.

## 5. What real data said, and the two things it caught

The endpoint was run against the outlet's actual July 2026: 30 trading days, ₹1.04 crore of
fuel across four products, ₹3.29 lakh of gross margin.

**`partial: true`.** Correct — 30 July has readings outstanding. The flag is doing exactly what
§13.35 asks: the totals are a floor and the screen says so, rather than presenting an
incomplete month as a complete one.

**Shares summing to 100.01%.** Five values each quantised to two places need not close. My
first API test asserted `== 100.00` and had passed *by luck of those particular fixtures*,
which is the worst kind of green.

The fix was to weaken the assertion and pin the real behaviour in its own test, **not** to make
the shares close. Largest-remainder rounding would print a percentage that is not the share of
the figure printed beside it — a worse lie than a hundredth of a point, in a system whose whole
premise is that plausible-but-wrong numbers are the enemy. The visual cost of the drift is nil;
0.01% is sub-pixel.

**The lesson is about the test, not the arithmetic.** An assertion that happens to hold for one
fixture and is not guaranteed by the code is a trap for whoever changes the fixture.

## 6. Charts without money arithmetic, again

§14 forbids `value / total` in JavaScript. Phase 13 solved it for bar heights with
`bar_height_pct`; this phase needed the same thing for slices, so `share_pct` is its sibling —
same `Decimal` quantisation, same ready-made percentage string, and `None` rather than
`"0.00%"` when the share is unknowable, because a wedge drawn at zero asserts "this sold
nothing" and that is a different claim (§6.8's rule reaching the geometry).

What *is* computed in the client is the arc: a running offset and some trigonometry over a
percentage the server already divided. That is geometry on a figure, not arithmetic on money —
no rupee value passes through it. Worth stating explicitly in the module, because the next
reader's first instinct will be that a chart doing maths has broken the rule.

`ui/chart.js`'s own header had predicted this phase: *"A pie chart of expense categories
answers neither and would be the first thing to add if this file ever grows."* It grew, and the
prediction was safe because the rule it stated did not depend on which shapes existed.

## 7. Colour by index, never by rank

Six hues, assigned by **position in a stable list**. The tempting alternative — colour by size,
so the biggest series is always the accent — repaints the chart the day diesel overtakes petrol.
The eye trusts colour more than it trusts a label, so a colour that moves between renders is
worse than a long bar.

This is also not §12's forbidden second palette. §12 rules out *per-user configurability* — a
theme that varies by role, a light/dark switch. One dark shell with colour-coded **data** is
neither, and the distinction is the same one §12 already draws about shipping one palette that
happens to be dark.

## 8. What this phase did not do

* **No migration, no new table, no new column.** Like Phases 13, 14 and 15 — every figure
  already existed and the work was reaching it.
* **No new credit SQL.** Credit over a window comes from summing `DayCash.credit_sales_total`
  per date, which preserves §13.20's snapshot/computed distinction. A direct join
  `CreditSale → Shift.business_date` would have been simpler and would have silently flattened
  reconciled and unreconciled days into one number.
* **No behavioural test of the screen** (§13.18 stands). The structural half is covered — the
  module is reachable from the entry point, imports resolve, no `parseFloat`, no external host,
  no `innerHTML` — and the arc geometry was checked headlessly. The rest needs a person.
