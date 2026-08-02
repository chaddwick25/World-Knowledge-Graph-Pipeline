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
SECRET_KEY = 'django-insecure-i%)wb*%s81#y-rrf!fw9v$quoo0!&@)-2f0o9os!b*i&53xtqj'

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True
ALLOWED_HOSTS = ['localhost', '127.0.0.1']

# ============================================================================
# FILE PATHS CONFIGURATIONS + Overrides
# ============================================================================
# BASE DATA DIRECTORY (HOT Storage)
BASE_DATA_DIR = os.getenv('BASE_DATA_DIR')

# OSM Planet File Path, this is the groundtruth for the WorldKG Pipeline
_default_planet_osm = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/planet/latest.osm.pbf') if BASE_DATA_DIR else None
PLANET_OSM_FILE_PATH = os.getenv('PLANET_OSM_FILE_PATH', _default_planet_osm)

# OSM Polygon Files Directory, this is the directory where the polygon files are stored
_default_polygon_files_dir = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/osm_polygon_files') if BASE_DATA_DIR else None
POLYGON_FILES_DIR = os.getenv('POLYGON_FILES_DIR', _default_polygon_files_dir)

# Optional legacy polygon files directory (e.g., pre-existing tree with subgraphs)
_default_legacy_polygon_files_dir = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/osm_polygon_files_old') if BASE_DATA_DIR else None
LEGACY_POLYGON_FILES_DIR = os.getenv('LEGACY_POLYGON_FILES_DIR', _default_legacy_polygon_files_dir)

# Geofabrik index URL used for polygon discovery
GEOFABRIK_INDEX_URL = os.getenv('GEOFABRIK_INDEX_URL', 'https://download.geofabrik.de/index-v1.json')

# OSM Wikidata Extractions Directory, this is the directory where the wikidata extractions are stored
_default_osm_wikidata_extractions = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/osm_wikidata_extractions') if BASE_DATA_DIR else None
OSM_WIKIDATA_EXTRACTIONS_DIR = os.getenv('OSM_WIKIDATA_EXTRACTIONS_DIR', _default_osm_wikidata_extractions)

# Analysis Service Paths
_default_asset_bundles_dir = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/filtered_snapshots') if BASE_DATA_DIR else None
ASSET_BUNDLES_DIR = os.getenv('ASSET_BUNDLES_DIR', _default_asset_bundles_dir)

_default_pbf_cache_dir = os.path.join(BASE_DATA_DIR, 'pbf_cache') if BASE_DATA_DIR else None
PBF_CACHE_DIR = os.getenv('PBF_CACHE_DIR', _default_pbf_cache_dir)

_default_preprocessed_dir = os.path.join(BASE_DATA_DIR, 'preprocessed') if BASE_DATA_DIR else None
PREPROCESSED_DIR = os.getenv('PREPROCESSED_DIR', _default_preprocessed_dir)

# OSM Wikidata Extractions for Contintents
_default_continents_output_dir = os.path.join(OSM_WIKIDATA_EXTRACTIONS_DIR, 'continents') if OSM_WIKIDATA_EXTRACTIONS_DIR else None
OSM_CONTINENTS_OUTPUT_DIR = os.getenv('OSM_CONTINENTS_OUTPUT_DIR', _default_continents_output_dir)

_default_osm_wikidata_temp_dir = os.path.join(OSM_WIKIDATA_EXTRACTIONS_DIR, 'temp') if OSM_WIKIDATA_EXTRACTIONS_DIR else None
OSM_WIKIDATA_TEMP_DIR = os.getenv('OSM_WIKIDATA_TEMP_DIR', _default_osm_wikidata_temp_dir)



# FastText Model Configuration
_default_fasttext_model_path = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/models/fasttext/cc.en.300.bin') if BASE_DATA_DIR else None
FASTTEXT_MODEL_PATH = os.getenv('FASTTEXT_MODEL_PATH', _default_fasttext_model_path)

_default_fasttext_tuned_dir = os.path.join(BASE_DATA_DIR, 'OSM-PBF-FILES/models/fasttext_tuned') if BASE_DATA_DIR else None
FASTTEXT_TUNED_DIR = os.getenv('FASTTEXT_TUNED_DIR', _default_fasttext_tuned_dir)

_default_worldkg_ontology_ttl = os.path.join(
    BASE_DATA_DIR, 'OSM-PBF-FILES/world_kg_ontology/WorldKG_Ontolgy.ttl'
) if BASE_DATA_DIR else None
WORLDKG_ONTOLOGY_TTL_PATH = os.getenv(
    'WORLDKG_ONTOLOGY_TTL_PATH', _default_worldkg_ontology_ttl
)

