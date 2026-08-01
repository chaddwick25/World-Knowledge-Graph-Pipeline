#!/bin/bash
# Initialize Recipe Builder Dependencies
# Ensures all prerequisites are met before using Recipe Builder
#
# This script:
# 1. Starts Docker containers and creates databases
# 2. Runs Django migrations
# 3. Verifies system is ready
#
# Usage: ./scripts/initialize_recipe_builder.sh

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"

echo ""
echo "=========================================="
echo -e "${CYAN}RECIPE BUILDER INITIALIZATION${NC}"
echo "=========================================="
echo ""
echo "This script will prepare your system for the Recipe Builder."
echo "It will:"
echo "  1. Start Docker containers and create databases"
echo "  2. Run Django migrations"
echo "  3. Verify system is ready"
echo ""

# ─────────────────────────────────────────────────────────────
# Phase 0: Docker & Database Setup
# ─────────────────────────────────────────────────────────────
echo -e "${YELLOW}═══ PHASE 0: Docker & Database Setup ═══${NC}"
echo ""

# Check if Docker is running
echo -e "${BLUE}Checking Docker status...${NC}"
if ! docker info > /dev/null 2>&1; then
    echo -e "${RED}ERROR: Docker is not running${NC}"
    echo "Please start Docker and try again."
    exit 1
fi
echo -e "${GREEN}✓ Docker is running${NC}"
echo ""

# Check if containers are already running
echo -e "${BLUE}Checking PostgreSQL containers...${NC}"
POSTGRES_DEFAULT_RUNNING=$(docker ps --filter "name=postgres-default" --filter "status=running" --format "{{.Names}}" 2>/dev/null || echo "")
POSTGRES_VECTORS_RUNNING=$(docker ps --filter "name=postgres-vectors" --filter "status=running" --format "{{.Names}}" 2>/dev/null || echo "")

if [ -n "$POSTGRES_DEFAULT_RUNNING" ] && [ -n "$POSTGRES_VECTORS_RUNNING" ]; then
    echo -e "${GREEN}✓ PostgreSQL containers are already running${NC}"
    echo "  - postgres-default: running"
    echo "  - postgres-vectors: running"
else
    echo -e "${YELLOW}PostgreSQL containers not running. Starting databases...${NC}"
    echo ""
    
    # Run create_databases.sh
    bash "$PROJECT_ROOT/scripts/create_databases.sh"
    
    if [ $? -ne 0 ]; then
        echo -e "${RED}ERROR: Failed to create databases${NC}"
        exit 1
    fi
fi
echo ""

# ─────────────────────────────────────────────────────────────
# Phase 1: Django Migrations
# ─────────────────────────────────────────────────────────────
echo -e "${YELLOW}═══ PHASE 1: Django Migrations ═══${NC}"
echo ""


# Check if migration files exist
echo -e "${BLUE}Checking migration files...${NC}"
API_MIGRATIONS="$BACKEND_DIR/api/migrations"
VECTORS_MIGRATIONS="$BACKEND_DIR/vectors/migrations"

API_HAS_MIGRATIONS=$(find "$API_MIGRATIONS" -name "0*.py" 2>/dev/null | wc -l)
VECTORS_HAS_MIGRATIONS=$(find "$VECTORS_MIGRATIONS" -name "0*.py" 2>/dev/null | wc -l)

if [ "$API_HAS_MIGRATIONS" -eq 0 ] || [ "$VECTORS_HAS_MIGRATIONS" -eq 0 ]; then
    echo -e "${YELLOW}Migration files not found. Creating migrations...${NC}"
    docker compose exec backend python manage.py makemigrations
    echo -e "${GREEN}✓ Migration files created${NC}"
else
    echo -e "${GREEN}✓ Migration files exist${NC}"
fi
echo ""

# Run migrations
echo -e "${BLUE}Applying migrations...${NC}"
docker compose exec backend python manage.py migrate

if [ $? -ne 0 ]; then
    echo -e "${RED}ERROR: Failed to apply migrations${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Migrations applied successfully${NC}"
echo ""

# ─────────────────────────────────────────────────────────────
# Phase 1.5: Continents Recipe (PBF Extraction + OSM Boundaries)
# ─────────────────────────────────────────────────────────────
echo -e "${YELLOW}═══ PHASE 1.5: Continents Recipe ═══${NC}"
echo ""

# Load environment variables
if [ -f "$PROJECT_ROOT/.env" ]; then
    source "$PROJECT_ROOT/.env"
fi

# Check if planet file is configured
if [ -z "$PLANET_OSM_FILE_PATH" ]; then
    echo -e "${YELLOW}⚠ PLANET_OSM_FILE_PATH not set. Skipping continents recipe.${NC}"
    echo "  Set this in .env to enable continent extraction and boundary generation."
