# Phase 13 — Reporting: Plan, Decisions, and Test Inventory

> Follows the `phase-5` … `phase-12-plan.md` pattern: written **before** the code, with §9's
> checklist filled in as it is met and §10 recording what actually shipped where it differs.

---

## Context

Twelve phases have built 24 routers, 1,302 tests and a working frontend. Every figure the
system knows is reachable — **one shift at a time.** There is no way to ask "how did this week
go", and no way to be told "Tuesday was ₹800 short" without opening Tuesday and looking.

§11 describes Phase 13 in six words: *"Reporting — daily summary, 7-day rolling view, variance
alerts."* That is the scope. What this plan adds is the argument about **which figures a report
is allowed to compute**, because that is where a reporting layer breaks a cash system:

- §5.2 stores `expected_closing` **and every component** precisely so a report shows what the
  manager was told *on the day*, not a recomputation. §14 forbids recomputing a finalised day.
- But a business date nobody reconciled has **no snapshot at all**, and a 7-day view that
  silently drops yesterday because nobody clicked "create summary" is a report lying by
  omission — the opposite of §4.7's *"the abnormal day becomes visible instead of reassigned."*
- And **profit is not snapshotted anywhere.** `daily_cash_summaries` stores `metered_fuel_sales`
  as one number with no per-fuel breakdown and no margin, so any fuel or profit detail in a
  report is *necessarily* computed live — including on a finalised day.

That last point is the sharpest thing in this phase and it is not obvious. Phase 13's daily
report will show a per-fuel breakdown computed **today** next to a `metered_fuel_sales` figure
frozen **then**. If somebody backdated a price revision, those two disagree — and §5.2 warns
that *"the total and its own explanation disagree, and the explanation is the part he can
check."* Rather than hide that, this phase **detects and reports it** (D3).

### The state this phase inherits

Phase 12 was audited at the start of this session and **no defect was found**: 1,302 tests
pass, `alembic` at `0014`, 100% coverage on `app/main.py`, `app/core/security.py`,
`app/core/jwks.py`, `app/api/v1/client_config.py`, CSP and money guardrails verified by hand.
Step 0 is therefore already done and produces **no commit**, as in Phase 11 and 12.

### What already exists and must be reused, not re-derived

The exploration found that nearly every term Phase 13 needs is already built and tested:

| Need | Existing | Location |
|---|---|---|
| Rate / margin lookup | `pricing.rate_at`, `pricing.margin_at` (both **raise**, never return 0) | `app/services/pricing.py:71,108` |
| Shift valuation | `readings.shift_sales(db, shift=, price_only=)` → `list[SalesLine]` | `app/services/readings.py:262` |
| Profit arithmetic | `sales.dealer_profit(quantity, margin)` | `app/services/sales.py:302` |
| Per-day §6.4 totals | `cash.day_totals(db, outlet_id=, business_date=)` → `DayTotals` | `app/services/cash.py:559` |
| §6.4 closing figure | `cash.expected_closing(opening_balance=, totals=)` — **pure** | `app/services/cash.py:597` |
| §6.5 chain | `cash.previous_summary`, `cash.opening_balance_from` | `app/services/cash.py:633,655` |
| Date-ranged aggregate | `expenses.totals_by_category_range` — joins through `Shift.business_date` | `app/services/expenses.py:214` |
| Range-cap pattern | `_MAX_SUMMARY_RANGE_DAYS`, 422 `INVALID_DATE_RANGE` | `app/api/v1/expenses.py:864,886` |
| Timezone-correct today | `shifts.outlet_today(tz_name)` | `app/services/shifts.py:79` |

`pricing.py:11`'s own docstring already names this phase: *"Sales (Phase 5), the cash engine
(Phase 10) and **reporting (Phase 13)** all call it rather than reimplementing it."*

---

## 1. Step 0 — Phase 12 audit

Done this session. `pytest` → **1,302 passed**; `alembic current` → `0014 (head)`; mount
ordering, CSP, `parseFloat`/`innerHTML`/external-host guarantees and the 100% coverage claim
all verified independently. **No commit.**

---

## 2. Spec amendment (Step 1, its own commit)

`Spec: what a report may compute, before Phase 13`

