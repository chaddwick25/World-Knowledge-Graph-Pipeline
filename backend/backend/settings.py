from pathlib import Path
import os
from dotenv import load_dotenv
import dj_database_url

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env file in the project root
PROJECT_ROOT = BASE_DIR.parent
load_dotenv(os.path.join(PROJECT_ROOT, '.env'))

# Add the project root to the Python path to allow imports from the root
import sys
sys.path.insert(0, str(PROJECT_ROOT))

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/4.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
# Set DJANGO_SECRET_KEY in production; the insecure value is a dev default.
SECRET_KEY = os.getenv(
    'DJANGO_SECRET_KEY',
    'django-insecure-i%)wb*%s81#y-rrf!fw9v$quoo0!&@)-2f0o9os!b*i&53xtqj',
)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True
# Deployed backend hosts (Tailscale funnel / custom domain), set on the
# homeserver env. Without this, Django answers 400 DisallowedHost for the
# funnel hostname.
_extra_hosts = [h.strip() for h in os.getenv('ALLOWED_EXTRA_HOSTS', '').split(',') if h.strip()]
ALLOWED_HOSTS = ['localhost', '127.0.0.1', *_extra_hosts]

# ============================================================================
# FILE PATHS CONFIGURATIONS + Overrides
# ============================================================================
# BASE DATA DIRECTORY (HOT Storage)
BASE_DATA_DIR = os.getenv('BASE_DATA_DIR')

# OSM Planet File Path, this is the groundtruth for the WorldKG Pipeline.
# Derived from BASE_DATA_DIR — the history PBF is the file the pipeline
# actually consumes (was env-overridable pre-2026-09-25; compose and .env
# both pointed here).
_default_planet_osm = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/planet/history-260209.osm.pbf') if BASE_DATA_DIR else None
PLANET_OSM_FILE_PATH = _default_planet_osm

# OSM Polygon Files Directory, this is the directory where the polygon files are stored
_default_polygon_files_dir = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/osm_polygon_files') if BASE_DATA_DIR else None
POLYGON_FILES_DIR = _default_polygon_files_dir

# Optional legacy polygon files directory (e.g., pre-existing tree with subgraphs)
_default_legacy_polygon_files_dir = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/osm_polygon_files_old') if BASE_DATA_DIR else None
LEGACY_POLYGON_FILES_DIR = _default_legacy_polygon_files_dir

# Geofabrik index URL used for polygon discovery
GEOFABRIK_INDEX_URL = 'https://download.geofabrik.de/index-v1.json'

# OSM Wikidata Extractions Directory, this is the directory where the wikidata extractions are stored
_default_osm_wikidata_extractions = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/osm_wikidata_extractions') if BASE_DATA_DIR else None
OSM_WIKIDATA_EXTRACTIONS_DIR = _default_osm_wikidata_extractions

# Analysis Service Paths
_default_asset_bundles_dir = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/filtered_snapshots') if BASE_DATA_DIR else None
ASSET_BUNDLES_DIR = _default_asset_bundles_dir

_default_pbf_cache_dir = os.path.join(BASE_DATA_DIR, 'pbf_cache') if BASE_DATA_DIR else None
PBF_CACHE_DIR = _default_pbf_cache_dir

_default_preprocessed_dir = os.path.join(BASE_DATA_DIR, 'preprocessed') if BASE_DATA_DIR else None
PREPROCESSED_DIR = _default_preprocessed_dir

# OSM Wikidata Extractions for Continents
_default_continents_output_dir = os.path.join(OSM_WIKIDATA_EXTRACTIONS_DIR, 'continents') if OSM_WIKIDATA_EXTRACTIONS_DIR else None
OSM_CONTINENTS_OUTPUT_DIR = _default_continents_output_dir

_default_osm_wikidata_temp_dir = os.path.join(OSM_WIKIDATA_EXTRACTIONS_DIR, 'temp') if OSM_WIKIDATA_EXTRACTIONS_DIR else None
OSM_WIKIDATA_TEMP_DIR = _default_osm_wikidata_temp_dir



