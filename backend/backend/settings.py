import os
import sys
from datetime import datetime
from pathlib import Path
import dj_database_url
from django.utils import timezone
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent
load_dotenv(os.path.join(PROJECT_ROOT, '.env'))
***REMOVED*** Add the project root to the Python path to allow imports from the root
sys.path.insert(0, str(PROJECT_ROOT))

***REMOVED*** SECURITY WARNING: keep the secret key used in production secret!
***REMOVED*** Set DJANGO_SECRET_KEY in production; the insecure value is a dev default.
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY')

***REMOVED*** SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True
***REMOVED*** Extra hosts come from the homeserver env (Tailscale funnel / custom domain).
_extra_hosts = [h.strip() for h in os.getenv('ALLOWED_EXTRA_HOSTS', '').split(',') if h.strip()]
ALLOWED_HOSTS = ['localhost', '127.0.0.1', *_extra_hosts]

***REMOVED*** ============================================================================
***REMOVED*** FILE PATHS CONFIGURATIONS + Overrides
***REMOVED*** ============================================================================
***REMOVED*** BASE DATA DIRECTORY (HOT Storage)
BASE_DATA_DIR = os.getenv('BASE_DATA_DIR')

***REMOVED*** History PBF — the file the pipeline actually consumes.
_default_planet_osm = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/planet/history-260209.osm.pbf') if BASE_DATA_DIR else None
PLANET_OSM_FILE_PATH = _default_planet_osm

_default_polygon_files_dir = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/osm_polygon_files') if BASE_DATA_DIR else None
POLYGON_FILES_DIR = _default_polygon_files_dir

***REMOVED*** Legacy tree with subgraphs (pre-existing layout)
_default_legacy_polygon_files_dir = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/osm_polygon_files_old') if BASE_DATA_DIR else None
LEGACY_POLYGON_FILES_DIR = _default_legacy_polygon_files_dir

***REMOVED*** Geofabrik index URL used for polygon discovery
GEOFABRIK_INDEX_URL = 'https://download.geofabrik.de/index-v1.json'

_default_osm_wikidata_extractions = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/osm_wikidata_extractions') if BASE_DATA_DIR else None
OSM_WIKIDATA_EXTRACTIONS_DIR = _default_osm_wikidata_extractions

***REMOVED*** Analysis Service Paths
_default_asset_bundles_dir = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/filtered_snapshots') if BASE_DATA_DIR else None
ASSET_BUNDLES_DIR = _default_asset_bundles_dir

_default_pbf_cache_dir = os.path.join(BASE_DATA_DIR, 'pbf_cache') if BASE_DATA_DIR else None
PBF_CACHE_DIR = _default_pbf_cache_dir

_default_preprocessed_dir = os.path.join(BASE_DATA_DIR, 'preprocessed') if BASE_DATA_DIR else None
PREPROCESSED_DIR = _default_preprocessed_dir

***REMOVED*** Continents extractions + PBF files
_default_continents_output_dir = os.path.join(OSM_WIKIDATA_EXTRACTIONS_DIR, 'continents') if OSM_WIKIDATA_EXTRACTIONS_DIR else None
OSM_CONTINENTS_OUTPUT_DIR = _default_continents_output_dir

_default_osm_wikidata_temp_dir = os.path.join(OSM_WIKIDATA_EXTRACTIONS_DIR, 'temp') if OSM_WIKIDATA_EXTRACTIONS_DIR else None
OSM_WIKIDATA_TEMP_DIR = _default_osm_wikidata_temp_dir

***REMOVED*** FastText Model Configuration
_default_fasttext_model_path = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/models/fasttext/cc.en.300.bin') if BASE_DATA_DIR else None
FASTTEXT_MODEL_PATH = _default_fasttext_model_path

_default_fasttext_tuned_dir = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/models/fasttext_tuned') if BASE_DATA_DIR else None
FASTTEXT_TUNED_DIR = _default_fasttext_tuned_dir

