# OSM-Wikidata Primitives → Django DB → Vue Map Hydration

> **Focus:** how OSM and Wikidata primitives are materialized into Django
> database records during `init_planet`, and how those records hydrate the
> Vue 3 frontend's map visualization and search-filter configuration.
>
> **Key idea:** the DB is the single source of truth for all OSM/Wikidata/
> Geofabrik metadata. The frontend does not discover countries from the
> filesystem or call external APIs at runtime. It reads DB-backed REST
> endpoints that filter on `is_geovectors_supported`. The data contract
> bridging the backend primitives and the frontend visualization is
> `country_relations.json`, fused into DB rows during `init_planet`.

---

## 1. The Primitive → DB → Frontend Flow

**Primitives (external sources):**
- Planet PBF (155GB history)
- Geofabrik metadata (poly files, URLs)
- Wikidata SPARQL (QIDs, admin levels)
- WorldKG Ontology (`WorldKG_Ontolgy.ttl`)
- GeoVectors TSVs (pre-trained embeddings)
- `country_relations.json` (glue data contract)

**Django DB (single source of truth), via `init_planet` (16 steps: register,
extract, sync, prebuild, ...):**
- default DB (port 5432):
  - `CountryPipelineProfile` ← ISO, slug, Wikidata URI, Geofabrik URL,
    GeoVectors TSV path, bbox
  - `SubgraphProfile` ← slug, QID, admin_level, bbox, paths
  - `EligibleCountry` ← `is_geovectors_supported` (READY/PENDING)
  - `OsmBoundary` ← high-precision bbox from recipes
  - `OSMWikiDataHierarchy` ← admin_level hierarchy
- Redis cache:
  - `worldkg:class:*` ← ontology classes (from TTL)
  - `worldkg:wikidata_to_wkg` ← QID → class map

**REST endpoints (`api/views/`):**
- `/api/recipes/regions-map-data/`
- `/api/snapshot-jobs/<iso>/`
- `/api/nca/subdivisions/?country_code=XX`

**Vue 3 frontend (`frontend-v3/`):**
- `WorldKGMap.vue`, Leaflet map, eligibility filtering
- `Home.vue`, year selector, pipeline controls
- `SemanticSearchPanel.vue`, search filters (QIDs, subdivisions)
- `PlanetInitPanel.vue`, planet init controls

---

## 2. The `country_relations.json` Glue Layer

**Source:** `data/country_relations.json` (in the backend data directory)

`country_relations.json` is the data contract that fuses OSM + Wikidata +
Geofabrik + GeoVectors metadata into a single per-country record. Each
entry contains:

```json
{
  "cuba": {
    "iso": "CU",
    "slug": "cuba",
    "name": "Cuba",
    "wikidata_uri": "http://www.wikidata.org/entity/Q241",
    "geofabrik_url": "https://download.geofabrik.de/...",
    "geovectors_tags_tsv": "cuba-tags.tsv.gz",
    "geovectors_location_tsv": "cuba-location.tsv.gz",
    "bbox": [-84.97, 19.83, -74.13, 23.38],
    "osm_relation_id": 3999234,
    "continent": "central-america"
  }
}
```

During `init_planet` step 3 (`sync_hierarchy`), this file is imported into
the DB via `import_country_relations`, creating `CountryPipelineProfile`
rows. The DB becomes the single source of truth; the JSON file is no longer
read at runtime.

---

## 3. DB-Backed Configuration Models

### 3.1 CountryPipelineProfile

**Source:** `core/models/country_profile.py`

| Field | Source | Purpose |
|---|---|---|
| `iso_code` | `country_relations.json` | Primary key for country lookup |
| `slug` | `country_relations.json` | Hyphenated slug (matches GeoVectors TSV filenames) |
| `osm_relation_id` | Wikidata SPARQL | OSM relation ID for osmium extract |
| `country_relations_payload` | Full JSON entry | Wikidata/Geofabrik/GeoVectors metadata |
| `bbox` | `country_relations.json` / `OsmBoundary` | Canonical bounding box |
| `geovectors_tags_tsv_path` | `prebuild_country_paths` | Resolved filesystem path |
| `geovectors_location_tsv_path` | `prebuild_country_paths` | Resolved filesystem path |

### 3.2 SubgraphProfile

| Field | Source | Purpose |
|---|---|---|
| `country_profile` | FK to `CountryPipelineProfile` | Parent country |
| `slug` | Geofabrik hierarchy | Hyphenated subdivision slug |
| `wikidata_id` | Wikidata SPARQL | QID for subdivision search |
| `admin_level` | Wikidata | 4/6/8 (state/county/city) |
| `bbox` | Geofabrik poly / `OsmBoundary` | Subdivision bounding box |
| `subgraph_pbf_path` | `prebuild_subgraphs` | PBF file for this subdivision |
| `subgraph_poly_path` | `prebuild_subgraphs` | Poly file for spatial filtering |
| `subgraph_pickle_path` | `prebuild_subgraphs` | NLE pickle path (if any) |