# FastText Model Configuration
_default_fasttext_model_path = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/models/fasttext/cc.en.300.bin') if BASE_DATA_DIR else None
FASTTEXT_MODEL_PATH = _default_fasttext_model_path

_default_fasttext_tuned_dir = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/models/fasttext_tuned') if BASE_DATA_DIR else None
FASTTEXT_TUNED_DIR = _default_fasttext_tuned_dir

# Redis configuration for Channels, Celery, and WorldKG services
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', '6379'))
WORLDKG_REDIS_DB = int(os.getenv('WORLDKG_REDIS_DB', '2'))

# WebSocket host/port for pipeline progress URLs (returned to frontend)
WS_HOST = os.getenv('WS_HOST', 'localhost')
WS_PORT = os.getenv('WS_PORT', '8000')

# Single Snapshot Configuration for Temporal Pipeline
# Used by Vue-triggered preprocessing to generate only one snapshot per country
SINGLE_SNAPSHOT_DATE = '2025_12_31'
# Derive year from date (e.g., '2025_12_31' -> 2025)
SINGLE_SNAPSHOT_YEAR = int(SINGLE_SNAPSHOT_DATE.split('_')[0])

# Osmium Tool Configuration
OSMIUM_BINARY_PATH = os.getenv(
    'OSMIUM_BINARY_PATH',
    str(Path(PROJECT_ROOT) / 'osmium-tool' / 'build' / 'src' / 'osmium')
)
OSMIUM_EXECUTABLE = os.getenv(
    'OSMIUM_EXECUTABLE',
    OSMIUM_BINARY_PATH
)

# ============================================================================
# PRE-FLIGHT ENTROPY GATING (WorldKG Pipeline)
# ============================================================================
# Skip full pipeline if Shannon entropy of WorldKG class distribution is below this.
# Low entropy = few dominant classes = semantically homogeneous region.
MIN_PREFLIGHT_SHANNON_ENTROPY = 1.5

# Skip full pipeline if entropy delta vs previous fingerprint is below this.
# Low delta = no meaningful semantic change since last run.
MIN_PREFLIGHT_ENTROPY_DELTA = 0.2

# ============================================================================
# GOOGLE PLACES VALIDATION (Stage 10 — Amplification Layer)
# ============================================================================
GOOGLE_VALIDATION_ENABLED = False
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY', None)

# ============================================================================
# COLD STORAGE DATA DIRECTORY
# ============================================================================
COLD_STORAGE_BASE_DIR = os.getenv('COLD_STORAGE_BASE_DIR')

# OSM corpus used for fine-tuning and retraining.
# Repo-local corpus (backend/data/corpus) — matches the former
# CORPUS_DIR=.env value; derived, not env-overridable.
_default_corpus_dir = os.path.join(BASE_DIR, 'data', 'corpus')
CORPUS_DIR = _default_corpus_dir

_default_graph_assets_dir = os.path.join(COLD_STORAGE_BASE_DIR, 'graph_assets') if COLD_STORAGE_BASE_DIR else None
GRAPH_ASSETS_DIR = _default_graph_assets_dir

# GeoVectors Embeddings Root (for TSV files used in pickle generation)
# This is the primary source directory for the scan_embeddings command.
_default_embeddings_dir = os.path.join(COLD_STORAGE_BASE_DIR, 'embeddings') if COLD_STORAGE_BASE_DIR else None
EMBEDDINGS_ROOT = _default_embeddings_dir

# Fallback embeddings root for systems where embeddings live elsewhere
# (e.g. on a separate mount). The scan_embeddings command checks this
# before falling back to EMBEDDINGS_ROOT. Machine-specific opt-in — env-only.
_extra_embeddings_dir = os.getenv('EXTRA_EMBEDDINGS_ROOT', None)
EXTRA_EMBEDDINGS_ROOT = _extra_embeddings_dir

# Country overrides JSON path (cold storage) — now also contains embedding_splits section
_default_overrides_json = os.path.join(COLD_STORAGE_BASE_DIR, 'overrides.json') if COLD_STORAGE_BASE_DIR else None
OVERRIDES_JSON_PATH = _default_overrides_json

