# Phase 12 — Frontend: Plan, Decisions, and Verification

> Matches the `phase-5/6/7/8/9/10/11-plan.md` pattern, and follows them in being written
> **before** the code rather than finalised at implementation time. §8 records what actually
> shipped where it differs from this document, and the checklist marks in §7 are filled in as
> they are met.

---

## Context

Eleven phases have built ~10,000 lines of API across 24 routers and 1,213 tests, and **not one
line of it has ever been used by a human being.** Every rule in CLAUDE.md — §4.7's
confirm-don't-assume chain, §6.11's receipt threshold, §6.4's shortfall term — has been
verified against a test client and never against a salesman with a phone in a forecourt.

Phase 12 is the slot where that changes. §11 describes it in six words: *"Frontend — minimal
HTML/CSS/JS forms and tables."* That is the scope. What this plan adds is not more scope but
more **craft**: the same forms and tables, built so they feel like a native application rather
than an intranet page. The reason is not vanity. §4.7 says the whole day is typed in after the
fact, in one sitting, by somebody who would rather be doing something else, and §6.8 warns that
a form which fights its user teaches that user to type figures that balance. An interface that
is fast, physical and honest is a *control*, not a decoration.

The requirement is therefore two things at once, and the second is the harder one:

| | |
|---|---|
| **Coverage** | Every router gets a screen — all 24, including the admin reference data and the audit log. Nothing is left as "curl it". |
| **Craft** | Apple's fluid-interface vocabulary: interruptible springs, 1:1 direct manipulation, velocity handoff, momentum projection, rubber-banded boundaries, translucent material and depth. |

**And it must be done with zero dependencies.** §14: *"Add a frontend framework, bundler, or
npm dependency"* is a **Do not**. So Motion, Framer Motion, Vaul and every spring library are
out. The physics is hand-written — see D3. That constraint turns out to be affordable: the
whole motion system is under 200 lines, because Apple's model needs exactly three functions.

### The state this phase inherits

Phase 11 shipped 1,213 tests, `alembic` at `0014`, 100% coverage on every touched router.
**It has not been re-verified in this session** — Step 0 below is that verification, and it is
the first thing the phase does.

### What already exists for the frontend, and is better than expected

The API was not written blind to this phase. Three things are already in place and should be
used rather than re-derived:

- **`GET /api/v1/me`** (`app/api/v1/me.py`) exists *specifically* for this phase; its docstring
  says so. It returns `{id, full_name, phone, outlet_id, role}` and is the correct app-boot
  call — a valid token with no membership 403s here, which distinguishes "signed in" from
  "provisioned and authorised".
- **`GET /api/v1/shifts/{id}/readings`** returns a `Worksheet` with every opening pre-filled
  from the chain and a `requires_anchor` flag per line. Its docstring: *"This route is what
  makes 'zero typing on a normal day' true."* The hardest screen is already served by one call.
- **`GET /api/v1/shifts/{id}/cash-position`** returns every term of §6.4 plus `gap`,
  `declared_cash` and an `incomplete` flag, and writes nothing. The reconciliation screen is
  one GET.

### The one contradiction in the spec, flagged loudly per §14

§12 lists **"Role-based UI theming, dark mode, i18n"** as out of scope, and this plan proposes
a dark interface. These are not the same thing, and the distinction is the whole of D2:

> All three items in that line are **per-user configurability** features — a theme that varies
> by role, a toggle between two themes, a language switch. Shipping **one** visual identity
> that happens to be dark adds no toggle, no second palette, no persistence and no setting. It
> is the app's look, not a mode.

If the owner reads that line as forbidding a dark interface outright, D2 flips to the light
"document-like" palette and **nothing else in this plan changes** — the material, motion and
layout system is palette-independent by construction.

---

## 1. Phase 11 audit — Step 0

Runs before any code, per the standing rule. The user's opening question in this session —
*"confirm whether Phase 11 was completed properly, and test it"* — is exactly this step, and it
is answered by doing it rather than by asserting it.

- [ ] `docker compose up -d db`, then `pytest` — expect **1,213 passed**
- [ ] `pytest` a **second time against the same database** — the leaked-row check; expect 1,213
- [ ] `alembic upgrade head` → `0014`; `alembic check` → no new operations
- [ ] `alembic downgrade base && alembic upgrade head` → clean round-trip
- [ ] `pytest --cov=app` → confirm the claimed 100%
- [ ] Read `tests/test_audit_coverage.py` and **deliberately break it**: neuter one
      `audit.record` call and confirm it fails naming that endpoint. A structural test never
      run red is a test that passes for unknown reasons
