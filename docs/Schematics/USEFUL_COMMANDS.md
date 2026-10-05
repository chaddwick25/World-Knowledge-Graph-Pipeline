## Useful Commands
Even thugh I use AI. I still like to run the commands manually to understand what's happening.

# Remove pycache    
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; find . -name "*.pyc" -delete 2>/dev/null; echo "Done - caches cleared"

# Flush redis
redis-cli FLUSHALL 2>/dev/null; echo "Done - Redis flushed"

# Compose Commands
docker compose -f 'docker-compose.yml' -f 'compose.override.yml' build
docker compose -f 'docker-compose.yml' -f 'compose.override.yml' up -d postgres-default postgres-vectors redis
docker compose -f 'docker-compose.yml' -f 'compose.override.yml' up -d backend worker
docker compose logs -f worker

docker compose -f docker-compose.yml -f compose.override.yml run --rm -e NVIDIA_VISIBLE_DEVICES="" --entrypoint="" worker bash -c "source /app/.venv/bin/activate && celery -A pipeline.celery_app worker --loglevel=info --pool=solo --concurrency=1"


# Django Commands
docker compose exec backend python manage.py migrate
docker compose exec backend python manage.py migrate --database=vectors

# Create a superuser (interactive prompt)
docker compose exec backend python manage.py createsuperuser

## Osmium Command
docker compose exec backend which osmium


## Splitting malaysia-singapore-brunei into three separate embeddings
docker compose exec backend python manage.py preprocess_embeddings  # splits workers from hyperparams.yaml embedding_splits.workers
============================================================
Scanning embeddings in: /media/thanos/f5d95b0c-9c60-433c-a76f-3c12175d4f27/OSM/embeddings
Snapshot: 2025_12_31  Dry run: False
============================================================

──────────────────────────────────────────────────
  Malaysia             → READY                ✓ location
  Singapore            → READY                ✓ location
  Brunei               → READY                ✓ location
  US                   → NO_EMBEDDINGS       

──────────────────────────────────────────────────
  NO_EMBEDDINGS: 1
  READY: 3
  Total: 4

DB results: 1 created, 3 updated

## Merging the 5 sharded US embeddings into a single embedding
docker compose exec backend python manage.py shell -c "
from pipeline.tasks.planet_initialization_pipeline_steps.step_0h_prebuild_embeddings import step_0j_prebuild_merge_us_embeddings
from pipeline.config import CountryConfig
import time

cfg = CountryConfig(
    iso='US',
    name='United States',
    slug='us',
    continent='north-america',
    pipeline_run_id=f'us-merge-test-{int(time.time())}'
)
result = step_0j_prebuild_merge_us_embeddings(cfg.to_dict())
print('Result:', result)
"

## Scanning Embeddings -  validates splits and merges from the previous steps
docker compose exec backend python manage.py scan_embeddings


dancing808
**********************************************************************************************

# 1. Backfill (sets snapshot_id on all 3M rows, country_code on 1.1M)
docker compose exec backend python manage.py backfill_partition_keys

# 2. Verify the monolith has partition keys populated
docker compose exec postgres-vectors psql -U vector_user -d vector_db -c "
  SELECT count(*) AS total,
         count(snapshot_id) AS with_snap,
         count(country_code) AS with_cc
  FROM semantic_search_osmentity;
"

# 3. Verify API endpoints still work (the BREAKS fixes are conditional)
curl -s http://localhost:8000/api/semantic-search/gv-tags/ \
  -H "Content-Type: application/json" \
  -d '{"query_tags": {"amenity": "restaurant"}, "top_k": 3}' | python -m json.tool

# 4. Verify the prototype partition still works alongside the monolith
docker compose exec backend python manage.py prototype_physical_sharding --skip-data

**********************************************************************************************
## MapQA Parser — Training, Testing, and Query Execution
**********************************************************************************************

# Retrain the parser (includes natural-language QA pairs augmentation)
# Artifacts are written to backend/data/mapqa_parser/artifacts/
docker compose exec backend python manage.py train_mapqa_parser

# Retrain with custom CSV path or concept models disabled
docker compose exec backend python manage.py train_mapqa_parser --csv-path /path/to/custom.csv
docker compose exec backend python manage.py train_mapqa_parser --no-concept-models

# Run parser unit tests (22 tests — classification, concepts, DAG, multi-entity, edge cases)
docker compose exec backend python -m pytest tests/unit/test_mapqa_parser.py -v

