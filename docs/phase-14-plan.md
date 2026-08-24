# Phase 14 — Users: Plan, Decisions, and Test Inventory

> Follows the `phase-5` … `phase-13-plan.md` pattern: written **before** the code, with §10's
> checklist filled in as it is met and §11 recording what actually shipped where it differs.

---

## Context

Thirteen phases have built 25 routers and a working frontend. §8's permission table has said
*"Manage users, nozzles, customers — admin only"* since Phase 2. **Nozzles and customers both
got routers. Users never did.**

The only way to add a manager or an attendant today is:

1. Create the person by hand in the Supabase dashboard (Authentication → Users).
2. Copy their UUID.
3. `python -m app.jobs.provision_user --user-id … --full-name … --role …` from a shell on the
   server.

`provision_user.py` is candid about why it is a command and not an endpoint — *"§8 makes user
management admin-only, which creates a chicken-and-egg: the very first admin has no admin to
create them"* — and equally candid about what the UUID paste costs: *"A mismatch produces a
user who authenticates successfully and is then refused with PROFILE_NOT_PROVISIONED
forever."* That bootstrap argument is correct and **the command stays**. What it does not
justify is every *subsequent* user going through the same three-system dance.

### The two consequences already visible in the product

This is not only an admin-convenience gap. Two things are broken in the app today **because
nothing can turn a `user_id` into a person**:

| Where | What it does now | Why |
|---|---|---|
| `app/static/js/screens/today.js:150` | renders the literal string **`"another attendant"`** | there is no endpoint that resolves `shift.attendant_id` to a name |
| `app/static/js/screens/today.js:364` `openShiftSheet` | offers **only** `business_date` | `POST /shifts` accepts `attendant_id` (`shifts.py:105,231`) and a manager may open a shift in someone else's name — but the UI has no picker, so through the app it is impossible |

`app/api/v1/shortfalls.py:792` already solves half of this privately, with an inline
`{row.id: row.full_name}` lookup over `UserProfile` for the shortfall ledger. That is the third
place that wants a roster and the first that built one — for itself.

### Why this phase, and why now

Yuvraj is about to hand-test V1 with a month of July data, specifically including *"how the
screen varies for users in hierarchy"*. That test needs three logins — attendant, manager,
admin — and creating them must not require a shell. This phase is the last thing standing
between the finished V1 and that test.

**It is deliberately not the bank/IOCL module.** §12 scopes bank balances, the IOCL virtual
account and PAD-settlement reporting as **one post-V1 module built together**, after the
hand-test and the deployment. Nothing here touches that.

### What already exists and must be reused, not re-derived

| Need | Existing | Location |
|---|---|---|
| Role hierarchy | `Role`, `satisfies()` — a `>=` rank compare, never equality | `app/core/roles.py` |
| Role floor + outlet | `require_role(minimum, outlet=get_default_outlet_id)` → `Actor` | `app/api/deps.py:~200` |
| Bootstrap create | `provision_user.py`'s flush-before-membership ordering | `app/jobs/provision_user.py:98-103` |
| Supabase REST client shape | `StorageBackend` Protocol + `SupabaseStorage` + `LocalStorage` + `@lru_cache` + `build_storage(settings)` | `app/services/storage.py` |
| Admin CRUD router shape | pre-check → `db.add` → `flush` → `audit.record` → `commit` → `refresh` → `logger.info` | `app/api/v1/credit_customers.py:~380` |
| Two-model role split | `CreditCustomerListItem` (lean) vs `CreditCustomerResponse` (full) — *"a filter is a line of code someone can delete without any test noticing; a type that simply has no `phone` field cannot leak one"* | `app/api/v1/credit_customers.py` |
| Immutable-field mechanism | omit from the Update model + `ConfigDict(extra="forbid")` → 422 `VALIDATION_ERROR` | `app/api/v1/expense_categories.py` |
| Audit | `audit.record(db, *, outlet_id, table_name, record_id, action, changed_by, old_values, new_values)` — **caller commits** | `app/services/audit.py:55` |
| Bounded-list cap | `_MAX_ROWS = 500`, the considered §9 cursor exemption | `fuel_types.py`, `expense_categories.py`, `credit_customers.py` |
| Admin screen kit | `loadInto`, `activePill`, `wireSubmit`, `sheetFooter` | `app/static/js/screens/admin.js` |
| Full list+create+edit screen | one `xSheet(existing, onDone)` for both modes | `app/static/js/screens/admin_customers.js` |
| Name lookup precedent | the inline `UserProfile` join for the shortfall ledger | `app/api/v1/shortfalls.py:792` |

---

## 1. Step 0 — Phase 13 audit (no commit)

Run **before** any Phase 14 code, per the house rule that the previous-phase audit finds
defects the plan did not anticipate.

- `pytest` — record the count; Phase 13 closed at 1,302+ and green.
- `alembic current` → must be `0014 (head)`; `alembic downgrade base && alembic upgrade head`.
- `pytest --cov=app` — confirm the Phase 13 modules (`app/services/reporting.py`,
  `app/api/v1/reports.py`) are at 100%.
- Re-verify the four structural guarantees Phase 13 leaned on: `test_audit_coverage.py`,
  `test_errors.py`'s two `pg_constraint` walks, `test_routes.py`'s equality assertion, and
  `test_frontend_assets.py::test_every_router_is_reachable_from_a_screen`.