- [ ] Verify the twelve Phase 11 retrofits by reading each `@router.post` / `@router.patch` in
      the seven reference-data routers, not by trusting the AST test that was written with them
- [ ] Confirm `GET /audit-logs` is admin-only and outlet-scoped, and that a row from another
      outlet is genuinely absent

**If a defect is found it is fixed test-first** (§10, no exceptions) and becomes Step 0's
commit. If none is found there is no commit, as in Phase 11.

---

## 2. Spec amendment (Step 1, its own commit)

`Spec: what a frontend needs from the API, before Phase 12`

| § | Amendment |
|---|---|
| §2 | Add the frontend layout contract: `app/static/`, ES modules, no build step. Restate the "no HTML strings in Python" rule as still holding — the mount serves files, it does not render |
| §11 | Expand the Phase 12 line from six words to the screen inventory and the two new config endpoints |
| §12 | Clarify the "dark mode" exclusion as **per-user theming**, not a single dark palette (D2). If the owner disagrees, this amendment records the light palette instead |
| §13.18 (new) | **The frontend has no automated behavioural tests.** Structural and serving tests exist in Python; the JS logic itself is verified by hand. No npm means no Jest/Vitest, and adding one would breach §14. Recorded as a decision, not discovered later as a gap |
| §13.19 (new) | **The access token lives in `sessionStorage`.** Not an httpOnly cookie, because the API is a bearer-token API and Supabase issues the token to the browser. XSS on this origin can read it; the mitigations are a strict CSP, no third-party script, and no `innerHTML` on server data |
| §14 | Five new guardrails — see §5 below |
| §16 | Add `SUPABASE_ANON_KEY` (public by design; it is meant to ship in a browser — distinct from `SUPABASE_SERVICE_KEY`, which must never leave the server) |

---

## 3. Decisions

### D1 — One `StaticFiles` mount, added last, and it cannot shadow the API

`app/main.py` gains, **after** `include_router(api_router)`:

```python
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")
```

Three properties make this safe, and each is worth stating because each looks like a risk:

1. **It cannot shadow `/api/v1`.** Starlette matches routes in registration order; the API
   router, `/docs` and `/openapi.json` are all registered first.
2. **It cannot break `tests/test_routes.py`.** That module enumerates `APIRoute` objects and
   `app.openapi()["paths"]`. A mount is a `Mount`, appears in neither, and so is invisible to
   *"every route lives under /api/v1"* and to the authentication sweep. Verified by reading
   `_iter_api_routes` — this is the single largest structural risk in the phase and it is
   already handled.
3. **The path is resolved from `__file__`, never the cwd**, so `uvicorn` started from any
   directory serves the same files.

`html=True` serves `index.html` at `/`. Combined with D5's hash routing, **no SPA fallback
rewrite is needed at all** — there is only ever one document path.

**Rejected:** a top-level `web/` directory. `app/static/` ships inside the package that
`pyproject.toml` already installs, so deployment has one artefact rather than two that can
drift. It needs one `[tool.setuptools.package-data]` line, which is not a build step.

### D2 — One dark palette, no toggle — which is why it is not §12's "dark mode"

Argued in Context above. Concretely: tokens are defined once on `:root` with no
`prefers-color-scheme` fork and no `[data-theme]` override, because there is only one theme to
choose. What the palette **does** honour, because these are accessibility signals rather than
preferences:

- `prefers-reduced-motion: reduce` → springs collapse to short opacity cross-fades, all
  overshoot removed, gesture tracking retained (reduced motion means *gentler*, not *dead*).
- `prefers-reduced-transparency: reduce` → materials go near-solid, `backdrop-filter` dropped.
- `prefers-contrast: more` → solid surfaces with defined borders.

### D3 — The spring is 60 lines of vanilla JS, and it is the whole motion system

§14 forbids npm, so Apple's damping/response model is implemented directly. The mapping from
Apple's two designer parameters to a physical spring is exact:

```
stiffness k = (2π / response)²
damping   c = 4π · dampingRatio / response
```

integrated per `requestAnimationFrame` tick with semi-implicit Euler. The API is deliberately
tiny, and every property that makes motion feel alive falls out of it:

| Function | Why it exists |
|---|---|
| `spring(get, set, {damping, response})` | Holds live `value` **and `velocity`**. `.to(target)` retargets **without restarting** — the interruptibility that §3 of the design skill calls the single most important principle |
| `project(velocity, d = 0.998)` | `(v/1000)·d/(1−d)` — Apple's exponential-decay projection, not the physics-textbook `v²/2a`. Decides *where a flick was going* before choosing a snap target |
| `rubberband(overshoot, dimension, c = 0.55)` | Progressive resistance at a boundary. A hard stop reads as frozen |