# Run executor integration tests (12 tests — endpoint, executor, plan)
docker compose exec backend python -m pytest tests/integration/test_mapqa_executor.py -v

# Run all MapQA + spectral + community tests together
docker compose exec backend python -m pytest tests/unit/test_mapqa_parser.py tests/integration/test_mapqa_executor.py tests/unit/test_spectral_analysis_service.py tests/unit/test_graph_signal_service.py tests/unit/test_community_detection_service.py -v

# Run the factor-node table tests (16 tests — writer, drift sign alignment,
# diffusion parity vs numpy, amenity command, executor flag on/off)
docker compose exec backend python -m pytest tests/unit/test_factor_node_tables.py -v

# ── Self-supervised training-data generation (docs/plans/completed/MAPQA_TEMPLATE_COVERAGE_EXPANSION_PLAN.md) ──
# Generate PostGIS-verifiable Q&A pairs for templates #1/#2/#4/#5/#8 from
# OsmEntity ground truth in processed countries. Appends to
# {MAPQA_PARSER_DATA_DIR}/training_data/natural_language_qa_pairs.csv
# (idempotent — replaces its own prior (country, snapshot, template) rows).
docker compose exec backend python manage.py generate_mapqa_training_data --country BZ
docker compose exec backend python manage.py generate_mapqa_training_data --country BZ,JM,IE --per-class 250
docker compose exec backend python manage.py generate_mapqa_training_data --all-processed --dry-run
docker compose exec backend python manage.py generate_mapqa_training_data --all-processed --retrain
# Optional LLM paraphrase tier (Ollama via LLMService — off by default)
MAPQA_LLM_AUGMENTATION_ENABLED=true docker compose exec backend python manage.py generate_mapqa_training_data --country BZ --llm

# Generator unit tests (11 tests, needs test DBs with vector + postgis extensions)
docker compose exec backend python -m pytest tests/unit/test_mapqa_question_generator.py -v --reuse-db

# Paraphraser unit tests (16 tests, no DB needed)
docker compose exec backend python -m pytest tests/unit/test_mapqa_paraphraser.py -v --reuse-db

# Quick parser test (no DB needed) — verify all 5 templates classify correctly
docker compose exec backend python manage.py shell -c "
from semantic_search.services.query_parser_service import QueryParserService
QueryParserService.reset_instance()
parser = QueryParserService.get_instance()
tests = [
    'Which bars are within 50m of Hollywood Blvd?',
    'How far is Union Station from downtown LA?',
    'What amenity is available at Union Station?',
    'Which restaurant is nearest to Union Station?',
    'What is west of Union Station?',
    'italian food near a bus station',
]
for q in tests:
    r = parser.parse(q)
    print(f'{r[\"template\"]:40s} conf={r[\"confidence\"]:.3f}  valid={r[\"validation\"][\"valid\"]}  Q: {q[:50]}')
    print(f'  concepts: {[(c[\"type\"], c[\"text\"]) for c in r[\"concepts\"]]}')
"

# Full end-to-end query test via the execute-query endpoint (needs DB + graph artifact)
docker compose exec backend python manage.py shell -c "
from django.test import Client
import json
c = Client(HTTP_HOST='localhost')
tests = [
    'italian food near a bus station',
    'Which bars are within 50m of Belmopan?',
    'How far is Belmopan from Belize City?',
    'Which restaurant is nearest to Belmopan?',
    'What is west of Belmopan?',
]
for q in tests:
    r = c.post('/api/nca/execute-query/',
               data=json.dumps({'query': q, 'country_code': 'BZ', 'snapshot_date': '2025_12_31'}),
               content_type='application/json')
    if r.status_code == 200:
        body = r.json()
        parsed = body['parsed']
        result = body['result']
        print(f'Q: {q}')
        print(f'  Template: {parsed[\"template\"]}  conf={parsed[\"confidence\"]:.3f}')
        print(f'  Answer: {result[\"answer\"]}')
        print(f'  Results: {len(result[\"results\"])}  latency: {result[\"latency_ms\"]}ms')
        print(f'  Trace: {[t[\"step\"] for t in result[\"trace\"]]}')
        print()
    else:
        print(f'Q: {q}  -> HTTP {r.status_code}')
"

**********************************************************************************************
## Graph Endpoints — Spectral, Community, Event Diffusion
**********************************************************************************************