If the audit finds a defect, it is fixed in its own commit **before** Step 1 — Phase 6, 9 and
10 each found one this way.

---

## 2. Spec amendment (Step 1, its own commit)

`Spec: who may sign in, before Phase 14`

§14 requires asking before adding a rule the spec does not have, and CLAUDE.md currently ends
at Phase 13. This commit lands **before any code**.

| § | Amendment |
|---|---|
| §5.1 `user_profiles` | State that `id` is **immutable** and equal to `auth.users.id`, in the same shape as `fuel_types.code` — changing it orphans fifteen tables' foreign keys *and* breaks token resolution. State that the API sets `created_by` (the CLI's NULL means "system", and now has a counterpart that means "an admin, by name") |
| §5.1 `outlet_memberships` | State that **`is_active` on the membership is how a person is retired**, and that `user_profiles.is_active` is reserved for a multi-outlet "gone everywhere" act V1 has no way to mean (D6) |
| §8 | One new row at the **manager** floor: *"List the outlet roster (name and role only, to fill a picker)"*. The existing *"Manage users, nozzles, customers"* row is unchanged — it was always admin, and now it is reachable |
| §11 | Add the **Phase 14** entry: the user router, the `AuthBackend`, the roster picker, and the two screens it repairs |
| §13.25 (new) | **A user is created in two systems, and the window between them is not transactional.** Supabase Auth is written first, the database second, with a best-effort compensating delete. What an orphan costs, and how it is repaired (D3) |
| §13.26 (new) | **Email and password are not in this database and cannot be changed through this API.** Neither can a password be reset here. Supabase owns the credential; §5.1 already refuses to mirror it (D5) |
| §13.27 (new) | **The last active admin at an outlet cannot be demoted or deactivated through the API**, because there is no privileged caller left to undo it. `provision_user.py --force` is the documented repair path (D4) |
| §14 | Six new guardrails — see §8 below |
| §16 | `SUPABASE_SERVICE_KEY` gains a second consumer and joins the prod-boot validator (M2) |

---

## 3. Decisions

### D1 — One new router, `app/api/v1/users.py`, four endpoints

No migration, no new table, no new column. Alembic head stays `0014`. Everything this phase
needs already exists in `user_profiles` and `outlet_memberships` from migration `0002`;
Phase 14 is about *reaching* them, the way Phase 13 was about *presenting* figures that
already existed.

| Method | Path | Floor | Response |
|---|---|---|---|
| `GET` | `/api/v1/users` | **manager** | `list[UserListItem]` |
| `GET` | `/api/v1/users/{user_id}` | **admin** | `UserResponse` |
| `POST` | `/api/v1/users` | **admin** | `201 UserResponse` |
| `PATCH` | `/api/v1/users/{user_id}` | **admin** | `UserResponse` |

`router = APIRouter(tags=["identity"])`, matching `me.py` — this router answers the same
question about other people that `/me` answers about you. No `prefix=`; full paths on each
decorator, as everywhere else.

**Path-collision analysis** for `router.py`'s mandatory comment: `/users` and
`/users/{user_id}` have no static sibling under the parameterised segment, so there is no
`/fuel-prices/current`-shaped hazard. Register `users.router` immediately after `me.router`.

### D2 — Two response models, split by *route*, not by a runtime filter

Copied verbatim in reasoning from `credit_customers.py`: *"a filter is a line of code someone
can delete without any test noticing; a type that simply has no `phone` field cannot leak
one. It also means the OpenAPI schema tells the truth about what each role sees."*

- **`UserListItem`** — `id`, `full_name`, `role`, `is_active`. This is the picker payload and
  the name-resolution payload. **No phone.**
- **`UserResponse`** — the above plus `phone`, `profile_is_active`, `created_at`. Admin only,
  fetched when a row is opened for editing.

**Why the list sits at the manager floor and not attendant.** An attendant cannot read another
attendant's shift at all (`deps.py`'s `NOT_YOUR_SHIFT`), so `today.js`'s "another attendant"
branch only ever renders for a manager or admin — the one caller who needs a name. And the
shift-open form refuses an attendant a foreign `attendant_id` outright (`shifts.py:233`), so an
attendant has nothing to pick from. Nobody below manager has a use for the roster, and §8's
default is that staff detail sits above the attendant floor.

**Sort order is `role, full_name`.** The Postgres `membership_role` enum's label order is
`admin, manager, attendant` (migration `0002`) — descending privilege, which happens to be the
order an admin wants to read a staff list in. Worth a comment, because it looks like an
accident and is not.

### D3 — `POST /users` creates the Supabase Auth user first, then the two rows

This is the phase's central decision and the reason it is more than a CRUD router.

A new `app/services/supabase_auth.py`, structurally identical to `app/services/storage.py`:

```python
class AuthBackend(Protocol):
    """What this codebase needs from an identity provider, and nothing more."""

    def create_user(self, *, email: str, password: str) -> UUID: ...
    def delete_user(self, *, user_id: UUID) -> None: ...
```

- `SupabaseAuth` — `httpx.Client` based at `{SUPABASE_URL}/auth/v1`, `POST /admin/users` with
  `{"email": …, "password": …, "email_confirm": true}` and the same two headers Storage sends
  (`Authorization: Bearer <service_key>` **and** `apikey: <service_key>`). Injectable
  `client=` for `httpx.MockTransport`, headers applied **per request** not as client defaults —
  the reason `storage.py` gives, verbatim: baking them into a constructor-built client makes
  them untestable the moment a test supplies its own.