House values, applied consistently: **damping `1.0` / response `0.35`** for everything that
was not thrown; **damping `0.8` / response `0.3`** only for sheets and cards released from a
real gesture. Bounce is earned by momentum, never decorative.

**Every animation reads the live presentation value on interrupt**, never the logical target.
Gesture-driven motion uses no CSS transitions and no `@keyframes` — those cannot be grabbed
mid-flight. Only `transform` and `opacity` are animated, so everything stays on the compositor.

### D4 — Two new endpoints, because the alternative is hardcoding config in JavaScript

This is the one place the phase touches the API, and it needs the owner's assent.

**`GET /api/v1/auth-config`** — *unauthenticated*, returns `{supabase_url, supabase_anon_key}`.
The login screen cannot authenticate without these, so they cannot sit behind auth. Both are
public by design: the anon key is built to ship in browsers. This **extends
`tests/test_routes.py::_UNAUTHENTICATED_PATHS`**, which that test's own docstring says should be
*"a deliberate, reviewed act"* — so the exemption lands with its reason written next to it.

**`GET /api/v1/client-config`** — *attendant floor*, returns `tz_display`,
`expense_review_threshold`, `expense_receipt_threshold`, `max_upload_bytes`, `outlet_name`.

The second one earns its place on a rule the spec already states. §6.7 and §6.11:
*"Changing it must not require a deploy."* If the UI hardcodes ₹5,000 to warn before an upload
is demanded, then raising the threshold silently desynchronises the warning from the rule, and
a config value that "must not require a deploy" now requires one. §3 rule 4's *"display
conversion to Asia/Kolkata happens in the frontend only"* has the same shape — the zone is
`TZ_DISPLAY`, server config, and copying it into a JS constant creates a second source of truth.

Neither endpoint returns `SUPABASE_SERVICE_KEY`, `SUPABASE_JWT_SECRET` or `DATABASE_URL`. A
test asserts that by name, because that is the kind of thing a later edit adds by accident.

### D5 — Hash routing, one document, no framework

`#/today`, `#/shifts/{id}/readings`, `#/admin/prices`. Roughly 60 lines: parse `location.hash`,
match against a table of patterns, mount a screen module, unmount the old one.

**Why one document rather than 18 HTML files.** The access token lives in memory (D9), so a
full page load on every navigation would re-read `sessionStorage` and re-fetch `/me` each time —
and, more to the point, would make every transition a white flash. Fluid motion between screens
is impossible across document loads.

**Why hash and not History API.** A hash never reaches the server, so deep-linking
`/shifts/abc/readings` needs no SPA rewrite rule and cannot be caught by the `StaticFiles`
mount. One less moving part, and D1 stays a one-liner.

ES modules (`<script type="module">`) load natively in every browser this targets. **No
bundler, no transpiler, no `package.json`.**

### D6 — One `api.js`, and the Idempotency-Key is minted per *submission*, not per *fetch*

The single choke point for every call. It owns:

- `Authorization: Bearer <token>` on every request
- **401 handling by code, not by status.** `TOKEN_EXPIRED` → refresh silently and retry **once**;
  `INVALID_TOKEN` / `NOT_AUTHENTICATED` → route to login. `app/core/security.py` says the two
  codes exist precisely so *"the frontend can refresh silently rather than bouncing the user to
  a login screen"* — reading the status alone throws that away
- **The error envelope's two shapes.** `detail` is a **string** for business errors and an
  **array of field objects** when `code === "VALIDATION_ERROR"`. Branch on the code, never on
  `typeof`. Field-level errors are rendered against the field they name in `loc`
- `request_id` surfaced in every error toast — §9 puts it in the envelope so a user can quote it
- The 26 mapped `_CONSTRAINT_ERRORS` codes get human sentences; anything unmapped shows the
  server's `detail` verbatim rather than a generic apology

**Which calls carry a key, because it is not "every POST".** Required on every money-creating
`POST` and on **every `/reversals` route**: collections, expenses, credit sales, credit
repayments, non-fuel sales, bank deposits, shortfalls, shortfall settlements. **Not** carried by
`POST /shifts`, `POST /shifts/{id}/readings` (naturally idempotent — `UNIQUE (shift_id,
nozzle_id)` returns 409 `READING_ALREADY_EXISTS` and the client `PATCH`es instead),
`POST /uploads/receipt` (M8), `POST /daily-summaries`, or any reference-data `POST`. Sending one
where it is not expected is harmless; omitting one where it is required is **400
`IDEMPOTENCY_KEY_REQUIRED`**, so the table lives in `api.js` next to the route, not in a
developer's memory.

