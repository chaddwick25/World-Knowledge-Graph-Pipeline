#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

# Get the directory where the script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PROJECT_ROOT="$SCRIPT_DIR/.."

# Start the test database containers in detached mode.
echo "Starting test databases..."
docker-compose -f "$PROJECT_ROOT/docker-compose.test.yml" --project-directory "$PROJECT_ROOT" up -d

# Wait for the databases to be ready.
echo "Waiting for databases to be ready..."
sleep 10

# Run Django migrations on the test database.
echo "Running migrations on the test database..."
cd "$PROJECT_ROOT/backend"
poetry run python manage.py migrate --settings=backend.settings_test

echo "Test database setup complete."
