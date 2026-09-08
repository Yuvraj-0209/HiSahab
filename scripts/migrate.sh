#!/usr/bin/env bash
#
# Bring the database up to head. Run by Railway as the pre-deploy command
# (see .railway/railway.ts), between the build and the deploy.
#
# WHY THIS RUNS AT ALL (Phase 18). Until now the start command lived only in the Railway
# dashboard and did nothing but launch uvicorn. Fifteen migrations landed and not one of
# them ever ran against Supabase; docs/phase-17-notes.md records the gap as known and
# deliberately deferred. That is the same class of failure CLAUDE.md §3 rule 9 forbids --
# the schema moving by a route the repository does not control.
#
# WHAT A FAILURE HERE MEANS. Railway's contract for a pre-deploy command: "If your
# command fails, it will not be retried and the deployment will not proceed." So a
# non-zero exit below means the new container never takes traffic and the PREVIOUS
# deployment keeps serving. That is deliberate. Serving new code against a half-migrated
# schema produces silently wrong money figures rather than a crash, and CLAUDE.md's
# preamble names wrong-but-plausible numbers as this project's primary failure mode.
#
# This runs ONCE PER DEPLOYMENT, in its own container -- not once per replica -- so there
# is no concurrent-migration race even if the replica count is raised later.
set -euo pipefail

# --- Which connection Alembic uses -------------------------------------------------
#
# Supabase gives two URLs. The transaction pooler (port 6543) multiplexes many clients
# onto few server connections, which suits a web app and breaks DDL: pgbouncer in
# transaction mode loses prepared statements and session state, so migrations fail
# intermittently and confusingly rather than cleanly. .env.example has said so since
# Phase 1, and production's DATABASE_URL *is* the pooler -- verified, not assumed.
#
# MIGRATION_DATABASE_URL is OPTIONAL and falls back to DATABASE_URL, which is the right
# answer for local development and for CI, where one direct URL serves both.
#
# It is deliberately NOT in app/core/config.py. No application code reads it -- only this
# script does -- and putting it in Settings would imply a runtime consumer that does not
# exist. Overriding the variable is what makes it take effect: alembic/env.py resolves
# the URL from get_settings().DATABASE_URL, and this process exits immediately after, so
# the override cannot reach the served application.
if [ -n "${MIGRATION_DATABASE_URL:-}" ]; then
  echo "migrate.sh: using MIGRATION_DATABASE_URL (direct connection)"
  export DATABASE_URL="${MIGRATION_DATABASE_URL}"
else
  echo "migrate.sh: using DATABASE_URL (MIGRATION_DATABASE_URL is unset)"
fi

echo "migrate.sh: alembic current"
alembic current

echo "migrate.sh: alembic upgrade head"
alembic upgrade head

echo "migrate.sh: migrations complete"