_default_downloads_dir = os.path.join(BASE_DATA_DIR, 'downloads') if BASE_DATA_DIR else str(BASE_DIR / 'downloads')
DOWNLOADS_DIR = _default_downloads_dir

# WorldKG ontology TTL path (lives under the OSM-PBF-FILES mount, matching
# the former WORLDKG_ONTOLOGY_PATH=.env / compose values)
_default_worldkg_ontology = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/world_kg_ontology/WorldKG_Ontolgy.ttl') if BASE_DATA_DIR else None
WORLDKG_ONTOLOGY_PATH = _default_worldkg_ontology

# Continents root directory (for continent PBF files) — same directory as
# OSM_CONTINENTS_OUTPUT_DIR (continents extractions); matches the compose
# override that was previously required.
_default_continents_root = os.path.join(OSM_WIKIDATA_EXTRACTIONS_DIR, 'continents') if OSM_WIKIDATA_EXTRACTIONS_DIR else None
CONTINENTS_ROOT = _default_continents_root

# Pipeline logs directory (categorized run logs: gv-nle, pipeline, tests, uslp)
_default_logs_dir = os.path.join(BASE_DATA_DIR, 'logs') if BASE_DATA_DIR else None
LOGS_DIR = _default_logs_dir

# Wikidata candidate cache directory (JSON caches per country)
_default_wikidata_cache_dir = os.path.join(BASE_DATA_DIR, 'data/wikidata_cache') if BASE_DATA_DIR else None
WIKIDATA_CACHE_DIR = _default_wikidata_cache_dir

# MapQA parser data directory — training data + model artifacts.
# Layout (see docs/plans/MAPQA_PARSER_BUILD_ORDER.md §8):
#   {MAPQA_PARSER_DATA_DIR}/raw/           — MapQA dataset (mounted from host via MAPQA_DATASET_DIR)
#   {MAPQA_PARSER_DATA_DIR}/training_data/ — mapqa_template_mapping.csv (generated)
#   {MAPQA_PARSER_DATA_DIR}/artifacts/     — vectorizer.pkl, classifier.pkl, etc.
# Env-driven: defaults to BASE_DATA_DIR/mapqa_parser (the /app/data mount in Docker).
_default_mapqa_parser_dir = os.path.join(BASE_DATA_DIR, 'mapqa_parser') if BASE_DATA_DIR else None
MAPQA_PARSER_DATA_DIR = _default_mapqa_parser_dir

# k-NN graph artifact directory — serialized graphs from Step 5c.
# Layout:
#   {GRAPH_ARTIFACT_DIR}/{country_code}_{snapshot_id}.graphml
# These are loaded at query time by QueryExecutorService for graph-based
# operators (heat kernel diffusion, Dijkstra, BFS) per the Spatial-Agent
# paper's GeoFlow Graph execution model.
_default_graph_artifact_dir = os.path.join(BASE_DIR, 'data', 'graph_artifacts')
GRAPH_ARTIFACT_DIR = _default_graph_artifact_dir

# Step 5c run report directory — JSON reports capturing solver selection,
# timing, graph dimensions, eigenvalue quality, and factor-row write status.
# Layout:
#   {SPECTRAL_REPORT_DIR}/step5c_{country}_{snapshot}_{run_id}.json
_default_spectral_report_dir = os.path.join(BASE_DIR, 'data', 'spectral_reports')
SPECTRAL_REPORT_DIR = _default_spectral_report_dir

# Factor-node tables (docs/plans/FACTOR_NODE_RUNTIME_JOINS_PLAN.md).
# The MapQA executor resolves SUPPORT/factor nodes via SQL joins against
# the factor_* tables (written by Steps 5c/5d) instead of loading GraphML
# + NetworkX + scipy at request time.  The legacy runtime graph path has
# been removed; the table path is authoritative.  PostGIS remains as a
# spatial fallback for templates without factor-table coverage.
FACTOR_NODE_TABLES_ENABLED = True

# Eigenbasis coherence (GRAPH_SPECTRAL_FEEDBACK_HARDENING_PLAN Phase 1):
# False (default) = lenient — NULL fingerprint_id (pre-migration rows) still
# resolves with a trace note. True = strict — NULL is treated as a mismatch,
# forcing the PostGIS fallback during the transition window.
FACTOR_EIGENBASIS_STRICT = False

