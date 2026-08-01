***REMOVED*** Toronto Open Data CKAN Integration

Django app for fetching and storing Toronto Open Data via CKAN API, including metadata, quality scores, and actual dataset content in PostgreSQL.

***REMOVED******REMOVED*** Overview

This app implements a complete data ingestion pipeline that:
1. Fetches dataset metadata from Toronto's CKAN API
2. Retrieves quality scores (Bronze/Silver/Gold grading)
3. Downloads CSV/JSON data files
4. Parses and stores data in dedicated database tables

***REMOVED******REMOVED*** Database Schema

***REMOVED******REMOVED******REMOVED*** Metadata Layer (3 tables)
- **CkanDataset** - Dataset catalog information
- **QualitySnapshot** - Time-series quality scores
- **CkanResource** - File metadata (CSV/JSON URLs, formats, sizes)

***REMOVED******REMOVED******REMOVED*** Content Layer (3 tables)
- **TrafficVolume** - Traffic intersection data
- **TtcSubwayDelay** - TTC delay incidents
- **CafetoLocation** - Parklet/cafe locations

***REMOVED******REMOVED*** Management Commands

***REMOVED******REMOVED******REMOVED*** 1. Ingest Metadata Only
```bash
***REMOVED*** Fetch metadata and quality scores for all TARGET_DATASETS
python manage.py ingest_toronto_metadata

***REMOVED*** Fetch specific datasets
python manage.py ingest_toronto_metadata --datasets traffic-volumes-at-intersections-for-all-modes
```

***REMOVED******REMOVED******REMOVED*** 2. Download Data Files
```bash
***REMOVED*** Download and parse all CSV/JSON files
python manage.py download_toronto_data

***REMOVED*** Download specific dataset
python manage.py download_toronto_data --dataset ttc-subway-delay-data

***REMOVED*** Force re-download even if not modified
python manage.py download_toronto_data --force

***REMOVED*** Append data instead of truncating
python manage.py download_toronto_data --no-truncate
```

***REMOVED******REMOVED******REMOVED*** 3. Full Ingestion Pipeline
```bash
***REMOVED*** Run complete workflow: metadata + quality + data download
python manage.py ingest_toronto_full

***REMOVED*** Skip data download (metadata only)
python manage.py ingest_toronto_full --skip-download

***REMOVED*** Process specific datasets
python manage.py ingest_toronto_full --datasets traffic-volumes ttc-subway-delay
```

***REMOVED******REMOVED*** REST API Endpoints

***REMOVED******REMOVED******REMOVED*** Datasets
- `GET /api/toronto/datasets/` - List all datasets with latest quality scores
- `GET /api/toronto/datasets/{id}/` - Dataset detail with resources
- `POST /api/toronto/datasets/sync_metadata/` - Trigger metadata sync

***REMOVED******REMOVED******REMOVED*** Quality Scores
- `GET /api/toronto/quality/` - List all quality snapshots
- `GET /api/toronto/quality/dashboard/` - Quality dashboard for all datasets
- `GET /api/toronto/quality/history/{dataset_id}/` - QA score history

***REMOVED******REMOVED******REMOVED*** Traffic Data
- `GET /api/toronto/traffic/` - Query traffic volume data
  - Filter: `?date=2024-01-15&intersection_id=INT-001`
  - Search: `?search=King+Street`

***REMOVED******REMOVED******REMOVED*** TTC Delays
- `GET /api/toronto/ttc-delays/` - Query TTC delay data
  - Filter: `?station=Union&line=Yonge&date=2024-01-15`
  - Search: `?search=signal+problem`

***REMOVED******REMOVED******REMOVED*** CaféTO Locations
- `GET /api/toronto/cafeto/` - Query parklet locations
  - Filter: `?ward=14&status=Active`
  - Search: `?search=coffee`

***REMOVED******REMOVED*** Configuration

