# Phase 26 — The Summary tab, rebuilt for reading: Plan

> Written before the code and approved by the owner on 5 October; "What shipped" is filled in as
> commits land. No migration, no table, no business rule, no npm dependency. One new read
> endpoint.

## Context

The owner used the Summary tab (Phase 19, rebuilt in React in Phase 23) and asked for six things:

| Ask | What is there today |
|---|---|
| Remove "How the money arrived" | `PaymentMixCard`: five share bars. **Owner confirmed: remove it.** The split still lives on each day's screen |
| Quantity sold is a big box with half of it empty; show it in the top box, with non-fuel sales too | `QuantityCard` is a whole grid cell holding two numbers. The headline has four stats and no non-fuel figure, though `non_fuel_sales_total` is already in the response |
| Expenses by category: interactive; hover or click a category to see where that money went, by date, like the ledger | `ExpensesCard` draws static share bars labelled by `code`. No endpoint lists expense rows across a window |
| Fuel: a bigger pie on the left, litres and margin on the right in larger type | `FuelCard` stacks a `size-52` donut over a legend of 15px text inside a half-width cell |
| Udhaar in this window: too much information, hard to understand | Three rows: issued, repaid in cash, repaid on card or UPI. **Owner chose:** a four-step bridge, the top five customers owing at the window's end, and a link to the statement |
| Make the page premium, interactive and easy to understand | — |

## Previous-phase audit

Phase 25 shipped to `main` and to production on 5 October (`faacdaa`, Railway SUCCESS; migration
`0016` applied by `preDeploy`). The audit of the Summary tab for this phase found one live
defect:

