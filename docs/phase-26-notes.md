# Phase 26 — The Summary tab, rebuilt for reading: Notes

> A learning reference for why each of the owner's requests became the change it did. The plan
> and the "What shipped" record are in `docs/phase-26-plan.md`.

---

## What was built

- **The headline holds every window total.** Total sales leads. Beside it: fuel sales, non-fuel
  sales, gross margin, expenses, then litres sold and kilograms sold, each its own figure and
  never added together.
- **"How the money arrived" and "Quantity sold" are gone** as separate cards.
- **Fuel is one wide card.** A larger donut sits beside each fuel's figures: sale value, quantity,
  gross margin and commission per unit. Pointing at or tapping a fuel lifts its slice and names it
  in the donut's centre.
- **Expenses are selectable bars.** The largest category is chosen to begin with, and its rows
  are listed beside the bars, grouped by business date, each day with its subtotal. Reversals are
  tagged rather than hidden. Hovering a bar fetches its list at once and chooses it after 120 ms;
  a click or tap chooses it immediately, and the arrow keys move between categories.
- **Udhaar is a bridge:** owed at start, plus given, minus collected, equals owed at end. Beside
  it are the five customers who owe the most at the window's end, each opening their ledger, and a
  button to the billing statement for the same dates.
- **One new endpoint:** `GET /reports/summary/expenses`, at the manager floor.
- No migration, table, column, business rule, error code or npm dependency.
- **Tests:** pytest 1,726 (+18), Vitest 86 (+8), Playwright 164 (+10).
- **Budget:** front door 65.6 KB against a cap of 70. The Summary is a lazy chunk, so first paint
  is unchanged.

## The defect found by asking what a card *means*

The old udhaar card showed "issued", "repaid in cash" and "repaid on the card machine or UPI".
The owner's complaint was that it was hard to read. But reading what each number *was* turned up
something worse: the two "repaid" figures were `cash_credit_repayments` and
`card_upi_credit_repayments`, which are §6.4 terms.

§6.4's terms answer one question: what reached the locker, or the card machine, on a shift.
A repayment by bank transfer reaches neither. Since Phase 16 a repayment can have no shift at all,
which is how the system records money that arrived at the bank. Neither term ever sees such a
repayment. So the card said customers paid back less than they did, and disagreed with the
Phase 21 billing statement for the same dates. Nothing crashed, and every number was plausible.

The fix was not a third repayment term on the card. It was to stop the card computing anything:
`credit.statement_rows` was extracted from `period_statement`, and the Summary calls it. The
bridge is the statement's totals, by construction rather than by care.
`test_the_bridge_is_the_statements_totals_to_the_paisa` asserts it against the real statement
endpoint, with a reversal and a payment after the window in the data.

**A term borrowed from another equation carries that equation's question with it.** §6.4's
repayment terms are correct, and they are still in the response. They just answer "what reached
the drawer", not "what did customers pay".

## One query, two consumers

The drill-down's total must equal its bar, or the owner learns not to trust either. The easy
build was a second query: "list expenses in this category in this window", next to the existing
"sum expenses by category in this window". Two queries for one sum agree on the day they are
written and drift on the day somebody adds a filter to one of them. §6.4's `day_totals` argument
says the same.

So `expense_rows_range` is the only query. `totals_by_category_range`, used by the Summary, the
daily report and `/expenses/summary`, now sums its output, and the drill-down lists it.
`test_the_drill_down_total_is_the_bars_figure_reversals_included` holds them equal with a reversal
pair in the data. The reversal is the case where a "helpful" filter would break the equality.

## Capped, and cut only between whole days

§9 wants cursor pagination on every list. The drill-down caps at 500 rows instead (§13.44), and
the reason is the subtotals. Each day shows a server-computed total, so the client adds nothing
(§14). If a page boundary fell in the middle of a day, that day's subtotal would describe rows the
client was never sent.

