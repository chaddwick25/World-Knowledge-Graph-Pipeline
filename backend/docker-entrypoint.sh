#!/bin/bash
set -e

echo "Waiting for postgres-default to be ready..."
until pg_isready -h "$POSTGRES_HOST" -p "${POSTGRES_PORT:-5432}" -U "$POSTGRES_USER" -d "$POSTGRES_DB"; do
  echo "postgres-default is unavailable - sleeping"
  sleep 2
done
echo "postgres-default is ready"

echo "Waiting for postgres-vectors to be ready..."
until pg_isready -h "$PGVECTOR_HOST" -p "${PGVECTOR_PORT:-5432}" -U "$PGVECTOR_USER" -d "$PGVECTOR_DB"; do
  echo "postgres-vectors is unavailable - sleeping"
  sleep 2
done
echo "postgres-vectors is ready"

# Extensions + orphaned pg_type cleanup live in Python (core.management
# commands.db_preflight) rather than in this script: the bash version parsed
# credentials by colon-joining them, which breaks on a password containing a
# colon, and it duplicated connection handling that Django already owns.
echo "Running database preflight (extensions + pg_type cleanup)..."
python manage.py db_preflight

# Seed /app/.venv from the image bootstrap if the named volume is empty
# (e.g. on first run or after a volume wipe). The bootstrap lives under /opt
# so it is not hidden by the ./backend:/app bind mount.
if [ ! -f "/app/.venv/bin/python" ]; then
    echo "/app/.venv is empty; seeding from image bootstrap..."
    cp -a /opt/venv_bootstrap/. /app/.venv/
fi

# Only the backend container runs migrations.  The worker waits for the
# backend to finish by polling the django_migrations table.
# Both containers share the same entrypoint; the RUN_MIGRATIONS env var
# (set in docker-compose.yml) controls which role we play.
if [ "$RUN_MIGRATIONS" = "false" ]; then
  echo "Worker container — skipping migrations, waiting for backend..."
  # Fail fast rather than "proceeding anyway": the worker cannot function
  # without the schema, and silently continuing turns a startup race into
  # confusing downstream errors (e.g. missing tables mid-task).  The timeout
  # is env-driven so slow/remote databases can be accommodated.
  MAX_WAIT="${MIGRATION_WAIT_SECONDS:-300}"
  WAITED=0
  while [ $WAITED -lt $MAX_WAIT ]; do
    APPLIED=$(PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "${POSTGRES_PORT:-5432}" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "
      SELECT count(*) FROM django_migrations WHERE app = 'core' AND name = '0001_initial';
    " 2>/dev/null || echo "0")
    if [ "$APPLIED" -ge "1" ]; then
      echo "Backend migrations detected ($APPLIED core migrations applied). Proceeding."
      break
    fi
    echo "Waiting for backend to finish migrations... (${WAITED}s/${MAX_WAIT}s)"
    sleep 5
    WAITED=$((WAITED + 5))
  done
  if [ $WAITED -ge $MAX_WAIT ]; then
    echo "ERROR: timed out after ${MAX_WAIT}s waiting for backend migrations on $POSTGRES_HOST:$POSTGRES_DB."
    echo "       The worker cannot run without the schema. Raise MIGRATION_WAIT_SECONDS if the DB is slow or remote."
    exit 1
  fi
else
  echo "Backend container — running migrations..."

  # makemigrations is opt-in (default: off).  Generating migration files is a
  # build/CI concern, not a runtime one — running it on every boot adds a full
  # Django model-introspection pass that stalls startup.  Set RUN_MAKEMIGRATIONS=true
  # in dev or CI when schema changes need to be detected.
  if [ "$RUN_MAKEMIGRATIONS" = "true" ]; then
    echo "Running makemigrations..."
    python manage.py makemigrations
  else
    echo "Skipping makemigrations (set RUN_MAKEMIGRATIONS=true to enable)."
  fi

  echo "Running migrations for default database..."
  python manage.py migrate

  echo "Running migrations for vectors database..."
  python manage.py migrate --database=vectors

  # Planet initialization now runs as a Docker startup step (replaces the
  # former Celery canvas of step_0a..step_0m tasks). Idempotent — safe to run
  # on every boot. Disable with RUN_INIT_PLANET=false (e.g. for the worker,
  # which reuses this entrypoint).
  #
  # No pre-check here: init_planet owns its own lock and now also owns the
  # run's *state*.  The old bash pre-check asked "is there a COMPLETED row
  # ever?", which disagreed with the command's "COMPLETED for today" and could
  # seal a partial build behind a permanent skip.  One source of truth, in
  # Python, testable.
  if [ "$RUN_INIT_PLANET" = "false" ]; then
    echo "Skipping init_planet (RUN_INIT_PLANET=false)."
  else
    echo "Running init_planet..."
    # Best-effort at the container level: a failed chain must not stop the API
    # from serving. It is no longer silent — init_planet marks the snapshot
    # FAILED with the failed step names and the next boot resumes.
    python manage.py init_planet || echo "WARNING: init_planet reported failures; see logs above."
  fi
fi

echo "Entrypoint setup complete!"

# Warm the platform LLM (Ollama) so the first user query does not pay the
# 5-15s cold model-reload penalty. Fail-soft — skips when the LLM is
# disabled or Ollama is not up yet.
python manage.py warm_llm 2>/dev/null || echo "LLM warm-up skipped/failed."

# Warm the execute-query data path (Postgres buffers + OS page cache on
# OsmEntity / USLP / factor tables) so the first enrichment query does
# not read cold (~25s observed → ~3.5s warm). Fail-soft like warm_llm.
python manage.py warm_query_path 2>/dev/null || echo "Query-path warm-up skipped/failed."

# Execute the main command
exec "$@"
