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

echo "Running makemigrations..."
poetry run python manage.py makemigrations

echo "Running migrations for default database..."
poetry run python manage.py migrate

echo "Running migrations for vectors database..."
poetry run python manage.py migrate --database=vectors

echo "Entrypoint setup complete!"

# Execute the main command
exec "$@"