# Spectral query (uses cached fingerprint from Step 5c if available)
curl -s http://localhost:8000/api/nca/spectral-query/ \
  -H "Content-Type: application/json" \
  -d '{"query": "what is the spatial connectivity of this region", "country_code": "BZ", "snapshot_date": "2025_12_31"}' | python -m json.tool

# Community detection query (Louvain on k-NN graph)
curl -s http://localhost:8000/api/nca/community-query/ \
  -H "Content-Type: application/json" \
  -d '{"query": "what communities exist in this region", "country_code": "BZ", "snapshot_date": "2025_12_31"}' | python -m json.tool

# Event diffusion query (heat kernel diffusion from a source entity)
curl -s http://localhost:8000/api/nca/event-diffusion-query/ \
  -H "Content-Type: application/json" \
  -d '{"query": "Belmopan", "country_code": "BZ", "snapshot_date": "2025_12_31"}' | python -m json.tool

# Temporal drift query (requires 2+ snapshots for comparison)
curl -s http://localhost:8000/api/nca/temporal-query/ \
  -H "Content-Type: application/json" \
  -d '{"query": "how has this area changed", "country_code": "BZ", "snapshot_date": "2025_12_31"}' | python -m json.tool

**********************************************************************************************
## k-NN Graph — Build, Serialize, and Inspect (Step 5c artifact)
**********************************************************************************************

# Build the k-NN graph for a country/snapshot and serialize to GraphML
# This is normally done by Step 5c during the pipeline, but can be run manually
docker compose exec backend python manage.py shell -c "
from semantic_search.services.knn_graph_service import KNNGraphService
import networkx as nx, os, time
from django.conf import settings

knn = KNNGraphService()
t0 = time.time()
G = knn.build_graph(country_code='BZ', snapshot_id='2025_12_31')
print(f'Built graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges in {time.time()-t0:.1f}s')

graph_dir = getattr(settings, 'GRAPH_ARTIFACT_DIR')
os.makedirs(graph_dir, exist_ok=True)
path = os.path.join(graph_dir, 'bz_2025_12_31.graphml')
nx.write_graphml(G, path)
print(f'Serialized to {path} ({os.path.getsize(path)} bytes)')
"

# Verify the graph artifact exists on disk
ls -lh backend/data/graph_artifacts/

# Check graph artifact from inside the container
docker compose exec backend ls -lh /app/data/graph_artifacts/

**********************************************************************************************
## USLP Calibration — Hyperparameter Threshold Validation
**********************************************************************************************

# Run USLP threshold calibration (uses worker container for GPU reporting)
# Do NOT change YAML thresholds without explicit approval
docker compose exec worker python manage.py calibrate_uslp_thresholds --country BZ --snapshot-date 2025_12_31

# Run USLP calibration tests
docker compose exec backend python -m pytest tests/unit/test_uslp_threshold_calibration.py -v

**********************************************************************************************
## Database Indexes — Migration 0011 (wkg_class B-tree + tags GIN)
**********************************************************************************************

# Check migration state
docker compose exec backend python manage.py showmigrations worldkg_nca

# Run the index migration (after clean DB reset)
docker compose exec backend python manage.py migrate worldkg_nca

# Verify indexes exist on the parent table
docker compose exec postgres-vectors psql -U vector_user -d vector_db -c "
  SELECT indexname, indexdef
  FROM pg_indexes
  WHERE tablename = 'semantic_search_osmentity'
  ORDER BY indexname;
"

# Verify GIN index on tags is used in query plans
docker compose exec postgres-vectors psql -U vector_user -d vector_db -c "
  EXPLAIN (ANALYZE, BUFFERS)
  SELECT * FROM semantic_search_osmentity
  WHERE tags ? 'amenity'
  LIMIT 10;
"

# Verify B-tree index on wkg_class is used
docker compose exec postgres-vectors psql -U vector_user -d vector_db -c "
  EXPLAIN (ANALYZE, BUFFERS)
  SELECT * FROM semantic_search_osmentity
  WHERE wkg_class = 'wkgs:Amenity'
  LIMIT 10;
"

**********************************************************************************************
## Database Reset and Rebuild
**********************************************************************************************

# Full database destruction. Default keeps migration files (the fresh-DB E2E
# needs the real chain, including semantic_search/0006 + worldkg_nca/0018).
# Pass --wipe-migrations for the nuclear option (deletes migration files).
bash ./scripts/drop_databases_v2.sh
bash ./scripts/drop_databases_v2.sh --wipe-migrations