***REMOVED*** Redis configuration for Channels, Celery, and WorldKG services
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', '6379'))
WORLDKG_REDIS_DB = int(os.getenv('WORLDKG_REDIS_DB', '2'))

***REMOVED*** WebSocket host/port for pipeline progress URLs (returned to frontend)
WS_HOST = os.getenv('WS_HOST', 'localhost')
WS_PORT = os.getenv('WS_PORT', '8000')

***REMOVED*** Single snapshot per country (Vue-triggered preprocessing)
SINGLE_SNAPSHOT_DATE = '2025_12_31'
SINGLE_SNAPSHOT_YEAR = int(SINGLE_SNAPSHOT_DATE.split('_')[0])

***REMOVED*** Osmium Tool Configuration
OSMIUM_BINARY_PATH = os.getenv(
    'OSMIUM_BINARY_PATH',
    str(Path(PROJECT_ROOT) / 'osmium-tool' / 'build' / 'src' / 'osmium')
)
OSMIUM_EXECUTABLE = os.getenv(
    'OSMIUM_EXECUTABLE',
    OSMIUM_BINARY_PATH
)

***REMOVED*** ============================================================================
***REMOVED*** PRE-FLIGHT ENTROPY GATING (WorldKG Pipeline)
***REMOVED*** ============================================================================
***REMOVED*** Values: entropy section of pipeline/hyperparams.yaml; dataclass defaults
***REMOVED*** (1.5 / 0.2) are the code-level fallback.

***REMOVED*** ============================================================================
***REMOVED*** GOOGLE PLACES VALIDATION (Stage 10 — Amplification Layer)
***REMOVED*** ============================================================================
GOOGLE_VALIDATION_ENABLED = False
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY', None)

***REMOVED*** ============================================================================
***REMOVED*** COLD STORAGE DATA DIRECTORY
***REMOVED*** ============================================================================
COLD_STORAGE_BASE_DIR = os.getenv('COLD_STORAGE_BASE_DIR')

***REMOVED*** Repo-local corpus (backend/data/corpus) — derived, not env-overridable.
_default_corpus_dir = os.path.join(BASE_DIR, 'data', 'corpus')
CORPUS_DIR = _default_corpus_dir

_default_graph_assets_dir = os.path.join(COLD_STORAGE_BASE_DIR, 'graph_assets') if COLD_STORAGE_BASE_DIR else None
GRAPH_ASSETS_DIR = _default_graph_assets_dir

***REMOVED*** GeoVectors embeddings root (TSV files used in pickle generation)
_default_embeddings_dir = os.path.join(COLD_STORAGE_BASE_DIR, 'embeddings') if COLD_STORAGE_BASE_DIR else None
EMBEDDINGS_ROOT = _default_embeddings_dir

***REMOVED*** Fallback embeddings root (separate mount); machine-specific opt-in — env-only.
_extra_embeddings_dir = os.getenv('EXTRA_EMBEDDINGS_ROOT', None)
EXTRA_EMBEDDINGS_ROOT = _extra_embeddings_dir

***REMOVED*** Country overrides JSON path (cold storage)
_default_overrides_json = os.path.join(COLD_STORAGE_BASE_DIR, 'overrides.json') if COLD_STORAGE_BASE_DIR else None
OVERRIDES_JSON_PATH = _default_overrides_json

_default_downloads_dir = os.path.join(BASE_DATA_DIR, 'downloads') if BASE_DATA_DIR else str(BASE_DIR / 'downloads')
DOWNLOADS_DIR = _default_downloads_dir

***REMOVED*** WorldKG ontology TTL (under the OSM-PBF-FILES mount)
_default_worldkg_ontology = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/world_kg_ontology/WorldKG_Ontolgy.ttl') if BASE_DATA_DIR else None
WORLDKG_ONTOLOGY_PATH = _default_worldkg_ontology

