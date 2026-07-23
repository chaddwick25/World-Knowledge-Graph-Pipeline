#!/bin/bash
# =========================================
# EDA Vector Search Toolkit - E2E Test Suite
# =========================================
# Usage: bash scripts/e2e_test_suite.sh
# Run from project root directory
#
# Tests:
#   1. Path verification
#   2. Database connectivity
#   3. Migration status
#   4. Model imports (all 9 apps)
#   5. Service imports
#   6. API endpoint smoke test
#   7. Osmium binary check
#   8. Database record counts

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BACKEND_DIR="$PROJECT_ROOT/backend"

PASS=0
FAIL=0
WARN=0

pass() { echo "   ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "   ✗ $1"; FAIL=$((FAIL + 1)); }
warn() { echo "   ⚠ $1"; WARN=$((WARN + 1)); }

# Detect Poetry or fall back to plain python
if [ -f "$BACKEND_DIR/pyproject.toml" ] && command -v poetry > /dev/null 2>&1; then
    PY_CMD="poetry run python"
else
    PY_CMD="python"
fi

echo "========================================="
echo "  EDA Vector Search Toolkit"
echo "  End-to-End Test Suite"
echo "========================================="
echo "  Date: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  Root: $PROJECT_ROOT"
echo "========================================="
echo

# ─────────────────────────────────────────
# Test 1: Path Verification
# ─────────────────────────────────────────
echo "Test 1: Path Verification"
echo "-----------------------------------------"

cd "$BACKEND_DIR"

# Check osmium binary
OSMIUM_PATH=$(cd "$BACKEND_DIR" && $PY_CMD -c "
import os, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
import django; django.setup()
from django.conf import settings
print(settings.OSMIUM_BINARY_PATH)
" 2>/dev/null | tail -1)

if [ -n "$OSMIUM_PATH" ] && [ -x "$OSMIUM_PATH" ]; then
    pass "Osmium binary: $OSMIUM_PATH"
elif [ -n "$OSMIUM_PATH" ] && [ -f "$OSMIUM_PATH" ]; then
    warn "Osmium binary exists but not executable: $OSMIUM_PATH"
else
    fail "Osmium binary not found: $OSMIUM_PATH"
fi

# Check polygon files directory
POLY_DIR=$(cd "$BACKEND_DIR" && $PY_CMD -c "
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
import django; django.setup()
from django.conf import settings
print(settings.POLYGON_FILES_DIR)
" 2>/dev/null | tail -1)

if [ -d "$POLY_DIR" ]; then
    POLY_COUNT=$(find "$POLY_DIR" -name "*.poly" 2>/dev/null | wc -l)
    pass "Polygon files dir: $POLY_DIR ($POLY_COUNT .poly files)"
else
    warn "Polygon files dir not found: $POLY_DIR (optional for initial setup)"
fi

echo

# ─────────────────────────────────────────
# Test 2: Database Connectivity
# ─────────────────────────────────────────
echo "Test 2: Database Connectivity"
echo "-----------------------------------------"

cd "$BACKEND_DIR"
$PY_CMD -c "
import os, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
import django; django.setup()
from django.db import connections

results = []
for alias in ['default', 'vectors']:
    try:
        conn = connections[alias]
        conn.ensure_connection()
        results.append(f'PASS:{alias}')
    except Exception as e:
        results.append(f'FAIL:{alias}:{e}')

for r in results:
    print(r)
" 2>&1 | grep -E '^(PASS|FAIL):' | while IFS= read -r line; do
    if [[ "$line" == PASS:* ]]; then
        DB_NAME="${line#PASS:}"
        pass "Database '$DB_NAME' connected"
    elif [[ "$line" == FAIL:* ]]; then
        REST="${line#FAIL:}"
        DB_NAME="${REST%%:*}"
        ERROR="${REST#*:}"
        fail "Database '$DB_NAME' failed: $ERROR"
    fi
done

echo

# ─────────────────────────────────────────
# Test 3: Migration Status
# ─────────────────────────────────────────
echo "Test 3: Migration Status"
echo "-----------------------------------------"

cd "$BACKEND_DIR"

# Check default database
UNAPPLIED_DEFAULT=$($PY_CMD manage.py showmigrations --list --database=default 2>/dev/null | grep '\[ \]' | wc -l)
if [ "$UNAPPLIED_DEFAULT" -eq 0 ]; then
    pass "Default DB: All migrations applied"
else
    fail "Default DB: $UNAPPLIED_DEFAULT unapplied migration(s)"
fi

# Check vectors database
UNAPPLIED_VECTORS=$($PY_CMD manage.py showmigrations --list --database=vectors 2>/dev/null | grep '\[ \]' | wc -l)
if [ "$UNAPPLIED_VECTORS" -eq 0 ]; then
    pass "Vectors DB: All migrations applied"
else
    fail "Vectors DB: $UNAPPLIED_VECTORS unapplied migration(s)"
fi

# Check for pending model changes
PENDING=$($PY_CMD manage.py makemigrations --dry-run --check 2>&1 || true)
if echo "$PENDING" | grep -q "No changes detected"; then
    pass "No pending model changes"
else
    warn "Pending model changes detected (run makemigrations)"
fi

echo

# ─────────────────────────────────────────
# Test 4: Model Imports
# ─────────────────────────────────────────
echo "Test 4: Model Imports (all apps)"
echo "-----------------------------------------"

cd "$BACKEND_DIR"
$PY_CMD -c "
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
import django; django.setup()

tests = [
    ('extraction', 'from extraction.models import PbfFile, RegionHierarchy, PbfExtract'),
    ('orchestration', 'from orchestration.models import ProcessingSession, Task'),
    ('analysis', 'from analysis.models import AssetBundle, TemporalSnapshot'),
    ('vectors', 'from vectors.models import OsmEmbedding'),
    ('spatial_semantics', 'from spatial_semantics.models import CensusTract, DriftScore'),
    ('semantic_search', 'from semantic_search.models import ProjectionTrainingPair, ProjectionHeadCheckpoint, EmbeddingComparison, TrainingMetrics, OsmEntity, SpatialLink, SnapshotDiff'),
    ('toronto_data', 'from toronto_data.models import CkanDataset, QualitySnapshot'),
]

for app_name, import_stmt in tests:
    try:
        exec(import_stmt)
        print(f'PASS:{app_name}')
    except Exception as e:
        print(f'FAIL:{app_name}:{e}')
" 2>&1 | grep -E '^(PASS|FAIL):' | while IFS= read -r line; do
    if [[ "$line" == PASS:* ]]; then
        APP="${line#PASS:}"
        pass "Models import: $APP"
    elif [[ "$line" == FAIL:* ]]; then
        REST="${line#FAIL:}"
        APP="${REST%%:*}"
        ERROR="${REST#*:}"
        fail "Models import: $APP - $ERROR"
    fi
done

echo

# ─────────────────────────────────────────
# Test 5: Service Imports
# ─────────────────────────────────────────
echo "Test 5: Service Imports"
echo "-----------------------------------------"

cd "$BACKEND_DIR"
$PY_CMD -c "
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
import django; django.setup()

services = [
    ('extraction.ExtractionService', 'from extraction.services.extraction_service import get_e_core_list'),
    ('extraction.PbfTimestampExtractor', 'from extraction.services.pbf_timestamp_extractor import PbfTimestampExtractor'),
    ('semantic_search.FastTextService', 'from semantic_search.services.fasttext_service import FastTextEmbeddingService'),
    ('semantic_search.DeepWalkService', 'from semantic_search.services.deepwalk_service import WeightedDeepWalkService'),
    ('semantic_search.KNNGraphService', 'from semantic_search.services.knn_graph_service import KNNGraphService'),
    ('semantic_search.InductiveSpatialService', 'from semantic_search.services.inductive_spatial_service import InductiveSpatialService'),
    ('semantic_search.SpatialLinkPredictionService', 'from semantic_search.services.spatial_link_prediction_service import SpatialLinkPredictionService'),
    ('semantic_search.IterativeEntityAlignmentService', 'from semantic_search.services.iterative_entity_alignment_service import IterativeEntityAlignmentService'),
    ('spatial_semantics.TagAnalyzer', 'from spatial_semantics.services.tag_analyzer import TagAnalyzer'),
]

for name, import_stmt in services:
    try:
        exec(import_stmt)
        print(f'PASS:{name}')
    except Exception as e:
        print(f'FAIL:{name}:{e}')
" 2>&1 | grep -E '^(PASS|FAIL):' | while IFS= read -r line; do
    if [[ "$line" == PASS:* ]]; then
        SVC="${line#PASS:}"
        pass "Service: $SVC"
    elif [[ "$line" == FAIL:* ]]; then
        REST="${line#FAIL:}"
        SVC="${REST%%:*}"
        ERROR="${REST#*:}"
        fail "Service: $SVC - $ERROR"
    fi
done

echo

# ─────────────────────────────────────────
# Test 6: API Endpoint Smoke Test
# ─────────────────────────────────────────
echo "Test 6: API Endpoints"
echo "-----------------------------------------"

# Check if server is already running
if curl -s http://localhost:8000/api/ > /dev/null 2>&1; then
    SERVER_RUNNING=true
    pass "Django server already running on port 8000"
else
    SERVER_RUNNING=false
    warn "Django server not running on port 8000 (skipping API tests)"
fi

if $SERVER_RUNNING; then
    # Test key endpoints
    ENDPOINTS=(
        "/api/pbf/"
        "/api/status/initial/"
    )

    for EP in "${ENDPOINTS[@]}"; do
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:8000${EP}" 2>/dev/null)
        if [ "$HTTP_CODE" = "200" ]; then
            pass "GET $EP -> $HTTP_CODE"
        elif [ "$HTTP_CODE" = "405" ]; then
            pass "GET $EP -> $HTTP_CODE (method not allowed, endpoint exists)"
        else
            fail "GET $EP -> $HTTP_CODE"
        fi
    done
fi

echo

# ─────────────────────────────────────────
# Test 7: Osmium Binary
# ─────────────────────────────────────────
echo "Test 7: Osmium Binary"
echo "-----------------------------------------"

if [ -n "$OSMIUM_PATH" ] && [ -x "$OSMIUM_PATH" ]; then
    VERSION=$("$OSMIUM_PATH" --version 2>&1 | head -1)
    pass "Osmium version: $VERSION"
elif command -v osmium > /dev/null 2>&1; then
    VERSION=$(osmium --version 2>&1 | head -1)
    warn "Using system osmium: $VERSION (not settings.OSMIUM_BINARY_PATH)"
else
    fail "No osmium binary available"
fi

echo

# ─────────────────────────────────────────
# Test 8: Database Record Counts
# ─────────────────────────────────────────
echo "Test 8: Database Record Counts"
echo "-----------------------------------------"

cd "$BACKEND_DIR"
$PY_CMD -c "
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
import django; django.setup()

from django.apps import apps

# Default database models
default_models = [
    ('extraction.PbfFile', 'default'),
    ('extraction.RegionHierarchy', 'default'),
    ('orchestration.ProcessingSession', 'default'),
    ('orchestration.Task', 'default'),
    ('analysis.AssetBundle', 'default'),
]

# Vectors database models
vector_models = [
    ('vectors.OsmEmbedding', 'vectors'),
    ('semantic_search.OsmEntity', 'vectors'),
    ('semantic_search.ProjectionTrainingPair', 'vectors'),
    ('semantic_search.ProjectionHeadCheckpoint', 'vectors'),
    ('semantic_search.SpatialLink', 'vectors'),
    ('semantic_search.SnapshotDiff', 'vectors'),
]

for model_path, db in default_models + vector_models:
    try:
        app_label, model_name = model_path.split('.')
        Model = apps.get_model(app_label, model_name)
        count = Model.objects.using(db).count()
        print(f'PASS:{model_path}:{count}:{db}')
    except Exception as e:
        print(f'FAIL:{model_path}:{e}')
" 2>&1 | grep -E '^(PASS|FAIL):' | while IFS= read -r line; do
    if [[ "$line" == PASS:* ]]; then
        REST="${line#PASS:}"
        MODEL="${REST%%:*}"
        REST2="${REST#*:}"
        COUNT="${REST2%%:*}"
        DB="${REST2#*:}"
        pass "$MODEL: $COUNT records ($DB DB)"
    elif [[ "$line" == FAIL:* ]]; then
        REST="${line#FAIL:}"
        MODEL="${REST%%:*}"
        ERROR="${REST#*:}"
        fail "$MODEL: $ERROR"
    fi
done

echo

# ─────────────────────────────────────────
# Summary
# ─────────────────────────────────────────
echo "========================================="
echo "  Test Summary"
echo "========================================="
echo "  Passed:   $PASS"
echo "  Failed:   $FAIL"
echo "  Warnings: $WARN"
echo "========================================="

if [ "$FAIL" -gt 0 ]; then
    echo "  RESULT: ✗ FAILED"
    echo "========================================="
    exit 1
else
    echo "  RESULT: ✓ PASSED"
    echo "========================================="
    exit 0
fi