# Rebuild from scratch (migrations kept, so no makemigrations needed)
docker compose -f 'docker-compose.yml' -f 'compose.override.yml' up -d
docker compose exec backend python manage.py migrate
docker compose exec backend python manage.py migrate --database=vectors

# Manual ontology load (init_planet also runs enrich_worldkg_classes)
docker compose exec backend python manage.py enrich_worldkg_classes --load-ontology ../data/worldkg_ontology_sample.json

# Init planet (runs as Docker entrypoint step, gated by RUN_INIT_PLANET=true;
# rebuilds the ontology cache, parser model, and amenity embeddings)
docker compose exec backend python manage.py init_planet
# Or run a single step:
docker compose exec backend python manage.py init_planet --step register_planet

# Manual post-reset rebuilds (decoupled from init_planet):
docker compose exec backend python manage.py compute_amenity_class_mappings
docker compose exec backend python manage.py romanize_names
# Country leaf partitions for the pipeline:
docker compose exec backend python manage.py create_country_partitions --country XX --mv-only

**********************************************************************************************
## Spectral / Temporal Pipeline Steps (Step 5c / 5d)
**********************************************************************************************

# Run Step 5c (graph spectral analysis) manually for a country
# This builds the k-NN graph, computes spectral features, and serializes the graph to GraphML
docker compose exec backend python manage.py shell -c "
from pipeline.tasks.country_pipeline_steps.step_5c_graph_spectral import step_5c_graph_spectral_analysis
from pipeline.envelopes import CountryEnvelope
import uuid

env = CountryEnvelope(
    iso='BZ',
    name='Belize',
    snapshot_date='2025_12_31',
    pipeline_run_id=str(uuid.uuid4()),
)
step_5c_graph_spectral_analysis.apply(args=[env])
"

# Run Step 5d (temporal drift) manually — requires 2+ snapshots
docker compose exec backend python manage.py shell -c "
from pipeline.tasks.country_pipeline_steps.step_5d_temporal_drift import step_5d_temporal_drift
from pipeline.envelopes import CountryEnvelope
import uuid

env = CountryEnvelope(
    iso='BZ',
    name='Belize',
    snapshot_date='2025_12_31',
    pipeline_run_id=str(uuid.uuid4()),
)
step_5d_temporal_drift.apply(args=[env])
"

# Check if spectral fingerprint exists for a country
docker compose exec backend python manage.py shell -c "
from semantic_search.models import GraphSpectralFingerprint
fps = GraphSpectralFingerprint.objects.filter(region='BZ')
for fp in fps:
    print(f'Region={fp.region} snapshot={fp.snapshot.snapshot_date} lambda2={fp.algebraic_connectivity} gap={fp.spectral_gap} nodes={fp.node_count}')
"

**********************************************************************************************
## Factor-Node Tables — Runtime Joins for MapQA (FACTOR_NODE_RUNTIME_JOINS_PLAN.md)
**********************************************************************************************

# Factor tables live on the vectors DB (models in worldkg_nca, migrations 0012/0013).
# Apply migrations to the vectors DB explicitly:
docker compose exec backend python manage.py migrate --database=vectors

# Verify the tables exist
docker compose exec postgres-vectors psql -U vector_user -d vector_db -c "\dt factor_*"

# Precompute amenity embeddings (MapQA vocab → factor_amenity_embedding, 621 rows)
# Also runs as an init_planet step before `finalize`
docker compose exec backend python manage.py compute_amenity_embeddings
docker compose exec backend python manage.py init_planet --step compute_amenity_embeddings --no-lock

# Inspect factor rows for a country (populated by Step 5c/5d)
docker compose exec postgres-vectors psql -U vector_user -d vector_db -c "
  SELECT country_code, snapshot_id, count(*),
         count(eigen_loadings) AS with_loadings,
         count(louvain_community) AS with_community
  FROM factor_spectral_node_metric
  GROUP BY 1, 2 ORDER BY 1, 2;
"
docker compose exec postgres-vectors psql -U vector_user -d vector_db -c "
  SELECT country_code, snapshot_from_id, snapshot_to_id, count(*)
  FROM factor_drift_node_metric
  GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;
"

# Smoke-test the SQL-only heat-kernel diffusion (factor tables) for a country
# that has completed Step 5c — one pgvector inner-product query per call
docker compose exec backend python manage.py shell -c "
from semantic_search.services.factor_resolution_service import FactorResolutionService
from worldkg_nca.models import SpectralNodeMetric
import time