- `LocalAuth` — the dev/test double, exactly `LocalStorage`'s role. Mints a `uuid4()`, keeps an
  in-memory `email → id` map so a repeat raises the same `AUTH_USER_EXISTS`, and never touches
  the network. Never reachable in production once M2 lands.
- `build_auth(settings)` + `@lru_cache _cached_auth(url, key)`; the FastAPI `get_auth()`
  dependency lives in `app/api/deps.py`, because `app/services/__init__.py` requires services
  to be importable without FastAPI.

**`email_confirm: true`** so the person can sign in the moment they are created. Sending a
confirmation email would need SMTP configured in the Supabase project, which is one more thing
that must be working before Yuvraj can log in as a manager on a Tuesday afternoon.

**The order, and what the window costs.** Supabase is written first because we cannot know the
`id` until it answers, and `user_profiles.id` must equal `auth.users.id`:

```
1.  auth_user_id = auth.create_user(email=…, password=…)      ← outside the DB transaction
2.  try:
3.      db.add(UserProfile(id=auth_user_id, …, created_by=actor.user.id))
4.      db.flush()                                            ← FK ordering; provision_user.py:98
5.      db.add(OutletMembership(user_id=auth_user_id, outlet_id=actor.outlet_id,
6.                              role=payload.role.value, created_by=actor.user.id))
7.      db.flush()
8.      audit.record(… table_name="user_profiles",       action=insert …)
9.      audit.record(… table_name="outlet_memberships",  action=insert …)
10.     db.commit()
11. except Exception:
12.     db.rollback()
13.     auth.delete_user(user_id=auth_user_id)                ← best effort, logged if it fails
14.     raise
```

**Two audit rows, not one**, because `record_id` points at a row *in a named table*: an admin
later asking `table_name='outlet_memberships'` "who made Ramesh a manager" must find it there.

**If the compensating delete at step 13 also fails**, the auth user is orphaned. That fails
**closed**: they can obtain a token and are then refused everywhere with 403
`PROFILE_NOT_PROVISIONED`. It is logged at `logger.error` with the id, and the repair is
`provision_user.py`, which is exactly the tool for "an auth user exists, give it a profile".
§13.25 records this rather than pretending the two writes are atomic.

**There is no local duplicate pre-check, and that is a genuine break from house style.** Every
other create in this codebase pre-checks its unique key and raises a specific 409 before
inserting (`_refuse_duplicate_phone`, `CATEGORY_CODE_EXISTS`). Here the unique key is the
email, which **this database does not store** (§5.1 refuses to mirror the credential). Supabase
is the only authority, so a duplicate surfaces from step 1 as 409 `AUTH_USER_EXISTS` — and its
detail text names the repair, because the admin is genuinely stuck otherwise:

> *"That email already has a Supabase account. If they should have access here, run
> `python -m app.jobs.provision_user --user-id <their uuid> --full-name … --role …`."*

### D4 — The last active admin cannot be demoted or deactivated

The sharpest rule in the phase. `provision_user.py` already argues it for the CLI, where
`--force` exists as the escape hatch:

> *"a careless re-run with the wrong `--role` would otherwise silently demote the only admin
> and lock everyone out of §8's admin-only actions."*

**An HTTP endpoint has no `--force`, because there is no caller left who could send it.** So it
refuses outright. On `PATCH`, when the target's membership is currently `admin` and active, and
the change would either set `role != admin` **or** `is_active = false`, count the other admins
who could still sign in:

```sql
SELECT count(*) FROM outlet_memberships m
JOIN user_profiles p ON p.id = m.user_id
WHERE m.outlet_id = :outlet AND m.role = 'admin'
  AND m.is_active AND p.is_active AND m.user_id <> :target
```

Zero → **409 `LAST_ADMIN_AT_OUTLET`**, detail naming `provision_user.py --force` as the repair.
The `p.is_active` join matters: an admin whose *profile* was deactivated by the CLI cannot sign
in either, and counting them would leave the outlet locked out by an entirely correct query.

**Self-demotion with another admin present is allowed**, and so is self-deactivation. Both take
effect on the caller's very next request (403 `INSUFFICIENT_ROLE` / `MEMBERSHIP_INACTIVE`),
which is startling but correct and recoverable by the other admin. Refusing it would be a
second rule guarding a case D4 already covers.

### D5 — What cannot be changed, and how that is enforced

| Field | Mechanism | Why |
|---|---|---|
| `id` | not a path-addressable payload field anywhere | it is `auth.users.id`; changing it breaks token resolution and orphans fifteen tables' FKs |
| `email` | **absent from `UserUpdate`** + `extra="forbid"` → 422 | not a column here at all (§5.1). Supabase dashboard only |
| `password` | same | write-only, at create, and never read back |
| `outlet_id` | never accepted; taken from `actor.outlet_id` | §5.0 tenancy; a client-supplied outlet is how a role leaks across a boundary |

`UserUpdate` therefore carries exactly `full_name`, `phone`, `role`, `is_active`. The
mechanism is `expense_categories.py`'s, not a hand-written rejection branch, and for the reason
that file states: *"the silent version is the dangerous one — an admin who 'renamed' a category
and got a 200 back would reasonably believe it worked."*

### D6 — Deactivation writes `outlet_memberships.is_active`, never the profile flag