**The idempotency subtlety, which is the whole reason §6.10 exists.** The key is minted with
`crypto.randomUUID()` when a form is **first submitted** and held on that form instance until it
succeeds. Every retry — automatic on network failure, or manual from the toast — reuses it.
Minting per `fetch()` call would make retries create duplicate money rows, which is the exact
failure §6.10 was built to prevent, reintroduced in the client. The three replay outcomes are
distinct in the UI: replayed (silent success), `REQUEST_IN_PROGRESS` 409 (spinner, retry after a
beat), `IDEMPOTENCY_KEY_REUSED` 422 (a client bug — surfaced loudly, never retried).

### D7 — Money is a string from the wire to the pixel, and JavaScript never does arithmetic on it

§3 rule 1 says `Decimal` end to end. JS has no decimal type and `0.1 + 0.2 !== 0.3` there too.
So the rule for this phase is stronger than "use a library":

> **The frontend performs no arithmetic on money, ever.** Every figure it displays — totals,
> gaps, outstanding balances, variances — is computed server-side and rendered as received.

This is already true of the API: `cash-position` returns `gap`, `credit-customers/outstanding`
returns the balance, `collections` returns `totals_by_mode`. There is nothing left to add up.
`money.js` formats a string for display (grouping, ₹ prefix) **without** parsing it to a Number,
and it is the only module permitted to touch a money value. `parseFloat` appears nowhere.

One trap this closes, from `collections.py`'s own docstring: `declared_cash: null` and `"0.00"`
are **different answers** and must not be coalesced — §6.8's "zero as an answer, never zero as an
omission". `null` renders as "not declared", `"0.00"` renders as ₹0.00.

### D8 — Timestamps arrive in UTC and are converted once, at the edge

§3 rule 4. `time.js` wraps `Intl.DateTimeFormat` with the zone from D4's `client-config`, and is
the only place a UTC instant becomes text. `business_date` is a **DATE and never a timestamp** —
it is rendered as-is with no zone conversion, because converting it is precisely the §6.1 bug
that column exists to prevent.

### D9 — Supabase password grant by `fetch`, no SDK

Per the owner's choice. `auth.js` POSTs to
`{SUPABASE_URL}/auth/v1/token?grant_type=password` with the anon key, receives
`{access_token, refresh_token, expires_in}`, and refreshes via `grant_type=refresh_token` on a
`TOKEN_EXPIRED`. Access token held **in memory**; refresh token in `sessionStorage` (§13.19
records the trade-off). Sign-out clears both. This is three `fetch` calls — supabase-js would be
an npm dependency for no gain.

### D10 — The reading worksheet is the phase's centrepiece, and it is where design meets §4.7

§4.7 is the longest argument in CLAUDE.md and its conclusion is a UI requirement:
*"pre-filled, not typeable, and confirmed against the physical meter"* — because an assumed
opening *"converts theft into a debt owed by someone who did nothing wrong."*

So the worksheet line is built as:

- The chained opening rendered **large and non-editable**, as a statement the meter should agree
  with, not as a filled-in field.
- `opening_confirmed` is a **required boolean with no default** in `ReadingCreate`, and the UI
  must not pre-satisfy it. Confirmation is an explicit physical act — a press that springs and
  commits on release, with feedback on pointer-*down* per §1 of the design vocabulary.
- **"Doesn't match"** reveals `opening_reading` + `opening_variance_reason`, and the row changes
  material state to show it will be flagged. The abnormal day becomes *more* visible, not less.
- `requires_anchor` lines are visually distinct and admin-only; a non-admin sees why, not a
  dead control.
- `testing_quantity` is presented as a question requiring an answer, never a pre-filled `0`
  (§4.2 — the omission it guards against is invisible and permanent).

### D11 — Every create form is a sheet, and every sheet is grabbable

Bottom sheets with `setPointerCapture`, 1:1 tracking that respects the grab offset, velocity
history over the last few `pointermove` events, `project()` at release to decide dismiss-vs-
settle, and `rubberband()` at the top edge. Dismissal is decided by **velocity sign**, not by
position — a fast flick down dismisses from anywhere.

§7 spatial consistency: a sheet leaves along the path it entered, and popovers take
`transform-origin` from the control that opened them. Sheets **materialise** — blur radius and
scale animate together — rather than fading, so the surface reads as glass arriving.

Chrome (top bar, tab bar) is translucent with content scrolling underneath, with a scroll-edge
gradient mask instead of a 1px divider. Never a translucent surface stacked on another.

### D12 — Role gates rendering; the server gates everything