***REMOVED*** Continents root (same directory as the continents extractions)
_default_continents_root = os.path.join(OSM_WIKIDATA_EXTRACTIONS_DIR, 'continents') if OSM_WIKIDATA_EXTRACTIONS_DIR else None
CONTINENTS_ROOT = _default_continents_root

***REMOVED*** Categorized run logs (gv-nle, pipeline, tests, uslp)
_default_logs_dir = os.path.join(BASE_DATA_DIR, 'logs') if BASE_DATA_DIR else None
LOGS_DIR = _default_logs_dir

***REMOVED*** Wikidata candidate cache (JSON caches per country)
_default_wikidata_cache_dir = os.path.join(BASE_DATA_DIR, 'data/wikidata_cache') if BASE_DATA_DIR else None
WIKIDATA_CACHE_DIR = _default_wikidata_cache_dir

***REMOVED*** MapQA parser data — training data + model artifacts (layout in
***REMOVED*** docs/plans/MAPQA_PARSER_BUILD_ORDER.md §8).
_default_mapqa_parser_dir = os.path.join(BASE_DATA_DIR, 'mapqa_parser') if BASE_DATA_DIR else None
MAPQA_PARSER_DATA_DIR = _default_mapqa_parser_dir

***REMOVED*** k-NN graph artifacts from Step 5c, loaded at query time for graph-based
***REMOVED*** operators (heat kernel diffusion, Dijkstra, BFS).
_default_graph_artifact_dir = os.path.join(BASE_DIR, 'data', 'graph_artifacts')
GRAPH_ARTIFACT_DIR = _default_graph_artifact_dir

***REMOVED*** Step 5c run reports (solver selection, timing, eigenvalue quality).
_default_spectral_report_dir = os.path.join(BASE_DIR, 'data', 'spectral_reports')
SPECTRAL_REPORT_DIR = _default_spectral_report_dir

***REMOVED*** Factor-node tables (docs/plans/FACTOR_NODE_RUNTIME_JOINS_PLAN.md).
FACTOR_NODE_TABLES_ENABLED = True

***REMOVED*** Eigenbasis coherence (GRAPH_SPECTRAL_FEEDBACK_HARDENING_PLAN Phase 1):
***REMOVED*** strict = NULL fingerprint_id forces the PostGIS fallback.
FACTOR_EIGENBASIS_STRICT = False

***REMOVED*** USLP — runtime truth in pipeline/hyperparams.yaml (uslp section);
***REMOVED*** dataclass defaults are the code-level fallback.

CELERY_BROKER_URL = f"redis://{os.getenv('REDIS_HOST', 'localhost')}:{REDIS_PORT}/0"

***REMOVED*** ── Celery Result Backend ──
***REMOVED*** Custom Django DB backend with chord-in-chain fix (django-celery-results
***REMOVED*** 2.0.0's DatabaseBackend loses ChordCounter records for chords in a chain,
***REMOVED*** re-training subgraphs 2-7x; see docs/issues/CHORDCOUNTER_DOES_NOT_EXIST_BUG.md).
CELERY_RESULT_BACKEND = os.environ.get(
    "CELERY_RESULT_BACKEND",
    "pipeline.celery_results_backend:PatchedDatabaseBackend",
)
***REMOVED*** Results expire after 48 hours (matches longest pipeline run + buffer)
CELERY_RESULT_EXPIRES = 60 * 60 * 48  ***REMOVED*** 48 hours

***REMOVED*** Task/result serialization — must be consistent across broker + backend
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]

***REMOVED*** Ack on completion (not receipt) so worker death re-delivers in-flight tasks
***REMOVED*** (Step 6 vanished during the 2026-08-21 IE freeze without this).
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

***REMOVED*** Deployed frontend origins (Netlify domain) via CORS_EXTRA_ORIGINS; the
***REMOVED*** literals below are the local dev defaults.
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