# Redis configuration for Channels, Celery, and WorldKG services
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', '6379'))
WORLDKG_REDIS_DB = int(os.getenv('WORLDKG_REDIS_DB', '2'))

# Single Snapshot Configuration for Temporal Pipeline
# Used by Vue-triggered preprocessing to generate only one snapshot per country
SINGLE_SNAPSHOT_DATE = os.getenv(
    'SINGLE_SNAPSHOT_DATE',
    '2025_12_31'
)
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
MIN_PREFLIGHT_SHANNON_ENTROPY = float(os.getenv('MIN_PREFLIGHT_SHANNON_ENTROPY', '1.5'))

# Skip full pipeline if entropy delta vs previous fingerprint is below this.
# Low delta = no meaningful semantic change since last run.
MIN_PREFLIGHT_ENTROPY_DELTA = float(os.getenv('MIN_PREFLIGHT_ENTROPY_DELTA', '0.2'))

# ============================================================================
# GOOGLE PLACES VALIDATION (Stage 10 — Amplification Layer)
# ============================================================================
GOOGLE_VALIDATION_ENABLED = os.getenv('GOOGLE_VALIDATION_ENABLED', 'False').lower() in ('true', '1', 'yes')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY', None)

# ============================================================================
# COLD STORAGE DATA DIRECTORY
# ============================================================================
COLD_STORAGE_BASE_DIR = os.getenv('COLD_STORAGE_BASE_DIR')

# OSM corpus used for fine-tuning and retraining 
_default_corpus_dir = os.path.join(COLD_STORAGE_BASE_DIR, 'corpus') if COLD_STORAGE_BASE_DIR else None
CORPUS_DIR = os.getenv('CORPUS_DIR', _default_corpus_dir)

_default_graph_assets_dir = os.path.join(COLD_STORAGE_BASE_DIR, 'graph_assets') if COLD_STORAGE_BASE_DIR else None
GRAPH_ASSETS_DIR = os.getenv('GRAPH_ASSETS_DIR', _default_graph_assets_dir)

# GeoVectors Embeddings Root (for TSV files used in pickle generation)
# This is the primary source directory for the scan_embeddings command.
_default_embeddings_dir = os.path.join(COLD_STORAGE_BASE_DIR, 'embeddings') if COLD_STORAGE_BASE_DIR else None
EMBEDDINGS_ROOT = os.getenv('EMBEDDINGS_ROOT', _default_embeddings_dir)

# Fallback embeddings root for systems where embeddings live elsewhere
# (e.g. on a separate mount). The scan_embeddings command checks this
# before falling back to EMBEDDINGS_ROOT.
_extra_embeddings_dir = os.getenv('EXTRA_EMBEDDINGS_ROOT', None)
EXTRA_EMBEDDINGS_ROOT = _extra_embeddings_dir

# Country overrides JSON path (cold storage)
_default_overrides_json = os.path.join(COLD_STORAGE_BASE_DIR, 'overrides.json') if COLD_STORAGE_BASE_DIR else None
OVERRIDES_JSON_PATH = os.getenv('OVERRIDES_JSON_PATH', _default_overrides_json)

# Embedding splits/merges config (cold storage)
_default_embedding_splits_cfg = os.path.join(COLD_STORAGE_BASE_DIR, 'embedding_splits.json') if COLD_STORAGE_BASE_DIR else None
EMBEDDING_SPLITS_CONFIG_PATH = os.getenv('EMBEDDING_SPLITS_CONFIG_PATH', _default_embedding_splits_cfg)

_default_downloads_dir = os.path.join(BASE_DATA_DIR, 'downloads') if BASE_DATA_DIR else str(BASE_DIR / 'downloads')
DOWNLOADS_DIR = os.getenv('DOWNLOADS_DIR', _default_downloads_dir)

# WorldKG ontology TTL path
_default_worldkg_ontology = os.path.join(BASE_DATA_DIR, 'world_kg_ontology/WorldKG_Ontolgy.ttl') if BASE_DATA_DIR else None
WORLDKG_ONTOLOGY_PATH = os.getenv('WORLDKG_ONTOLOGY_PATH', _default_worldkg_ontology)

# Continents root directory (for continent PBF files)
_default_continents_root = os.path.join(BASE_DATA_DIR, 'osm_wikidata_extractions/continents') if BASE_DATA_DIR else None
CONTINENTS_ROOT = os.getenv('CONTINENTS_ROOT', _default_continents_root)

