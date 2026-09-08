# Phase 18 — Notes

> The learning reference, not a changelog. This phase changes no business rule and adds no
> table; it repairs the pipeline that was supposed to carry the last seventeen.

---

## 1. The phase Phase 17 wrote for me

§9 of `docs/phase-17-notes.md` ends with two open items, both raised and both deliberately
left alone rather than bundled into an unrelated change:

- *"Railway still has no `alembic upgrade head` in its start command."*
- *"Railway did not auto-deploy the push, and that is now a known fact rather than a
  suspicion."*

That deferral was right — a cash-engine fix should not also change how deployment works — but
it is only right once. Both are now closed, and the previous phase's own words are the reason
this one exists at all. Writing down a deferred defect is what makes it a phase rather than a
thing everyone forgot.

## 2. The bug had not bitten, and that was luck rather than safety

Production is at `0015` and `HEAD` needs `0015`, so the schema is currently *correct*. That
sounds like the problem is theoretical. It is not — it is arithmetic:

```
git diff --stat e5dd95a..HEAD -- alembic/    # empty
```

The reason it is empty is that Phase 17 added `0016_expense_paid_from.py` **and then reverted
it** when the owner proposed the simpler model. Had that revert not happened, the deployed app
would have been running against a database missing a column its code selected.

Worth stating plainly because it changes how the fix should be read: nothing was broken yet,
so nothing needed repairing *urgently*, and the machinery could therefore be introduced on a
deploy where the migration step is a guaranteed no-op. That is the best possible first outing
for something that runs before every future boot. A fix shipped under pressure, on the day a
real migration is waiting, would have had no such rehearsal.

## 3. A healthy-looking `source` block is not a working connection

The single most misleading thing in this investigation:

```json
"source": { "branch": "main", "repo": "Yuvraj-0209/HiSahab" }
```

That is exactly correct, and it is what Railway reports whether or not the GitHub App can
still see the repository. Railway keeps the block; the delivery is what stops. So the
configuration API can only ever prove the *intent* is right, never that the wiring is.

The evidence that actually distinguishes the two is in the deployment list, not the config:

- every deployment carries `commitHash e5dd95a` (31 Aug), while six commits sat on `main`;
- `git rev-parse HEAD`, `origin/main`, and `gh api …/commits/main` all agreed on `5581838…`,
  so the commits were unambiguously on GitHub;
- the one recent deployment has meta `{"reason":"deploy"}` with **no `branch` and no
  `commitHash`** — the `railway up --ci` upload Phase 17 describes, not a git build.

**The generalisation: to test an integration, look at what it produced, not at how it is
configured.** The same shape as Phase 17's *"verify a deploy by asking the app, not the
dashboard"*, one layer further out.

## 4. Eliminating a cause is worth as much as finding one

Railway's docs list four causes for "autodeploy on, nothing deploys". One of them —
*Wait for CI* — was disprovable in a single command:

```bash
ls -a .github        # (no .github dir)
```

No workflows means no check suites, means nothing for Railway to wait on. Crossing that off
before touching the dashboard costs seconds and removes a genuinely plausible suspect. A
watch-path filter was ruled out as a *sole* cause the same way: four of the six stuck commits
touch only `docs/` and `CLAUDE.md`, but `207ee1e` and `eca12ad` change `app/`, so no path
pattern explains the full outage.

What remains needs a human at the GitHub App installation page, because the `gh` CLI's OAuth
token cannot read app installations (`user/installations` returns 403 — it requires a
GitHub-App-authorised token). Knowing *why* a check is impossible is worth recording, or the
next person spends the same ten minutes discovering the same 403.

## 5. Config-as-code, because the bug *was* the drift

The start command lived only in the Railway dashboard. That is the whole defect, stated
structurally: the code's schema requirements and the deploy's behaviour could evolve
independently, with nothing able to reconcile them and no review able to see both.

CLAUDE.md §3 rule 9 says migrations happen only via Alembic and the schema is never edited by
hand. A start command that silently declines to run Alembic is the same rule broken from the
other end — the schema not moving when the code says it must.

So the deploy configuration is version-controlled, and the fix is not "somebody re-types the
start command correctly" but "the configuration arrives in the same commit as the migration
that needs it".

## 6. The CLI moved the design twice, mid-implementation

`railway.json` was written, and the CLI answered the first `variables --set` with a
deprecation notice: Config as Code stops being read on **2026-12-01**. Reading the docs it
pointed at changed the plan twice over, and both changes were improvements.

**First: `preDeployCommand` is a better mechanism than chaining onto the start command.**
The original design was `alembic upgrade head && uvicorn …`, which works and which the plan
defended at length. Railway has a first-class field for exactly this, and its contract is the
half that matters:

> *"If your command fails, it will not be retried and the deployment will not proceed."*

That is the fail-closed behaviour that was wanted, provided by the platform rather than by
`set -e`. And it is strictly better on the point the start-command version could only warn
about: a pre-deploy command runs **once per deployment, in its own container**, not once per
replica. §8's hazard did not need documenting-around; it stopped existing.

**Second: Config as Code is deprecated, so `railway.json` would have to be done twice.**
The replacement, `.railway/railway.ts`, is not free — it needs an npm dependency, which
CLAUDE.md §2 and §14 forbid outright. That was the owner's call to make and it was put to
them explicitly rather than decided quietly. They took the non-deprecated path.