# USLP (Unsupervised Spatial Link Prediction) Configuration
# USLP_THRESHOLD lives in hyperparams.yaml (uslp.threshold); the value below
# is the code fallback when the YAML key is absent. USLP_USE_GPU /
# USLP_GPU_DEVICE stay env-driven (per deployment / per country tuning).
# The scale limits are code constants.
USLP_THRESHOLD = 0.7
USLP_TOP_K = 50
USLP_LIMIT = 1000000000
USLP_MAX_HEADS = 1000000000
# USLP_USE_GPU: 'auto' (default), 'true', or 'false'
USLP_USE_GPU = os.getenv('USLP_USE_GPU', 'true').lower()
USLP_GPU_DEVICE = os.getenv('USLP_GPU_DEVICE', 'cuda:0')
USLP_USE_FP64 = False

CELERY_BROKER_URL = f"redis://{os.getenv('REDIS_HOST', 'localhost')}:{REDIS_PORT}/0"

# ── Celery Result Backend ──
# Custom Django DB backend with chord-in-chain fix.
#
# django-celery-results 2.0.0's DatabaseBackend has a bug where
# ChordCounter records are not reliably created for chords embedded in a
# chain (Step 5 NLE chord). This caused on_chord_part_return to raise
# ChordCounter.DoesNotExist, marking tasks as FAILURE and triggering
# re-delivery — each subgraph was trained 2-7x instead of once (Norway:
# 9h47m instead of ~4h50m).
#
# Our PatchedDatabaseBackend (pipeline/celery_results_backend.py) fixes
# this by replacing ChordCounter with the standard Celery
# fallback_chord_unlock() polling task. TaskResult records are still
# written to Django's DB, so /api/snapshot-jobs/<country>/<date>/results/
# works.
#
# See: docs/issues/CHORDCOUNTER_DOES_NOT_EXIST_BUG.md
CELERY_RESULT_BACKEND = os.environ.get(
    "CELERY_RESULT_BACKEND",
    "pipeline.celery_results_backend:PatchedDatabaseBackend",
)
# Results expire after 48 hours (matches longest pipeline run + buffer)
CELERY_RESULT_EXPIRES = 60 * 60 * 48  # 48 hours

# Task/result serialization — must be consistent across broker + backend
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]

# Ack tasks on completion (not receipt) so they survive worker death/crash.
# Without this, a worker freeze/kill silently loses the in-flight task —
# Celery never re-delivers it.  This caused Step 6 to vanish during the
# 2026-08-21 IE freeze, requiring manual re-dispatch.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1


# Deployed frontend origins (Netlify domain), set on the homeserver env via
# CORS_EXTRA_ORIGINS; the literals below are the local dev defaults.
_extra_origins = [o.strip() for o in os.getenv('CORS_EXTRA_ORIGINS', '').split(',') if o.strip()]
CORS_ALLOWED_ORIGINS = [
    'http://localhost:8080',
    'http://127.0.0.1:8080',
    'http://localhost:8081',
    'http://127.0.0.1:8081',
    'http://localhost:5173',
    'http://127.0.0.1:5173',
    *_extra_origins,
]

CORS_ALLOW_HEADERS = [
    'content-type',
    'x-csrftoken',
    'x-requested-with',
]

CORS_ALLOW_CREDENTIALS = True

# Behind the Tailscale Funnel + nginx: TLS terminates upstream, so tell
# Django to trust the X-Forwarded-Proto header (nginx sets it from $scheme,
# overriding any client-supplied value). Without this, request.is_secure()
# is False on public requests and Django's CSRF Origin check rejects the
# browser's https:// Origin on every unsafe request ("Origin checking
# failed").
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# CSRF Trusted Origins (required for Django 4.0+) — same origin set as CORS.
CSRF_TRUSTED_ORIGINS = [
    'http://localhost:8080',
    'http://127.0.0.1:8080',
    'http://localhost:8081',
    'http://127.0.0.1:8081',
    'http://localhost:5173',
    'http://127.0.0.1:5173',
    *_extra_origins,
]