### 3.3 EligibleCountry

| Field | Source | Purpose |
|---|---|---|
| `country_code` | `scan_embeddings` | ISO code |
| `status` | `scan_embeddings` | READY / PENDING |
| `tsv_path` | `scan_embeddings` | Path to the country's TSV |

`is_geovectors_supported` is derived from `EligibleCountry.status == READY`.

---

## 4. Frontend Hydration: Map Eligibility

### 4.1 The REST Endpoint

`GET /api/recipes/regions-map-data/` returns a list of countries with their
`is_geovectors_supported` flag:

```json
[
  {
    "iso": "CU",
    "name": "Cuba",
    "is_geovectors_supported": true,
    "bbox": [-84.97, 19.83, -74.13, 23.38]
  },
  {
    "iso": "CR",
    "name": "Costa Rica",
    "is_geovectors_supported": false,
    "bbox": [...]
  }
]
```

### 4.2 Vue Map Eligibility Pattern

**Source:** `frontend-v3/src/components/WorldKGMap.vue`

The frontend determines which countries can run the pipeline via
`is_geovectors_supported`:

- **Map**: unsupported countries render with `fillColor: 'transparent'`,
  `color: 'transparent'`, no hover/click handlers
- **Sidebar dropdown**: filters to supported countries only
- **Pipeline button**: disabled for unsupported selections
- **Click on unsupported country**: silently ignored

The map visualization is **hydrated from DB records**. The frontend does not
discover countries from the filesystem or call external APIs at runtime. The
DB is the single source of truth.

---

## 5. Frontend Hydration: Search Filters

### 5.1 Subdivision Search by Wikidata QID

All search endpoints (gv-tags, gv-nle, hybrid, triplet-search) accept an
optional `subdivision_qid` param. The `subdivision_resolver` utility
resolves QIDs to bbox/polygon via `SubgraphProfile` records.

1. User selects "Havana" in the search panel.
2. `GET /api/nca/subdivisions/?country_code=CU` returns `SubgraphProfile`
   rows for Cuba (Havana, Santiago, ...).
3. User picks Havana (QID: Q1565).
4. Search request: `/api/nca/execute-query/?country_code=CU
   &subdivision_qid=Q1565&...`
5. `subdivision_resolver.resolve(Q1565)` →
   `SubgraphProfile.objects.get(wikidata_id='Q1565')` → bbox →
   `geom__within` filter on the `OsmEntity` query.

### 5.2 Year Selector from SnapshotJob

`Home.vue` reads `SnapshotJob` records for snapshot date badges:

```
GET /api/snapshot-jobs/CU/
  → [{snapshot_date: "2025_12_31", status: "COMPLETED"}, ...]

Frontend renders year selector with badges:
  2025_12_31 ✓ (COMPLETED)
  2024_12_31 ✓ (COMPLETED)
  2023_12_31 ⏳ (RUNNING)
```

---

## 6. Slug Conventions

- **Country slugs**: hyphens (not underscores) to match on-disk GeoVectors
  TSV filenames. E.g. `cuba`, `united-kingdom`, `czech-republic`.
- **Subgraph slugs**: hyphenated, derived from the Geofabrik hierarchy. E.g.
  `havana`, `santiago-de-cuba`.
- **Special cases**:
  - `great-britain` TSVs are copied to `united-kingdom` naming (step 7,
    `copy_gb_to_uk`)
  - Multi-country TSVs (GB, MY/SG/BN) are split into per-country TSVs
    (step 9, `split_embeddings`)
  - US regional shards (5 files) are merged into single US TSVs (step 10,
    `merge_us_embeddings`)

---

## 7. CDD (Config-Driven Development) Principles

- **NEVER hardcode** country metadata, bounding boxes, or slugs.
- **Wikidata QID is the primary identifier** for countries. ISO codes are
  secondary attributes.
- All country lookups go through `CountryOverrideService`,
  `HierarchyCacheService`, or `get_country_relations_dict()`.
- The Wikidata admin_level hierarchy IS the pipeline config:
  - `admin_level=2` → Country (pipeline entry point)
  - `admin_level=4/6/8` → Subgraphs (parallel tasks from Celery group)
- Subgraphs are discovered from the DB (`SubgraphProfile`) first, with
  filesystem fallback.
- OSM + Wikidata are the two external configuration sources.
- `import_country_relations` command fuses OSM + Wikidata config into DB.

---

## 8. Invariants

- The DB is the single source of truth for all country/subdivision metadata.
- The frontend does not discover countries from the filesystem or call
  external APIs at runtime.
- `is_geovectors_supported` is derived from `EligibleCountry.status`.
- Country slugs use hyphens (not underscores) to match GeoVectors TSV
  filenames.
- `country_relations.json` is the glue data contract, fused into DB rows
  during `init_planet` step 3.
- Subdivision search uses Wikidata QIDs resolved via `SubgraphProfile`
  records.
