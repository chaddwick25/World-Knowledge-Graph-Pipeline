***REMOVED***!/bin/bash

***REMOVED*** Exit immediately if a command exits with a non-zero status.
set -e

***REMOVED*** Get the directory where the script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PROJECT_ROOT="$SCRIPT_DIR/.."

***REMOVED*** Start the test database containers in detached mode.
echo "Starting test databases..."
docker-compose -f "$PROJECT_ROOT/docker-compose.test.yml" --project-directory "$PROJECT_ROOT" up -d

***REMOVED*** Wait for the databases to be ready.
echo "Waiting for databases to be ready..."
sleep 10

***REMOVED*** Run Django migrations on the test database.
echo "Running migrations on the test database..."
cd "$PROJECT_ROOT/backend"
poetry run python manage.py migrate --settings=backend.settings_test

echo "Test database setup complete."