**The udhaar card understated repayments.** It showed `cash_credit_repayments` and
`card_upi_credit_repayments`, the two §6.4 terms. Those are *drawer* terms: they answer "what
reached the locker or the card machine on a shift". Every `bank_transfer` repayment, and every
repayment with no shift (§5.2's "arrived at the bank"), was absent. So the card's "repaid" was
smaller than what customers actually paid, and it disagreed with the Phase 21 statement for the
same dates. A test pins it before the fix.

## Decisions

### D1 — Mostly frontend; the server gains what the screen cannot compute

- Asks 2 and 4 need no new data.
- Asks 3 and 5 need data the response does not carry.
- Every money figure, share and bar geometry stays server-side (§3 rule 1, §14).

### D2 — The expense drill-down is its own endpoint, fetched on demand

- **Route:** `GET /reports/summary/expenses?from&to&category=CODE`, manager floor (§8 row added).
- **Not embedded in `/reports/summary`:** a year of expense rows would ride along on every load of
  the tab, for a list somebody opens one category at a time.
- **Prefetch:** the client prefetches on hover, so the list is there by the time it is chosen.
- **Response shape:** grouped by business date, newest first. Each day carries its
  server-computed subtotal, and the response carries the grand total. The client adds nothing.
- **Cap:** 500 rows with `truncated`. The totals are over every row, so they stay exact under the
  cap (§13.44, the §13.31 precedent).
- **Reversals** are listed and tagged, and they net into the totals, which is
  `totals_by_category_range`'s convention.
- **Window and category:**
  - The window is resolved and validated exactly as the summary's own, up to 366 days.
  - An unknown category code at this outlet returns 404 `CATEGORY_NOT_FOUND`, an existing code.

### D3 — One row query feeds the bar and the list

`expenses.expense_rows_range(...)` is the single query. `totals_by_category_range` sums its
output, and the drill-down lists it. Two queries for one sum is the drift §6.4's `day_totals`
argument warns about.

### D4 — Expense categories: names, a bar scale, largest first

- **Fields:** `expenses_by_category` rows gain `display_name` and `bar_pct`. `bar_pct` is the
  amount relative to the largest category (that bar is full width). `share_pct` stays the
  "% of all expenses" label.
- **Order: largest first.** Phase 19 kept a stable alphabetical order for colour identity, and
  that reason is gone: the new chart is one accent hue with the selection highlighted, which also
  ends the six-colour wrap at a seventh category.

### D5 — Udhaar is a bridge, and it reads the statement

- **Refactor:** `credit.statement_rows(...)` is extracted from `period_statement`, which keeps its
  behaviour, and the summary calls it. So the bridge and the Phase 21 statement agree to the
  paisa.
- **The `credit` block:**

  | Field | Statement source |
  |---|---|
  | `owed_at_start` | `owed_before` |
  | `given` | `udhaar_in` |
  | `collected` | `repaid_in`, every mode |
  | `owed_at_end` | `billed` |
  | `owes_today` | `owes_today` |

- **Bridge geometry:** each step has an `offset_pct` and a `width_pct`, computed server-side
  against the walk's extent. The geometry is `null` when any figure is negative or the extent is
  zero. A negative balance is legitimate (§6.6) but has no honest bar.
- **`top_owing`:** at most five customers, by `owed_at_end`, each linking to their ledger.
- **Untouched §6.4 terms:** `credit_sales_total` and the two repayment terms stay in the response.
  They are §6.4 window terms, and the screen no longer shows them.

### D6 — `payment_mix` leaves the response with its card

Nothing else reads it. The component amounts (`cash_sales`, `card_total`, …) stay, because they
are §6.4 terms.

### D7 — Interaction and motion

- **Expenses:**
  - **Layout:** one wide card. On desktop, the bars are on the left and the rows on the right; on
    a phone, the rows sit under the bars.
  - **Selection:** the largest category is preselected, so the panel is never empty. Where hover
    exists, hovering selects after a 120 ms intent delay. Leaving the bars keeps the selection, so
    the pointer can travel into the list. A click or tap selects.
  - **Keyboard:** the bars are a radio group with arrow keys.
- **Fuel:**
  - **Layout:** one wide card, a larger donut beside each fuel's figures in larger type.
  - **Hover:** hovering or focusing a fuel dims the other slices, and the donut's centre shows
    that fuel's value. That is the server's string, swapped, never tweened.
- **Headline:** total sales leads. The grid holds fuel sales, non-fuel sales, gross margin,
  expenses, then one stat per unit of measure, never summed.
- **Motion:**
  - Bars grow on first view (the existing GSAP `scaleX` over the server's width).
  - The list cross-fades when the category changes.
  - Selection is a CSS colour and opacity transition.
  - Nothing counts up (§14), and everything collapses under reduced motion.

### D8 — Page order

Headline → sales by day → fuel → expenses → udhaar → provenance. Each card below the trend is
full width.

### Mechanical decisions

| | |
|---|---|
| M1 | The drill-down's day subtotal and grand total are `Decimal` sums in the router over the full row list, before the cap |
| M2 | Bridge geometry reuses `reporting.share_pct` with the extent as its total, so a paisa rounds the same way as every other bar |
| M3 | The fuel card keeps `categoryColour` by position: the fuels' order comes from `fuel_breakdown_range`, which is stable |
| M4 | The drill-down query key is `["/reports/summary/expenses", {from, to, category}]` through `useApiQuery`, so prefetch and render share a cache entry |

## Build order

1. `CLAUDE.md: Phase 26`: §6.6 note, §8 row, §11 entry, §13.44.
2. `Phase 26: one expense query; the drill-down endpoint`: the service, the router, and tests
   written first.
3. `Phase 26: the udhaar bridge reads the statement`: `statement_rows`, the `credit` block, and
   the bank-transfer test written first.
4. `Phase 26: the Summary screen`: the chart components, the screen, fixtures and Vitest.
5. `Phase 26 notes`.

## Error codes introduced

None. The drill-down reuses `CATEGORY_NOT_FOUND`, `INVALID_DATE_RANGE` and
`BUSINESS_DATE_IN_FUTURE`.

## Verification checklist

- [ ] The drill-down:
  - [ ] An attendant gets 403.
  - [ ] Another outlet's rows are absent.
  - [ ] An unknown category gets 404.
  - [ ] Reversals are tagged and net out.
  - [ ] The total equals the summary bar to the paisa.
  - [ ] Day subtotals are correct.
  - [ ] Rows are ordered by business date, not `created_at`.
  - [ ] The cap sets `truncated`, and the totals stay exact.
- [ ] Udhaar:
  - [ ] A shift-less bank-transfer repayment appears in `collected`.
  - [ ] Every bridge figure equals the statement's total for the same window.
  - [ ] The geometry is right, and `null` on a negative balance.
  - [ ] `top_owing` has at most 5 rows, sorted.
- [ ] Expenses are largest first, with `display_name`, and the largest `bar_pct` is `100.00%`.
- [ ] Vitest:
  - [ ] A category's rows show on hover and on click.
  - [ ] The largest category is preselected.
  - [ ] "margin not entered" shows, never ₹0.
  - [ ] Litres and kilograms are shown separately.
  - [ ] The money-arrived card is gone.
  - [ ] The bridge strings are assigned untouched.
- [ ] Playwright smoke in both palettes; mid-transition frames looked at.
- [ ] `npm run budget` unchanged (the Summary chunk is lazy).
- [ ] By hand: each category's list total against its bar, and the bridge against
  `#/credit/statement` for the same dates.

## What shipped

*(filled in as commits land)*

## Still owed by the owner

- A real trading month entered, to see the drill-down and the bridge on real money.