frs = FactorResolutionService()
anchor = (SpectralNodeMetric.objects.using('vectors')
          .filter(snapshot_id='2025_12_31', country_code='BZ',
                  eigen_loadings__isnull=False)
          .values_list('osm_id', flat=True).first())
print('anchor osm_id:', anchor)
t0 = time.time()
ranked = frs.diffusion_rank(anchor, t=1.0, snapshot_id='2025_12_31',
                            country_code='BZ', limit=10, trace=[])
print(f'top-10 in {(time.time()-t0)*1000:.0f}ms:')
for r in ranked:
    print(f'  {r[\"osm_id\"]:>12}  score={r[\"score\"]:.6f}')
"

# G4 availability check (Spatial-Agent §3.3 — is a factor row executable?)
docker compose exec backend python manage.py shell -c "
from semantic_search.services.factor_resolution_service import FactorResolutionService
print(FactorResolutionService().check_availability([123, 456], '2025_12_31', 'BZ'))
"

# The factor-table path is authoritative and always on. The shadow rollout
# flags (FACTOR_NODE_TABLES_SHADOW / FACTOR_NODE_TABLES_ENABLED) and the
# legacy runtime graph path were removed; FACTOR_NODE_TABLES_ENABLED is a
# dead setting (see docs/issues/FACTOR_NODE_REVIEW_FINDINGS.md F1).

# Run the executor end-to-end (table path)
docker compose exec backend python manage.py shell -c "
from semantic_search.services.query_parser_service import QueryParserService
from semantic_search.services.query_executor_service import QueryExecutorService
parser = QueryParserService.get_instance()
parsed = parser.parse('italian food near a bus station')
r = QueryExecutorService.execute(parsed, country_code='BZ', snapshot_date='2025_12_31', question='italian food near a bus station')
print('answer:', r['answer'])
print('trace steps:', [t['step'] for t in r['trace']])
"

# One-time test-DB setup (pytest --reuse-db persists these):
# pgvector extension must exist in BOTH test databases
docker compose exec postgres-vectors psql -U vector_user -d vector_db -c "CREATE DATABASE test_vector_db OWNER vector_user;"
docker compose exec postgres-vectors psql -U vector_user -d test_vector_db -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS postgis;"
docker compose exec postgres-default psql -U django_user -d test_django_db -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS postgis;"

**********************************************************************************************
## Query Modes — Three Independent Search Endpoints
**********************************************************************************************

# Mode 1: Structured JSON (tags + name) — FastText + romanizer
# Search by OSM tags (semantic type matching)
curl -s -X POST http://localhost:8000/api/nca/semantic-triplet-search/ \
  -H "Content-Type: application/json" \
  -d '{"country_code": "South Korea", "query_tags": {"amenity": "cafe"}, "top_k": 5}' \
  | python3 -m json.tool

# Search by name in any language (romanizer cross-script matching)
curl -s -X POST http://localhost:8000/api/nca/semantic-triplet-search/ \
  -H "Content-Type: application/json" \
  -d '{"country_code": "South Korea", "query_tags": {"name": "파리바게뜨"}, "top_k": 5}' \
  | python3 -m json.tool

# Mode 2: Natural language name search — FastText + romanizer
# Type a name in any language or script (cross-script matching)
curl -s -X POST http://localhost:8000/api/nca/semantic-triplet-search/ \
  -H "Content-Type: application/json" \
  -d '{"country_code": "South Korea", "natural_query": "paris bagueete", "top_k": 5}' \
  | python3 -m json.tool

# Korean same-script substring search
curl -s -X POST http://localhost:8000/api/nca/semantic-triplet-search/ \
  -H "Content-Type: application/json" \
  -d '{"country_code": "South Korea", "natural_query": "원탕", "top_k": 5}' \
  | python3 -m json.tool

# Mode 3: Natural language template (MapQA parser) — parser + executor only
# Type a full geospatial question (separate endpoint, independent of Modes 1-2)
curl -s -X POST http://localhost:8000/api/nca/execute-query/ \
  -H "Content-Type: application/json" \
  -d '{"query": "Which bars are within 50m of Hollywood Blvd?", "country_code": "US"}' \
  | python3 -m json.tool