| § | Amendment |
|---|---|
| §8 | Three new permission rows at the **manager** floor: read the daily report; read the rolling range; read the variance alerts. Attendant ❌ throughout — §8 already says *"Read all shifts / reports: ❌ manager ✅ admin ✅"* and these are reports |
| §11 | Expand the Phase 13 line from six words to the endpoint and screen inventory below |
| §13.20 (new) | **A report reads the snapshot where one exists and computes live where none does, and always says which.** The `source` field is not decoration; it is the difference between "what the manager was told" and "what is true now" |
| §13.21 (new) | **Profit in a report is per fuel type, and the combined total is withheld when any fuel with sales has no margin.** Petrol and diesel margins have never been entered here, so this is the live case on day one, not a hypothetical |
| §13.22 (new) | **The fuel breakdown on a snapshotted day is computed live and may disagree with the stored `metered_fuel_sales`.** The report detects and reports the disagreement rather than hiding it (D3) |
| §13.23 (new) | **Variance alerts are derived on every read and cannot be individually dismissed.** Clearing one means reviewing the row it points at. Also: alerts are windowed, so an open flag older than the window is not surfaced here |
| §13.24 (new) | **The range report is O(days × shifts) for unreconciled days.** Capped at 31 days; revisit when a real query count exists |
| §14 | Four new guardrails — see §7 below |
| §16 | Add `VARIANCE_ALERT_THRESHOLD=100.00`, with the same "must not require a deploy" note §6.7 and §6.11 carry, and the same honest admission that the figure is a guess |

---

## 3. Decisions

### D1 — A new `reports.py` router, not new params on `daily_summaries.py`

Adding `from`/`to` to `GET /daily-summaries` is the cheapest change and it is **wrong**, for a
reason that is already pinned by a test. `tests/test_cash_permissions.py:172`
`test_the_daily_summary_is_never_recomputed_on_read` asserts that `list_summaries`,
`read_summary` and `update_summary` contain neither `day_totals` nor `expected_closing=`. That
test is correct and must not be weakened — those routes serve the **record**, and the record is
never recomputed.

Phase 13 needs a route that *does* compute, for days that have no record. Those are two
different contracts and they get two different modules. `daily_summaries.py` is untouched.

### D2 — Three endpoints, mapping one-to-one onto §11's three nouns

All at the **manager** floor, all read-only, all outlet-scoped from `actor.outlet_id` (never
`DEFAULT_OUTLET_ID` — `audit_logs.py:139` states the rule).

| Endpoint | §11's noun |
|---|---|
| `GET /api/v1/reports/daily/{business_date}` | daily summary |
| `GET /api/v1/reports/range?from=&to=` | 7-day rolling view (default: last 7 days ending today-at-outlet) |
| `GET /api/v1/reports/variance-alerts?from=&to=` | variance alerts |

**Path-collision analysis** for `router.py`'s comment: `/reports/range` and
`/reports/variance-alerts` are static; `/reports/daily/{business_date}` is one segment deeper
and its parent segment `daily` is static. Nothing collides, but the two static routes are
declared **first** anyway, matching the `/fuel-prices/current` and
`/credit-customers/outstanding` precedent.

### D3 — `source` is a field, and a snapshotted day still gets a live fuel breakdown

Per business date the report reports **where each half of the figure came from**:

- **Cash half** — `snapshot` when a `daily_cash_summaries` row exists (read verbatim, nothing
  recomputed); `computed` when none does; `no_trading` when the date has no shifts and no
  summary; `unavailable` when live computation refused (a missing price).
- **Fuel half** — **always live.** There is no stored per-fuel breakdown to read.

Which produces the one genuinely new control in this phase. On a `snapshot` day the report
compares its live `fuel_sales_total` against the stored `metered_fuel_sales`:

```
breakdown_reconciles      = (fuel_sales_total == snapshot.metered_fuel_sales)
snapshot_metered_fuel_sales = the stored figure, always shown alongside
```

A mismatch means a price was backdated under a closed day, or a reading changed beneath it.
§11 names exactly this hazard as the reason `fuel_prices` needed an audit trail: *"a backdated
`effective_from` … can revalue a closed shift."* Nothing in the system detects it today.
**This does, and it never silently picks a winner** — both figures are on screen.

### D4 — Profit is per fuel, and the total is withheld rather than partial

`shift_sales(price_only=False)` raises 409 `NO_MARGIN_FOR_DATE` on the first fuel with no
margin, which would refuse the entire report on **every petrol day** — §6.3's argument, one
phase later. So reporting does **not** flip that flag. It calls
`shift_sales(price_only=True)` for values, then looks up `margin_at` **per distinct fuel type**
inside a `try/except AppError`, and computes profit with `sales.dealer_profit`.

The catching lives in `app/services/reporting.py`, not in `pricing.py` — `margin_at`'s raise is
correct and stays correct; the reporting layer is the one with a reason to tolerate a gap.

Per fuel line: `margin_per_unit` and `gross_fuel_margin` are `None` with
`margin_unavailable_reason: "NO_MARGIN_FOR_DATE"`. **Never `0`.**

At the top level:

```
gross_fuel_margin_total = None  unless every fuel with non-zero sales has a margin
fuels_missing_margin    = ["PETROL", "DIESEL"]      # always populated, never omitted
profit_basis            = "gross fuel margin on quantity sold; excludes stock revaluation (§13.7)"
```

