# Phase 12 — Frontend: Notes

> The narrative counterpart to `phase-12-plan.md`. The plan says what was built; this says
> **why**, and records what went wrong on the way. Written to be read six months from now by
> somebody trying to understand a decision, not as a changelog.

---

## 1. Eleven phases of correctness had never met a person

Phase 11 ended with 1,213 tests, 100% coverage on every touched router, and an application no
human being had ever used. Every rule in this document — §4.7's confirm-don't-assume chain,
§6.11's receipt threshold, §6.4's shortfall term — had been verified against `httpx.AsyncClient`
and never against a salesman with a phone.

That gap is the whole of Phase 12, and it is worth being precise about what kind of gap it is.
It is not that the code was untested. It is that a test client never mistypes a meter reading,
never has one bar of signal, and never gives up on a form and writes the number that makes the
total balance. §6.8 already knew this — *"blocking on it teaches staff to type figures that
balance"* — which means the interface is a **control**, not a presentation layer over one.

That reframing decided most of the design. The reading worksheet does not pre-tick
`opening_confirmed` because §4.7 says an assumed opening converts theft into a debt owed by
somebody who did nothing wrong. The close button does no client-side arithmetic because §14
says checking it there teaches the wrong lesson. The screens are opinionated in exactly the
places the document is opinionated, and nowhere else.

## 2. The bug that took the whole application down, and why nothing caught it

`ui/sheet.js` and `ui/toast.js` both imported `project` from `motion/gesture.js`. It is
exported by `motion/spring.js`.

In ES modules that is a **link-time** error. The browser refuses the entire module graph, so
`main.js` never executes and the page is blank white. No console line about the login screen,
no failed request, nothing that names the problem.

Every test passed. That is the part worth keeping:

- `node --check` parses **one file at a time** and cannot know what another module exports.
- The mount served all fifteen files with a 200 and the correct content type.
- The import-graph orphan test checked that the target **file** existed — never that the
  names came out of it.

The owner found it by opening the page. §13.18 admits the frontend has no behavioural tests,
and this is precisely the shape of failure that admission covers: invisible in Python, total
in a browser. So the fix was not the two-line correction; it was
`test_every_named_import_resolves_to_a_real_export`, written first, failing by name on both
files, per §10.

*Lesson: "the file exists" and "the file exports what you asked for" are different questions,
and only the second one is the one that matters.*

## 3. Three times a text search lied, and the third was the worst

This codebase already records two occasions where a structural test written as a grep tripped
over the comment documenting the rule it checked. Phase 10: *"left as a text search, it would
have taught the next person to delete the comment."*

Phase 12 hit it a third time, in a form neither earlier case had.
`test_every_router_is_reachable_from_a_screen` searched raw source for each router's URL
prefix. I deliberately broke the audit-log screen to prove the test could fail — and it
**passed**. That module's own docstring says *"Phase 11 built `GET /audit-logs`"*, and the
prose describing the endpoint satisfied the search for it.

The earlier cases were comments *breaking* a test, which is annoying and gets noticed. This
was a comment **silently satisfying** one, which is the version that never gets noticed at
all — the test would have sat there green forever, guarding nothing.

Comments are stripped before every scan in `tests/test_frontend_assets.py` now, and the rules
themselves are stated in the **Python docstrings** rather than in the JavaScript being
scanned.

*The transferable lesson: when you deliberately break a structural test to check it fails,
break it in the place the test is actually looking. I broke the API call and the comment kept
it green.*

## 4. Supabase changed the ground under §8, and it took a Phase 2 change inside Phase 12

The owner's project would not accept a login, and the reason was not a misconfiguration.
Supabase has moved new projects from a shared HS256 secret to asymmetric **JWT signing keys**
— ES256 over P-256. `app/core/security.py` verified HS256 only, and `pyproject.toml` carried
a comment that had become false: *"Supabase signs access tokens with HS256, which is
symmetric — so no cryptography extra is needed."*

Two things about how this was handled are worth recording.

**It was confirmed, not inferred.** The project's JWKS listed one ES256 key; a throwaway user
was created, signed in, and its token's header read `{"alg":"ES256","kid":...}` matching that
key. Then the user was deleted. Guessing here would have meant either a wasted dependency or a
wasted afternoon.