Add to `.env`:
```bash
***REMOVED*** Toronto CKAN API
TORONTO_CKAN_BASE_URL=https://ckan0.cf.opendata.inter.prod-toronto.ca
TORONTO_CKAN_SSL_VERIFY=false  ***REMOVED*** Dev only - use certifi in prod
TORONTO_CKAN_TIMEOUT=30

***REMOVED*** Data download settings
TORONTO_DATA_DIR=/tmp/toronto_data_downloads
TORONTO_BULK_INSERT_BATCH_SIZE=1000
```

***REMOVED******REMOVED*** Target Datasets

The app is configured to ingest these three datasets:
1. **traffic-volumes-at-intersections-for-all-modes** - Traffic volume data
2. **ttc-subway-delay-data** - TTC subway delay incidents
3. **cafeto-curb-lane-parklet-cafe-locations** - CaféTO parklet locations

***REMOVED******REMOVED*** Django Admin

All models are registered in Django admin at `/admin/`:
- View datasets with inline quality snapshots and resources
- Filter by grade, status, date ranges
- Search across all fields

***REMOVED******REMOVED*** Architecture

***REMOVED******REMOVED******REMOVED*** Service Layer
- **BaseCkanService** - Shared CKAN API logic following best practices
- **CkanMetadataService** - Fetches metadata using `package_show` action
- **CkanQaService** - Fetches quality scores from `catalogue-quality-scores`
- **DatasetDownloadService** - Downloads and parses CSV/JSON files

***REMOVED******REMOVED******REMOVED*** Data Flow
```
CKAN API → CkanMetadataService → CkanDataset/CkanResource tables
         → CkanQaService → QualitySnapshot table
         → DatasetDownloadService → TrafficVolume/TtcSubwayDelay/CafetoLocation tables
```

***REMOVED******REMOVED*** Example Usage

***REMOVED******REMOVED******REMOVED*** Fetch metadata and view in admin
```bash
python manage.py ingest_toronto_metadata
***REMOVED*** Visit http://localhost:8000/admin/toronto_data/ckandataset/
```

***REMOVED******REMOVED******REMOVED*** Download traffic data and query via API
```bash
python manage.py download_toronto_data --dataset traffic-volumes-at-intersections-for-all-modes
***REMOVED*** Query: http://localhost:8000/api/toronto/traffic/?date=2024-01-15
```

***REMOVED******REMOVED******REMOVED*** Full pipeline with specific dataset
```bash
python manage.py ingest_toronto_full --datasets ttc-subway-delay-data
```

***REMOVED******REMOVED*** Performance Optimization

***REMOVED******REMOVED******REMOVED*** Bulk Inserts
The download service uses Django's `bulk_create()` with configurable batch sizes (default: 1000 rows) for efficient data import.

***REMOVED******REMOVED******REMOVED*** Incremental Updates
- Tracks `last_modified` and `last_downloaded_at` timestamps
- Only downloads files that have been updated
- Configurable truncate vs append strategy

***REMOVED******REMOVED******REMOVED*** Database Indexes
All models include strategic indexes on:
- Date fields for time-based queries
- Foreign keys for joins
- Search fields (location, station, business name)
- Spatial coordinates (latitude, longitude)

***REMOVED******REMOVED*** Testing

Run Django checks:
```bash
python manage.py check
```

Test metadata ingestion (dry run):
```bash
python manage.py ingest_toronto_metadata --verbosity 2
```

***REMOVED******REMOVED*** Troubleshooting

***REMOVED******REMOVED******REMOVED*** SSL Certificate Errors
Toronto's CKAN API uses an internal TLS chain. For development, SSL verification is disabled by default. For production, install `certifi` and set `TORONTO_CKAN_SSL_VERIFY=true`.

***REMOVED******REMOVED******REMOVED*** CSV Parsing Errors
The download service stores raw CSV data in the `raw_data` JSONField for debugging. Check this field if parsing fails.

***REMOVED******REMOVED******REMOVED*** Missing Dependencies
```bash
pip install -r requirements.txt
python manage.py migrate toronto_data
```
