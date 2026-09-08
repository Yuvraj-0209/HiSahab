# Phase 17 — Notes

> The learning reference, not a changelog. The plan lived in the conversation rather than in
> a `docs/phase-17-plan.md`; this is what the building of it taught.

---

## 1. The third phase caused by a user, and the first where the *first fix* was wrong too

Phase 15 came from *"I did that, and I can't see the day."* Phase 16 from *"before I feed any
of these values, I need the ledger to say what they actually owed."* This one from a
screenshot and *"this cash position part now shows 60k cash surplus which is exactly the
amount spent thru cash."*

What makes it different is that the phase was **built twice**. The first version added
`expenses.paid_from` so each expense could name which pile paid it. It was complete —
migration, model, service, API, form, seven tests, all green — and it was reverted, because
the owner proposed something better.

## 2. The first diagnosis was wrong, and the user had already falsified it

Before reading any code I said the app was double-subtracting expenses, once in
`accountable_cash` and once in `expected_closing`. It fit the symptom exactly: the surplus
equalled the expense total.

It was wrong — `DayTotals.cash_sales` excludes expenses and `expected_closing` subtracts them
once. Four minutes of reading killed it.

**The user had already told me so.** He said the expected closing "matches perfectly", which
under a double-count it could not have. The falsifying observation was in his message before
I formed the hypothesis.

The lesson isn't "read the code first" — I'd have claimed to believe that already. It is that
**a hypothesis explaining the number exactly is the most dangerous kind.** ₹60,169 matching
the expense total was evidence of *some* relationship; I read it as evidence for the specific
one I'd guessed.

## 3. The code stated its own false assumption, in English

`shift_cash_position`'s docstring said:

> Cash repayments and settlements are added; cash expenses are subtracted, because all three
> physically pass through the salesman's hands during the shift…

Not a description of arithmetic — a **claim about the physical world**, and the entire bug is
that it stopped being true when the office started paying bills from the locker. It wasn't
wrong when written.

This project's habit of writing *why* made a fifteen-minute diagnosis possible. It also
suggests a practice: **an assumption stated in prose is a testable claim about reality, and
deserves re-checking whenever reality reports a surprise.**

## 4. The better design removed the question instead of answering it

`paid_from` answered *which pile paid this bill?* — correctly, per expense. Its own §13 entry
had to concede that **nothing could check the answer**, so a large bill left on the default
reproduced the exact phantom the column existed to remove.

The owner's reformulation:

> whether it gets subtracted before entering the locker or after entering the locker it's one
> and the same thing, the total sum would remain the same

Cash goes into the locker; expenses come out. `accountable_cash` stops subtracting them
entirely. **A rule with nothing to fill in cannot be filled in wrongly** — and it deleted one
line of logic instead of adding a column, a migration, an enum, a form control and a
`CASCADE`-shaped inheritance rule on reversals.

Worth noticing *when* the better design arrived: after the complicated one was built. Having
the full version in front of him is plausibly what made the simplification visible. The cost
of the wrong path was about an hour, and it was recoverable because nothing had been pushed.

## 5. I asked the same question three times after it had been answered

The owner stated his model, I asked; he restated it with reasoning, I asked again with
reworded options; he restated it a third time and interrupted the tool call.

One of those option sets was **internally contradictory** — it offered "keep gap at zero"
alongside "revert the column", which together restore exactly the original bug. That is the
tell: I was generating options mechanically rather than thinking about whether they could
coexist.

The correct move after the second statement was to say the consequence once in prose and act.
Saved to memory as `take-the-simpler-model-when-offered`.

## 6. Reverting well is a design decision

`git revert` over `reset`, so the abandoned direction stays legible. `paid_from` was a
reasonable answer to the right question, and a future reader hitting the same problem should
be able to find both it and the reason it lost. A `reset --hard` would have left this file
asserting a history nobody could check.

The revert also made the second attempt *smaller* rather than larger: the tree returned
byte-identical to `origin/main`, so the real change is one deleted term, reviewable in a
single diff hunk.

## 7. The guard is the test that proves nothing moved

`test_expected_closing_still_subtracts_every_cash_expense` matters more than the 30 July
regression. The day equation was correct before this phase, §6.5 chains days, and a leak into
it would have silently moved every opening balance afterwards.

The regression test proves the fix; the guard proves the blast radius. **A change to one of
two similar equations needs a test on the one you did not change.**

## 8. A pre-existing bug found by fixing an adjacent one

While rewriting the screen's term breakdown I noticed `card_upi_credit_repayments` — §6.4's
twelfth term, added in Phase 16 — was never rendered. The listed lines did not sum to the
figure printed above them, and nobody had noticed because nobody adds up a column of numbers
on a screen to check.

Fixed in passing. The general shape: **rewriting a display is when its arithmetic gets
audited**, because rendering each term forces you to enumerate them against the source.

## 9. What this leaves for next time

**§13.33 is a real cost, not a solved problem.** A salesman who pays a bill from his own hand
now shows a gap that size. It is true and explained by the expense rows — but if that gap
starts being read as noise rather than as "check the expense list", the model needs revisiting.
That is the failure mode §5.2 names for any flag nobody acts on.

**Railway still has no `alembic upgrade head` in its start command.** This phase needed no
migration so it did not bite, but the next schema change will deploy against an unmigrated
database. Raised and deliberately left alone rather than bundled into an unrelated change.

> **Closed in Phase 18** — `.railway/railway.ts` declares a pre-deploy command running
> `scripts/migrate.sh`, so the database is migrated between build and deploy and a failed
> migration stops the deployment. See `docs/phase-18-notes.md`.

**Railway did not auto-deploy the push, and that is now a known fact rather than a
suspicion.** The service is configured to build `main` from GitHub, the push landed
(`git ls-remote` confirmed the SHA), and four minutes later `get-status` still reported
yesterday's deployment. `railway up --ci` built and shipped it in about two minutes.

Two things worth carrying forward. `redeploy` was the wrong tool and reading its description
saved a wasted step: it **reuses the existing build**, so it would have re-shipped the old
commit while looking like a fix. And **verify a deploy by asking the app, not the dashboard** —
`get-status` said SUCCESS while my first three asset checks came back empty, which turned out
to be my own wrong URL (`/static/js/...`; the mount is at `/`). A green status plus a failing
fetch is ambiguous, and only the fetch against the right path resolves it.

> **Diagnosed in Phase 18.** The service's `source` block reads perfectly healthy
> (`{repo, branch: "main"}`) while nothing deploys, which is the trap: Railway keeps that
> block even when the GitHub App has lost access to the repository. See
> `docs/phase-18-notes.md`.

**Three user-caused phases in a row.** The pattern each time: a figure the system was
confident about, that somebody with the physical facts could see was impossible. Worth asking
the owner what *else* looks wrong, rather than waiting for the next screenshot.