***REMOVED*** TLS terminates at nginx upstream (Tailscale Funnel) — trust X-Forwarded-Proto
***REMOVED*** so request.is_secure() and Django's CSRF Origin check work on public requests.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

***REMOVED*** Same origin set as CORS (required for Django 4.0+).
CSRF_TRUSTED_ORIGINS = [
    'http://localhost:8080',
    'http://127.0.0.1:8080',
    'http://localhost:8081',
    'http://127.0.0.1:8081',
    'http://localhost:5173',
    'http://127.0.0.1:5173',
    *_extra_origins,
]

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.AllowAny',
    ],
    'UNAUTHENTICATED_USER': None,
}

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

***REMOVED*** Django Channels
ASGI_APPLICATION = 'backend.asgi.application'
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            'hosts': [(REDIS_HOST, REDIS_PORT)],
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

***REMOVED*** WorldKG Pipeline snapshot date range defaults
WORLDKG_SNAPSHOT_START_YEAR = 2021
WORLDKG_SNAPSHOT_END_YEAR = 2025
***REMOVED*** Short aliases for SnapshotDatesView / SnapshotJobStatusView.
SNAPSHOT_START_YEAR = WORLDKG_SNAPSHOT_START_YEAR
SNAPSHOT_END_YEAR = WORLDKG_SNAPSHOT_END_YEAR

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

***REMOVED*** MapQA self-supervised training-data generation — gates the optional LLM
***REMOVED*** paraphrase tier (rule-based tier always on; see
***REMOVED*** docs/plans/completed/MAPQA_TEMPLATE_COVERAGE_EXPANSION_PLAN.md §4).
MAPQA_LLM_AUGMENTATION_ENABLED = False

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

LANGUAGE_CODE = 'en-us'
USE_I18N = True
USE_TZ = True
STATIC_URL = 'static/'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

GEODATA_CONFIG = {
    'download_dir': os.getenv('GEODATA_DIR', os.path.join(BASE_DIR, 'data', 'geodata')),
    'bulk_insert_batch_size': 1000,
    'default_ssl_verify': False,
    'default_timeout': 30,
}