# Parser-only test (see template classification without execution)
curl -s -X POST http://localhost:8000/api/nca/execute-query/ \
  -H "Content-Type: application/json" \
  -d '{"query": "How far is Union Station from downtown LA?", "country_code": "US"}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Template: {d[\"parsed\"][\"template\"]}  conf={d[\"parsed\"][\"confidence\"]:.3f}'); print(f'Concepts: {[(c[\"type\"],c[\"text\"]) for c in d[\"parsed\"][\"concepts\"]]}')"

**********************************************************************************************
## Name Romanizing — Cross-Script Name Preprocessing (NAME_ROMANIZING_FRAMEWORK_PLAN.md)
**********************************************************************************************

# Romanize ALL entities across all countries (idempotent, resumable)
docker compose -f docker-compose.yml -f compose.override.yml exec backend python manage.py romanize_names

# Romanize one country (e.g., South Korea — ~4 min at ~4,400 entities/sec)
docker compose -f docker-compose.yml -f compose.override.yml exec backend python manage.py romanize_names --country-code KR

# Dry run (see samples without writing to DB)
docker compose -f docker-compose.yml -f compose.override.yml exec backend python manage.py romanize_names --country-code KR --dry-run

# Test with a small batch first
docker compose -f docker-compose.yml -f compose.override.yml exec backend python manage.py romanize_names --country-code KR --limit 1000

# Custom batch size (larger = faster for big countries)
docker compose -f docker-compose.yml -f compose.override.yml exec backend python manage.py romanize_names --country-code KR --batch-size 10000

# Verify romanized names in the DB
docker compose exec postgres-vectors psql -U vector_user -d vector_db -c "
  SELECT tags->>'name' AS name, name_romanized
  FROM semantic_search_osmentity
  WHERE name_romanized IS NOT NULL AND name_romanized != '' AND country_code='KR'
  LIMIT 10;
"

# Check romanization progress (how many entities still need processing)
docker compose exec postgres-vectors psql -U vector_user -d vector_db -c "
  SELECT country_code,
         COUNT(*) FILTER (WHERE name_romanized IS NULL) AS pending,
         COUNT(*) FILTER (WHERE name_romanized IS NOT NULL) AS done
  FROM semantic_search_osmentity
  WHERE tags ? 'name'
  GROUP BY country_code ORDER BY pending DESC;
"

# Run the romanizer unit tests (45 tests)
docker compose exec backend python -m pytest tests/unit/test_romanizers.py -v

# Quick registry test (no DB needed)
docker compose exec backend python manage.py shell -c "
from semantic_search.services.romanizing_names.registry import RomanizerRegistry
print('Supported:', RomanizerRegistry.list_supported())
print('Korean:  파리바게뜨 →', repr(RomanizerRegistry.auto_romanize('파리바게뜨')))
print('French:  café →', repr(RomanizerRegistry.auto_romanize('café')))
print('Spanish: Señor Plaza →', repr(RomanizerRegistry.auto_romanize('Señor Plaza')))
print('Irish:   Banc na hÉireann →', repr(RomanizerRegistry.auto_romanize('Banc na hÉireann')))
print('English: Paris Baguette →', repr(RomanizerRegistry.auto_romanize('Paris Baguette')))
"

# Test cross-script search (Korean entity matching English misspelling)
curl -s -X POST http://localhost:8000/api/nca/semantic-triplet-search/ \
  -H "Content-Type: application/json" \
  -d '{"country_code": "South Korea", "natural_query": "paris bagueete", "top_k": 5}' \
  | python3 -c "import sys,json; [print(r['tags']['name']) for r in json.load(sys.stdin)['results']]"

# Test Korean name query via query_tags
curl -s -X POST http://localhost:8000/api/nca/semantic-triplet-search/ \
  -H "Content-Type: application/json" \
  -d '{"country_code": "South Korea", "query_tags": {"name": "파리바게뜨"}, "top_k": 5}' \
  | python3 -c "import sys,json; [print(r['tags']['name']) for r in json.load(sys.stdin)['results']]"

# Test Korean same-script substring search
curl -s -X POST http://localhost:8000/api/nca/semantic-triplet-search/ \
  -H "Content-Type: application/json" \
  -d '{"country_code": "South Korea", "natural_query": "원탕", "top_k": 5}' \
  | python3 -c "import sys,json; [print(r['tags']['name']) for r in json.load(sys.stdin)['results']]"

**********************************************************************************************
## MCP Agent Stack — Ollama + Goose (docs/Schematics/08_Agent_MCP_LLM/04_Docker_Agent_Stack.md)
**********************************************************************************************