Both flags are live and carry distinct codes: `deps.py:151` → 403 `PROFILE_INACTIVE`,
`deps.py:228` → 403 `MEMBERSHIP_INACTIVE`. They mean different things:

- **membership inactive** = "no longer works at this outlet" — `"Your access to this outlet has
  been revoked."`
- **profile inactive** = "gone from every outlet"

In V1 there is one outlet, so the membership flag is a complete lockout already (every
protected route resolves through `require_role`). Exposing both toggles would be two switches
that do the same thing here and diverge confusingly the day there are two outlets. So the API
writes the membership flag; `user_profiles.is_active` keeps its current single writer — the
CLI — and §5.1 gains a sentence saying so.

There is **no `DELETE`**, here or anywhere (§3 rule 6), and `provision_user.py`'s own note
applies with force: roughly fifteen tables hold FKs to `user_profiles.id` and none cascade, so
a user who has done anything is undeletable by construction as well as by policy.

### D7 — `PATCH` is one endpoint writing up to two tables, and audits per table

`full_name` / `phone` land on `user_profiles`; `role` / `is_active` land on
`outlet_memberships`. One `PATCH` may touch either, both, or (with an empty body) neither —
422 `NO_FIELDS_TO_UPDATE`, the house code.

`audit.record` is called **once per table actually changed**, with `AuditAction.update` and
both sides populated, and the `_audit_snapshot` taken **before** the mutation. §14 is explicit
that a deactivation is an ordinary `update` and never `status_change` — that label means a
*shift* lifecycle move.

Splitting this into `PATCH /users/{id}` and `PATCH /users/{id}/membership` was considered and
rejected: it exposes a schema detail (role lives on a join table) as a URL, and an admin
changing a name and a role in one sheet would issue two requests that can half-fail.

### D8 — The roster picker, and the two repairs it makes possible

The frontend half of the phase, and the reason the endpoint is worth building this week rather
than next month.

1. **`today.js` resolves the attendant's name.** When `satisfies(me.role, "manager")`, fetch
   `GET /users` once and map `shift.attendant_id → full_name`. The literal
   `"another attendant"` becomes the person's name. The existing string stays as the fallback
   when the fetch fails — a roster that will not load must not blank the shift card.
   **Gated on role**, because `GET /users` is manager-floor and an attendant would get a 403
   for a name they were never going to need.
2. **`openShiftSheet` gains an attendant picker**, `select()`-based, shown only to manager and
   above and defaulting to the caller. An attendant sees the form exactly as it is today and
   the server keeps defaulting `attendant_id` to `actor.user.id` (`shifts.py:231`).

**This is not a permission control.** §8: hiding a button is UX. `shifts.py:233` still refuses
an attendant a foreign `attendant_id` with 403 `NOT_YOUR_SHIFT`, and the screen still handles
that 403 as a real outcome.

### D9 — `POST /users` takes no `Idempotency-Key`

§6.10 scopes idempotency to *"every `POST` that creates a money record"*. A user is reference
data, exactly like a credit customer or an expense category, neither of which carries a key —
`api.js`'s `NEEDS_IDEMPOTENCY` list is explicitly not "every POST", and its comment says why:
*"reference-data POSTs create rows a human is looking at."*

The retry hazard is real but it is Supabase's, not this system's: a timed-out `POST /users`
retried creates the auth user twice, and the second attempt is refused with
`AUTH_USER_EXISTS` — a clear error, not a duplicate row. That is a better outcome than
fingerprinting a request whose body contains a password.

### D10 — Email is validated by hand, not by adding `email-validator`

`pydantic.EmailStr` needs the `email-validator` package, and §14 says ask before adding a
dependency. §16 already set the precedent by hand-rolling content sniffing rather than
installing `python-magic`: *"about twenty lines for three signatures, nothing to install."*

So `email: str` with `min_length=3, max_length=320` and a `_looks_like_email` validator —
exactly one `@`, non-empty on both sides, a `.` in the domain, no whitespace. Supabase is the
real authority and rejects the rest; this check exists to catch a typo before we spend a
network round trip on it, not to be RFC 5322.

### D11 — `uq_outlet_memberships_user_outlet` moves into `_CONSTRAINT_ERRORS`

The most specific thing the exploration turned up. `tests/test_errors.py` currently excludes
that constraint from its `pg_constraint` walk, with this reason:

> *"Only reachable from `app/jobs/provision_user.py`, a CLI command run by one operator. A
> traceback in that operator's terminal is a fine outcome; there is no HTTP caller to hand a
> 500 to, and mapping it would be an entry no request can ever produce (§14's rule against
> dead code)."*

**Phase 14 makes every word of that false.** The exclusion is deleted from
`_NOT_REACHED_BY_THE_ERROR_HANDLER` and an entry is added to `_CONSTRAINT_ERRORS`:

```python
"uq_outlet_memberships_user_outlet": (
    409,
    "MEMBERSHIP_EXISTS",
    "That user already has a role at this outlet.",
),
```

reusing the code the endpoint's own pre-check raises, so the loser of a race cannot tell
whether it lost the `SELECT` or lost the insert — the convention `errors.py` states.

### D12 — The Users screen goes in its own module, and the hub lists it

`app/static/js/screens/admin_users.js`, not appended to `admin.js` — that file is already 694
lines and holds four screens, and `admin_customers.js` / `admin_pricing.js` are the precedent.
It imports `loadInto`, `activePill`, `wireSubmit` and `sheetFooter` from `admin.js` rather than
re-deriving them.