**It is an improvement, and saying so matters more than filing it as a workaround.** Under
HS256 this server held a secret that could **mint** a token for any user. Under ES256 it holds
a public key: it can verify a signature and cannot forge one. A leak of this application's
environment no longer grants the ability to impersonate anybody. The dependency (§14 says ask
first, and the owner was asked) buys a strictly better security posture, not just compatibility.

HS256 was deliberately **not** dropped. `tests/conftest.py` mints HS256 tokens, and that is
what keeps 1,300 tests offline and free of a live Supabase project. All 1,279 existing tests
stayed green through the change precisely because both schemes are supported.

The dispatch reads the algorithm from the token's own header, which is attacker-controlled, so
that is the part that needed care rather than the ES256 verification itself. Both classic
attacks are tested against real forged tokens — and the confusion token had to be built by
hand with `hmac`, because `jwt.encode` refuses to sign with a PEM key. Testing through PyJWT
would have proven only that PyJWT declines to help.

## 5. Springs, and the one assertion I got wrong

§14 forbids npm, so Apple's damping/response model is implemented directly — about 200 lines
across `spring.js` and `gesture.js`. The mapping to a physical spring is exact and the
integrator is unremarkable. Three details are not:

- **Substepped at 1/240s.** A backgrounded tab delivers one enormous frame, and a stiff spring
  integrated in a single large step does not merely look wrong — it goes numerically unstable
  and the value runs to infinity, putting a sheet somewhere off-screen with no way back.
- **Settle thresholds relative to distance travelled**, so a spring animating a 900px sheet and
  one animating a 0..1 opacity both stop when imperceptibly close.
- **Velocity sampled over a 60ms window**, not from the last two events. A single inter-frame
  delta on a real touchscreen is noisy enough that a steady drag reports near zero, and the
  flick then dies at the release point.

Node turned out to be installed, so the pure functions are genuinely tested — 16 assertions,
no `package.json`, skipped where node is absent so the suite still passes without a JavaScript
toolchain.

**One of those assertions was wrong on its first run**, and I loosened nothing to make it pass.
It bounded per-frame velocity *change* during a reversal, on the theory that a large jump would
read as a "brick wall". But `k·displacement` alone reaches ~32,000 px/s² at these parameters —
about 537 px/s of velocity change per 60fps frame. The test was measuring the spring's real
acceleration and calling it a defect. It was rewritten to assert the two things that would
actually look wrong on screen: velocity crosses zero exactly once, and position never teleports.

Continuity at the moment of interruption is proven separately and exactly, by asserting that
`to()` leaves value and velocity untouched.

## 6. What the frontend cost, honestly

The owner asked why V1 has no framework. The rule predates this phase (§2, §12, §14), but it is
worth recording what it actually bought and cost, because a future reader will ask again.

**Bought:** no build step. No `node_modules`, no bundler config, nothing that stops working
because a Node version moved. The browser loads the files the server sent. For a single-outlet
app maintained by one person learning backend, that is a real form of durability.

**Cost, and it is not nothing:**

- **No reactive state.** Every mutation calls `renderX()` again, which refetches and rebuilds
  the entire screen. On four nozzles nobody notices; on a 500-row list it would be sluggish.
- **`readings.js` is ~600 lines**, a good part of it DOM construction a framework would have
  made declarative.
- **No component tests** (§13.18) — which is exactly what let §2's import bug reach a browser.

The API does not care either way. §2 requires that a mobile app be able to do everything this
page can against identical endpoints, and nothing in Phase 12 weakened that.

## 7. Two things the structural tests found that review had not

Both were written to guard the future and caught something in the present on their first run.

**`test_every_router_is_reachable_from_a_screen`** found that nothing called
`GET /attachments/{id}/url`. Receipts could be uploaded and never looked at again — an expense
displayed "receipt attached" and offered no way to check it, which makes the receipt control
theatre from the reviewer's side. That is a feature gap, not a test failure, and no amount of
reading the code had surfaced it.

**The import-resolution test** is described in §2 above.

The pattern is the one Phase 11's notes name: *"the gap survived seven phases precisely because
nothing failed when it was missing."* A screen that was never built is that shape exactly — the
API works, every test passes, and the feature does not exist for anybody using the application.

