# Phase 14 — Notes

> The learning reference, not a changelog. `docs/phase-14-plan.md` is what was decided and
> why; this is what the building of it taught, including the things the plan got wrong.

---

## 1. The gap was three years old and nobody noticed, because nothing failed

§8's permission table has read *"Manage users, nozzles, customers — ❌ ❌ ✅"* since Phase 2.
Nozzles got a router in Phase 3. Customers got one in Phase 9. Users got a **shell command**,
and thirteen phases went by without anything complaining, because the command works.

That is the same shape as the audit gap §11 describes — *"it survived seven phases precisely
because nothing failed when it was missing"* — and it is worth naming the difference. The
audit gap is now pinned by `tests/test_audit_coverage.py`, which walks the AST and fails when
a new endpoint forgets. **There is no equivalent test for a permission-table row with no
endpoint behind it**, and building one would mean parsing a Markdown table into route
assertions, which is more machinery than the problem deserves.

So the honest statement is: this class of gap is still only caught by somebody reading §8 next
to `ls app/api/v1/`. Two of the three symptoms had been sitting in the product the whole time
(§4 below) and neither had been reported.

---

## 2. Two systems, one person, and why the order is forced

This is the first module in the codebase that **writes** to Supabase Auth. Everything before it
only ever *verified* a token, which `app/core/security.py` is explicit is pure local
computation — no network call at all.

The order is not a preference:

```
user_profiles.id  ==  auth.users.id  ==  the JWT's `sub` claim
```

Only Supabase can mint that value, so Supabase is written first and the database second. Which
means there is a window where the account exists and the profile does not, and **no amount of
care closes it** — it is two systems without a shared transaction.

What can be done is bound the damage:

| | |
|---|---|
| Database write fails | rollback, then delete the account again (best effort) |
| The compensating delete *also* fails | the account is orphaned, logged at `error` **with its id** |
| An orphan is used | it authenticates fine and is refused everywhere with 403 `PROFILE_NOT_PROVISIONED` |
| Repair | `python -m app.jobs.provision_user`, which is exactly the "an account exists, give it a profile" tool |

**It fails closed, and that is the property worth checking for.** An orphan can log in and do
nothing. The opposite ordering — database first, then Supabase — would have produced a profile
with no account, which is harmless too, but it is unbuildable: there is no id to write.

Two alternatives were considered and rejected, and the reasons are in §13.25. The interesting
one is generating the UUID locally and passing it to Supabase, which would make the whole thing
rollback-safe. It depends on the GoTrue admin API accepting a caller-supplied `id`, which is
version-specific — and a silent change there would put us back in this paragraph with no test
failing.

### The compensation swallows its own failure, deliberately

```python
try:
    auth.delete_user(user_id=auth_user_id)
except Exception:
    logger.error("orphaned auth user: ...", extra={"auth_user_id": str(auth_user_id)})
raise
```

The caller is already receiving an error. Replacing it with a *different* error about the
cleanup would hide what actually went wrong, and the cleanup's failure is not the caller's
problem to act on. What must not happen is silence — hence the id in the log, which is the only
thread back to the orphaned account.

---

## 3. The rule with no undo

§13.27 — the last active admin at an outlet cannot be demoted or deactivated — is the sharpest
thing in this phase, and its reasoning is worth keeping because it generalises.

`provision_user.py` already guards this for the CLI and explains the stake: *"a careless re-run
with the wrong `--role` would otherwise silently demote the only admin and lock everyone out of
§8's admin-only actions."* There it is **recoverable**, because `--force` exists and anybody
with shell access outranks every API role.

**Over HTTP there is no `--force`, because there is nobody left who could send it.** Every
admin-only endpoint requires an admin; remove the last one and the set of callers who could
restore them is empty. So the endpoint refuses, and the refusal text names the shell command,
because that is the only thing that can still get in.

### The subtle half is the count

```sql
SELECT count(*) FROM outlet_memberships m
JOIN user_profiles p ON p.id = m.user_id
WHERE m.outlet_id = :outlet AND m.role = 'admin'
  AND m.is_active AND p.is_active AND m.user_id <> :target
```