A partial total labelled "total" is precisely the plausible-but-wrong number this document
exists to prevent. Note `readings.py:851` already coalesces `line.profit or Decimal("0.00")`
when totalling a shift — **that coalescing is not copied here**, because that endpoint 409s
when a margin is missing and so never reaches the case this one is built for.

### D5 — Quantities are reported per unit, never as one number

`quantity_by_unit: dict[str, Decimal]` — `{"litre": "...", "kilogram": "..."}`, the shape
`ShiftSales` already uses (`readings.py:184`). §4.5: adding kilograms of CBG to litres of
petrol produces a number with no meaning. There is deliberately **no** `total_quantity` field.

### D6 — The chart gets its bar heights from the server, as CSS percentage strings

§14 forbids arithmetic on money in JavaScript, and a bar chart is `value / max × height` —
arithmetic on money. The resolution is that the **server** does it, in `Decimal`, and sends a
value the client can only assign:

```
bar_height_pct: "73.21%"     # presentation hint. The client sets style.height = this.
```

The client divides nothing, parses nothing and never calls `Number()`. It is also the only
construction that makes the chart **provably** consistent with the table beneath it, since both
come from the same Decimal pass. Named `bar_height_pct` and documented in the router as a
display hint, not a money field.

SVG is built with `document.createElementNS` via a new `svgEl()` in `dom.js` — `el()` uses
`createElement`, which silently produces a useless HTML element for `<svg>` tags. No
`innerHTML` anywhere (the structural test forbids it, and `<svg>` is exactly where somebody
reaches for it).

### D7 — Alerts are derived from signals that already exist

Six kinds, each grounded in a stored flag or a stored figure. Nothing new is persisted.

| kind | Source |
|---|---|
| `variance_exceeds_threshold` | `abs(summary.variance) > VARIANCE_ALERT_THRESHOLD`, **strictly `>`** per §6.7/§6.11's boundary convention |
| `day_not_reconciled` | a business date with shifts and no `daily_cash_summaries` row |
| `summary_requires_review` | `daily_cash_summaries.requires_review` (§13.16) |
| `unreviewed_flagged_expenses` | `expenses.requires_review AND reviewed_at IS NULL` (§6.7) |
| `reading_requires_review` | `nozzle_readings.requires_review` (§13.10) |
| `open_shift_on_a_past_date` | `shifts.status = 'open'` with `business_date < outlet_today` |

`variance IS NULL` is **not** an alert of kind `variance_exceeds_threshold` — null means nobody
counted, not "no discrepancy" (§6.8, and `money.js:20-31` states it for the client). It surfaces
as `day_not_reconciled` only if there is no summary row at all; a summary with
`actual_counted = NULL` is the normal case under §6.5's locker model and is **not** an alert.

### D8 — The range is capped at 31 days, and defaults to 7

`_MAX_REPORT_RANGE_DAYS = 31`. Tighter than `/expenses/summary`'s 366 because that endpoint
reads rows and this one may compute a full §6.4 pass per unreconciled day. 31 covers both §11's
7-day view and a month. `from > to` → 422 `INVALID_DATE_RANGE`; span over the cap → the same
code, reusing `expenses.py:886`'s exact shape. A `to` in the future → 422
`BUSINESS_DATE_IN_FUTURE`, evaluated in `TZ_DISPLAY` via `shifts.outlet_today` (§6.1).

### D9 — Config, not a literal

`VARIANCE_ALERT_THRESHOLD: Decimal = Decimal("100.00")` in `Settings`, validated `> 0` by the
existing `_threshold_must_be_positive` validator (extend its field list), exposed on
`GET /client-config` so the screen labels a row "over threshold" using the same number the
server used. §6.7 and §6.11 both say *"changing it must not require a deploy"*, and a threshold
hardcoded in JavaScript desynchronises the moment it moves.

**₹100 is a guess** and is recorded as such in §16 and in the owner's open-questions list,
exactly as `EXPENSE_RECEIPT_THRESHOLD`'s ₹5,000 is.

---

## 4. Response shapes

Declared inline in `app/api/v1/reports.py` under a `# --- schemas ---` banner (house
convention — there is no `app/schemas/`). Money is plain `Decimal | None` on responses;
`condecimal` is input-side only. Every derived-money response carries a constant prose
`*_basis` field, as `CashPositionResponse` and `SummaryResponse` do.