***REMOVED*** Default maximum temporal range for PBF files without explicit max_timestamp
DEFAULT_TEMPORAL_RANGE_END = datetime(2024, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

***REMOVED*** Spatial Semantics Configuration
SPATIAL_SEMANTICS_CONFIG = {
    'fasttext_model_path': FASTTEXT_MODEL_PATH,
    'drift_threshold': 0.10,
    'target_amenities': ['cafe', 'restaurant', 'fast_food', 'bar', 'pub'],
    'hnsw_m': 16,
    'hnsw_ef_construction': 64,
    ***REMOVED*** Type-asserting tag keys — entities without any are noise in name search
    ***REMOVED*** (e.g. a bench whose name= is literally "벤치", or traffic-sign text).
    'type_asserting_keys': [
        'amenity', 'shop', 'tourism', 'place', 'highway', 'building',
        'office', 'leisure', 'natural', 'landuse', 'railway', 'aeroway',
        'waterway', 'boundary', 'historic', 'military', 'man_made',
        'public_transport', 'route', 'craft', 'healthcare', 'education',
        'addr:housenumber', 'addr:street', 'contact:phone', 'ref',
    ],
    ***REMOVED*** Cosine-distance threshold for semantic name matching (0.5; lowered from
    ***REMOVED*** 0.75 to keep FastText OOV-collapse noise out).
    'name_distance_threshold_default': 0.5,
    ***REMOVED*** Substring match boost for cross-script / misspelled names.
    'name_substring_boost': 1.0,
}

TIME_ZONE = 'America/Toronto'

***REMOVED*** ============================================================================
***REMOVED*** SERVICE-LEVEL + PIPELINE CONFIGURATION
***REMOVED*** ============================================================================
***REMOVED*** Single source of truth for env-backed service config. Services read these
***REMOVED*** via django.conf.settings (lazily), so tests patch settings, not os.environ.
***REMOVED*** Framework flags DJANGO_SETTINGS_MODULE / RUN_MAIN are the only exempt reads.

***REMOVED*** ── LLM services (Ollama native / OpenAI-compatible) ──
***REMOVED*** LLM_* interactive on the 4070; RESEARCH_LLM_* batch orchestrator on the 2070.
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
***REMOVED*** Opt-in: research loop follow-up nameSearch/structuredSearch calls.
RESEARCH_FOLLOWUP_TOOLS = os.getenv('RESEARCH_FOLLOWUP_TOOLS', '0')

***REMOVED*** ── Request-path caches + thresholds ──
SNAPSHOT_ID_CACHE_TTL_SECONDS = os.getenv('SNAPSHOT_ID_CACHE_TTL_SECONDS', '')
LLM_ANSWER_CACHE_TTL_SECONDS = os.getenv('LLM_ANSWER_CACHE_TTL_SECONDS', '')
MAPQA_LLM_FALLBACK_CONFIDENCE = os.getenv('MAPQA_LLM_FALLBACK_CONFIDENCE', '')

***REMOVED*** ── Pipeline GPU / parallel tuning ──
***REMOVED*** Operator-edited source: pipeline/hyperparams.yaml (uslp / deepwalk / entropy
***REMOVED*** / gv_nle / parallel_upsert / enrichment / embedding_splits sections);
***REMOVED*** dataclass field defaults are the code-level fallback.

***REMOVED*** ── Trace adapter (core/services/trace_service.py) ──
***REMOVED*** Raw strings; sinks: none|console|file|langfuse|memory.
TRACE_SINK = os.getenv('TRACE_SINK', '')
TRACE_SAMPLE_RATE = os.getenv('TRACE_SAMPLE_RATE', '')
TRACE_DETERMINISTIC_STAGES = os.getenv('TRACE_DETERMINISTIC_STAGES', '')
TRACE_QUEUE_SIZE = os.getenv('TRACE_QUEUE_SIZE', '')
TRACE_FLUSH_INTERVAL = os.getenv('TRACE_FLUSH_INTERVAL', '')
TRACE_SINK_URL = os.getenv('TRACE_SINK_URL', '')
TRACE_SINK_PUBLIC_KEY = os.getenv('TRACE_SINK_PUBLIC_KEY', '')
TRACE_SINK_SECRET_KEY = os.getenv('TRACE_SINK_SECRET_KEY', '')

***REMOVED*** ── Continent-extraction recipe runtime parameters ──
***REMOVED*** Env-overridable; also set at runtime by run_continents_recipe /
***REMOVED*** PlanetInitializationService.extract_continents().
FOLDER_PATH = os.getenv('FOLDER_PATH')
SOURCE_PBF_PATH = os.getenv('SOURCE_PBF_PATH')
OUTPUT_BASE_DIR = os.getenv('OUTPUT_BASE_DIR')

***REMOVED*** ── Auth / host trust ──
***REMOVED*** Empty invite code = registration closed.
SIGNUP_INVITE_CODE = os.getenv('SIGNUP_INVITE_CODE', '')
TRUSTED_LOCAL_HOSTS = os.getenv('TRUSTED_LOCAL_HOSTS', 'localhost,127.0.0.1')

***REMOVED*** ── Feature flags ──
***REMOVED*** Single-pass PBF encoding (FastText + NLE in one traversal).
GEOVECTORS_SINGLE_PASS = True
***REMOVED*** Serve the precomputed link-candidate table before on-the-fly scoring.
ENABLE_PRECOMPUTED_LINK_CANDIDATES = True
***REMOVED*** Google Places enrichment key (secret — env-only).
GOOGLE_PLACES_API_KEY = os.getenv('GOOGLE_PLACES_API_KEY', None)
***REMOVED*** DeepWalk — see the deepwalk section of pipeline/hyperparams.yaml.
