***REMOVED***!/bin/bash
***REMOVED*** =========================================
***REMOVED*** K80 Server Migration Helper
***REMOVED*** =========================================
***REMOVED*** Usage: bash scripts/migrate_to_k80.sh
***REMOVED*** Run from project root directory
***REMOVED***
***REMOVED*** This script helps set up the EDA Vector Search Toolkit
***REMOVED*** on a new server with a Tesla K80 GPU.

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BACKEND_DIR="$PROJECT_ROOT/backend"

echo "========================================="
echo "  K80 Server Migration Helper"
echo "========================================="
echo "  Date: $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================="
echo

***REMOVED*** ─────────────────────────────────────────
***REMOVED*** Step 0: Pre-flight checks
***REMOVED*** ─────────────────────────────────────────
echo "Step 0: Pre-flight checks"
echo "-----------------------------------------"

***REMOVED*** Check Python version
PYTHON_VERSION=$(python --version 2>&1)
echo "   Python: $PYTHON_VERSION"

***REMOVED*** Check for CUDA / GPU
if command -v nvidia-smi > /dev/null 2>&1; then
    GPU_INFO=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -1)
    echo "   GPU: $GPU_INFO"
    
    if nvidia-smi 2>/dev/null | grep -q "K80"; then
        echo "   ✓ Tesla K80 detected"
    else
        echo "   ⚠ Tesla K80 not detected (found: $GPU_INFO)"
        read -p "   Continue anyway? (y/N): " confirm
        [[ "$confirm" != "y" ]] && exit 1
    fi
else
    echo "   ⚠ nvidia-smi not found (no CUDA drivers?)"
    read -p "   Continue without GPU? (y/N): " confirm
    [[ "$confirm" != "y" ]] && exit 1
fi

***REMOVED*** Check .env file
if [ -f "$PROJECT_ROOT/.env" ]; then
    echo "   ✓ .env file found"
else
    echo "   ✗ .env file not found"
    echo "   Create .env with database credentials before continuing."
    echo ""
    echo "   Required variables:"
    echo "     POSTGRES_DB=django_db"
    echo "     POSTGRES_USER=django_user"
    echo "     POSTGRES_PASSWORD=<password>"
    echo "     PGVECTOR_DB=vector_db"
    echo "     PGVECTOR_USER=vector_user"
    echo "     PGVECTOR_PASSWORD=<password>"
    echo ""
    exit 1
fi

echo

***REMOVED*** ─────────────────────────────────────────
***REMOVED*** Step 1: Install dependencies
***REMOVED*** ─────────────────────────────────────────
echo "Step 1: Install Python dependencies"
echo "-----------------------------------------"

cd "$BACKEND_DIR"
if [ ! -d ".venv" ]; then
    python -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade "pip<24.1" setuptools wheel
pip install -r requirements.txt
echo "   ✓ Dependencies installed"

echo

***REMOVED*** ─────────────────────────────────────────
***REMOVED*** Step 2: Verify paths
***REMOVED*** ─────────────────────────────────────────
echo "Step 2: Verify configured paths"
echo "-----------------------------------------"

cd "$BACKEND_DIR"
python manage.py verify_paths || {
    echo ""
    echo "   ⚠ Path verification had issues."
    echo "   Update .env file with correct paths for this server."
    echo ""
    echo "   Key paths to configure:"
    echo "     OSMIUM_BINARY_PATH=/path/to/osmium"
    echo "     PLANET_OSM_FILE_PATH=/path/to/planet.osm.pbf"
    echo "     POLYGON_FILES_DIR=/path/to/polygon/files"
    echo "     REGIONAL_EXTRACTIONS_DIR=/path/to/extractions"
    echo ""
    read -p "   Continue anyway? (y/N): " confirm
    [[ "$confirm" != "y" ]] && exit 1
}

echo

***REMOVED*** ─────────────────────────────────────────
***REMOVED*** Step 3: Run database migrations
***REMOVED*** ─────────────────────────────────────────
echo "Step 3: Run database migrations"
echo "-----------------------------------------"

cd "$BACKEND_DIR"

echo "   Migrating default database (port 5432)..."
python manage.py migrate --database=default 2>&1 | tail -5
echo "   ✓ Default database migrated"

echo "   Migrating vectors database (port 5433)..."
python manage.py migrate --database=vectors 2>&1 | tail -5
echo "   ✓ Vectors database migrated"

echo

***REMOVED*** ─────────────────────────────────────────
***REMOVED*** Step 4: Initialize data
***REMOVED*** ─────────────────────────────────────────
echo "Step 4: Initialize data"
echo "-----------------------------------------"

echo "   Downloading polygon files from Geofabrik..."
python manage.py download_poly_files 2>&1 | tail -3
echo "   ✓ Polygon files downloaded"

echo "   Syncing region hierarchy to database..."
python manage.py sync_poly_regions 2>&1 | tail -3
echo "   ✓ Region hierarchy synced"

echo

***REMOVED*** ─────────────────────────────────────────
***REMOVED*** Step 5: Run E2E verification
***REMOVED*** ─────────────────────────────────────────
echo "Step 5: Run E2E verification"
echo "-----------------------------------------"

cd "$PROJECT_ROOT"
bash scripts/e2e_test_suite.sh

echo

***REMOVED*** ─────────────────────────────────────────
***REMOVED*** Complete
***REMOVED*** ─────────────────────────────────────────
echo "========================================="
echo "  ✓ K80 Server Migration Complete!"
echo "========================================="
echo
echo "  Next steps:"
echo "  1. Configure PLANET_OSM_FILE_PATH in .env"
echo "  2. Test extraction: python manage.py extract_region --help"
echo "  3. Test GV-NLE training on sample data"
echo "  4. Start Django server: python manage.py runserver 0.0.0.0:8000"
echo "  5. Start frontend: cd frontend && yarn serve"
echo
echo "  GPU verification:"
echo "  - nvidia-smi (check GPU status)"
echo "  - python -c 'import torch; print(torch.cuda.is_available())'"
echo
echo "  See docs/K80_DEPLOYMENT_CHECKLIST.md for full checklist."
echo "========================================="