# Hot storage path (SSD/NVME working files)
HOT_STORAGE_PATH = os.getenv('HOT_STORAGE_PATH', BASE_DATA_DIR)

# Cold storage path (embeddings on separate mount)
COLD_STORAGE_PATH = os.getenv('COLD_STORAGE_PATH', COLD_STORAGE_BASE_DIR)

# USLP (Unsupervised Spatial Link Prediction) Configuration
USLP_THRESHOLD = float(os.getenv('USLP_THRESHOLD', '0.7'))
USLP_TOP_K = int(os.getenv('USLP_TOP_K', '50'))
USLP_LIMIT = int(os.getenv('USLP_LIMIT', '10000000'))
USLP_MAX_HEADS = int(os.getenv('USLP_MAX_HEADS', '10000000'))
# USLP_USE_GPU: 'auto' (default), 'true', or 'false'
USLP_USE_GPU = os.getenv('USLP_USE_GPU', 'true').lower()
USLP_GPU_DEVICE = os.getenv('USLP_GPU_DEVICE', 'cuda:0')
USLP_USE_FP64 = os.getenv('USLP_USE_FP64', 'false').lower() == 'true'

CELERY_BROKER_URL = f"redis://{os.getenv('REDIS_HOST', 'localhost')}:6379/0"

# ── Celery Result Backend ──
# Use Django's SQL DB via django-celery-results for durable, queryable task
# results. Redis remains the broker (DB 0) for task routing.
CELERY_RESULT_BACKEND = "django-db"
# Keep the Redis URL fallback available for environments that have not yet
# adopted the SQL result backend.
CELERY_RESULT_BACKEND_URL = os.environ.get(
    "CELERY_RESULT_BACKEND_URL",
    f"redis://{os.getenv('REDIS_HOST', 'localhost')}:6379/1"
)
# Results expire after 48 hours (matches longest pipeline run + buffer)
CELERY_RESULT_EXPIRES = 60 * 60 * 48  # 48 hours

# Task/result serialization — must be consistent across broker + backend
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]





CORS_ALLOWED_ORIGINS = [
    'http://localhost:8080',
    'http://127.0.0.1:8080',
    'http://localhost:8081',
    'http://127.0.0.1:8081',
    'http://localhost:5173',
    'http://127.0.0.1:5173',
]

CORS_ALLOW_HEADERS = [
    'content-type',
]

# CSRF Trusted Origins (required for Django 4.0+)
CSRF_TRUSTED_ORIGINS = [
    'http://localhost:8080',
    'http://127.0.0.1:8080',
    'http://localhost:8081',
    'http://127.0.0.1:8081',
    'http://localhost:5173',
    'http://127.0.0.1:5173',
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
    'django_extensions',
    'extraction',
    'orchestration',
    'analysis',
    'api',
    'vectors',
    'backend',
    # 'toronto_data',
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
            'hosts': [(os.getenv('REDIS_HOST', '127.0.0.1'), 6379)],
        },
    },
}

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
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
WORLDKG_SNAPSHOT_START_YEAR = int(os.getenv('WORLDKG_SNAPSHOT_START_YEAR', '2021'))
WORLDKG_SNAPSHOT_END_YEAR = int(os.getenv('WORLDKG_SNAPSHOT_END_YEAR', '2025'))


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
    'backend.database_router.AppRouter',
    'backend.database_router.VectorDBRouter'
]


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

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.2/howto/static-files/

STATIC_URL = 'static/'

# Default primary key field type
# https://docs.djangoproject.com/en/4.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Toronto CKAN Configuration
TORONTO_CKAN_CONFIG = {
    'base_url': os.getenv('TORONTO_CKAN_BASE_URL', 'https://ckan0.cf.opendata.inter.prod-toronto.ca'),
    'api_version': os.getenv('TORONTO_CKAN_API_VERSION', '3'),
    'ssl_verify': os.getenv('TORONTO_CKAN_SSL_VERIFY', 'false').lower() == 'true',
    'timeout': int(os.getenv('TORONTO_CKAN_TIMEOUT', '30')),
    'user_agent': os.getenv('TORONTO_CKAN_USER_AGENT', 'EDA-Vector-Search-Toolkit/1.0'),
}

# Toronto Data Download Settings
TORONTO_DATA_DIR = os.getenv('TORONTO_DATA_DIR', os.path.join(BASE_DIR, 'toronto_data', 'data'))
TORONTO_BULK_INSERT_BATCH_SIZE = int(os.getenv('TORONTO_BULK_INSERT_BATCH_SIZE', '1000'))

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
}

TIME_ZONE = 'America/Toronto'