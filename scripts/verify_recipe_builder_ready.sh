#!/bin/bash
# Verify Recipe Builder Ready
# Quick status check to see if system is ready for Recipe Builder
#
# Usage: ./scripts/verify_recipe_builder_ready.sh

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo ""
echo -e "${CYAN}Recipe Builder Status Check${NC}"
echo "=========================================="
echo ""

# Load environment variables
if [ -f "$PROJECT_ROOT/.env" ]; then
    source "$PROJECT_ROOT/.env"
else
    echo -e "${RED}✗ .env file not found${NC}"
    exit 1
fi

ALL_OK=true

# Check Docker
echo -e "${BLUE}Docker:${NC}"
if docker info > /dev/null 2>&1; then
    echo -e "  ${GREEN}✓${NC} Docker is running"
else
    echo -e "  ${RED}✗${NC} Docker is not running"
    ALL_OK=false
fi

# Check PostgreSQL containers
echo -e "${BLUE}PostgreSQL Containers:${NC}"
POSTGRES_DEFAULT=$(docker ps --filter "name=postgres-default" --filter "status=running" --format "{{.Names}}" 2>/dev/null || echo "")
POSTGRES_VECTORS=$(docker ps --filter "name=postgres-vectors" --filter "status=running" --format "{{.Names}}" 2>/dev/null || echo "")

if [ -n "$POSTGRES_DEFAULT" ]; then
    echo -e "  ${GREEN}✓${NC} postgres-default is running"
else
    echo -e "  ${RED}✗${NC} postgres-default is not running"
    ALL_OK=false
fi

if [ -n "$POSTGRES_VECTORS" ]; then
    echo -e "  ${GREEN}✓${NC} postgres-vectors is running"
else
    echo -e "  ${RED}✗${NC} postgres-vectors is not running"
    ALL_OK=false
fi

# Check database connections
echo -e "${BLUE}Database Connections:${NC}"
if [ -n "$POSTGRES_DEFAULT" ]; then
    docker exec postgres-default psql -U $POSTGRES_USER -d $POSTGRES_DB -c "SELECT 1;" > /dev/null 2>&1
    if [ $? -eq 0 ]; then
        echo -e "  ${GREEN}✓${NC} Default database: Connected"
    else
        echo -e "  ${RED}✗${NC} Default database: Connection failed"
        ALL_OK=false
    fi
fi

if [ -n "$POSTGRES_VECTORS" ]; then
    docker exec postgres-vectors psql -U $PGVECTOR_USER -d $PGVECTOR_DB -c "SELECT 1;" > /dev/null 2>&1
    if [ $? -eq 0 ]; then
        echo -e "  ${GREEN}✓${NC} Vectors database: Connected"
    else
        echo -e "  ${RED}✗${NC} Vectors database: Connection failed"
        ALL_OK=false
    fi
fi

# Check critical tables
echo -e "${BLUE}Critical Tables:${NC}"
if [ -n "$POSTGRES_DEFAULT" ]; then
    CRITICAL_TABLES=("pbf_files" "region_hierarchy" "polygon_files")
    for table in "${CRITICAL_TABLES[@]}"; do
        TABLE_EXISTS=$(docker exec postgres-default psql -U $POSTGRES_USER -d $POSTGRES_DB -tAc "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = '$table');" 2>/dev/null || echo "f")
        if [ "$TABLE_EXISTS" = "t" ]; then
            echo -e "  ${GREEN}✓${NC} $table exists"
        else
            echo -e "  ${RED}✗${NC} $table missing"
            ALL_OK=false
        fi
    done
fi

# Check pgvector extension
echo -e "${BLUE}Extensions:${NC}"
if [ -n "$POSTGRES_VECTORS" ]; then
    PGVECTOR_EXISTS=$(docker exec postgres-vectors psql -U $PGVECTOR_USER -d $PGVECTOR_DB -tAc "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector');" 2>/dev/null || echo "f")
    if [ "$PGVECTOR_EXISTS" = "t" ]; then
        echo -e "  ${GREEN}✓${NC} pgvector extension installed"
    else
        echo -e "  ${RED}✗${NC} pgvector extension missing"
        ALL_OK=false
    fi
fi

echo ""
echo "=========================================="
if [ "$ALL_OK" = true ]; then
    echo -e "${GREEN}✓ System is ready for Recipe Builder${NC}"
    echo ""
    echo "You can now:"
    echo "  1. Start backend: cd backend && poetry run python manage.py runserver"
    echo "  2. Start frontend: cd frontend && npm run serve"
    echo "  3. Navigate to: http://localhost:8080/recipe-builder"
else
    echo -e "${RED}✗ System is NOT ready${NC}"
    echo ""
    echo "Please run: ./scripts/initialize_recipe_builder.sh"
fi
echo "=========================================="
echo ""