**The exception is narrow, and the narrowness is the point.** `railway` is a
devDependency, it is evaluated by the Railway CLI on a developer's machine, and **nothing it
installs is served to a browser or imported by the application**. §14's rule is about the
frontend — no framework, no bundler, no build step for `app/static` — and that is untouched:
the browser still loads hand-written ES modules from this origin.

The one thing worth verifying rather than assuming was whether adding `package.json` would
make Railpack build this as a Node project, which would produce an image with no Python and
no `alembic`. It does not, and the answer is in Railpack's own source rather than its docs:

```go
// Order is important here. The first provider that returns true from Detect() will be used.
&python.PythonProvider{},
&deno.DenoProvider{},
&dotnet.DotnetProvider{},
&node.NodeProvider{},
```

Python is checked before Node and `Detect` fires on `pyproject.toml`, so `package.json` is
inert at build time. `node_modules/` is gitignored for the same reason — it is a local tool,
not part of the artefact §2 ships.

**The general lesson: a deprecation notice in CLI output is worth reading, not dismissing.**
It arrived as noise attached to an unrelated command, and it changed both halves of the design.

## 7. The pooler rule was already written down, in Phase 1

`.env.example` has said this since before there was a Supabase project to say it about:

> *Alembic MUST use the direct connection (port 5432), NOT the transaction pooler (port 6543).
> Pooled connections break DDL and prepared statements, so migrations fail in confusing,
> intermittent ways. The app itself may use the pooler.*

It was framed as *"NOTE for when this moves to Supabase"*. It has moved, and production's
`DATABASE_URL` is the pooler — so the note stopped being advisory and became a live
constraint. `MIGRATION_DATABASE_URL` is that note enforced rather than a new invention, and
the comment has been reworded so it no longer describes a future that already happened.

Two details about its shape:

- **Optional, falling back to `DATABASE_URL`.** Local development and CI use one direct URL
  for both purposes; requiring a second variable there would add a fourth way to fail at boot
  for no benefit.
- **Not in `app/core/config.py`.** No application code reads it — only `scripts/migrate.sh`
  does. Putting it in `Settings` would imply a runtime consumer that does not exist, and the
  next reader would go looking for one.

## 8. A hazard that stopped existing, and one that did not

The start-command design had a concurrency hazard it could only *document*: run once per
replica, and at `numReplicas > 1` every replica races on `alembic upgrade head`, with the
losers crash-looping. The plan's answer was a comment addressed to whoever raised the replica
count.

`preDeploy` removes it. One container, one run, per deployment. **Worth noticing as a pattern:
the best fix for a hazard you were about to write a careful warning about is often a different
mechanism that cannot have it.** A warning is what you write when you have run out of
structural options, not the first thing to reach for.

**The hazard that remains is a stale Alembic stamp, and it bit locally during this phase.**
`alembic current` failed with *"Can't locate revision identified by '0016'"*. Phase 17 reverted
`0016_expense_paid_from.py` — but reverting the *file* does not un-apply the *migration*, so
this machine's database was stamped at a revision that no longer exists, with the `paid_from`
column still physically present.

Production was unaffected, and the reason is the very bug this phase fixes: it has never run a
migration, so it sits cleanly at `0015`. Verified rather than assumed, read-only, before
anything was applied.

The repair, for anyone else who ran `0016` locally:

```sql
update alembic_version set version_num = '0015';
alter table expenses drop column if exists paid_from;
drop type if exists expense_paid_from;
```

**The general point: `git revert` on a migration is not a database operation.** A reverted
migration leaves every database that already ran it ahead of the code, and the failure surfaces
later as an unrelated-looking Alembic error. A revert of an applied migration needs a
downgrade, or a stamp, or a new migration undoing it — never just the file deletion.

## 9. What this leaves for next time

**The GitHub App installation still needs a human.** Everything up to that point is
eliminated; the remaining diagnosis is at
`github.com/settings/installations` → Railway → Configure, checking repository access and any
pending permission update, then disconnecting and reconnecting the source in Railway.

**The verification bar is a commit hash, not a green tick.** Phase 17 was nearly fooled by
`redeploy`, which reuses the existing build and would have re-shipped the old commit while
looking like a fix. The test that a push deploys is: a *new* deployment whose `meta.commitHash`
equals the SHA just pushed, with `meta.branch: "main"` and `meta.reason: "deploy"`. A bare
`{"reason":"deploy"}` proves nothing.

**Watch paths, if any exist, must include `.railway/**` and `scripts/**`** — otherwise this
phase's own config commit is the one that gets filtered out, which would be a memorable way to
fail.

**The deploy config is applied by a CLI command, not by a push.** `.railway/railway.ts` is
evaluated by `railway config apply`, so committing a change to it does not by itself change
Railway. That is a real difference from the `railway.json` it replaced, and the reason
`railway config plan` belongs in §15's command list: the file is the source of truth, but
somebody still has to apply it. Railway publishes a GitHub Action
(`railwayapp/config`) that plans on a PR and applies on merge — worth adopting when this repo
gets CI, which is the next item.

**Autodeploy verification, 8 September.** The pipeline was proven end to end at 05:33 UTC:
`migrate.sh` logged all four markers against the direct connection and the deploy reached
SUCCESS. Four earlier deployments failed and the app stayed online throughout -- the
fail-closed design caught every one before it reached production.

**There is still no CI.** With no `.github/workflows`, nothing runs the 1537 tests before a
deploy; the pipeline now migrates automatically but takes the code's word that it works. That
is a larger decision than this phase, and Railway's *Wait for CI* only becomes available once
a workflow exists — worth raising before the next schema change rather than after one.
