#!/bin/bash
# Create Databases Script V2 (with PostGIS Support)
# This script creates fresh PostgreSQL databases for the EDA Vector Search Toolkit
# with PostGIS support for spatial_semantics app

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo "=========================================="
echo -e "${GREEN}DATABASE CREATION SCRIPT V2${NC}"
echo -e "${BLUE}(with PostGIS Support)${NC}"
echo "=========================================="
echo ""

# Check if .env file exists
if [ ! -f .env ]; then
    echo -e "${RED}ERROR: .env file not found!${NC}"
    echo "Please create .env file with database credentials."
    echo ""
    echo "Example .env file:"
    echo "  POSTGRES_DB=django_db"
    echo "  POSTGRES_USER=django_user"
    echo "  POSTGRES_PASSWORD=secret"
    echo "  PGVECTOR_DB=vector_db"
    echo "  PGVECTOR_USER=vector_user"
    echo "  PGVECTOR_PASSWORD=secret"
    exit 1
fi

# Load environment variables
source .env

echo -e "${BLUE}Configuration loaded:${NC}"
echo "  Default DB: $POSTGRES_DB (user: $POSTGRES_USER)"
echo "  Vectors DB: $PGVECTOR_DB (user: $PGVECTOR_USER)"
echo ""

# Step 1: Create Docker network if it doesn't exist
echo -e "${YELLOW}Step 1: Creating Docker network...${NC}"
docker network create eda-vector-search-toolkit_default 2>/dev/null || echo "  - Network already exists"
echo -e "${GREEN}✓ Network ready${NC}"
echo ""

# Step 2: Create volumes
echo -e "${YELLOW}Step 2: Creating Docker volumes...${NC}"
docker volume create eda-vector-search-toolkit_postgres_default_data
docker volume create eda-vector-search-toolkit_postgres_vectors_data
docker volume create eda-vector-search-toolkit_polygon_files
echo -e "${GREEN}✓ Volumes created${NC}"
echo ""

# Step 3: Start PostgreSQL containers
echo -e "${YELLOW}Step 3: Starting PostgreSQL containers...${NC}"
docker-compose up -d postgres-default postgres-vectors
echo -e "${GREEN}✓ Containers started${NC}"
echo ""

# Step 4: Wait for databases to be ready
echo -e "${YELLOW}Step 4: Waiting for databases to be healthy...${NC}"
echo "This may take 10-15 seconds..."

max_attempts=30
attempt=0

while [ $attempt -lt $max_attempts ]; do
    default_health=$(docker inspect --format='{{.State.Health.Status}}' postgres-default 2>/dev/null || echo "starting")
    vectors_health=$(docker inspect --format='{{.State.Health.Status}}' postgres-vectors 2>/dev/null || echo "starting")
    
    if [ "$default_health" = "healthy" ] && [ "$vectors_health" = "healthy" ]; then
        echo -e "${GREEN}✓ Both databases are healthy${NC}"
        break
    fi
    
    echo "  Attempt $((attempt + 1))/$max_attempts: default=$default_health, vectors=$vectors_health"
    sleep 2
    attempt=$((attempt + 1))
done

if [ $attempt -eq $max_attempts ]; then
    echo -e "${RED}ERROR: Databases failed to become healthy${NC}"
    echo "Check logs with: docker-compose logs postgres-default postgres-vectors"
    exit 1
fi
echo ""

# Step 5: Verify database connections
echo -e "${YELLOW}Step 5: Verifying database connections...${NC}"

# Test default database
echo "Testing default database connection..."
docker exec postgres-default psql -U $POSTGRES_USER -d $POSTGRES_DB -c "SELECT version();" > /dev/null 2>&1
if [ $? -eq 0 ]; then
    echo -e "  ${GREEN}✓ Default database: Connected${NC}"
else
    echo -e "  ${RED}✗ Default database: Connection failed${NC}"
    exit 1
fi

