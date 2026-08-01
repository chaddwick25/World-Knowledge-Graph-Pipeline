#!/bin/bash
# Drop All Databases Script V2
# This script completely removes all PostgreSQL databases and volumes for the EDA Vector Search Toolkit
# WARNING: This will delete ALL data. Use with caution!

set -e  # Exit on any error

# Load environment variables from the local env file so docker compose and the
# deletion paths have the values they need. .env.local takes precedence.
if [ -f .env.local ]; then
    set -a
    source .env.local
    set +a
elif [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Use the same compose files the project actually runs with.
DOCKER_COMPOSE="docker compose -f docker-compose.yml -f compose.override.yml"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo "=========================================="
echo -e "${RED}DATABASE DESTRUCTION SCRIPT V2${NC}"
echo "=========================================="
echo ""
echo -e "${YELLOW}WARNING: This will permanently delete:${NC}"
echo "  - All PostgreSQL containers"
echo "  - All database volumes and data"
echo "  - All migration history"
echo ""
read -p "Are you sure you want to continue? (yes/no): " confirm

if [ "$confirm" != "yes" ] && [ "$confirm" != "y" ]; then
    echo -e "${GREEN}Aborted. No changes made.${NC}"
    exit 0
fi

echo ""
echo -e "${BLUE}Starting database destruction...${NC}"
echo ""

# Step 1: Stop all containers
echo -e "${YELLOW}Step 1: Stopping all Docker containers...${NC}"
$DOCKER_COMPOSE down --remove-orphans
echo -e "${GREEN}✓ Containers stopped${NC}"
echo ""

# Step 2: Remove database data
echo -e "${YELLOW}Step 2: Removing database data directories...${NC}"

# PostgreSQL and Redis use host bind-mounts, not Docker-managed volumes, so
# `docker volume rm` does not reset them. They must be deleted from the host
# while the containers are stopped; otherwise the old data is re-mounted on
# the next `up` and the "reset" has no effect.
DEFAULT_DATA_DIR="${POSTGRES_DEFAULT_DATA_DIR:-}"
VECTORS_DATA_DIR="${POSTGRES_VECTORS_DATA_DIR:-}"
REDIS_DATA_DIR="${REDIS_DATA_DIR:-}"

for VAR_NAME in DEFAULT_DATA_DIR VECTORS_DATA_DIR REDIS_DATA_DIR; do
    if [ -z "${!VAR_NAME}" ]; then
        echo -e "${RED}Error: $VAR_NAME is not set.${NC}"
        echo "Set it in your .env or .env.local and source it before running this script:"
        echo "  source .env.local  # or export \$(cat .env.local | xargs)"
        exit 1
    fi
done

for DATA_DIR in "$DEFAULT_DATA_DIR" "$VECTORS_DATA_DIR" "$REDIS_DATA_DIR"; do
    if [ -d "$DATA_DIR" ]; then
        echo "  Removing bind-mount data directory: $DATA_DIR"
        sudo rm -rf "$DATA_DIR"
        echo "  ✓ Bind-mount data directory removed: $DATA_DIR"
    else
        echo "  - $DATA_DIR not found (already removed)"
    fi
done

# Also clean up any project-specific Docker volumes that might exist.
PROJECT_NAME="eda-vector-search-toolkit"
for vol in $(docker volume ls -q | grep -E "^${PROJECT_NAME//-/-?}[-_]" || true); do
    echo "  Removing Docker volume: $vol"
    docker volume rm "$vol" 2>/dev/null || echo "  - Could not remove volume $vol"
done

echo -e "${GREEN}✓ Database data removed${NC}"
echo ""

# Step 3: Remove any orphaned containers
echo -e "${YELLOW}Step 3: Cleaning up orphaned containers...${NC}"
$DOCKER_COMPOSE rm -f 2>/dev/null || echo "  - No orphaned containers found"
echo -e "${GREEN}✓ Cleanup complete${NC}"

# If a host Redis is still running, clear the DBs this project uses.
# The Redis bind-mount data directory is already removed above, but this
# also covers a non-container Redis instance on port 6379.
for db in 0 1 2; do
    redis-cli -n $db FLUSHDB 2>/dev/null || echo "  - Redis DB $db not available"
done
echo -e "${GREEN}✓ Redis state flushed${NC}"
echo ""

# Step 4: Verify removal
echo -e "${YELLOW}Step 4: Verifying removal...${NC}"
echo "Remaining volumes:"
docker volume ls | grep eda-vector-search-toolkit || echo "  - No project volumes found (good!)"
echo ""
echo "Remaining containers:"
docker ps -a | grep -E "postgres-default|postgres-vectors" || echo "  - No project containers found (good!)"
echo ""

# Step 4.5: Optionally clear Wikidata candidate cache
echo -e "${YELLOW}Step 4.5: Wikidata candidate cache...${NC}"
if [ -d "data/wikidata_cache" ] && [ "$(ls -A data/wikidata_cache 2>/dev/null)" ]; then
    echo "  Found cached Wikidata candidate files in data/wikidata_cache/:"
    ls data/wikidata_cache/*.json 2>/dev/null | head -10 | sed 's/^/    /'
    echo ""
    read -p "  Remove Wikidata candidate cache files? (yes/no): " remove_cache
    if [ "$remove_cache" = "yes" ] || [ "$remove_cache" = "y" ]; then
        rm -f data/wikidata_cache/*.json
        echo -e "  ${GREEN}\u2713 Wikidata cache cleared${NC}"
    else
        echo -e "  ${BLUE}Wikidata cache preserved${NC}"
    fi
else
    echo -e "  ${BLUE}No Wikidata cache files found (nothing to remove)${NC}"
fi
echo ""

# Step 5: Remove migration files
echo -e "${YELLOW}Step 5: Migration files...${NC}"
echo "Migration files present in:"
echo "  - backend/extraction/migrations/"
echo "  - backend/analysis/migrations/"
echo "  - backend/orchestration/migrations/"
echo "  - backend/vectors/migrations/"
echo "  - backend/toronto_data/migrations/"
echo "  - backend/semantic_search/migrations/"
echo ""
read -p "Do you want to remove migration files too? (yes/no): " remove_migrations

if [ "$remove_migrations" = "yes" ] || [ "$remove_migrations" = "y" ]; then
    echo "Removing migration files (keeping __init__.py)..."
    cd backend
    
    # List of all Django apps with migrations
    APPS=("extraction" "analysis" "orchestration" "vectors" "toronto_data" "semantic_search")
    
    for app in "${APPS[@]}"; do
        if [ -d "$app/migrations" ]; then
            echo "  Cleaning $app/migrations..."
            # Remove migration files except __init__.py
            find "$app/migrations/" -type f -name "*.py" ! -name "__init__.py" -delete 2>/dev/null || true
            find "$app/migrations/" -type f -name "*.pyc" -delete 2>/dev/null || true
            find "$app/migrations/" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
        fi
    done
    
    cd ..
    echo -e "${GREEN}✓ Migration files removed from all apps${NC}"
else
    echo -e "${BLUE}Migration files preserved${NC}"
fi
echo ""

# Summary
echo "=========================================="
echo -e "${GREEN}DATABASE DESTRUCTION COMPLETE (V2)${NC}"
echo "=========================================="
echo ""
echo "What was removed:"
echo "  ✓ PostgreSQL containers (default + vectors)"
echo "  ✓ PostgreSQL data directories (default + vectors)"
echo "  ✓ Redis data directory and keyspace (DBs 0, 1, 2)"
echo "  ✓ Project Docker volumes"
if [ "$remove_migrations" = "yes" ]; then
    echo "  ✓ Migration files (all 6 apps)"
fi
echo ""
echo "Next steps:"
echo "  1. Run:  docker compose -f docker-compose.yml -f compose.override.yml up -d"
echo "  2. Run:  cd backend && python manage.py makemigrations"
echo "  3. Run:  cd backend && python manage.py migrate"
echo "  4. Run:  cd backend && python manage.py migrate --database=vectors"
echo "  5. Load WorldKG ontology: python manage.py enrich_worldkg_classes \\"
echo "         --load-ontology ../data/worldkg_ontology_sample.json"
echo ""
echo "Note: If Redis is running, flush the WorldKG ontology cache too:"
echo "  redis-cli -n 2 FLUSHDB  (DB 2 = WorldKG ontology; avoids flushing other DBs)"

echo "Wikidata candidate cache is in data/wikidata_cache/*.json"
echo "  Re-harvest after rebuild:  manage.py harvest_wikidata_candidates --country <CODE>"
echo ""