`/me` gives the role, and the tab bar and controls are filtered by it. §8: *"Hiding a button is
UX, not a control."* So every screen still handles a 403 as a real outcome, and the phase adds
**no** client-side permission logic that is not mirrored server-side. Tabs by role:

| Role | Tabs |
|---|---|
| attendant | Today · Entry |
| manager | Today · Entry · Cash |
| admin | Today · Entry · Cash · Admin |

### D13 — Seven places this API will bite a naive client

Found by reading all 24 routers end to end. Each is a real inconsistency in the existing
surface, none is worth a breaking change now, and every one produces a *plausible wrong number*
rather than an error — which is the failure mode CLAUDE.md's opening paragraph names. They are
written here so the implementation handles them deliberately rather than discovering them one
support call at a time.

| # | Trap | How the client handles it |
|---|---|---|
| 1 | **Two incompatible cursor encodings.** `/shifts` keys on `(business_date, sequence)`; every other list keys on `(timestamp, uuid)` | Cursors are opaque and **owned by the list that issued them**. `list.js` stores the cursor next to its endpoint and never shares one |
| 2 | **`null` ≠ `0.00`** on `declared_cash`, `gap`, `variance`, `credit_limit`, `actual_counted` | Never `?? 0`, never `\|\| 0`. `null` renders as "not declared" / "no limit" / "not counted". D7's rule, and the single most dangerous line of JavaScript this phase could write |
| 3 | **Explicit-`null` PATCH semantics vary by router.** `null` is *ignored* everywhere except `credit_customers` (`credit_limit`, `vehicle_numbers` **clear**) and `credit_sales` (`quantity`, `vehicle_number` **clear**) | Forms build their PATCH body by **omitting** untouched fields, never by sending `null`. Clearing is an explicit user act on the two routers that support it |
| 4 | **Date/time typing is inconsistent between responses.** `business_date` is a `date` on `ShiftResponse` but an ISO **string** on `ShiftSales` and `FlaggedExpenseResponse`; `reviewed_at` is a string on `ExpenseResponse` and a `datetime` on `ReadingResponse` | `time.js` accepts both and normalises. It never assumes the wire type |
| 5 | **Every timestamp input must carry a UTC offset** (`effective_from`, `started_at`, `ended_at`, `at`) — a naive value is 422 | `<input type="datetime-local">` yields a **naive** string. A helper converts local wall-clock → offset-carrying ISO before send. This would otherwise 422 on the very first price entry |
| 6 | **`POST /uploads/receipt` is the only multipart route**, and the only one taking `shift_id` in the **body** | One `upload()` path in `api.js` that sets no `Content-Type` (the browser must set the boundary) and no `Idempotency-Key` (M8) |
| 7 | **`GET /expenses/summary` uses literal `from` / `to`** query names, and echoes them | Quoted keys; not renamed to `start`/`end` |

Two asymmetries that look like bugs and are not, so the UI must not "fix" them:

- **Booking a shortfall takes no `salesman_id`** (read from `shifts.attendant_id`, §14) but
  **recording a settlement requires one** — a settlement can pay down any salesman's balance.
- **`GET /credit-customers` is the lean, attendant-safe projection for *every* role** — no
  phone, no limit, no balance. The manager-only detail is a *different* route. The picker in a
  credit-sale form uses the lean list even when an admin is looking at it.

Also worth stating because it changes a screen: **`POST /shifts/{id}/shortfalls` returns 409
`NO_CASH_DECLARED`** when nobody has declared cash. There is no gap to book against a `null`, so
the booking control is disabled with that reason shown, not offered and then refused.

### Mechanical decisions

| # | Decision | Reason |
|---|---|---|
| M1 | `app/static/` holds all assets; one `package-data` line in `pyproject.toml` | One deployable artefact (D1) |
| M2 | ES modules, relative imports, no bundler | §14; browsers do this natively |
| M3 | No `innerHTML` on any server-derived value; DOM built with `textContent` / `createElement` | The token is readable by script (§13.19), so injected markup is the live risk |
| M4 | A strict `Content-Security-Policy` meta: `default-src 'self'`, no inline script | Enforces "no CDN" structurally, not by discipline |
| M5 | System font stack (`-apple-system, system-ui`), no webfont | §14's no-dependency rule, and the system face already ships optical sizing |
| M6 | Size-specific tracking: `-0.02em` on display text, `0` on body, positive on captions | One `letter-spacing` value is wrong somewhere |
| M7 | Spacing in `rem`, layout scales with the user's text size | Dynamic Type equivalent |
| M8 | Uploads use `FormData`; no `Idempotency-Key` on `POST /uploads/receipt` | §6.10's closing note — an attachment is not a money record |
| M9 | Lists paginate with the `{items, next_cursor}` envelope and an infinite-scroll sentinel | §9; never `OFFSET` |
| M10 | `truncated: true` on capped lists renders a visible notice | Silently showing a partial list is how a reader trusts a wrong total |
| M11 | Optional: a `skipif(shutil.which("node") is None)` test running the three pure motion functions | Tests the spring math where a runtime exists, adds no dependency where it does not |