# Start the LLM server and pull the model (one-time ~5.2 GB)
docker compose up -d ollama
docker compose exec ollama ollama pull qwen3:8b

# Run the agent — interactive TUI
docker compose run --rm goose session

# Run the agent — headless single task
docker compose run --rm goose run -t "Which cafes are within 50km of Belize City?"

# Goose config lives in goose/config/config.yaml (mounted as a DIRECTORY —
# goose rewrites it on session start; a file bind-mount fails with EBUSY).
# Agent instructions: goose/worldkg-recipe.yaml

# Check what GPUs each container sees (app = 4070 Ti Super, worker = RTX 2070)
docker compose exec ollama nvidia-smi -L
docker compose exec worker nvidia-smi -L

**********************************************************************************************
## MCP Tool Surface — 9 tools at /__mcp
**********************************************************************************************

# MCP wire smoke test (start a second Vite on 5174 first, then):
cd frontend-v3 && npm run dev -- --port 5174 --strictPort
node frontend-v3/scripts/mcp-smoke-test.mjs 5174

# Have goose list the tools it sees (via the worldkg-mcp extension)
docker compose run --rm goose run -t "List the MCP tools available on the worldkg-mcp server and report their names."

# MVP demo page — open in the browser
# http://localhost:5173/agent

**********************************************************************************************
## SSE Streaming — execute-query/stream
**********************************************************************************************

# Stream the full pipeline as events (parsed → executed → research →
# research_out → answer_delta → done). Requires the SSE Accept header.
curl -sN -H "Accept: text/event-stream" \
  "http://localhost:8000/api/nca/execute-query/stream/?query=Which%20cafes%20are%20within%2050km%20of%20Belize%20City%3F&country_code=Belize"

# G4 factor-table coverage check (which latent spaces are populated)
curl -s "http://localhost:8000/api/nca/factor-availability/?country_code=JM"

**********************************************************************************************
## Platform LLM — Warm-up, Enrichment, Caches
**********************************************************************************************

# Load the model into VRAM at container create (entrypoint hook). NOTE:
# `docker compose restart` does NOT re-run the entrypoint — use force-recreate.
docker compose exec backend python manage.py warm_llm
docker compose up -d --force-recreate backend

# LLM config (env): LLM_ENABLED, LLM_BASE_URL (http://ollama:11434/v1),
# LLM_MODEL (qwen3:8b), LLM_API_STYLE (native=Ollama /api/chat think:false,
# openai=/v1 for cloud swap), LLM_TIMEOUT (60s), LLM_ANSWER_CACHE_TTL_SECONDS (1800s)
docker compose exec backend printenv LLM_BASE_URL LLM_MODEL LLM_API_STYLE

# Enriched-answer cache (in-process, cleared on dev-server reload / recreate):
# repeated questions replay without LLM calls (~12s → ~2.5s)
time curl -s -X POST http://localhost:8000/api/nca/execute-query/ \
  -H "Content-Type: application/json" \
  -d '{"query": "Which cafes are within 50km of Belize City?", "country_code": "Belize"}' > /dev/null
time curl -s -X POST http://localhost:8000/api/nca/execute-query/ \
  -H "Content-Type: application/json" \
  -d '{"query": "Which cafes are within 50km of Belize City?", "country_code": "Belize"}' > /dev/null

# Snapshot cache (SNAPSHOT_ID_CACHE_TTL_SECONDS=120): the executor hot path.
# Call clear_snapshot_cache() after a pipeline backfills a new snapshot.
docker compose exec backend python manage.py shell -c "
import time
from worldkg_nca.snapshot_utils import get_latest_snapshot_id, clear_snapshot_cache
clear_snapshot_cache()
t0 = time.monotonic(); get_latest_snapshot_id()
print(f'cold: {(time.monotonic()-t0)*1000:.0f}ms')
t0 = time.monotonic(); get_latest_snapshot_id()
print(f'cached: {(time.monotonic()-t0)*1000:.0f}ms')
"

# Spatial geography expression index (GIST on geom::geography) — makes
# ST_DWithin(geom::geography, ...) an Index Scan instead of a partition scan
docker compose exec postgres-vectors psql -U vector_user -d vector_db -c "
  SELECT indexname FROM pg_indexes WHERE indexname='idx_osmentity_geom_geog';
"