```
DailyReportResponse
  business_date: date
  shifts: list[ShiftBrief]            # id, sequence, status, attendant_id, attendant_name
  cash: DayCashBlock
  fuel: list[FuelLine]
  fuel_sales_total: Decimal | None    # None if any line's quantity is unknown
  gross_fuel_margin_total: Decimal | None
  fuels_missing_margin: list[str]
  quantity_by_unit: dict[str, Decimal]
  breakdown_reconciles: bool | None            # snapshot days only
  snapshot_metered_fuel_sales: Decimal | None  # snapshot days only
  expenses_by_category: dict[str, Decimal]
  expenses_total: Decimal
  exceptions: ExceptionBlock
  incomplete: bool
  profit_basis / fuel_basis / cash_basis: str  (constants)

DayCashBlock
  source: "snapshot" | "computed" | "no_trading" | "unavailable"
  unavailable_reason: str | None
  opening_balance / opening_balance_source / expected_closing / actual_counted / variance
  + the eleven §5.2 components, each Decimal | None
  is_finalised / requires_review / review_note

FuelLine
  fuel_type_id, code, display_name, unit_of_measure
  quantity, rate_per_unit, sale_value: Decimal | None
  margin_per_unit, gross_fuel_margin: Decimal | None
  margin_unavailable_reason: str | None

RangeReportResponse
  from_ (alias "from"), to: str        # echoed as strings, /expenses/summary's precedent
  threshold: Decimal
  days: list[RangeDay]                 # every date in the window, gaps included
  basis: str

RangeDay
  business_date, source, unavailable_reason, shift_count
  metered_fuel_sales / non_fuel_sales_total / total_sales
  expected_closing / actual_counted / variance    (each Decimal | None)
  is_finalised, requires_review, alert: bool
  bar_height_pct: str                  # D6

AlertsResponse
  from_, to: str; threshold: Decimal; items: list[Alert]; basis: str

Alert
  kind, business_date, detail: str, amount: Decimal | None,
  shift_id: UUID | None, count: int | None
```

**`no_trading` semantics, decided rather than left to fall out:** sales are `"0.00"` (nothing
was dispensed — that is a real zero) but `expected_closing` / `actual_counted` / `variance` are
`null` (nothing was reconciled). **`computed` days with no prior summary at all** get
`expected_closing = null` — the §6.5 chain cannot be anchored — while their sales figures are
still valid.

---

## 5. Build order

| Step | What | Commit message |
|---|---|---|
| 0 | Phase 12 audit — **done, no defect, no commit** | — |
| 1 | Spec amendment + this plan | `Spec: what a report may compute, before Phase 13` |
| 2 | `VARIANCE_ALERT_THRESHOLD` in `Settings`, `/client-config`, `.env.example` | `Phase 13 Step 2: the threshold that decides what is worth looking at` |
| 3 | `app/services/reporting.py` + its unit tests | `Phase 13 Step 3: profit per fuel, and never a partial total` |
| 4 | `GET /reports/daily/{business_date}` + D3's reconciliation check | `Phase 13 Step 4: the day, and whether its explanation still adds up` |
| 5 | `GET /reports/range` | `Phase 13 Step 5: seven days, and which of them were guessed` |
| 6 | `GET /reports/variance-alerts` | `Phase 13 Step 6: what needs a manager's eyes` |
| 7 | `ui/chart.js`, `svgEl` in `dom.js`, three screens, `main.js` wiring | `Phase 13 Step 7: a shape you can read at a glance` |
| 8 | Structural guarantees | `Phase 13 Step 8: a report that recomputes a snapshot fails the suite` |
| 9 | Docs | `Phase 13 Step 9: plan and notes docs` |

### Files touched

