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

# Seed /app/.venv from the image bootstrap if the named volume is empty
# (e.g. on first run or after a volume wipe). The bootstrap lives under /opt
# so it is not hidden by the ./backend:/app bind mount.
if [ ! -f "/app/.venv/bin/python" ]; then
    echo "/app/.venv is empty; seeding from image bootstrap..."
    cp -a /opt/venv_bootstrap/. /app/.venv/
fi

echo "Running makemigrations..."
python manage.py makemigrations

echo "Running migrations for default database..."
python manage.py migrate

echo "Running migrations for vectors database..."
python manage.py migrate --database=vectors

echo "Entrypoint setup complete!"

# Execute the main command
exec "$@"
