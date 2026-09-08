// Railway Infrastructure as Code — the deploy configuration for this project.
//
// WHY THIS FILE EXISTS (Phase 18). Until now the start command lived ONLY in the
// Railway dashboard, so it could not change in the same commit as the migration that
// needed it. Fifteen migrations landed and not one of them ever ran against Supabase;
// docs/phase-17-notes.md records the gap as known and deliberately deferred. That is
// the same class of failure CLAUDE.md §3 rule 9 forbids — the schema moving by a route
// the repository does not control — so the configuration is version-controlled here,
// reviewed alongside the code it deploys.
//
// ON THE npm DEPENDENCY. CLAUDE.md §2 and §14 forbid npm dependencies. This file needs
// the `railway` package to be evaluated, and the owner granted an explicit exception for
// it. The exception is narrow and worth stating precisely: it is a DEPLOY-TIME developer
// tool, it is devDependencies-only, and NOTHING it installs is served to a browser or
// imported by the application. §14's rule is about the frontend — no framework, no
// bundler, no build step for the assets in app/static — and that rule is untouched: the
// browser still loads hand-written ES modules from this origin and nothing is compiled.
//
// The alternative was railway.json, which is deprecated and stops being read on
// 2026-12-01. Choosing it would have meant doing this migration twice.
import { defineRailway, github, preserve, project, service } from "railway/iac";

export default defineRailway(() => {
  const hisahabApi = service("hisahab-api", {
    source: github("Yuvraj-0209/HiSahab"),

    // MIGRATE BEFORE SERVING, and never serve without migrating.
    //
    // preDeploy runs between the build and the deploy, in its OWN container, with the
    // service's variables and private network. Railway's contract is the half that
    // matters here: "If your command fails, it will not be retried and the deployment
    // will not proceed." So a failed migration means the new container never takes
    // traffic and the PREVIOUS deployment keeps serving.
    //
    // That is the intended behaviour, not a rough edge. Serving new code against a
    // half-migrated schema produces silently wrong money figures rather than a crash,
    // and CLAUDE.md's preamble names wrong-but-plausible numbers as this project's
    // primary failure mode. Old-but-consistent beats new-but-broken.
    //
    // It also runs ONCE PER DEPLOYMENT rather than once per replica, which is why this
    // is preferable to chaining `alembic upgrade head` onto the start command: there is
    // no concurrent-migration race to reason about if `replicas` is ever raised.
    //
    // scripts/start.sh is the wrapper it calls; see that file for why Alembic needs its
    // own database URL.
    preDeploy: "bash scripts/migrate.sh",

    start: "uvicorn app.main:app --host 0.0.0.0 --port $PORT",

    // /api/v1/health, not /health — CLAUDE.md §3 rule 3 puts health checks under
    // /api/v1 like everything else ("No exceptions, not even health checks that
    // obviously won't change"). app/api/v1/health.py round-trips the database, so a
    // green check means "reachable", not merely "the process is alive".
    healthcheck: "/api/v1/health",
    healthcheckTimeout: 60,

    // One replica, one cash chain. CLAUDE.md §13.12: an outlet has one drawer, and only
    // one shift may be open at a time. Raising this is a business decision, not a
    // scaling one.
    replicas: { "asia-southeast1-eqsg3a": 1 },

    domains: ["hisahab.com"],

    // preserve() means "keep the value already set in Railway". Secrets are NEVER
    // written into this file — CLAUDE.md §16, no secrets in the repository.
    env: {
      CORS_ALLOWED_ORIGINS: preserve(),
      DATABASE_URL: preserve(),
      DEFAULT_OUTLET_ID: preserve(),
      DEFAULT_OUTLET_NAME: preserve(),
      ENV: preserve(),
      EXPENSE_RECEIPT_THRESHOLD: preserve(),
      EXPENSE_REVIEW_THRESHOLD: preserve(),
      MAX_FLOW_RATE_LPM: preserve(),
      MAX_UPLOAD_BYTES: preserve(),
      // Phase 18. The direct (port 5432) Supabase URL that Alembic uses instead of the
      // transaction pooler. See scripts/migrate.sh and .env.example.
      MIGRATION_DATABASE_URL: preserve(),
      SIGNED_URL_TTL_SECONDS: preserve(),
      SUPABASE_ANON_KEY: preserve(),
      SUPABASE_JWT_SECRET: preserve(),
      SUPABASE_SERVICE_KEY: preserve(),
      SUPABASE_STORAGE_BUCKET: preserve(),
      SUPABASE_URL: preserve(),
      TZ_DISPLAY: preserve(),
      VARIANCE_ALERT_THRESHOLD: preserve(),
    },
  });

  return project("hisahab", {
    resources: [hisahabApi],
  });
});
