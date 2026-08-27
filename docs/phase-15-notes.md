# Phase 15 — Notes

> The learning reference, not a changelog. `docs/phase-15-plan.md` is what was decided and
> why; this is what the building of it taught.

---

## 1. The first phase caused by a user rather than by the document

Every phase up to fourteen came out of `CLAUDE.md`: a section described something, and the
work was to build it. This one came out of somebody using the application and saying *"I did
that, and I can't see the day."*

That difference shows in what the investigation found. **Nothing was broken in the sense the
test suite understands.** 1499 tests passed. Every endpoint behaved exactly as specified.
`GET /daily-summaries` returned precisely the rows it should. `_resolve_window` resolved a
window that was correct by its own docstring. The cash position wrote nothing, as §8 requires.

The defect was in the *joins between* correct parts — which is the half no unit test looks at,
and the half §13.18 already admits is checked by a person. It took a person eight months of
project time to arrive.

## 2. A missing row read as a missing day

This is the one worth carrying forward, because it generalises past this screen.

`GET /daily-summaries` reads one table. Anything absent from that table is absent from the
screen. For a list of *records* that is not merely acceptable, it is the definition. But a
manager is not looking for a list of summary records — they are looking for **a list of days**,
and the day they most need is precisely the one with no record yet.

The endpoint was right. The screen asked it the wrong question.

The fix needed no endpoint, because both halves of the answer were already exposed:
`GET /shifts` says which dates traded, `GET /daily-summaries` says which were reconciled, and
the join is one `Map`. Reaching for a new endpoint would have been the expensive way to
discover that the data was never the problem.

## 3. Writing a guardrail does not stop you breaking it

§14 gained *"do not label a read-only screen with a verb that implies a write"*, committed
before any code, with the reasoning spelled out over five lines.

Three commits later, `days.js` shipped a draft with **two** buttons breaking it: "Lock the
shift" and "Finalise this day", both on handlers whose entire action was `navigate(...)`. The
act happens one screen further on. They were caught by re-reading the file, not by the rule
existing.

Which is the same lesson §11 records about the audit gap surviving seven phases —
*"nothing failed when it was missing"* — arriving in a place where **no structural test is
possible**. There is no AST walk that can tell a verb from a noun. This one stays a matter of
reading, and the honest thing is to say so rather than pretend the §14 entry closed it.

## 4. The window default had a carefully-defended half and an unexamined half

`_resolve_window`'s docstring spent a paragraph on why `to` is *today at the outlet* and not
today in UTC — at 23:00 IST the UTC date is still yesterday, so a UTC default would drop the
current trading day from every evening's report. That reasoning is correct and is untouched.

What it never asked was whether `to` should be **today at all**. §4.7 has said since Phase 4
that the whole day is typed in after the fact, in one sitting, often long afterwards. The two
facts sat in the same repository for three phases without meeting.

A carefully-argued detail is not evidence that the thing it is a detail *of* was ever
examined. The prose was doing what good prose does — making the reader confident — and the
confidence was about the wrong half of the sentence.

## 5. The out-of-order reconcile was found by building the fix

`POST /daily-summaries` has never refused an out-of-order date. `previous_summary` finds the
most recent summary before the date, so reconciling the 4th before the 3rd chains the 4th's
opening from the 2nd — skipping a day's cash, permanently, since §5.2 stores
`expected_closing` precisely so nothing can rewrite it later.

**Nobody had ever hit it, and the reason is uncomfortable**: unreconciled days were invisible,
so nobody could reconcile the wrong one. The bug was hidden by the defect being fixed.

That is worth naming as a shape. A worklist that surfaces several pending items at once is
exactly the change that makes an ordering hazard reachable, and the two belong in the same
commit — a UI that offers a list of things to do is an implicit claim that doing them in any
order is safe.

The client mirrors the server rule (oldest-first, one button) rather than relying on it.
§8's *"hiding a button is UX, not a control"* cuts both ways: the control is the 409, and the
sorting is the politeness that means nobody meets it.

## 6. `?? null` is fine; `?? 0` is the one that kills

§14 is emphatic that `?? 0` on a money value is the most dangerous two characters this project
can write in a client. The merge needed a default for a day with no summary at all:

```js
format(day.summary?.expected_closing ?? null, { absent: "—" })
```

`?? null` is not a softer version of the forbidden thing — it is the opposite of it. It says
*there is no figure*, and `format`'s `absent` renders an em dash. `?? 0` would have said the
day expected nothing in the locker, which for an unreconciled day of ₹362,195 in sales is a
plausible-looking lie of exactly the kind this document opens by warning about.

## 7. What is still only checked by a person

Unchanged from §13.18, and worth restating because this phase moved four screens' worth of
markup between files:

- that the lifecycle strip is legible at a glance rather than five grey dots
- that the worklist reads as a queue and not as a second copy of the day list
- that a manager, seeing "Not reconciled" for the first time, knows what to press

`test_every_named_import_resolves_to_a_real_export` is what made the move safe — it is the
only automated thing standing between a four-file reorganisation and a blank screen. Everything
above it is somebody's eyes.