`SECTIONS` in `admin.js` gains a row — that array *is* the Admin tab's index, and a screen
missing from it is reachable only by typing a URL:

```js
{ label: "Users", hint: "Who may sign in, and what they may do", route: "#/admin/users" },
```

---

## 4. Mechanical decisions

| # | Decision |
|---|---|
| **M1** | `GET /users` takes `include_inactive: bool = Query(default=False)`, matching every other reference list. `_MAX_ROWS = 500` — a staff roster is bounded reference data, the same considered §9 cursor exemption `fuel_types` and `credit_customers` take |
| **M2** | `SUPABASE_SERVICE_KEY` joins `_supabase_auth_must_be_configured_in_prod`'s required tuple. **This closes an existing hole rather than adding coupling:** `build_storage` silently falls back to `LocalStorage` when the key is absent, so a misconfigured production has been writing receipts to a temp directory with nothing complaining. Note this is a behaviour change for prod boot and belongs in the notes |
| **M3** | No resolver dependency for `/users/{user_id}`. Every other router uses `resolve_outlet_from_x` because the row carries its own `outlet_id`; a membership's outlet is **not derivable from `user_id` alone** (that is the whole point of §5.1's per-outlet role). So `require_role(Role.admin)` with the default outlet resolver, and an explicit 404 `USER_NOT_FOUND` in the handler when no membership exists at `actor.outlet_id`. The side effect is *better* than the house pattern: a user at another outlet is a 404 here, never `credit_customers`' 403 — existence is not leaked across tenants |
| **M4** | The `role` payload field is typed `Role` (a `StrEnum`), so an unknown value is a Pydantic 422 before any query runs. It is written to the column as `role.value`, matching `provision_user.py` |
| **M5** | `users.py` is **not** added to `test_audit_coverage.py::test_the_seven_retrofitted_routers_are_audited`'s parametrize list. That list names the seven routers Phase 11 retrofitted and is a historical record; an eighth entry would misname it. The general AST walk covers the new router automatically, which is the point of it being discovery-based |
| **M6** | A new `clean_users` sweeper fixture in `conftest.py`, in the `clean_shifts` / `clean_expense_categories` style, because Phase 14 is the first phase that creates `user_profiles` rows **over HTTP** — `make_user`'s teardown only knows about ids it handed out. Its comment should carry `clean_credit`'s reasoning: a leaked row fails *the next test*, on a constraint, pointing at the wrong test entirely |
| **M7** | `FRIENDLY` in `api.js` gains entries only where the server's own `detail` is insufficient — `LAST_ADMIN_AT_OUTLET` and `AUTH_PROVIDER_UNAVAILABLE`. `AUTH_USER_EXISTS` does **not** get one: its detail already names the repair command, and duplicating it here is how that map becomes a second copy of the API's error text that drifts |
| **M8** | The password field uses `type: "password"` and `autocomplete: "new-password"`, is never pre-filled, and is never echoed back — `UserResponse` has no such field, so there is nothing to leak. `_audit_snapshot` covers only real columns, so a password cannot reach `audit_logs` even by accident |

---

## 5. Response shapes

```jsonc
// GET /api/v1/users            (manager)
[
  { "id": "…", "full_name": "Ramesh Kumar", "role": "attendant", "is_active": true }
]

// GET /api/v1/users/{id}       (admin)
{
  "id": "…",
  "full_name": "Ramesh Kumar",
  "phone": "+919812345678",
  "role": "attendant",
  "is_active": true,             // the MEMBERSHIP flag (D6)
  "profile_is_active": true,     // read-only here; only the CLI writes it
  "created_at": "2026-08-24T09:12:44.101Z"
}

// POST /api/v1/users           (admin)  -> 201, body is UserResponse
{ "email": "ramesh@example.com", "password": "…", "full_name": "Ramesh Kumar",
  "role": "attendant", "phone": "+919812345678" }

// PATCH /api/v1/users/{id}     (admin)  -> 200, body is UserResponse
{ "full_name": "…", "phone": "…", "role": "manager", "is_active": false }
```

---

## 6. Build order

Each step is one commit; the whole phase is pushed after the docs commit closes it.

| # | Commit message | Contents |
|---|---|---|
| 0 | *(no commit)* | Phase 13 audit (§1) |
| 1 | `Spec: who may sign in, before Phase 14` | `CLAUDE.md` only (§2) |
| 2 | `Phase 14 Step 2: an identity provider behind a protocol` | `app/services/supabase_auth.py`, `get_auth()` in `deps.py`, `SUPABASE_SERVICE_KEY` in the prod validator (M2), `tests/test_supabase_auth.py` modelled line-for-line on `tests/test_storage.py`, `LocalAuth` override in `conftest.py`'s `client` fixture |
| 3 | `Phase 14 Step 3: the last admin cannot lock everyone out` | `app/api/v1/users.py` — all four endpoints, D4's guard, D11's `_CONSTRAINT_ERRORS` entry and the deleted exclusion, registration in `router.py`, `clean_users` (M6) |
| 4 | `Phase 14 Step 4: the roster, and a shift with a name on it` | `admin_users.js`, the `SECTIONS` row, the `main.js` route and import, `today.js`'s name resolution and attendant picker, `FRIENDLY` entries, `prefixes` map entry in `test_frontend_assets.py` |
| 5 | `Phase 14 Step 5: creating a user is two writes in two systems` | `tests/test_users.py` and the new `# --- user_profiles / outlet_memberships ---` sections in `tests/test_reference_data_audit.py` |
| 6 | `Phase 14 Step 6: plan and notes docs` | `docs/phase-14-plan.md`, `docs/phase-14-notes.md` |