## 8. The mount's one sharp edge, found by the suite rather than by reasoning

`StaticFiles` at `/` is a catch-all, so anything registered *after* `create_app` returns is
unreachable. That broke all 21 tests in `test_permissions.py` and both in
`test_shift_permissions.py` — every one of which builds an app and then attaches a `/_test/...`
route to exercise a role dependency in isolation.

`create_app` gained an explicit `serve_ui` flag rather than anything clever with
`app.router.default`, so the constraint is visible in the signature (§2: boring and explicit
over clever). Both directions are pinned: a late route is shadowed with the mount, reachable
without it.

Worth noting what did *not* break: `tests/test_routes.py`. A `Mount` is not an `APIRoute`, so it
appears in neither `app.openapi()["paths"]` nor the dependency walk — which was predicted in the
plan and held.

## 9. Verification

```
pytest                                 ->  1302 passed  (1213 before Phase 12)
pytest, second run, same database      ->  1302 passed
alembic current                        ->  0014 (head) -- this phase adds NO migration
alembic check                          ->  no new upgrade operations
100% coverage on app/main.py, app/core/security.py, app/core/jwks.py,
    app/api/v1/client_config.py
node tests/motion_assertions.mjs       ->  16 assertions passed
34 JavaScript modules, ~10,000 lines of frontend including CSS
```

Structural assertions no value test makes:

- Every JS module parses (`node --check`); a syntax error is a blank page and a green suite
- Every **named import resolves to a real export** — the §2 bug, pinned
- Every module is reachable from `main.js`'s import graph; no orphans
- **Every router in `app/api/v1/` is called by a screen**, discovered by directory listing,
  with `health` exempt and its reason beside it
- No import from off-origin; no `innerHTML`; no `parseFloat` in the money path
- The API still answers **with the mount installed** — the assertion that tells the two
  registration orders apart
- `client-config` never returns `SUPABASE_SERVICE_KEY`, `SUPABASE_JWT_SECRET` or
  `DATABASE_URL`, asserted by name

Each new structural test was **deliberately broken once** and confirmed to fail naming the
right file. That is how §3's comment problem was found.

Verified against the owner's live Supabase project, not only against fixtures: a real ES256
token reaches `GET /api/v1/me`, and all nine admin endpoints answer 200.

## 10. Open items, carried forward

**New:**

- **§7's checklist blocks C through G are still unticked**, and they are the ones that need a
  person: type a real day from the paper register on a phone and time it against paper; have a
  salesman confirm an opening reading against a physical meter and disagree on purpose; review
  the motion frame-by-frame. None of this is automatable, and §13.18 says so.
- **The password I generated for the owner's account should be changed.** It was created
  because the project had no user at all.
- **`SUPABASE_SERVICE_KEY` is now populated** in the owner's `.env`. Correct for Phase 8's
  storage, and gitignored — but it is the key that must never reach a browser, and a test now
  asserts by name that no endpoint returns it.

**Carried forward, all still open, all still load-bearing:**

- **Petrol and diesel dealer margins have never been entered.** Profit reporting covers CBG
  only, and Phase 13 is built on it. The admin pricing screen now states this positively
  rather than leaving it as an absence.
- The first opening balance must be seeded before any day can be finalised
- No way to write off a shortfall (§13.15) — now stated on the ledger screen itself
- Audit trail retention (§13.17)
- CBG's real max flow rate in kg/min; whether testing quantities are recorded on paper and in
  what unit; `EXPENSE_RECEIPT_THRESHOLD` = ₹5,000 is still a guess; the real expense-category
  and credit-customer lists
- Does a salesman hold a change float overnight? Is a surplus ever booked? The cash position
  screen currently says V1 has no record type for one.

## 11. The check no test replaces

Reconcile one real day against the paper register, on the phone it will actually be used on,
before real money touches this. Phase 10's notes said it and it has not stopped being true:
*"The arithmetic is tested to the paisa and the coverage is 100%, and neither fact says the
numbers mean what the owner thinks they mean."*

Phase 12 adds one of its own. Every screen in this phase renders figures a person will act on —
a gap with somebody's name attached, a limit that decides whether a sale is refused. The suite
proves the client displays what the server sent. It cannot prove the layout makes the right
number the obvious one, and that is the failure mode an interface has that an API does not.