# LLM / enrichment / snapshot-cache unit tests (all mocked — no Ollama needed)
docker compose exec backend python -m pytest tests/unit/test_llm_service.py \
  tests/unit/test_query_enrichment_service.py tests/unit/test_snapshot_cache.py -v

**********************************************************************************************
## Verification — System Checks and Syntax Validation
**********************************************************************************************

# Django system check
docker compose exec backend python manage.py check

# Migration consistency check (should report no changes)
docker compose exec backend python manage.py makemigrations --check --dry-run

# Syntax check all modified Python files
cd backend && python -c "
import ast, glob
for f in glob.glob('**/*.py', recursive=True):
    if '/migrations/' in f: continue
    with open(f) as fh: ast.parse(fh.read())
print('OK')
"

# Stale import check (should return nothing)
grep -r "from extraction\|import extraction\|from orchestration\|import orchestration" backend/ --include="*.py" | grep -v "/migrations/"
grep -r "from analysis\|import analysis" backend/ --include="*.py" | grep -v "/migrations/"
grep -r "TemporalSnapshot\|AssetBundle" backend/ --include="*.py" | grep -v "/migrations/"
**********************************************************************************************
## Self-Hosted Deployment — nginx, Funnel, Login Guard (SELF_HOSTED_DEPLOYMENT_PLAN.md)
**********************************************************************************************

# Rebuild the SPA with the same-origin API base (dist is mounted into nginx, no restart needed)
cd frontend-v3 && VITE_API_BASE_URL=/api npm run build

# nginx service (serves the SPA, proxies /api + /ws to backend:8000)
docker compose up -d nginx
docker compose logs -f nginx

# Tailscale Funnel — targets nginx :80 (NOT backend :8000)
tailscale funnel status
sudo tailscale funnel --bg 80            # public https://thanos.tail560528.ts.net -> :80
sudo tailscale funnel --https=443 off    # stop exposing

# .env changes need a RECREATE, not a restart (restart keeps the old env)
docker compose up -d --force-recreate backend

# ── Auth guard smoke tests (simulate the public host locally) ──
# Guarded endpoint, no session -> 401
curl -s -H "Host: thanos.tail560528.ts.net" http://localhost/api/system/summary/
# Login flow: csrf cookie -> login -> session -> me
curl -s -c /tmp/cj.txt http://localhost/api/auth/csrf/
TOKEN=$(grep csrftoken /tmp/cj.txt | awk '{print $7}')
curl -s -b /tmp/cj.txt -c /tmp/cj.txt -H "X-CSRFToken: $TOKEN" -H "Content-Type: application/json" \
  -d '{"username":"testfriend","password":"testpass123"}' http://localhost/api/auth/login/
SID=$(grep sessionid /tmp/cj.txt | awk '{print $7}')
curl -s -H "Cookie: sessionid=$SID" -H "Host: thanos.tail560528.ts.net" http://localhost/api/auth/me/
# Guarded endpoint WITH session -> 200
curl -s -o /dev/null -w "%{http_code}\n" -H "Cookie: sessionid=$SID" -H "Host: thanos.tail560528.ts.net" http://localhost/api/system/summary/

# Registration (gated by SIGNUP_INVITE_CODE in .env)
curl -s -b /tmp/cj.txt -H "X-CSRFToken: $TOKEN" -H "Content-Type: application/json" \
  -d '{"invite_code":"<code>","username":"newfriend","password":"longenough123"}' http://localhost/api/auth/register/

# WebSocket guard: public + no session -> 403, public + session -> 101
curl -s -m 3 -i -N -H "Host: thanos.tail560528.ts.net" -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  http://localhost/ws/pipeline/12345678-1234-1234-1234-123456789abc/ | head -1
curl -s -m 3 -i -N -H "Host: thanos.tail560528.ts.net" -H "Cookie: sessionid=$SID" \
  -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  http://localhost/ws/pipeline/12345678-1234-1234-1234-123456789abc/ | head -1

# Admin gate: public host -> 404, localhost -> login redirect
curl -s -o /dev/null -w "%{http_code}\n" -H "Host: thanos.tail560528.ts.net" http://localhost/admin/
curl -s -o /dev/null -w "%{http_code}\n" http://localhost/admin/

# Create a test account from the shell
docker compose exec backend python manage.py shell -c "
from django.contrib.auth.models import User
u, _ = User.objects.get_or_create(username='testfriend')
u.set_password('testpass123'); u.save(); print('ok')
"