### Files touched

**New:** `app/services/supabase_auth.py`, `app/api/v1/users.py`,
`app/static/js/screens/admin_users.js`, `tests/test_supabase_auth.py`, `tests/test_users.py`,
`docs/phase-14-{plan,notes}.md`.

**Modified:** `CLAUDE.md`, `app/core/config.py` (M2), `app/core/errors.py` (D11),
`app/api/deps.py` (`get_auth`), `app/api/v1/router.py`, `app/static/js/main.js`,
`app/static/js/api.js` (M7), `app/static/js/screens/admin.js` (`SECTIONS`),
`app/static/js/screens/today.js` (D8), `tests/conftest.py` (M6, `LocalAuth`),
`tests/test_errors.py` (D11), `tests/test_frontend_assets.py` (`prefixes`).

**No migration. Alembic head stays `0014`.**

### The six structural gates a new router must clear

1. `test_routes.py` — under `/api/v1/`, and `require_role` on **every** endpoint (nothing is
   added to `_UNAUTHENTICATED_PATHS`, which is asserted by equality).
2. `test_audit_coverage.py` — a literal `audit.record(...)` call node inside every
   `@router.post` / `@router.patch` body, imported as `from app.services import audit` (the
   walker requires the bare name `audit`).
3. `test_errors.py` — D11.
4. `test_frontend_assets.py` — `"users": "/users"` in `prefixes`, **and** the string `/users`
   present in a comment-stripped JS line.
5. `test_frontend_assets.py` — `main.js` must `import { renderUsers } from "./screens/admin_users.js"`,
   and that file must carry a matching `export async function renderUsers`.
6. `test_static_mount.py` — no external host, no `innerHTML`, no `parseFloat`.

---

## 7. Error codes introduced

| Code | Status | Raised when |
|---|---|---|
| `USER_NOT_FOUND` | 404 | no membership for that `user_id` at the caller's outlet (M3 — covers "another outlet" too) |
| `MEMBERSHIP_EXISTS` | 409 | pre-check, and the `_CONSTRAINT_ERRORS` race loser (D11) |
| `LAST_ADMIN_AT_OUTLET` | 409 | D4 |
| `AUTH_USER_EXISTS` | 409 | Supabase refuses a duplicate email; detail names `provision_user.py` |
| `AUTH_USER_REJECTED` | 422 | Supabase 4xx that is not a duplicate — a weak password, chiefly |
| `AUTH_PROVIDER_UNAVAILABLE` | 502 | transport failure or 5xx from Supabase Auth. The `STORAGE_UNAVAILABLE` shape: *"a dependency being down is not a bug — the client should know it can retry, which a bare 500 does not communicate"* |

Reused unchanged: `NO_FIELDS_TO_UPDATE` (422), `VALIDATION_ERROR` (422, for immutable fields
via `extra="forbid"`), `INSUFFICIENT_ROLE` / `NOT_A_MEMBER` (403).

---

## 8. New §14 guardrails

- **Never accept `user_profiles.id` from a client, and never generate one.** It must equal
  `auth.users.id` or the person authenticates successfully and is refused forever (§5.1, §13.26)
- **Never store or log an email or a password in this database.** Supabase owns the credential
  and §5.1 refuses to mirror it; `_audit_snapshot` covers columns only, so neither can reach
  `audit_logs` (§5.1, §13.26)
- **Never let the API demote or deactivate the last active admin at an outlet.** There is no
  privileged caller left to undo it; `provision_user.py --force` is the repair path (§13.27)
- **Never take `outlet_id` from a user-management payload.** It comes from `actor.outlet_id`;
  a client-supplied outlet is how a role leaks across a tenancy boundary (§5.0, §8)
- **Never deactivate a person by writing `user_profiles.is_active` from the API.** That flag
  means "gone from every outlet" and V1 has no way to mean it; retire the membership (§13.26)
- **Never treat the Supabase-then-database create as atomic.** It is two systems; compensate
  best-effort, log the orphan loudly, and repair with the CLI (§13.25)

---

## 9. Test inventory — every way this can break

### A. The last-admin guard (D4) — the phase's central risk
- The only admin cannot be demoted to manager → 409 `LAST_ADMIN_AT_OUTLET`, **and the row is
  unchanged** (re-read it; a refusal that half-wrote is the failure mode)
- The only admin cannot be deactivated → 409
- With a **second** admin present, both moves succeed
- An admin whose *profile* is inactive does **not** count as a second admin — the `p.is_active`
  join, asserted by making one and confirming the demotion is still refused
- An admin at **another outlet** does not count
- A **deactivated** membership does not count
- Demoting a *manager* to attendant is never blocked
- An admin may demote **themselves** when another admin exists, and their next request is 403

### B. Creation across two systems (D3)
- Happy path: one auth call, one `user_profiles` row, one `outlet_memberships` row, **two**
  audit rows, and the new person can immediately authenticate and reach `/me`