**New** — `app/services/reporting.py`; `app/api/v1/reports.py`;
`app/static/js/ui/chart.js`; `app/static/js/screens/reports.js` (three exported render
functions, `daily_summaries.js`'s two-exports-one-module precedent);
`tests/test_reporting_service.py`; `tests/test_reports_daily.py`;
`tests/test_reports_range.py`; `tests/test_reports_alerts.py`;
`docs/phase-13-plan.md`; `docs/phase-13-notes.md`.

**Modified** — `app/core/config.py` (threshold + validator list);
`app/api/v1/client_config.py` (expose it); `app/api/v1/router.py` (include, with the
collision comment); `app/static/js/dom.js` (`svgEl`); `app/static/js/main.js` (imports +
three `route(...)` registrations); `app/static/js/screens/cash.js` (a link into Reports);
`app/static/app.css` (chart classes); `tests/test_frontend_assets.py` (`prefixes["reports"]`);
`tests/test_cash_permissions.py` (`_ROUTERS` += reports.py — **not** `_SHIFT_SCOPED`, exactly
as `daily_summaries.py` is handled); `tests/test_client_config.py`; `.env.example`;
`CLAUDE.md`.

**No migration.** `alembic` stays at `0014` — Phase 13 adds no table and no column. This is a
checkable claim and §9.A asserts it.

### Frontend wiring — the four places a screen must appear

1. `import { renderReports, renderDailyReport, renderAlerts } from "./screens/reports.js";`
2. Three `route(...)` calls with `{ tab: "cash", role: "manager" }`. **Register
   `#/reports/alerts` before `#/reports/:businessDate`** — `router.js`'s `resolve()` iterates
   `routes` in registration order and takes the first regex match, so the parameterised
   pattern would otherwise swallow `alerts` as a business date. Same hazard as the backend's.
3. A link from `screens/cash.js` so the screen is reachable without typing a hash.
4. `tests/test_frontend_assets.py::prefixes` gains `"reports": "/reports"`, and a **real
   `api.get("/reports/…")` call** must exist — a docstring mentioning it does not satisfy the
   check, which strips comments precisely because a comment once silently satisfied it.

---

## 6. Error codes

**No new codes.** Every refusal reuses an existing one: 422 `INVALID_DATE_RANGE`
(`/expenses/summary`'s), 422 `BUSINESS_DATE_IN_FUTURE` (§6.1's), 403 `INSUFFICIENT_ROLE`,
401 `NOT_AUTHENTICATED`. `NO_PRICE_FOR_DATE` and `NO_MARGIN_FOR_DATE` are **caught** by the
reporting service and surface as field-level reasons, never as HTTP failures.

No new table means **no new `_CONSTRAINT_ERRORS` entry** — and `tests/test_errors.py`'s
schema-driven sweep proves that rather than trusting it.

---

## 7. New §14 guardrails

- **Do not recompute a snapshotted day's cash figures in a report.** Read the stored row. §5.2
  keeps `expected_closing` and its components so a report can show what the manager was told;
  recomputing destroys the only record of that, and §6.5 chains days so it would not stay local
- **Do not sum a partial profit into a total.** A fuel with no margin makes the combined figure
  unknowable, not smaller. Withhold it and name the fuels excluded (§13.7, §13.21)
- **Do not sum quantities across units of measure.** Litres and kilograms do not add (§4.5)
- **Do not divide a money value in JavaScript to size a chart bar.** The server sends a CSS
  percentage string; the client assigns it. `value / max` is arithmetic on money one language
  further out, and it is exactly the kind that looks harmless (§3 rule 1, D6)

---

## 8. Test inventory — every way this can break

Grouped by failure class. Each line is one test. **Money is asserted as
`Decimal(body[...]) == Decimal("...")`, never on a string or a float; null is asserted with
`is None`, never with a falsy check** — the house idiom from `tests/test_cash_position.py`.

### A. Snapshot vs live — the phase's central risk

1. A finalised day's report returns `source: "snapshot"` and every cash figure **byte-identical**
   to the `daily_cash_summaries` row
2. A day with a created-but-not-finalised summary is also `"snapshot"` — components are written
   at create, so the record exists
3. A day with shifts and no summary returns `source: "computed"`
4. A date with no shifts and no summary returns `source: "no_trading"`
5. **Changing a fuel price after a day is snapshotted does not move any figure in `cash`**
6. …and **does** move the live `fuel` breakdown — asserted explicitly, because that is the
   asymmetry D3 exists to surface
7. …and therefore sets `breakdown_reconciles: false` with **both** figures present
8. A snapshot day whose live breakdown agrees returns `breakdown_reconciles: true`
9. `breakdown_reconciles` is `null` (not `false`) on a `computed` day — there is nothing to
   reconcile against
10. Reversing a collection under a finalised day leaves the snapshot untouched
11. A shift reopened beneath a finalised day surfaces `requires_review` and leaves
    `expected_closing` byte-identical (§13.16)
12. **Structural:** an `ast` test asserting no function in `reports.py` that reads a snapshot
    also calls `day_totals` for that same date — the Phase 13 analogue of
    `test_the_daily_summary_is_never_recomputed_on_read`
13. **Structural:** `daily_summaries.py` is unmodified — its three read functions still contain
    neither `day_totals` nor `expected_closing=`

### B. Profit and margin

14. A CBG-only day returns a real `gross_fuel_margin` and a non-null `gross_fuel_margin_total`
15. A petrol day returns `margin_per_unit: null`, `gross_fuel_margin: null`,
    `margin_unavailable_reason: "NO_MARGIN_FOR_DATE"` — and **never `"0.00"`**
16. A mixed CBG+petrol day returns CBG's figure per line and
    `gross_fuel_margin_total: null` with `fuels_missing_margin: ["PETROL"]`
17. `fuels_missing_margin` is `[]`, not absent, when coverage is complete
18. **The endpoint does not 409** on a missing margin — asserted by status, because
    `shift_sales(price_only=False)` would
19. A fuel with a margin but **zero sales** does not appear in `fuels_missing_margin`
20. A margin entered *after* the day still values that day at the margin effective at
    `started_at`, not today's
21. Two margin revisions on either side of the shift resolve to the earlier one
22. `profit_basis` contains the words *"gross fuel margin on quantity sold"* (§13.7), asserted
    by substring
23. A margin of exactly `0.00` is a real margin — `gross_fuel_margin` is `"0.00"`, and the fuel
    is **not** listed as missing

### C. Price gaps and degradation

24. A missing price on one day in a 7-day window marks **only that day** `unavailable` with
    `unavailable_reason: "NO_PRICE_FOR_DATE"`; the other six render normally
25. A missing price on the requested day of `/reports/daily` returns **200** with
    `source: "unavailable"`, not a 409
26. An `unavailable` day contributes nothing to the window and does not render as `0`
27. A snapshotted day with a missing price still returns its **cash** block from the snapshot —
    the snapshot needs no price — while the fuel block reports the gap
28. `bar_height_pct` on an `unavailable` day is `"0.00%"`, and the row is visually distinct

### D. The §6.5 chain, live computation

29. A `computed` day with a prior counted summary opens at `actual_counted` and reports
    `opening_balance_source: "counted"`
30. A `computed` day with a prior uncounted summary opens at `expected_closing` and reports
    `"carried"`
31. A `computed` day with **no** prior summary anywhere returns `expected_closing: null` while
    its sales figures remain valid — the chain cannot be anchored, and that is not an error
32. The prior summary is the most recent one, not literally `business_date - 1` — proven with a
    gap day between
33. **§6.5's worked example, as a test:** a ₹200 shortage on Monday appears in Monday's variance
    and is absent from Tuesday's opening

### E. Date, window and timezone

34. `to` defaults to today **in `TZ_DISPLAY`**, not UTC — pinned by freezing a UTC instant that
    is a different calendar date in IST
35. `from` defaults to `to - 6 days` (seven days inclusive), asserted by counting rows
36. Both boundaries are **inclusive**
37. `from > to` → 422 `INVALID_DATE_RANGE`
38. Span exactly 31 days → 200; 32 days → 422 (boundary, strictly `>`)
39. `to` in the future → 422 `BUSINESS_DATE_IN_FUTURE`
40. `from == to` → a one-day window, 200
41. Every date in the window appears exactly once, **including dates with no shifts** — a
    seven-day window always returns seven rows
42. Rows are ordered by `business_date` and the order is asserted, not assumed
43. A malformed date (`?from=notadate`) → 422 `VALIDATION_ERROR`, not a 500
44. A shift crossing midnight is attributed to its `business_date`, never `date(created_at)`
    (§6.1) — the 24-hour-outlet case
45. Two shifts on one business date are aggregated into one row, not two

### F. Money correctness

46. The eleven cash components on a `computed` day equal `cash.day_totals` term for term
47. `expected_closing` on a `computed` day equals `cash.expected_closing(...)` exactly
48. Reversals net out in every aggregate — collections, expenses, credit, non-fuel, deposits,
    shortfalls — one test per table
49. A reversed row is **not** double-counted and **not** dropped: a ₹500 expense plus its
    reversal contributes `0`, not `500` or `-500`
50. `expenses_by_category` matches `expenses.totals_by_category_range` for the same window
51. A booked shortfall reduces `expected_closing` by exactly its amount (§6.4)
52. A card-paid non-fuel sale raises `total_sales` and leaves the cash side unchanged (§6.4's
    worked example)
53. A `bank_transfer` credit repayment does not enter the cash figure; a `cash` one does
54. A `card` expense does not reduce cash; a `cash` one does
55. **No float appears anywhere:** structural check over `reports.py` and `reporting.py` for
    `"float("`, `"sa.Float"`, `": float"` — via `test_cash_permissions.py`'s `_ROUTERS`
56. Decimal round-trips through JSON as a **string** with two places — `"1000.00"`, never
    `"1000.0"` or `1000.0`
57. A figure of exactly `0.00` serialises as `"0.00"` and is distinguishable from `null`

### G. Units (§4.5)

58. A CBG line reports `unit_of_measure: "kilogram"`; petrol reports `"litre"`
59. `quantity_by_unit` has two keys on a mixed day and never a summed third
60. **There is no `total_quantity` field** — asserted by key absence, so adding one fails
61. Quantity serialises to **three** decimal places, money to two

### H. Alerts

62. A variance of `threshold + 0.01` alerts; exactly `threshold` does not (strictly `>`)
63. A **negative** variance of the same magnitude alerts — direction does not exempt it
64. `variance: null` produces **no** `variance_exceeds_threshold` alert
65. …and a summary with `actual_counted: null` is **not** an alert at all (§6.5's normal case)
66. A date with shifts and no summary produces `day_not_reconciled`
67. A `no_trading` date produces no alert
68. `summary.requires_review` produces `summary_requires_review` carrying the `review_note`
69. An unreviewed flagged expense produces one alert with the correct `count`
70. A **reviewed** flagged expense produces none
71. A reading with `requires_review` produces `reading_requires_review` with its `shift_id`
72. An open shift on a past date alerts; an open shift on **today** does not
73. The threshold in the response equals the configured value, and changing config changes both
    the response and which days alert — proven by monkeypatching, not by reading the constant
74. Alerts are ordered deterministically, asserted
75. An empty window returns `items: []` with a 200, never a 404
76. **Nothing is written:** `count(*)` on `audit_logs` and on every alert-source table is
    unchanged across the call

### I. Permissions and tenancy

77. Attendant → 403 `INSUFFICIENT_ROLE` on all three endpoints
78. Manager → 200 on all three
79. Admin → 200 on all three
80. Unauthenticated → 401 `NOT_AUTHENTICATED`
81. A user with an **inactive** membership → 403 `MEMBERSHIP_INACTIVE`
82. A `test_role_floors` walking every route × every role in one parametrized sweep, matching
    `test_daily_summaries_api.py:498`
83. **Another outlet's data is absent** — asserted by *inserting* a second outlet's shift,
    summary and expense, not by inferring from an empty page
84. The outlet comes from `actor.outlet_id` — structural check that `DEFAULT_OUTLET_ID` and
    `get_default_outlet_id` appear nowhere in `reports.py`

### J. Reads write nothing

85. Each of the three endpoints leaves `count(*)` on `audit_logs` unchanged
86. …and creates no `daily_cash_summaries` row for a `computed` day — the live figure is
    **never persisted as a side effect**, which would silently convert a guess into a record
87. Calling `/reports/daily` twice returns identical bodies (no hidden state)
88. **Structural:** no `@router.post`, `@router.patch` or `@router.delete` in `reports.py`, so
    `test_audit_coverage.py` stays silent for the right reason — and the moment one is added it
    will demand an `audit.record` call

### K. Structural / suite-level

89. `reports.py` appears in `tests/test_frontend_assets.py::prefixes`
90. `"/reports"` appears in a real `api.get(...)` under `app/static/js/`, with comments stripped
91. Every route lives under `/api/v1/` (`test_routes.py`)
92. Every route carries `get_current_user` in its dependency tree; `_UNAUTHENTICATED_PATHS`
    remains exactly `{"/health", "/auth-config"}` (equality, not containment)
93. No `.offset(` and no `@router.delete` in `reports.py`
94. `alembic current` is still `0014`; `alembic check` reports no new operations
95. `pytest` green **twice back to back against the same database** — the leaked-row check
96. Every new JS module parses (`node --check`) and is reachable from `main.js`'s import graph
97. Every named import in the new modules resolves to a real export
98. No `innerHTML` / `outerHTML` / `insertAdjacentHTML` in `chart.js` — the module most likely
    to reach for it
99. No `parseFloat` in any new JS module
100. No external host referenced by the chart or its styles
101. `test_errors.py`'s constraint sweep still passes with no new exemption

### L. Frontend behaviour (hand-checked, §13.18)

102. `null` renders as words — "not reconciled", "not counted", "no margin entered" — and
     **never** as ₹0.00
103. A `computed` day is visually distinct from a `snapshot` day at a glance
104. `breakdown_reconciles: false` renders both figures and says which is which
105. Profit carries the §13.7 label wherever it appears
106. CBG quantities read "kg" and petrol "L"
107. The chart's bar heights come from `bar_height_pct` — verified by changing one value
     server-side and seeing only that bar move
108. Every plotted value also appears in the table beneath it
109. A wide table scrolls inside `.scroll-x`; the page body never scrolls horizontally
110. `prefers-reduced-motion`, `prefers-reduced-transparency` and `prefers-contrast: more` all
     behave — the chart uses tokens, not hardcoded colours
111. An attendant forcing `#/reports` by hash is redirected **and** would get a real 403
112. `request_id` is visible on every error
113. Loading, error and empty states all render (the `"Loading…"` string, `errorCard`, `empty`)
114. `#/reports/alerts` resolves to the alerts screen, **not** to the day screen with
     `businessDate: "alerts"` — the route-ordering hazard, checked by hand as well as by reading

### M. Performance

115. A 31-day window of fully-snapshotted days completes in one bounded pass — asserted by
     query count, not wall clock
116. A 7-day window of entirely unreconciled days returns correct figures (the expensive path)
117. A window with 3 shifts per day aggregates correctly and does not fan out per nozzle

---

## 9. Verification checklist

### A — suite and migration health
- [x] `pytest` green twice back to back against the same database
- [x] Test count recorded; `alembic` still at `0014` — **this phase adds no migration**
- [x] `alembic check` clean; `downgrade base && upgrade head` round-trips
- [x] 100% coverage on `app/services/reporting.py` and `app/api/v1/reports.py`

### B — the structural guarantees (Step 8)
- [x] Every new structural test **deliberately broken once** and confirmed to fail naming the
      right file. Break it **where the test actually looks** — Phase 12's notes record a test
      that stayed green because a comment satisfied its search
- [x] A report that recomputes a snapshotted day fails the suite
- [x] `reports.py` with no screen fails `test_every_router_is_reachable_from_a_screen`

### C — the domain rules a report must not soften
- [x] A snapshotted figure is never recomputed
- [x] A partial profit total is never presented as a total
- [x] Litres and kilograms are never added
- [x] `null` is never coalesced to zero, in Python or JavaScript
- [x] No money value is divided in JavaScript

### D — the checks no test replaces
- [ ] Compare one real week's report against the paper register
- [ ] Confirm ₹100 is the right variance threshold with the owner
- [ ] Confirm the six alert kinds are the ones the owner actually wants to be told about

---

## 10. What actually shipped

**1,403 tests, up from 1,302.** 101 new across four files. **100% coverage on
`app/services/reporting.py` and `app/api/v1/reports.py`.** `alembic` still at `0014` —
this phase added **no migration**, as predicted. Suite green twice back to back against the
same database; `alembic check` reports no new operations.

| Step | Commit |
|---|---|
| 0 | *(no commit — the Phase 12 audit found no defect)* |
| 1 | `Spec: what a report may compute, before Phase 13` |
| 2 | `Phase 13 Step 2: the threshold that decides what is worth looking at` |
| 3 | `Phase 13 Step 3: profit per fuel, and never a partial total` |
| 4–7 | `Phase 13 Steps 4-7: the endpoints, and the screens that reach them` |
| 8 | `Phase 13 Step 8: a report that recomputes a snapshot fails the suite` |
| 9 | `Phase 13 Step 9: plan and notes docs` |

**Where it differs from the plan above.**

- **Steps 4–7 landed as one commit, and a test decided that.** The plan had four. The moment
  `reports.py` existed, `tests/test_frontend_assets.py` failed with *"new router(s) with no
  entry in this test's prefix map"* — exactly what Phase 12 built it to do. A router cannot
  land green without a screen, so the phase's real unit of work is endpoint-plus-screen. The
  structural test dictated the commit granularity, which is the best outcome one can have.
- **One test file, not three.** `tests/test_reports_api.py` covers all three endpoints; they
  share fixtures and splitting them would have meant three copies of `_window` and the two
  fuel fixtures.
- **`dom.js` gained `svgEl`**, which the plan anticipated, but not the reason it could not be
  a flag on `el()`: `document.createElement("rect")` does not fail — it silently returns an
  HTML unknown element that lays out as nothing. Nor that `className` on an SVG element is a
  read-only `SVGAnimatedString`, so it must be set as an attribute.
- **A zero margin turned out to be unreachable.** `ck_fuel_margins_margin_positive` refuses
  it, so the planned "a ₹0.00 margin is a real margin" test could not be written from data.
  It became a test asserting the *constraint*, so anyone relaxing it lands on the reasoning.
- **The fourth occurrence of the comment-satisfies-its-own-test trap**, found by deliberately
  breaking the new structural test. See §3 of the notes.
- **No new error codes**, as predicted. No `_CONSTRAINT_ERRORS` entry, no `conftest.py`
  change — both predictions held, and the second was verified by running the suite twice
  rather than assumed.

---

## 11. Not in Phase 13

Charts beyond the bar/variance pair (no pie, no multi-series, no zoom); CSV/PDF export;
scheduled or emailed reports (§12's no-notifications rule); cross-outlet reporting (§12,
§13.6); stock or tank reconciliation (§12); the IOCL ledger and bank position (§12 — one
post-V1 module); per-transaction drill-down (§12); a stored alert with an acknowledge workflow
(D7, §13.23); any report that writes.

---

## 12. Still owed by the owner

**New:**
- Is ₹100 the right variance alert threshold? It is a guess, live on real money from day one
- Are the six alert kinds in D7 the right list — anything missing, anything noise?

**Carried forward, all still open, all still load-bearing:**
- **Petrol and diesel dealer margins have never been entered.** Phase 13 makes this visible in
  the product for the first time, via `fuels_missing_margin` — but it does not fix it
- The first opening balance must be seeded before any day can be finalised
- No way to write off a shortfall (§13.15)
- Audit trail retention (§13.17)
- CBG's real max flow rate in kg/min; whether testing quantities are recorded on paper and in
  what unit; `EXPENSE_RECEIPT_THRESHOLD` = ₹5,000 is still a guess; the real expense-category
  and credit-customer lists
- Does a salesman hold a change float overnight? Is a surplus ever booked?
