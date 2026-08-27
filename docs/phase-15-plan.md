# Phase 15 — The Cash tab: Plan, Decisions, and Test Inventory

> Follows the `phase-5` … `phase-14-plan.md` pattern: written **before** the code, with §11
> recording what actually shipped where it differs.
>
> Shorter than its predecessors, and deliberately so. This phase adds **no table, no
> migration and no endpoint**. One business rule, two backend changes and a screen
> reorganisation — everything else it touches, it deletes or renames.

---

## Context

Phase 12 built nineteen screens. Phase 13 added three more. **This is the first phase written
after somebody entered a real trading day through them**, and what it fixes is not a broken
endpoint but a tab that could not be used to do the thing it is named after.

The report was concrete: a day was entered end to end, closed, locked — and then could not be
found anywhere on the Cash tab. Checked against the dev database, nothing was lost:

```
shifts:                2026-07-01 seq 1  locked
                       2026-07-02 seq 1  locked
daily_cash_summaries:  2026-07-01 only          ← no row for 07-02
```

Three defects had stacked, and only one of them is a bug in the ordinary sense.

### (a) A read-only report was labelled with a write verb

`cash.js` rendered a **primary** button reading *"Reconcile this shift"*. Its entire action:

```js
on: { click: () => navigate(`#/shifts/${shift.id}/cash-position`) },
```

`GET /shifts/{id}/cash-position` is a report. §8 puts it at the manager floor precisely
*because* it is one, and `tests/test_cash_position.py` asserts it writes nothing. So doing
exactly what the button said produced no `daily_cash_summaries` row — and §5.2's entire
argument for storing `expected_closing` depends on that row existing.

The button was also rendered only while a shift was open, so once 2 July was locked the
affordance disappeared and there was no path back to it from the tab at all.

### (b) A day that traded and was never reconciled was structurally invisible

```python
select(DailyCashSummary)
    .where(DailyCashSummary.outlet_id == actor.outlet_id)
    .order_by(DailyCashSummary.business_date.desc())
```

One table, no join to `shifts`, no date spine. That is **correct for what it is** — and it
means the only day list on the tab could not show the one day needing attention. Nothing
anywhere said 2 July was waiting.

### (c) Reports were anchored to the calendar rather than to the trading

`_resolve_window` defaulted `to` to `outlet_today()`. Today was 27 August; the data was
1–2 July. Both reporting screens opened onto seven days of `no_trading` — **including the
`day_not_reconciled` alert, which exists for exactly this case and is windowed (§13.23)**.

§4.7 says the whole day is typed in *after the fact*. Anchoring the window on the calendar is
anchoring it on the wrong thing.

### And one date had two screens

`#/daily-summaries/{date}` showed the stored snapshot plus the count and finalise controls.
`#/reports/{date}` showed provenance, fuel, expenses and shifts. Same business date, two
tables, no link between them. That is the main reason the tab read as a menu of overlapping
reports rather than a place to do work.

---

## Decisions

### D1 — The worklist is a client-side merge, not a new endpoint

`GET /shifts` and `GET /daily-summaries` both exist, both are manager-floor, and both return
`business_date`. Which rows belong to which day is a **presentation** question, so it is
answered in the client (`days.js::mergeDays`).

*Rejected:* a `GET /days` endpoint. It would be a third read of two tables already exposed,
and §5.0's schema discipline has nothing to say about it — it is not a tenancy or correctness
concern, only a convenience one, and the convenience is one `Map`.

### D2 — Oldest first, and the button on one day only

§6.5's opening balance chains from `previous_summary` — *the most recent summary before this
date*, not literally yesterday. That looseness is correct (an outlet that was shut has no row
to chain from) and it means reconciling the 4th before the 3rd chains the 4th's opening from
the **2nd**, skipping a whole day's cash.

Nothing repairs that afterwards. §5.2 stores `expected_closing` so a later write *cannot*
rewrite it; §13.16 flags rather than moves. The wrong figure would be permanent, would
propagate into every later day, and would look entirely plausible.

**Both halves ship.** The server refuses with 409 `EARLIER_DAY_NOT_RECONCILED`; the client
sorts oldest-first and offers the button on one day, so nobody is refused to find out.