---

## 4. Build order

| Step | What | Commit message |
|---|---|---|
| 0 | Phase 11 audit; fix anything found, test-first | `Phase 12 Step 0: <defect>` *(only if one is found)* |
| 1 | Spec amendment + this plan | `Spec: what a frontend needs from the API, before Phase 12` |
| 2 | `SUPABASE_ANON_KEY`, `auth-config`, `client-config`, the `_UNAUTHENTICATED_PATHS` exemption | `Phase 12 Step 2: the two things a browser needs before it can ask anything` |
| 3 | The `StaticFiles` mount, `index.html`, CSP, design tokens, type scale | `Phase 12 Step 3: the app is served, and the API is not shadowed` |
| 4 | `motion/spring.js`, `motion/gesture.js` — springs, projection, rubber-banding | `Phase 12 Step 4: interruptible springs without a dependency` |
| 5 | `ui/` kit — sheet, toast, field, list, translucent nav | `Phase 12 Step 5: material and depth -- the component kit` |
| 6 | `api.js`, `auth.js`, `money.js`, `time.js`; login; `/me`; role-gated shell | `Phase 12 Step 6: sign in, and the shell knows who you are` |
| 7 | Today — shift spine, open/close/lock/reopen | `Phase 12 Step 7: the shift spine` |
| 8 | The reading worksheet + §4.7's confirm interaction + sales | `Phase 12 Step 8: confirm the meter, do not assume it` |
| 9 | Collections, expenses, receipt upload, non-fuel sales | `Phase 12 Step 9: the money that moves through a shift` |
| 10 | Credit — customers, sales, repayments, ledgers | `Phase 12 Step 10: udhaar` |
| 11 | Cash — position, shortfalls, deposits, daily summary | `Phase 12 Step 11: reconciling the locker` |
| 12 | Admin reference data + the audit log viewer | `Phase 12 Step 12: the knobs, and the record of who turned them` |
| 13 | The structural guarantees (§7.B) | `Phase 12 Step 13: a CDN script or a float in the money path fails the suite` |
| 14 | Docs | `Phase 12 Step 14: plan and notes docs` |

Tests are distributed across every step, never a separate pass. Step 13 is a *guarantee*, not a
test pass — the same role Phase 11's Step 7 played.

### The screen inventory — "full coverage" made checkable

Nineteen screens across four tabs. The right-hand column is what makes coverage a fact rather
than a claim: **every router in `app/api/v1/` appears exactly once.**

| Tab | Screen | Routers reached |
|---|---|---|
| — | Login | `auth-config` (+ Supabase directly) |
| Today | Shift spine — current shift, status, lifecycle | `shifts`, `shift_templates`, `me`, `client-config` |
| Today | Sales summary for the shift | `readings` (`/sales`) |
| Entry | **Reading worksheet** (D10) | `readings` |
| Entry | Collections | `collections` |
| Entry | Expenses (+ upload, review) | `expenses`, `uploads`, `attachments` |
| Entry | Non-fuel sales | `non_fuel_sales` |
| Entry | Credit sale | `credit_sales`, `credit_customers` (lean), `uploads` |
| Entry | Credit repayment | `credit_repayments` |
| Cash | **Cash position** — §6.4 term by term | `cash_position` |
| Cash | Shortfalls: book, settle, ledger | `shortfalls` |
| Cash | Bank deposits | `bank_deposits` |
| Cash | Daily summary — create, count, finalise | `daily_summaries` |
| Cash | Flagged expenses queue | `expenses` (`/flagged`) |
| Cash | Month-end expense summary | `expenses` (`/summary`) |
| Cash | Customer outstanding + ledger | `credit_customers` |
| Admin | Fuel types & nozzles | `fuel_types`, `nozzles` |
| Admin | Prices & margins (+ current rates) | `fuel_prices`, `fuel_margins` |
| Admin | Categories, customers, shift templates | `expense_categories`, `credit_customers`, `shift_templates` |
| Admin | Audit log viewer (filters + paging) | `audit_logs` |

`health` is reached by the boot check and has no screen. A structural test asserts the mapping:
every module in `app/api/v1/` is named by at least one screen module, discovered by **directory
listing, never a hardcoded list** — the same construction `tests/test_audit_coverage.py` uses,
so a Phase 13 router that ships with no screen fails the suite rather than the review.