# REST Framework Configuration
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.AllowAny',
    ],
    'UNAUTHENTICATED_USER': None,
}


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.gis',
    'django_celery_results',
    'rest_framework',
    'corsheaders',
    'core',
    'osmsnapshot',
    'api',
    'backend',
    'geodata',
    'semantic_search',
    'worldkg_nca',
    'igea',
    'channels',
    'geovectors_encoder',
    'pipeline',
]

# Django Channels
ASGI_APPLICATION = 'backend.asgi.application'
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            'hosts': [(os.getenv('REDIS_HOST', '127.0.0.1'), REDIS_PORT)],
        },
    },
}

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'backend.middleware.AdminHostGateMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'backend.middleware.PublicAuthGuardMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'backend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'backend.wsgi.application'

# WorldKG Pipeline snapshot date range defaults
WORLDKG_SNAPSHOT_START_YEAR = 2021
WORLDKG_SNAPSHOT_END_YEAR = 2025
# Short aliases used by SnapshotDatesView and SnapshotJobStatusView
# (TEMPORAL_SNAPSHOT_REFACTOR.md Phase C). Fall back to the WORLDKG_* names.
SNAPSHOT_START_YEAR = WORLDKG_SNAPSHOT_START_YEAR
SNAPSHOT_END_YEAR = WORLDKG_SNAPSHOT_END_YEAR

# Database
# https://docs.djangoproject.com/en/4.2/ref/settings/#databases

DATABASES = {
    'default': dj_database_url.config(
        default=(
            f"postgres://{os.getenv('POSTGRES_USER')}:{os.getenv('POSTGRES_PASSWORD')}@"
            f"{os.getenv('POSTGRES_HOST', 'localhost')}:5432/{os.getenv('POSTGRES_DB')}"
        ),
        conn_max_age=600,
        engine='django.contrib.gis.db.backends.postgis',
    ),
    'vectors': {
        'ENGINE': 'django.contrib.gis.db.backends.postgis',
        'NAME': os.getenv('PGVECTOR_DB'),
        'USER': os.getenv('PGVECTOR_USER'),
        'PASSWORD': os.getenv('PGVECTOR_PASSWORD'),
        'HOST': os.getenv('PGVECTOR_HOST', 'localhost'),
        'PORT': '5432',
        'CONN_MAX_AGE': 600,
        'OPTIONS': {
            'connect_timeout': 30,
            'options': '-c jit=off',
        },
    }
}

DATABASE_ROUTERS = [
    'backend.database_router.ShardRouter',
    'backend.database_router.AppRouter',
    'backend.database_router.VectorDBRouter',
]

# MapQA self-supervised training-data generation
# (docs/plans/completed/MAPQA_TEMPLATE_COVERAGE_EXPANSION_PLAN.md §4 — paraphrase tiers).
# The rule-based paraphrase tier is always on; this env var gates the optional
# LLM tier (Ollama via LLMService — no external API). Default off: generation
# must be deterministic and offline-safe.
MAPQA_LLM_AUGMENTATION_ENABLED = False


# Password validation
# https://docs.djangoproject.com/en/4.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/4.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.2/howto/static-files/

STATIC_URL = 'static/'

# Default primary key field type
# https://docs.djangoproject.com/en/4.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Geodata Configuration (replaces the legacy TORONTO_CKAN_CONFIG /
# TORONTO_DATA_DIR / TORONTO_BULK_INSERT_BATCH_SIZE settings)
GEODATA_CONFIG = {
    # Download dir lives under BASE_DIR/data (resolves to /app/data in
    # containers — see docs/.devin rules §2.7).
    'download_dir': os.getenv('GEODATA_DIR', os.path.join(BASE_DIR, 'data', 'geodata')),
    'bulk_insert_batch_size': 1000,
    'default_ssl_verify': False,
    'default_timeout': 30,
}

# Temporal Snapshot Configuration
# Default maximum temporal range for PBF files without explicit max_timestamp
# Set this to your planet file's last update date
from datetime import datetime
from django.utils import timezone