- `user_profiles.id` **equals** the id the auth backend returned
- `created_by` is the acting admin — the case where creator and subject differ
- The auth backend raising `AUTH_USER_EXISTS` leaves **no** `user_profiles` row and no audit row
- A **database failure after** the auth call triggers the compensating `delete_user` (assert the
  fake recorded the call), leaves no rows, and returns 500
- A **compensating-delete failure** still leaves no database rows and logs at `error`
- `AUTH_PROVIDER_UNAVAILABLE` on transport failure and on a 5xx, both 502, via
  `httpx.MockTransport` — `tests/test_storage.py`'s exact shape
- No `Idempotency-Key` is required (D9); the same key twice is simply ignored
- Email validation (D10): `@` count, empty local part, empty domain, no dot, whitespace → 422

### C. Permissions and tenancy
- `GET /users` — attendant 403, manager 200, admin 200
- `GET /users/{id}`, `POST`, `PATCH` — attendant and manager both 403 `INSUFFICIENT_ROLE`
- A `user_id` from another outlet → **404 `USER_NOT_FOUND`**, not 403 (M3), asserted by
  inserting one rather than inferring from an empty list
- A user with no membership at all → 404
- `GET /users` returns only this outlet's members, asserted by inserting a foreign one

### D. The lean/full split (D2)
- `UserListItem` carries **no** `phone` — asserted by key name, the way
  `test_me_never_exposes_an_email` is written
- A manager calling `GET /users/{id}` is 403, so the phone is unreachable at that floor by any
  route
- Sort order is role-descending then name

### E. Immutability (D5)
- `{"email": …}` on `PATCH` → 422; `{"password": …}` → 422; `{"id": …}` → 422;
  `{"outlet_id": …}` → 422 — all via `extra="forbid"`, and **each writes no audit row**
- Empty `PATCH` → 422 `NO_FIELDS_TO_UPDATE`
- An explicit `null` on `full_name` is ignored, not written (the column is NOT NULL)

### F. Deactivation semantics (D6)
- Deactivating a membership → that person's next request is 403 `MEMBERSHIP_INACTIVE`
- Their historical rows still read and report — a shift they attended still lists
- `user_profiles.is_active` is **untouched** by the endpoint, asserted directly
- Reactivating restores access
- There is no `DELETE` route on this router (assert via `app.openapi()`)

### G. Audit (§5.3, into `tests/test_reference_data_audit.py`)
- Create records **exactly two** rows, `old_values is None` on both, right `table_name` on each
- A `PATCH` of `full_name` records **one** row on `user_profiles` and **none** on
  `outlet_memberships`; a `PATCH` of `role` does the reverse; a `PATCH` of both records both
- `old_values` and `new_values` both populated and **genuinely different** — which fails if the
  snapshot is taken after the mutation
- `changed_by` is the **acting** admin, not the subject
- A refused write — 409 `LAST_ADMIN_AT_OUTLET`, 422 immutable field, 403 non-admin — records
  `count(*) == 0`, which is what proves the audit row and the change share one transaction
- Nothing records `status_change` (§14)
- No password and no email appears anywhere in `old_values` / `new_values`
- `X-Request-ID` propagates into `request_id`

### H. Constraint mapping (D11)
- `test_errors.py`'s broad `pg_constraint` walk passes with the exclusion **deleted**
- A concurrent double-create loses with 409 `MEMBERSHIP_EXISTS`, not an opaque 500 — provoked
  by inserting the membership directly and then calling the endpoint

### I. Structural / suite-level
- The six gates in §6 above
- `alembic current` is still `0014` and no migration was added
- `alembic downgrade base && upgrade head` still round-trips
- `pytest --cov=app` — 100% on `app/api/v1/users.py` and `app/services/supabase_auth.py`
- The suite is still **fully offline**: no test reaches a real Supabase project (`LocalAuth`
  and `httpx.MockTransport` only)

### J. Frontend behaviour (hand-checked, §13.18)
- The Admin tab lists **Users**; the screen loads, lists, creates, edits, deactivates
- Creating an attendant, signing out, signing in as them: **two tabs only**, no Cash, no Admin
- The same for a manager: three tabs, no Admin; and the Close-shift button appears
- As admin: four tabs, Lock/Reopen present
- `today.js` shows the attendant's **name**, not "another attendant"
- The shift-open sheet shows the picker for a manager and **not** for an attendant
- A manager who opens `#/admin/users` by typing the URL is bounced with a toast (courtesy) and
  the endpoint 403s regardless (control)
- The password field never round-trips; the create sheet clears on close

---

## 10. Verification checklist

**A — suite and migration health**
- [x] `pytest` green, count recorded and higher than Phase 13's
- [x] `alembic current` → `0014 (head)`; downgrade/upgrade round-trips
- [x] `pytest --cov=app` → 100% on both new modules

**B — the structural guarantees**
- [x] `test_audit_coverage.py`, `test_errors.py`, `test_routes.py`,
      `test_frontend_assets.py`, `test_static_mount.py` all pass **without** an exemption being
      added to any of them (D11 *deletes* an exclusion; nothing gains one)

**C — the domain rules this phase must not soften**
- [x] The last admin genuinely cannot be demoted or deactivated, and the row is unchanged after
      the refusal
- [x] No email and no password exists anywhere in `user_profiles`, `outlet_memberships` or
      `audit_logs` — checked with a direct SQL query, not by reading the code
- [x] `provision_user.py` still works unchanged and is still the documented bootstrap

