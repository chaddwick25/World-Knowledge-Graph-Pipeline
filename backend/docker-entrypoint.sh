#!/bin/bash
set -e

echo "Waiting for postgres-default to be ready..."
until pg_isready -h "$POSTGRES_HOST" -p 5432 -U "$POSTGRES_USER" -d "$POSTGRES_DB"; do
  echo "postgres-default is unavailable - sleeping"
  sleep 2
done
echo "postgres-default is ready"

echo "Waiting for postgres-vectors to be ready..."
until pg_isready -h "$PGVECTOR_HOST" -p 5432 -U "$PGVECTOR_USER" -d "$PGVECTOR_DB"; do
  echo "postgres-vectors is unavailable - sleeping"
  sleep 2
done
echo "postgres-vectors is ready"

echo "Creating extensions in postgres-default..."
PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p 5432 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "CREATE EXTENSION IF NOT EXISTS postgis;" \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"

echo "Creating extensions in postgres-vectors..."
PGPASSWORD="$PGVECTOR_PASSWORD" psql -h "$PGVECTOR_HOST" -p 5432 -U "$PGVECTOR_USER" -d "$PGVECTOR_DB" \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"

# Clean up orphaned pg_type entries from crashed migrations.
# A previous migration crash can leave an orphaned composite type in pg_type
# without a matching pg_class entry, causing UniqueViolation on
# pg_type_typname_nsp_index when Django retries CREATE TABLE.
echo "Cleaning orphaned pg_type entries..."
for db_host in "$POSTGRES_HOST:$POSTGRES_DB:$POSTGRES_USER:$POSTGRES_PASSWORD" "$PGVECTOR_HOST:$PGVECTOR_DB:$PGVECTOR_USER:$PGVECTOR_PASSWORD"; do
  IFS=':' read -r host db user pass <<< "$db_host"
  PGPASSWORD="$pass" psql -h "$host" -p 5432 -U "$user" -d "$db" -c "
    DO \$\$
    DECLARE r RECORD;
    BEGIN
      FOR r IN
        SELECT t.typname, t.oid
        FROM pg_type t
        LEFT JOIN pg_class c ON c.oid = t.typrelid
        WHERE t.typtype = 'c'
          AND t.typrelid != 0
          AND c.oid IS NULL
          AND t.typnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'public')
      LOOP
        EXECUTE format('DROP TYPE IF EXISTS %I CASCADE', r.typname);
        RAISE NOTICE 'Dropped orphaned type: %', r.typname;
      END LOOP;
    END \$\$;
  " 2>/dev/null || true
done

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
  MAX_WAIT=300
  WAITED=0
  while [ $WAITED -lt $MAX_WAIT ]; do
    APPLIED=$(PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p 5432 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "
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
    echo "WARNING: Timed out waiting for backend migrations. Proceeding anyway."
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
  # former Celery canvas of step_0a..step_0m tasks). Idempotent — safe to
  # run on every boot; short-circuits via the PlanetSnapshot lock if a
  # COMPLETED row already exists for today. Disable with RUN_INIT_PLANET=false
  # (e.g. for the worker container, which reuses this entrypoint).
  if [ "$RUN_INIT_PLANET" = "false" ]; then
    echo "Skipping init_planet (RUN_INIT_PLANET=false)."
  else
    echo "Running init_planet..."
    # Best-effort: failures don't abort container startup (matches the old
    # Celery chain's max_retries=1 semantics). Logs are surfaced via stderr.
    python manage.py init_planet || echo "WARNING: init_planet reported failures; see logs above."
  fi
fi

echo "Entrypoint setup complete!"

# Warm the platform LLM (Ollama) so the first user query does not pay the
# 5-15s cold model-reload penalty. Fail-soft — skips when the LLM is
# disabled or Ollama is not up yet.
python manage.py warm_llm 2>/dev/null || echo "LLM warm-up skipped/failed."

# Execute the main command
exec "$@"