The `p.is_active` join is what makes it correct. There are **two** deactivation flags
(§5.1), and an admin whose *profile* is off still has a membership row saying
`role = admin, is_active = true`. Counting them would let the last usable admin be demoted on
the strength of an entirely correct-looking query, and the outlet would be stranded by a bug
nobody could see in the SQL. Two tests exist for exactly that, one per flag.

### What the rule deliberately does not do

Self-demotion **is** allowed when a second admin exists, and takes effect on the very next
request. That reads as a footgun and is not: the rule protects the *outlet* from having no
administrator, not an individual from their own decision. A second rule guarding
self-demotion would guard nothing this one does not already cover, and would stop a real
thing — an owner handing over and stepping down.

---

## 4. Two bugs that were already in the product

Neither was introduced by this phase; both were **caused** by the missing router and were
invisible until something tried to use it.

**`today.js` rendered the literal string `"another attendant"`.** Not a placeholder somebody
forgot — there was genuinely no endpoint that could turn `shift.attendant_id` into a name.
`app/api/v1/shortfalls.py:792` had already hit this and solved it privately with an inline
`{row.id: row.full_name}` join for its own ledger, which is the tell: the third place to want a
roster is the point at which one should exist.

**A manager could not open a shift in a salesman's name through the app at all.**
`POST /shifts` has accepted `attendant_id` since Phase 4 and `shifts.py:231` defaults it to the
caller — but no screen ever sent one, because there was no list to pick from. The capability
existed, was tested, and was unreachable.

### And one that was not caused by this phase at all

```js
renderNoShift(container, { session, navigate });   // today.js:62 -- no `container` key
...
on: { click: () => openShiftSheet(context) },      // and openShiftSheet read context.container
```

So opening the **first shift of a day** posted successfully, showed "Shift opened.", and then
threw on the refresh. A reload showed the shift, which is presumably why it survived — it looks
like a slow page rather than an error. Reached only from the no-shift screen; the three sibling
handlers (`closeShift`, `lockShift`, `reopenShift`) come through `actionsCard`, which does pass
`container`.

**This is §13.18's stated cost being paid for the first time.** That section says plainly:
*"a refactor of `api.js` can break a form without failing the suite"*, and here a context object
with one missing key did it instead. The fix is a positional argument, which is the version
that cannot be forgotten — but note what did *not* find it: 1,428 passing tests, 100% Python
coverage, and five structural JS tests. A person clicking the button found it.

---

## 5. Things the tests found rather than confirmed

**`a@.com` passed the email check.** The first draft tested `"." in domain`, which accepts an
empty first label. This is why the parametrised case list is made of *near*-misses — `no@domain`,
`@example.com`, `two@@example.com`, `a@.com` — rather than one obviously broken string. A test
of `"nope"` alone would have passed against the broken validator.

**`caplog` sees nothing from a test-built app.** `create_app()` calls `configure_logging()`,
which does `root.handlers.clear()` — removing the handler pytest installed moments earlier. The
orphan-logging assertion passed against an empty string until it was noticed. Anything that
needs to assert on a log line from an app built inside the test must attach its own handler to
the specific logger; there is now a worked example in
`tests/test_users.py::test_a_failed_compensation_still_leaves_no_database_rows`.

**A structural test caught a *planning* error.** The build order put the router in Step 3 and
the screen in Step 4. `test_every_router_is_reachable_from_a_screen` refuses that split by
design, so the two had to land together. The plan was written with full knowledge of that test
and still got it wrong — which is a decent argument for running the structural tests early in a
phase rather than at the end.

---

## 6. Two decisions that read as inconsistencies and are not

### No `resolve_outlet_from_user`

Every other router with an id in the path has one, and the house rule is that the 404 belongs in
the dependency so a missing row reports as missing rather than as a permission failure.

This router has none, because **a membership's outlet is not derivable from a `user_id`**. That
is not an oversight in the schema; it is the entire point of §5.1 putting the role on a
per-outlet join table, so somebody can be a manager at one pump and an attendant at another.
Asking "which outlet does user X belong to" has no single answer by construction.