DEFAULT_TEMPORAL_RANGE_END = datetime(2024, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

# Spatial Semantics Configuration
SPATIAL_SEMANTICS_CONFIG = {
    'fasttext_model_path': FASTTEXT_MODEL_PATH,
    'drift_threshold': 0.10,
    'target_amenities': ['cafe', 'restaurant', 'fast_food', 'bar', 'pub'],
    'hnsw_m': 16,
    'hnsw_ef_construction': 64,
    # ── Name-search noise filtering ──────────────────────────────────────
    # OSM tag keys that assert an entity's type/identity.  Entities without
    # ANY of these keys are treated as noise in name-based search (e.g. a
    # node with only {"name": "벤치"} — a bench whose "name" is literally
    # "bench", or traffic-sign text leaked into name=).  Requiring at least
    # one type-asserting key eliminates the bulk of mis-tagged noise.
    'type_asserting_keys': [
        'amenity', 'shop', 'tourism', 'place', 'highway', 'building',
        'office', 'leisure', 'natural', 'landuse', 'railway', 'aeroway',
        'waterway', 'boundary', 'historic', 'military', 'man_made',
        'public_transport', 'route', 'craft', 'healthcare', 'education',
        'addr:housenumber', 'addr:street', 'contact:phone', 'ref',
    ],
    # Default cosine-distance threshold for semantic name matching.
    # Lowered from 0.75 (which let FastText OOV-collapse noise through) to
    # 0.5 — a tighter cutoff that still keeps genuine semantic matches.
    'name_distance_threshold_default': 0.5,
    # Score boost added when a query term appears as a substring of the
    # entity's name= tag (case-insensitive).  Helps cross-script and
    # misspelling cases where FastText embedding alone is insufficient.
    'name_substring_boost': 1.0,
}

TIME_ZONE = 'America/Toronto'

# ============================================================================
# SERVICE-LEVEL + PIPELINE CONFIGURATION
# ============================================================================
# Single source of truth for env-backed service config. Services read these
# via ``django.conf.settings`` — lazily (per instance / per call), so tests
# override with ``monkeypatch.setattr(settings, ...)`` or ``override_settings``
# instead of ``monkeypatch.setenv``. Empty string / None means "unset"; the
# services apply their own defaults and fail-soft parsing.
#
# Framework runtime flags are exempt: DJANGO_SETTINGS_MODULE (bootstrap) and
# RUN_MAIN (autoreloader sentinel) stay os.environ reads outside this file.

# ── LLM services (Ollama native / OpenAI-compatible) ─────────────────────────
# Interactive path (LLM_*) on the 4070; the batch research orchestrator uses
# RESEARCH_LLM_* on the 2070 (falls back to the platform instance when
# RESEARCH_LLM_BASE_URL is unset). Read per-instance by LLMService.
LLM_ENABLED = os.getenv('LLM_ENABLED', '1')
LLM_API_STYLE = os.getenv('LLM_API_STYLE', '')
LLM_BASE_URL = os.getenv('LLM_BASE_URL', '')
LLM_MODEL = os.getenv('LLM_MODEL', '')
LLM_TIMEOUT = os.getenv('LLM_TIMEOUT', '')
LLM_AVAILABILITY_CACHE_SECONDS = os.getenv('LLM_AVAILABILITY_CACHE_SECONDS', '')
RESEARCH_LLM_BASE_URL = os.getenv('RESEARCH_LLM_BASE_URL', '')
RESEARCH_LLM_MODEL = os.getenv('RESEARCH_LLM_MODEL', '')
RESEARCH_LLM_API_STYLE = os.getenv('RESEARCH_LLM_API_STYLE', '')
RESEARCH_LLM_TIMEOUT = os.getenv('RESEARCH_LLM_TIMEOUT', '')
# Opt-in: research loop follow-up nameSearch/structuredSearch calls.
RESEARCH_FOLLOWUP_TOOLS = os.getenv('RESEARCH_FOLLOWUP_TOOLS', '0')

# ── Request-path caches + thresholds ─────────────────────────────────────────
SNAPSHOT_ID_CACHE_TTL_SECONDS = os.getenv('SNAPSHOT_ID_CACHE_TTL_SECONDS', '')
LLM_ANSWER_CACHE_TTL_SECONDS = os.getenv('LLM_ANSWER_CACHE_TTL_SECONDS', '')
MAPQA_LLM_FALLBACK_CONFIDENCE = os.getenv('MAPQA_LLM_FALLBACK_CONFIDENCE', '')

# ── Pipeline GPU / parallel tuning (Steps 4 USLP, 5 GV-NLE, embedding upsert) ─
# Operator-edited source: backend/pipeline/hyperparams.yaml (gv_nle /
# parallel_upsert / enrichment / embedding_splits sections), loaded once at
# startup into ModelHyperparams.  The values below are code defaults only —
# they apply when the YAML key is absent (services read the dataclass, which
# falls back to these via pipeline/envelopes.py load_from_yaml()).
ENRICHMENT_WORKERS = 2
EMBEDDING_SPLITS_WORKERS = 1
GV_NLE_GPU_DEVICES = 'cuda:0,cuda:1'
GV_NLE_GPU_CONCURRENCY = '1,1'
GV_NLE_SMALL_PBF_MB = 0.3
PARALLEL_UPSERT_WORKERS = 1
PARALLEL_UPSERT_QUEUE_DEPTH = 2
PARALLEL_UPSERT_MIN_PBF_MB = 0
PARALLEL_UPSERT_CHUNK_SIZE = 20000

# ── Trace adapter (core/services/trace_service.py) ───────────────────────────
# Raw strings — the service normalizes/clamps. Sinks: none|console|file|
# langfuse|memory.
TRACE_SINK = os.getenv('TRACE_SINK', '')
TRACE_SAMPLE_RATE = os.getenv('TRACE_SAMPLE_RATE', '')
TRACE_DETERMINISTIC_STAGES = os.getenv('TRACE_DETERMINISTIC_STAGES', '')
TRACE_QUEUE_SIZE = os.getenv('TRACE_QUEUE_SIZE', '')
TRACE_FLUSH_INTERVAL = os.getenv('TRACE_FLUSH_INTERVAL', '')
TRACE_SINK_URL = os.getenv('TRACE_SINK_URL', '')
TRACE_SINK_PUBLIC_KEY = os.getenv('TRACE_SINK_PUBLIC_KEY', '')
TRACE_SINK_SECRET_KEY = os.getenv('TRACE_SINK_SECRET_KEY', '')

# ── Continent-extraction recipe runtime parameters ───────────────────────────
# Env-overridable, and also set at runtime by run_continents_recipe /
# PlanetInitializationService.extract_continents() before they call_command
# the extraction commands.
FOLDER_PATH = os.getenv('FOLDER_PATH')
SOURCE_PBF_PATH = os.getenv('SOURCE_PBF_PATH')
OUTPUT_BASE_DIR = os.getenv('OUTPUT_BASE_DIR')

# ── Auth / host trust ────────────────────────────────────────────────────────
# Empty invite code = registration closed.
SIGNUP_INVITE_CODE = os.getenv('SIGNUP_INVITE_CODE', '')
# Hosts treated as local by the admin gate + public auth guard.
TRUSTED_LOCAL_HOSTS = os.getenv('TRUSTED_LOCAL_HOSTS', 'localhost,127.0.0.1')

# ── Feature flags + model hyperparameter defaults ────────────────────────────
# Single-pass PBF encoding (FastText + NLE in one traversal).
GEOVECTORS_SINGLE_PASS = True
# Link-candidate view: serve the precomputed table before on-the-fly scoring.
ENABLE_PRECOMPUTED_LINK_CANDIDATES = True
# Google Places enrichment key (secret — env-only).
GOOGLE_PLACES_API_KEY = os.getenv('GOOGLE_PLACES_API_KEY', None)
# DeepWalk defaults — overridden per-run by pipeline/hyperparams.yaml.
DEEPWALK_K = 50
DEEPWALK_WORKERS = 26
DEEPWALK_USE_GPU = True
DEEPWALK_GPU_DEVICE = 'cuda:0'