else
    # Check if continents already extracted
    CONTINENT_PBF_COUNT=$(docker exec postgres-default psql -U $POSTGRES_USER -d $POSTGRES_DB -tAc "SELECT COUNT(*) FROM pbf_files WHERE pbf_file_type = 'CONTINENT' AND status = 'COMPLETED';" 2>/dev/null || echo "0")
    BOUNDARY_COUNT=$(docker exec postgres-default psql -U $POSTGRES_USER -d $POSTGRES_DB -tAc "SELECT COUNT(*) FROM osm_boundaries WHERE admin_level = 2;" 2>/dev/null || echo "0")
    
    if [ "$CONTINENT_PBF_COUNT" -ge 7 ] && [ "$BOUNDARY_COUNT" -gt 100 ]; then
        echo -e "${GREEN}✓ Continents already processed${NC}"
        echo "  - Continent PBFs: $CONTINENT_PBF_COUNT"
        echo "  - Country boundaries: $BOUNDARY_COUNT"
    else
        echo -e "${BLUE}Running continents recipe...${NC}"
        echo "  This will:"
        echo "  1. Extract 7-8 continent PBF files from planet"
        echo "  2. Extract OSM admin boundaries (countries)"
        echo "  3. Pre-render continent-level GeoJSON"
        echo "  Estimated time: ~30-60 minutes"
        echo ""
        
        docker compose exec backend python manage.py run_continents_recipe
        
        if [ $? -eq 0 ]; then
            echo -e "${GREEN}✓ Continents recipe completed successfully${NC}"
        else
            echo -e "${YELLOW}⚠ Continents recipe failed (non-critical)${NC}"
            echo "  You can run it manually later with:"
            echo "  docker compose exec backend python manage.py run_continents_recipe"
        fi
    fi
fi
echo ""

# ─────────────────────────────────────────────────────────────
# Phase 2: Verification
# ─────────────────────────────────────────────────────────────
echo -e "${YELLOW}═══ PHASE 2: System Verification ═══${NC}"
echo ""

# Load environment variables
if [ -f "$PROJECT_ROOT/.env" ]; then
    source "$PROJECT_ROOT/.env"
else
    echo -e "${RED}ERROR: .env file not found${NC}"
    exit 1
fi

# Verify critical tables exist
echo -e "${BLUE}Verifying database tables...${NC}"

CRITICAL_TABLES=(
    "pbf_files"
    "region_hierarchy"
    "polygon_files"
    "registered_services"
    "orchestration_processingsession"
)

TABLES_OK=true
for table in "${CRITICAL_TABLES[@]}"; do
    TABLE_EXISTS=$(docker exec postgres-default psql -U $POSTGRES_USER -d $POSTGRES_DB -tAc "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = '$table');" 2>/dev/null || echo "f")
    
    if [ "$TABLE_EXISTS" = "t" ]; then
        echo -e "  ${GREEN}✓${NC} $table"
    else
        echo -e "  ${RED}✗${NC} $table (missing)"
        TABLES_OK=false
    fi
done

if [ "$TABLES_OK" = false ]; then
    echo -e "${RED}ERROR: Some critical tables are missing${NC}"
    exit 1
fi
echo ""

# Verify pgvector extension
echo -e "${BLUE}Verifying pgvector extension...${NC}"
PGVECTOR_EXISTS=$(docker exec postgres-vectors psql -U $PGVECTOR_USER -d $PGVECTOR_DB -tAc "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector');" 2>/dev/null || echo "f")

if [ "$PGVECTOR_EXISTS" = "t" ]; then
    echo -e "${GREEN}✓ pgvector extension is installed${NC}"
else
    echo -e "${RED}✗ pgvector extension is missing${NC}"
    exit 1
fi
echo ""

# Test database connections
echo -e "${BLUE}Testing database connections...${NC}"

# Test default database
docker exec postgres-default psql -U $POSTGRES_USER -d $POSTGRES_DB -c "SELECT 1;" > /dev/null 2>&1
if [ $? -eq 0 ]; then
    echo -e "  ${GREEN}✓${NC} Default database: Connected"
else
    echo -e "  ${RED}✗${NC} Default database: Connection failed"
    exit 1
fi

# Test vectors database
docker exec postgres-vectors psql -U $PGVECTOR_USER -d $PGVECTOR_DB -c "SELECT 1;" > /dev/null 2>&1
if [ $? -eq 0 ]; then
    echo -e "  ${GREEN}✓${NC} Vectors database: Connected"
else
    echo -e "  ${RED}✗${NC} Vectors database: Connection failed"
    exit 1
fi
echo ""

# ─────────────────────────────────────────────────────────────
# Phase 3: Success Summary
# ─────────────────────────────────────────────────────────────
echo "=========================================="
echo -e "${GREEN}INITIALIZATION COMPLETE ✓${NC}"
echo "=========================================="
echo ""
echo "Your system is now ready for the Recipe Builder!"
echo ""
echo -e "${CYAN}Next Steps:${NC}"
echo ""
echo "1. Start the backend server:"
echo -e "   ${BLUE}docker compose up -d backend${NC}"
echo ""
echo "2. Start the frontend server (in another terminal):"
echo -e "   ${BLUE}cd frontend && npm run serve${NC}"
echo ""
echo "3. Navigate to the Recipe Builder:"
echo -e "   ${BLUE}http://localhost:8080/recipe-builder${NC}"
echo ""
echo "4. In the Recipe Builder, click 'Initialize System' to:"
echo "   - Register the planet file"
echo "   - Sync polygon regions"
echo "   - Register services"
echo ""
echo -e "${YELLOW}Note:${NC} The Recipe Builder's 'Initialize System' button handles"
echo "application-level initialization (planet file, polygons, services)."
echo "This script handled the infrastructure-level setup (databases, migrations)."
echo ""