# Test vectors database
echo "Testing vectors database connection..."
docker exec postgres-vectors psql -U $PGVECTOR_USER -d $PGVECTOR_DB -c "SELECT version();" > /dev/null 2>&1
if [ $? -eq 0 ]; then
    echo -e "  ${GREEN}✓ Vectors database: Connected${NC}"
else
    echo -e "  ${RED}✗ Vectors database: Connection failed${NC}"
    exit 1
fi
echo ""

# Step 6: Install PostGIS and create extensions
echo -e "${YELLOW}Step 6: Installing PostGIS and creating extensions...${NC}"

# Install PostGIS packages in postgres-vectors container
echo "  Installing PostGIS packages..."
docker exec postgres-vectors bash -c "
    export DEBIAN_FRONTEND=noninteractive && \
    apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends postgis postgresql-15-postgis-3 postgresql-15-postgis-3-scripts && \
    rm -rf /var/lib/apt/lists/*
"

if [ $? -eq 0 ]; then
    echo -e "  ${GREEN}✓ PostGIS packages installed${NC}"
else
    echo -e "  ${RED}✗ Failed to install PostGIS packages${NC}"
    exit 1
fi

# Create pgvector extension
echo "  Creating pgvector extension..."
docker exec postgres-vectors psql -U $PGVECTOR_USER -d $PGVECTOR_DB -c "CREATE EXTENSION IF NOT EXISTS vector;" > /dev/null 2>&1

if [ $? -eq 0 ]; then
    echo -e "  ${GREEN}✓ pgvector extension created${NC}"
else
    echo -e "  ${RED}✗ Failed to create pgvector extension${NC}"
    exit 1
fi

# Create PostGIS extension
echo "  Creating PostGIS extension..."
docker exec postgres-vectors psql -U $PGVECTOR_USER -d $PGVECTOR_DB -c "CREATE EXTENSION IF NOT EXISTS postgis;" > /dev/null 2>&1

if [ $? -eq 0 ]; then
    echo -e "  ${GREEN}✓ PostGIS extension created${NC}"
else
    echo -e "  ${RED}✗ Failed to create PostGIS extension${NC}"
    exit 1
fi

echo -e "${GREEN}✓ All extensions created${NC}"
echo ""

# Step 6.5: Verify PostGIS installation
echo -e "${YELLOW}Step 6.5: Verifying PostGIS installation...${NC}"
POSTGIS_VERSION=$(docker exec postgres-vectors psql -U $PGVECTOR_USER -d $PGVECTOR_DB -t -c "SELECT PostGIS_Version();" 2>/dev/null | xargs)

if [ -n "$POSTGIS_VERSION" ]; then
    echo -e "  ${GREEN}✓ PostGIS installed: $POSTGIS_VERSION${NC}"
else
    echo -e "  ${RED}✗ PostGIS verification failed${NC}"
    exit 1
fi
echo ""

# Step 7: Display database information
echo -e "${YELLOW}Step 7: Database information...${NC}"

echo "Default database tables:"
docker exec postgres-default psql -U $POSTGRES_USER -d $POSTGRES_DB -c "\dt" 2>/dev/null || echo "  - No tables yet (expected for new installation)"
echo ""

echo "Vectors database tables:"
docker exec postgres-vectors psql -U $PGVECTOR_USER -d $PGVECTOR_DB -c "\dt" 2>/dev/null || echo "  - No tables yet (expected for new installation)"
echo ""

echo "Vectors database extensions:"
docker exec postgres-vectors psql -U $PGVECTOR_USER -d $PGVECTOR_DB -c "\dx" 2>/dev/null
echo ""

# Step 7.3: Create Wikidata candidate cache directory
echo -e "${YELLOW}Step 7.3: Creating Wikidata candidate cache directory...${NC}"
mkdir -p data/wikidata_cache
echo -e "  ${GREEN}✓ data/wikidata_cache/ ready (Wikidata SPARQL harvest JSON cache)${NC}"
echo ""

# Step 7.5: Check Redis availability
echo -e "${YELLOW}Step 7.5: Checking Redis (WorldKG ontology cache)...${NC}"
if docker-compose ps redis 2>/dev/null | grep -q "Up"; then
    echo -e "  ${GREEN}✓ Redis container is running${NC}"
elif command -v redis-cli &>/dev/null && redis-cli -p 6379 ping &>/dev/null; then
    echo -e "  ${GREEN}✓ Redis (host) is running on port 6379${NC}"
else
    echo -e "  ${YELLOW}⚠ Redis not detected on port 6379${NC}"
    echo "  Redis is required for WorldKG ontology cache (worldkg_ontology_service.py)"
    echo "  Start with one of:"
    echo "    docker run -d -p 6379:6379 redis:7-alpine"
    echo "    sudo systemctl start redis"
fi
echo ""

# Step 8: Container status
echo -e "${YELLOW}Step 8: Container status...${NC}"
docker-compose ps postgres-default postgres-vectors
echo ""

# Summary
echo "=========================================="
echo -e "${GREEN}DATABASE CREATION COMPLETE (V2)${NC}"
echo "=========================================="
echo ""
echo "Databases created:"
echo "  ✓ postgres-default (port 5432)"
echo "    - Database: $POSTGRES_DB"
echo "    - User: $POSTGRES_USER"
echo "    - Status: $(docker inspect --format='{{.State.Health.Status}}' postgres-default)"
echo ""
echo "  ✓ postgres-vectors (port 5433)"
echo "    - Database: $PGVECTOR_DB"
echo "    - User: $PGVECTOR_USER"
echo "    - Extensions: vector $(docker exec postgres-vectors psql -U $PGVECTOR_USER -d $PGVECTOR_DB -t -c "SELECT extversion FROM pg_extension WHERE extname='vector';" 2>/dev/null | xargs)"
echo "    - Extensions: postgis $(docker exec postgres-vectors psql -U $PGVECTOR_USER -d $PGVECTOR_DB -t -c "SELECT extversion FROM pg_extension WHERE extname='postgis';" 2>/dev/null | xargs)"
echo "    - Status: $(docker inspect --format='{{.State.Health.Status}}' postgres-vectors)"
echo ""
echo "Connection strings:"
echo "  Default:  postgresql://$POSTGRES_USER:****@localhost:5432/$POSTGRES_DB"
echo "  Vectors:  postgresql://$PGVECTOR_USER:****@localhost:5433/$PGVECTOR_DB"
echo ""
echo "Next steps:"
echo "  1. Ensure Redis is running (WorldKG ontology cache):"
echo "     docker run -d -p 6379:6379 redis:7-alpine  # or systemctl start redis"
echo ""
echo "  2. Run migrations (both databases):"
echo "     cd backend"
echo "     python manage.py migrate"
echo "     python manage.py migrate --database=vectors"
echo ""
echo "  3. Load WorldKG ontology into Redis:"
echo "     python manage.py enrich_worldkg_classes \\"
echo "         --load-ontology ../data/worldkg_ontology_sample.json"
echo ""
echo "  4. Harvest Wikidata candidates for a country (OSM2KG / IGEA pipeline):"
echo "     python manage.py harvest_wikidata_candidates \\"
echo "         --country DE --cache-file ../data/wikidata_cache/de_candidates.json"
echo ""
echo "  5. Run IGEA alignment with harvested candidates:"
echo "     python manage.py harvest_wikidata_candidates \\"
echo "         --country DE --cache-file ../data/wikidata_cache/de_candidates.json \\"
echo "         --run-igea"
echo ""
echo "  6. Train geofenced GV-NLE (country-scoped DeepWalk with buffer zone):"
echo "     python manage.py train_gv_nle --country DE --buffer-deg 0.45"
echo "     # Or with a precise .poly boundary file:"
echo "     python manage.py train_gv_nle \\"
echo "         --poly-file data/osm_polygon_files/europe/germany.poly \\"
echo "         --buffer-deg 0.45"
echo ""
echo "  7. Start the application:"
echo "     python manage.py runserver"
echo ""
echo "  8. (Optional) Create superuser:"
echo "     python manage.py createsuperuser"
echo ""