So the outlet comes from the caller and the lookup is scoped to it. The side effect is *better*
than the pattern it deviates from: a user who exists at another outlet is a **404**, where
`credit_customers.py` answers 403 — which leaks existence across a tenancy boundary.

### No local duplicate check on create

Every other create in this codebase pre-checks its unique key and raises a specific 409 before
inserting. This one cannot: the unique key is the email, and §5.1 refuses to mirror the
credential, so Supabase is its only authority.

The consequence is a genuinely awkward state — an admin gets 409 `AUTH_USER_EXISTS` while
holding a real Supabase account they cannot attach — so the error **detail names the repair
command verbatim**. That is the difference between a refusal and a dead end, and it is the one
place in this codebase where an error message contains a shell command on purpose.

---

## 7. `SUPABASE_SERVICE_KEY` was a latent production bug

Planned as tidiness (M2: "add it to the prod validator"). It is not tidiness.

The key has had a consumer since Phase 8, and `build_storage`'s fallback is **silent**:

```python
if supabase_url and service_key:
    return SupabaseStorage(...)
return LocalStorage(root=Path(gettempdir()) / "hisahab-local-storage")
```

A production deployment that forgot the variable has been writing every receipt to a
machine-local temp directory — gone on reboot — and returning `file://` URIs as signed URLs.
Nothing logged, nothing raised, uploads returning 201.

Phase 14 adds a second consumer with the same shape, and two silent degradations is one more
than this deserved. **This is a breaking change for an existing deployment**: the variable must
be set before the app will boot. That is the correct direction — `config.py`'s own docstring
already says *"failing at startup is the only safe direction"* about the other three keys.

Worth generalising: **a config fallback that is right for dev and wrong for prod needs a
prod-time assertion, or it is a bug with a timer on it.**

---

## 8. Testing notes worth keeping

**`make_user`'s teardown adopts API-created rows.** Phase 14 is the first phase to insert
`user_profiles` over HTTP, and those rows carry `created_by` pointing at the acting admin —
who the fixture is about to delete. A separate `clean_users` fixture was the obvious answer and
does not work, for the reason that teardown's own comment already gives about `clean_shifts`:
declared with `usefixtures` it is set up first, torn down **last**, and the foreign key has
already blown up by then.

The fix extends the id list instead of adding two deletes:

```python
created.extend(row[0] for row in connection.execute(
    text("SELECT id FROM user_profiles WHERE created_by = ANY(:ids)").bindparams(ids=created)
))
```

`created_by IS NOT NULL` is an exact marker for "made through the API", because both the fixture
and `provision_user.py` leave it NULL — a system action has nobody to credit. And extending the
list rather than bolting on deletes means all seven existing cascade levels cover these rows,
and keep covering them when somebody adds an eighth.

**Provoke a rollback with a *different* user's id, not the caller's.** The first draft made the
auth double return the acting admin's id to collide on the primary key. It works, but the
admin's row is already in the request session's identity map (`get_current_user` loaded it), so
it raises a SQLAlchemy identity-map warning on top of the `IntegrityError` and muddies what the
test is demonstrating. A separate pre-made user collides just as hard and stays quiet.

**Assert the audit row lands on the right table.** This is the only endpoint in the codebase
where one `PATCH` writes two tables, so "did it record against `user_profiles` or
`outlet_memberships`" is a real question — `audit_logs.record_id` points at a row in a *named*
table, and recording both changes against one of them would pass every generic assertion while
losing "who made Ramesh a manager" forever. There are three tests: name-only, role-only, and
both.

---

## 9. Still owed by the owner

Unchanged from `docs/phase-14-plan.md` §13, and the two that block the July hand-test are:

- **Petrol and diesel dealer margins have never been entered.** Profit reporting covers CBG
  only, and Phase 13 made that visible via `fuels_missing_margin` without fixing it.
- **The first opening balance must be seeded** (§6.5, admin-only, one-time) before any day can
  be finalised.

And two that are cheap now and expensive later: CBG's real max flow rate in kg/min, and whether
testing quantities are recorded on paper at all — §14 marks both URGENT because they are live
on real money the moment data is entered.