### Files touched

**New** — `app/static/index.html`; `app/static/app.css`; `app/static/js/` with `main.js`,
`api.js`, `auth.js`, `money.js`, `time.js`, `motion/spring.js`, `motion/gesture.js`,
`ui/{sheet,toast,field,list,nav}.js`, and one module per screen under `js/screens/`;
`app/api/v1/client_config.py`; `tests/test_client_config.py`; `tests/test_static_mount.py`;
`tests/test_frontend_assets.py`; `docs/phase-12-plan.md`; `docs/phase-12-notes.md`.

**Modified** — `app/main.py` (the mount); `app/api/v1/router.py`; `app/core/config.py`
(`SUPABASE_ANON_KEY`); `pyproject.toml` (package-data); `tests/test_routes.py`
(`_UNAUTHENTICATED_PATHS` + reason); `.env.example`; `CLAUDE.md`.

### The `conftest.py` question

**Expected: no changes.** This phase adds no table, no money row and no fixture-worthy entity.
The two new endpoints are reads served by existing fixtures (`client`, `auth_headers`,
`make_user`). This will be *verified by running the suite twice*, not assumed — Phase 11 made
the same prediction and was right, and Phase 7 made it and was wrong about `collections`.

---

## 5. Error codes introduced

**None on the server**, and that is worth stating rather than leaving as an absence. The two new
endpoints are reads that either succeed or fail through the existing auth codes. Every other
code this phase touches already exists; the frontend's job is to **render** them, and the ones
that need a human sentence rather than the raw `detail` are: `SHIFT_LOCKED`, `SHIFT_NOT_OPEN`,
`NOT_YOUR_SHIFT`, `TOKEN_EXPIRED`, `PROFILE_NOT_PROVISIONED`, `CREDIT_LIMIT_EXCEEDED`,
`EXPENSE_REQUIRES_RECEIPT`, `UNREVIEWED_EXPENSES_EXIST`, `MISSING_NOZZLE_READINGS`,
`MISSING_COLLECTIONS`, `TOTALIZER_DECREASED`, `TESTING_EXCEEDS_THROUGHPUT`,
`ANCHOR_REQUIRES_ADMIN`, `OPENING_BALANCE_REQUIRES_ADMIN`, `IDEMPOTENCY_KEY_REUSED`.

**New §14 guardrails:**

- Do not perform arithmetic on a money value in JavaScript — render the server's figure (D7)
- Do not mint a fresh `Idempotency-Key` on a retry; the key belongs to the submission (D6)
- Do not pre-confirm a chained opening reading, or default `testing_quantity` to 0 in the UI (D10)
- Do not treat a hidden control as a permission check (D12, §8)
- Do not add a `<script src>` to any external host — the CSP forbids it and so does §14 (M4)

---

## 6. Not in Phase 12

Reporting — the daily summary view, the 7-day rolling window and variance alerts (Phase 13);
any offline write queue or service worker (§12's real-time exclusion, and a queue that holds
money rows on a phone needs its own design); OCR of receipts (§12); an outlet switcher (§12,
§13.6); i18n and per-user theming (§12, and D2 explains why one dark palette is not that); a
native or installable app (§12 — the API must permit one, V1 does not build one); push
notifications (§12); JS unit tests beyond M11's optional pure-function check (§13.18); charts of
any kind — Phase 13 decides what is worth plotting before anything plots it.

---

## 7. Verification checklist

### A — suite and migration health
- [ ] `pytest` green twice back to back against the same database, single process
- [ ] Test count recorded; `alembic` still at `0014` — **this phase adds no migration**
- [ ] `alembic check` clean; `downgrade base && upgrade head` round-trips
- [ ] 100% coverage on `app/api/v1/client_config.py` and the changed lines of `app/main.py`

### B — the structural guarantees (Step 13)
- [ ] `GET /api/v1/health` and one authenticated API route still return JSON **with the mount
      installed** — the shadowing test, and the reason D1 exists
- [ ] `GET /` returns `index.html`; an unknown path under the mount 404s rather than 200-ing the shell
- [ ] `tests/test_routes.py` passes with `/auth-config` added to the exemption list and **fails**
      if a second path is added without one
- [ ] No asset references an external host: no `src=`/`href=` with `http`, no `@import` off-origin
- [ ] `parseFloat` and `Number(` appear in no money path; the rule's prose lives in the **Python
      test docstring**, not in a JS comment, so the check cannot trip over its own documentation
      (the mistake Phase 10 and Phase 11 both record)