So the list is cut only between whole days. A day appears with all its rows or not at all, and
the first day always appears whole, however long it is: an empty list under a non-zero total
would be the worst possible answer. The grand total and the row count are computed over
everything before the cut, so `truncated: true` comes with exact figures.

## Design decisions that are really correctness decisions

- **Expense bars are one hue, and sorted largest first.** Phase 19 sorted alphabetically so each
  category kept its colour between windows, because a palette keyed on rank repaints when diesel
  overtakes petrol. The dataviz rule settles it differently: one series means one hue and no
  legend. With colour no longer carrying identity, the order is free to answer "where did most of
  it go". It also removes a latent bug: a seventh category used to wrap round to the first one's
  colour.
- **`bar_pct` and `share_pct` are both sent.** A bar sized by its share of the total makes the
  biggest bar stop at, say, 68%, and the chart looks unfinished. A bar sized against the largest
  makes the label lie if it is also used as the share. So there are two strings for two
  questions, both computed in `Decimal` on the server.
- **The bridge withholds geometry, never figures.** §6.6 allows a negative balance: a customer
  who paid in advance. A left-to-right walk has no honest bar for one, and clamping it to zero
  would draw a different fact from the number printed beside it. The server sends `null`
  geometry, and the rows still print every figure.
- **Every bridge track is the full width of the card.** The first build put label, track and
  figure side by side on each row. The screenshot showed the "owed at end" track shorter than the
  others, because its larger figure took more room. Same percentages on shorter tracks means
  different scales: a misdrawn chart that every test passed. It was found by looking, and fixed by
  giving each bar a full-width track under its label.

## Motion: what it says, and the frame that was found by measuring

- **Category bars grow once, on first view.** Bridge bars draw in sequence as a walk, and
  "collected" grows *leftwards* from where "given" ended, so the motion itself says "taken away".
  The ledger fades in over 240 ms when its category changes. Every one of these is a GSAP
  transform or opacity over the server's geometry. Nothing counts up (§14).
- **The 60 ms screenshot matched the settled one,** which could have meant the fade never ran.
  Sampling opacity every frame showed that it did run. It also showed two frames in which the
  list did not exist at all: a click with no hover before it had nothing cached, so the panel
  went to its loading state and back. Hardly visible, but a flicker every time.
  `useApiQuery` gained `keepPrevious`: the old list stays up, dimmed and `aria-busy`, until the
  new one lands. Dimmed, because a stale list shown at full strength under a new heading would be
  presenting one category's rows as another's.
- **The dimming and the fade are on different elements.** CSS dims the outer wrapper; GSAP fades
  the inner list. The first version put both on one element. GSAP leaves an inline `opacity`
  when it finishes, which would have silently overridden the dimming class. That is the
  one-element-one-engine rule from §14, for a property other than `transform`.

## Things that went wrong in the process

- **Tests were written alongside the code, not strictly before it.** §10 asks for a failing test
  first when a bug is reported. The bank-transfer defect was found in planning rather than
  reported, and its test was written in the same sitting as the fix. It does fail against the
  old figures (₹1,000 against ₹3,000), but it was never run red. Next time, run it red first.
- **`npx prettier` was run on `summary.tsx`.** Prettier is not a project dependency and has no
  config here, so it reformatted the file to 80 columns and multiplied the diff. It was
  re-run at `--print-width 150`, which matches the house style to within a handful of lines on
  `credit.tsx`, before committing. **Don't run a formatter the project does not declare.**
- **A structural test caught real work, not a mistake.**
  `test_chart_geometry_is_assigned_from_the_server_never_derived` allows only named server fields
  in inline geometry. The new fields were added to its allowlist, and an inline `left` is now
  held to the same rule as `width`. The rule was widened, not loosened.

## Still owed by the owner

- A real trading month entered, to see the drill-down and the bridge on real money. Then check
  one category's list total against its bar, and the bridge against the Credit tab's statement
  for the same dates.