**D — the checks no test replaces**
- [x] Sign in as all three roles and walk the tabs (§9 J)
- [x] Kill the Supabase project's network mid-create and confirm 502 with a retry-able message
- [x] `ENV=prod` with no `SUPABASE_SERVICE_KEY` refuses to boot (M2), and the notes record that
      this is a **new** requirement for an existing deployment

---

## 11. What actually shipped

**Everything in §3 and §4 shipped as written, with four deviations and three findings.**
1,491 tests pass (+63 over Phase 13's 1,428 at the start of this phase); `alembic current` is
still `0014`; `app/api/v1/users.py` and `app/services/supabase_auth.py` are both at 100%.

### Deviations from the plan

**§6's Step 3/Step 4 boundary was wrong, and a test said so.** The build order had the router
in one commit and the screen in the next. `test_every_router_is_reachable_from_a_screen`
refuses that: a router with no screen fails the suite, which is the entire point of it. The
two landed as one commit. Worth recording because the plan was written *knowing* about that
test and still got the boundary wrong — the test caught a planning error, not a coding one.

**M6 was rejected by the code it was based on.** The plan called for a `clean_users` sweeper
fixture in the `clean_expense_categories` style. `make_user`'s own teardown already explains
why that cannot work: *"pytest tears fixtures down in reverse setup order, and a
usefixtures-declared fixture is set up first, so it would run **after** this and the foreign
key would already have blown up."* Instead the teardown **adopts** anybody an API call
created — `created_by IS NOT NULL` is an exact marker, since both `make_user` and
`provision_user.py` leave it NULL — and extends its own id list, so all seven existing
cascade levels cover them and keep covering them when an eighth is added.

**D12's helper import needed four `export` keywords.** `loadInto`, `activePill`,
`wireSubmit` and `sheetFooter` were module-private in `admin.js`. Exporting them is what that
file's own comment already intends — *"One loader shape for every list screen"* — and is
reuse rather than a new abstraction.

**M2 turned out to be a defect, not a chore.** Adding `SUPABASE_SERVICE_KEY` to the prod-boot
validator was planned as tidiness. It is not: the key has had a consumer since Phase 8 and its
absence is **silent**, because `build_storage` falls back to `LocalStorage`. Any production
deployment that forgot it has been writing receipts to a machine-local temp directory and
handing out `file://` URIs as signed URLs. **This is a breaking change for an existing
deployment** — the variable must be set before the app will boot.

### Findings the tests produced

**A pre-existing Phase 12 bug, found while adding the picker.** `renderNoShift` is called with
`{ session, navigate }` (today.js:62) and `openShiftSheet` read `context.container`. So
opening the **first shift of a day** created it server-side and then threw on the refresh:
the toast appeared, the screen did not update, and a reload showed the shift. Reached only
from the no-shift screen — the other three callers come through `actionsCard`, which does pass
`container`. Fixed with a positional argument. §13.18 names this class precisely, and it is
the first time that stated cost has actually been paid.

**`a@.com` passed the hand-rolled email check.** The first draft tested `"." in domain`, which
accepts an empty first label. Caught by the parametrised test, which is why that test lists
near-misses rather than one obviously broken string. Now requires at least two non-empty
labels.

**`caplog` sees nothing from a test-built app.** `create_app()` calls `configure_logging()`,
which does `root.handlers.clear()` — removing the handler pytest installed. The orphan-logging
assertion silently passed against an empty string until it was rewritten to attach a handler
to the module's own logger. Worth knowing before the next test tries to assert on a log line.

### One thing that did not need building

D9 said `POST /users` takes no `Idempotency-Key`, and `api.js`'s `NEEDS_IDEMPOTENCY` list is
already a regex allowlist of money routes — so nothing had to change to achieve it. Recorded
because "no code was written" is the correct outcome of that decision and is invisible in the
diff.

---

## 12. Not in Phase 14

Password reset and email change (Supabase owns the credential — §13.26); invite emails and
SMTP; a self-service `PATCH /me`; multi-outlet membership management or an outlet switcher
(§12, §13.6); RLS; per-user preferences or theming (§12); soft-delete or merge of users; login
history or session listing; bank balances, the IOCL ledger and PAD reporting — **the post-V1
module, built together and after the hand-test** (§12); anything that would let a
`user_profiles` row be hard-deleted (§3 rule 6).

---

## 13. Still owed by the owner

**New:**
- Confirm the real staff list and each person's role before the July hand-test — the same
  argument §14 makes about the real expense-category list: seeding the real names means the app
  matches the paper register from day one
- Decide whether an admin should be able to see a person's email in the app at all. Today it is
  Supabase-dashboard-only by design (§5.1), which is correct and mildly inconvenient

**Carried forward, all still open, all still load-bearing:**
- **Petrol and diesel dealer margins have never been entered.** Phase 13 made this visible via
  `fuels_missing_margin`; it is still not fixed, and profit reporting still covers CBG only
- The first opening balance must be seeded before any day can be finalised (§6.5)
- CBG's real max flow rate in kg/min; whether testing quantities are recorded on paper, and in
  what unit (§14 marks both URGENT — they are live on real money)
- `EXPENSE_RECEIPT_THRESHOLD` = ₹5,000 and `VARIANCE_ALERT_THRESHOLD` = ₹100 are both guesses
- No way to write off a shortfall (§13.15); audit-trail retention (§13.17)
- Are §13.23's six alert kinds the right list?
- Does a salesman hold a change float overnight? Is a surplus ever booked?