- [ ] `client-config` cannot leak: asserted by **name** against `SUPABASE_SERVICE_KEY`,
      `SUPABASE_JWT_SECRET`, `DATABASE_URL`
- [ ] Every JS module is reachable from `index.html`'s import graph — no orphans
- [ ] **Every router in `app/api/v1/` is named by a screen module**, discovered by directory
      listing; a new router with no screen fails the suite
- [ ] Each new structural test **deliberately broken once** and confirmed to fail naming the
      right file

### C — the domain rules the UI must not soften
- [ ] The worksheet pre-fills every opening and requires zero typing on a normal day (§4.7)
- [ ] `opening_confirmed` cannot be satisfied without an explicit act; no default (D10)
- [ ] A mismatched opening demands a reason and shows the row will be reviewed
- [ ] `testing_quantity` is an answer, never a silent 0 (§4.2)
- [ ] `declared_cash: null` renders as "not declared" and `"0.00"` as ₹0.00 — never coalesced
- [ ] A retried submission creates **one** row — verified by pulling the network cable mid-POST
- [ ] Profit is labelled *gross fuel margin on quantity sold* wherever shown (§13.7)
- [ ] Every quantity displays the fuel's own `unit_of_measure`; CBG reads kg (§4.5)
- [ ] A shortfall screen never offers to book automatically (§14), and the control is disabled
      with a reason when nothing is declared rather than refused with 409 `NO_CASH_DECLARED`
- [ ] Booking sends no `salesman_id`; a settlement sends one (D13)
- [ ] The credit-sale customer picker uses the lean list for every role, including admin (D13)

### D — the seven traps (D13), each as an explicit check
- [ ] A `/shifts` cursor is never sent to another list endpoint
- [ ] No `?? 0` or `|| 0` anywhere near a money field; `null` renders as words, not ₹0.00
- [ ] A PATCH omits untouched fields rather than sending `null` — verified against
      `credit_customers`, where `null` genuinely clears
- [ ] A `datetime-local` input is converted to an offset-carrying ISO string before send;
      entering a fuel price succeeds on the first attempt rather than 422-ing on `NAIVE_TIMESTAMP`
- [ ] The upload sets no `Content-Type` header and no `Idempotency-Key`
- [ ] `expenses/summary` sends literal `from` / `to`
- [ ] Both wire shapes of `business_date` and `reviewed_at` render identically

### E — motion and material
- [ ] Every sheet can be grabbed mid-animation and reversed without a jump (interruptibility)
- [ ] A flick lands where `project()` says, not at the nearest edge to the release point
- [ ] Drag tracks 1:1 and respects the grab offset
- [ ] Boundaries rubber-band; nothing hard-stops
- [ ] Feedback appears on pointer-**down**
- [ ] `prefers-reduced-motion` cross-fades and keeps tracking; `prefers-reduced-transparency`
      solidifies; `prefers-contrast: more` renders borders
- [ ] Reviewed frame-by-frame at reduced speed, per the design skill's process note

### F — permissions and wayfinding
- [ ] An attendant sees two tabs and gets a real 403 if they force an admin route by hash
- [ ] Every screen answers: where am I, where can I go, how do I get out
- [ ] `request_id` is visible on every error

### G — the checks no test replaces
- [ ] Type one real day from the paper register, on a phone, timed against doing it on paper
- [ ] Have a salesman confirm an opening reading against a physical meter and disagree on purpose
- [ ] Confirm with the owner that the dark palette is wanted (D2) and the two new endpoints are
      acceptable (D4)
- [ ] Reconcile that day's figures against the register before real money touches it

---

## 8. What actually shipped

*Filled in at the end of the phase.*

---

## 9. Still owed by the owner

**New:**
- Is one dark palette acceptable given §12's wording (D2)?
- Are `auth-config` and `client-config` acceptable additions (D4)?
- A Supabase project with real user accounts — every attendant needs one, and
  `app/jobs/provision_user.py` must be run for each or `/me` 403s with `PROFILE_NOT_PROVISIONED`

**Carried forward, all still open, all still load-bearing:**
- Petrol and diesel dealer margins have never been entered — profit reporting covers CBG only,
  and Phase 13 is built on it
- The first opening balance must be seeded before any day can be finalised
- No way to write off a shortfall (§13.15)
- Audit trail retention (§13.17)
- CBG's real max flow rate in kg/min; whether testing quantities are recorded on paper, in what
  unit; `EXPENSE_RECEIPT_THRESHOLD` = ₹5,000 is still a guess; the real expense-category and
  credit-customer lists
- Does a salesman hold a change float overnight? Is a surplus ever booked?