Deliberately **not** `PRIOR_DAY_NOT_RECONCILED`, which governs *finalising*. A caller who
cannot tell which of the two they hit cannot tell which step to go and do.

### D3 — The report window follows the trading day

Default `to` becomes `latest_shift(...).business_date`, falling back to `outlet_today()`,
clamped to `<= today`. §13.30.

For a live outlet this changes nothing, because the latest trading day *is* today. The
`min(..., today)` clamp is belt and braces — §6.1 already refuses a future `business_date` —
and it is there so the `BUSINESS_DATE_IN_FUTURE` guard stays a statement about what the
**caller** asked for, and can never fire on a default the function chose itself.

*Rejected:* a from/to picker instead. It leaves every visit starting on an empty week, which
is the thing being fixed.

### D4 — One day, one screen, and §13.20 survives it

`renderDay` is built on the old `renderDailyReport`, because that screen already handled both
a stored snapshot and a live computation. Grafted on: the review banner, the count control,
the finalise controls, and the stored component list.

**The stored component list renders only when `source === "snapshot"`.** On a computed day
there is nothing stored to show, and rendering live figures under the heading *"As it stood on
the day"* would claim a permanence they do not have.

### D5 — Five dots, and `actual_counted` is not one of them

```
Entered → Closed → Locked → Reconciled → Finalised
```

Two of those are per *shift* and two are per *day*, which is the whole reason the sequence
confuses somebody meeting it. Drawing it beats documenting it, because the strip is in front
of the reader at the moment they wonder.

`actual_counted` is deliberately excluded. §6.5 is explicit that under the locker model most
days are never counted, so making it a step would mark every normal day incomplete and teach
a reader to ignore the strip.

---

## Test inventory

Backend, written first per §10:

| Test | Rule |
|---|---|
| `test_an_outlet_that_has_never_traded_gets_the_seven_days_ending_today` | §13.30 fallback; §6.1's outlet-vs-UTC rule, unchanged |
| `test_the_default_window_ends_on_the_most_recent_trading_day` | §13.30 |
| `test_the_alerts_window_follows_the_same_anchor` | the case that prompted it — `day_not_reconciled` was hidden by the old default |
| `test_a_day_cannot_be_reconciled_while_an_earlier_traded_day_is_not` | §6.5; asserts the detail names the day, and that **no row is written** |
| `test_reconciling_in_order_succeeds` | the guard does not block the normal path |
| `test_a_date_the_outlet_was_shut_is_not_an_obstacle` | "traded" means *has a shift*, per §13.20's `no_trading` |

Frontend, structural only (§13.18 — there is no behavioural runner, and adding one means npm):

- `test_every_named_import_resolves_to_a_real_export` — the real guard on a four-file move
- `test_every_router_is_reachable_from_a_screen` — `/daily-summaries` and `/reports` still called
- `test_no_money_value_is_parsed_into_a_float`, `test_no_module_builds_markup_from_a_string`
- `test_every_reporting_endpoint_is_reached_by_a_screen` — **widened**, see §11 below

---

## §11 — What shipped, where it differs

**One test changed rather than the code.** `test_every_reporting_endpoint_is_reached_by_the_reports_screen`
asserted its three paths against the literal filename `reports.js`. The per-path assertion is
what guards something; the filename was an assumption it had no opinion about. It now scans
`app/static/js/screens/`, **with comments stripped first** — `test_frontend_assets.py` learned
that prose describing an endpoint silently satisfies a raw text search, and that is the version
of the failure nobody notices.

**Two buttons written *in this phase* broke §14's new guardrail.** The worklist's "Lock the
shift" and "Finalise this day" actions only navigate — the act happens one screen further on.
They now read "Open the shift to lock it" and "Open the day to finalise it". Writing the rule
into §14 three commits earlier did not stop it; noticing it while re-reading did.

**The pill kind moved onto the state object.** Computed at two call sites as
`state.reached >= 4 ? "review" : "neutral"`, it painted a perfectly reconciled day in the
colour that is supposed to mean *look at this*.

**A double-tap guard on reconcile.** §6.10 correctly gives `POST /daily-summaries` no
idempotency key — the unique constraint makes it naturally idempotent and a retry gets 409.
That is the right server answer and a confusing thing to read directly after a success toast,
so the button disables itself in flight. Nothing about correctness rests on it.
