#!/bin/bash
# =========================================
# Database Migration Audit Script
# =========================================
# Usage: bash scripts/audit_migrations.sh
# Run from project root directory

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BACKEND_DIR="$PROJECT_ROOT/backend"

echo "========================================="
echo "  Database Migration Audit"
echo "========================================="
echo

# Step 1: Check for unapplied migrations
echo "1. Checking for unapplied migrations..."
echo "-----------------------------------------"

UNAPPLIED=$(cd "$BACKEND_DIR" && python manage.py showmigrations --list 2>/dev/null | grep '\[ \]' || true)

if [ -n "$UNAPPLIED" ]; then
    echo "⚠  UNAPPLIED MIGRATIONS FOUND:"
    echo "$UNAPPLIED"
    echo
    echo "   Run: cd backend && python manage.py migrate"
    echo "   For vectors DB: python manage.py migrate --database=vectors"
else
    echo "   ✓ All migrations applied on default database"
fi

echo

# Step 2: Check for pending model changes
echo "2. Checking for pending model changes..."
echo "-----------------------------------------"

PENDING=$(cd "$BACKEND_DIR" && python manage.py makemigrations --dry-run --check 2>&1 || true)

if echo "$PENDING" | grep -q "No changes detected"; then
    echo "   ✓ No pending migrations (models match schema)"
else
    echo "   ⚠  Pending model changes detected:"
    echo "$PENDING"
    echo
    echo "   Run: cd backend && python manage.py makemigrations"
fi

echo

# Step 3: Show migration status by app
echo "3. Migration status by app:"
echo "-----------------------------------------"

APPS=("extraction" "orchestration" "analysis" "api" "vectors" "semantic_search" "toronto_data" "analytical_models")

for APP in "${APPS[@]}"; do
    MIGRATION_COUNT=$(find "$BACKEND_DIR/$APP/migrations" -name "*.py" ! -name "__init__.py" 2>/dev/null | wc -l)
    if [ "$MIGRATION_COUNT" -gt 0 ]; then
        echo "   $APP: $MIGRATION_COUNT migration file(s)"
    else
        echo "   $APP: No migrations found"
    fi
done

echo

# Step 4: Verify database router
echo "4. Database Router Check:"
echo "-----------------------------------------"

cd "$BACKEND_DIR" && python -c "
from backend.database_router import VectorDBRouter, AppRouter
router = VectorDBRouter()

vector_apps = ['vectors', 'semantic_search']
for app in vector_apps:
    result = router.db_for_read(type('Model', (), {'_meta': type('Meta', (), {'app_label': app})()})())
    print(f'   {app} -> {result or \"default\"}')

default_apps = ['extraction', 'orchestration', 'analysis', 'api', 'toronto_data']
for app in default_apps:
    result = router.db_for_read(type('Model', (), {'_meta': type('Meta', (), {'app_label': app})()})())
    print(f'   {app} -> {result or \"default\"}')
" 2>/dev/null || echo "   ⚠  Could not verify database router"

echo
echo "========================================="
echo "  Audit Complete"
echo "========================================="
