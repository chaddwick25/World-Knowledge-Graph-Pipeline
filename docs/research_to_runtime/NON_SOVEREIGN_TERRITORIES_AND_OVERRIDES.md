# Non-Sovereign Territories & JSON Overrides Architecture

## Overview

This document describes how the pipeline handles **non-sovereign territories** (Wales, Scotland, England, Crown Dependencies) and **configuration overrides** through a unified JSON-driven architecture. The system uses two primary files:

1. **`overrides.json`**: Central configuration for country mappings, embedding splits/merges, and edge cases
2. **`non_sovereign_territories.py`**: Python registry for synthetic ISO codes (internal use)

---

## 1. File Locations

| File | Path | Purpose |
|------|------|---------|
| `overrides.json` | `settings.OVERRIDES_JSON_PATH` (env var, default `COLD_STORAGE_BASE_DIR/overrides.json`) | Cold storage, country overrides, embedding splits/merges, shared embeddings |
| `non_sovereign_territories.py` | `backend/core/services/planet_init/non_sovereign_territories.py` | Python registry for synthetic ISO codes |

---

## 2. `overrides.json` Structure

```json
{
  "country_iso_overrides": { },
  "embedding_edge_cases": { },
  "embedding_splits": { "splits": [], "merges": [] },
  "continents": { }
}
```

### 2.1 `country_iso_overrides`

Maps **official ISO codes** to pipeline-specific slugs for Geofabrik/embedding alignment.

```json
"country_iso_overrides": {
  "BS": { "country_slug": "the_bahamas" },
  "ZA": { "country_slug": "south_africa" },
  "GB": { "embedding_slug": "great-britain", "country_slug": "united-kingdom" },
  "VU": { "embedding_slug": "vanuatu", "country_slug": "wallis-et-futuna" },
  "IE": { "country_slug": "ireland" }
}
```

| Field | Description |
|-------|-------------|
| `country_slug` | Filesystem slug for embeddings/downloads |
| `embedding_slug` | GeoVectors dataset directory name |

---

### 2.2 `embedding_edge_cases`

Handles **shared embedding shards** across countries (memory optimization).

```json
"embedding_edge_cases": {
  "poland": {
    "shared_location_overrides": [{
      "override_type": "shared_locations",
      "source": "europe-east",
      "targets": ["poland"],
      "note": "Poland borrows europe-east location index"
    }]
  },
  "germany": {
    "shared_location_overrides": [{
      "override_type": "shared_locations",
      "source": "germany-node-location",
      "targets": ["germany-node-location", "germany-ways-location"]
    }],
    "shared_tag_overrides": [{
      "override_type": "shared_tags",
      "source": "germany-node-tags",
      "targets": ["germany-ways-tags"]
    }]
  },
  "united_states": {
    "shared_location_overrides": [{
      "override_type": "shared_locations",
      "source": "us-west",
      "targets": ["us-other"]
    }]
  }
}
```

| Override Type | Use Case |
|---------------|----------|
| `shared_locations` | Multiple shards reuse same location embeddings |
| `shared_tags` | Multiple shards reuse same tag embeddings |

---

### 2.3 `embedding_splits`: **Core Split/Merge Configuration**

Defines **spatial splits** (one TSV → many) and **merges** (many TSVs → one).

> **Boot-time validation (2026-09-11)**: `python manage.py verify_paths`
> loads `OVERRIDES_JSON_PATH` and checks every
> `embedding_splits.splits[].targets[].poly` against
> `POLYGON_FILES_DIR/overrides/` then root (the same resolution order as
> `EmbeddingSpatialSplitService.resolve_poly_path`), plus split `output`
> parent dirs (auto-created). Missing polys are ✗ errors; `--strict` exits 1.
> A broken overrides.json is now caught at boot, not mid-pipeline.

#### Splits

```json
"embedding_splits": {
  "splits": [
    {
      "source_tsv": "europe/great-britain-location/great-britain-location.tsv.gz",
      "continent": "europe",
      "continent_pbf": "europe",
      "targets": [
        {"slug": "scotland", "poly": "europe/united_kingdom/scotland.poly", "output": "scotland-location.tsv.gz"},
        {"slug": "england",  "poly": "europe/united_kingdom/england.poly",  "output": "england-location.tsv.gz"},
        {"slug": "wales",    "poly": "europe/united_kingdom/wales.poly",    "output": "wales-location.tsv.gz"}
      ]
    },
    {
      "source_tsv": "asia/asia-location/malaysia-singapore-brunei-location.tsv.gz",
      "continent": "asia",
      "continent_pbf": "asia",
      "targets": [
        {"slug": "malaysia", "poly": "asia/malaysia_singapore_brunei.poly", "output": "malaysia-location.tsv.gz"},
        {"slug": "singapore", "poly": "asia/malaysia_singapore_brunei.poly", "output": "singapore-location.tsv.gz"},
        {"slug": "brunei", "poly": "asia/malaysia_singapore_brunei.poly", "output": "brunei-location.tsv.gz"}
      ]
    },
    {
      "source_tsv": "europe/im-location/im-location.tsv.gz",
      "continent": "europe",
      "continent_pbf": "europe",
      "targets": [
        {"slug": "im", "poly": "europe/isle_of_man.poly", "output": "im-location.tsv.gz"}
      ],
      "note": "Source TSV not yet available in GeoVectors dataset"
    },
    {
      "source_tsv": "europe/channel-islands-location/channel-islands-location.tsv.gz",
      "continent": "europe",
      "continent_pbf": "europe",
      "targets": [
        {"slug": "gg", "poly": "europe/guernsey_jersey.poly", "output": "gg-location.tsv.gz"},
        {"slug": "je", "poly": "europe/guernsey_jersey.poly", "output": "je-location.tsv.gz"}
      ],
      "note": "Source TSV not yet available in GeoVectors dataset"
    }
  ],
  "merges": [
    {
      "slug": "us",
      "continent": "north-america",
      "output": "us-location.tsv.gz",
      "shards": [
        "north-america/us-other-location/us-midwest-location.tsv.gz",
        "north-america/us-other-location/us-northeast-location.tsv.gz",
        "north-america/us-other-location/us-pacific-location.tsv.gz",
        "north-america/us-south-location/us-south-location.tsv.gz",
        "north-america/us-other-location/us-west-location.tsv.gz"
      ]
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `source_tsv` | Path relative to `EMBEDDINGS_ROOT` |
| `continent` | Continent for PBF snapshot |
| `continent_pbf` | PBF file key (matches `CONTINENTS_ROOT`) |
| `targets[].slug` | **Lowercase ISO code** (e.g., `im`, `gg`, `je`) |
| `targets[].poly` | Polygon file relative to `POLYGON_FILES_DIR` |
| `targets[].output` | Output filename |

---

## 3. Non-Sovereign Territories Registry

**File**: `backend/core/services/planet_init/non_sovereign_territories.py`

Maps territories without official ISO 3166-1 codes to **synthetic 2-letter codes**.

### Registry

```python
NON_SOVEREIGN_TERRITORIES = {
    "WL": {  # Wales
        "name": "Wales",
        "slug": "wales",
        "continent": "europe",
        "parent_iso": "GB",
        "wikidata_qid": "Q25",
        "osm_relation_id": 58437,
    },
    "XS": {  # Scotland
        "name": "Scotland",
        "slug": "scotland",
        "continent": "europe",
        "parent_iso": "GB",
        "wikidata_qid": "Q22",
        "osm_relation_id": 58446,
    },
    "EN": {  # England
        "name": "England",
        "slug": "england",
        "continent": "europe",
        "parent_iso": "GB",
        "wikidata_qid": "Q21",
        "osm_relation_id": 58447,
    },
    "IM": {  # Isle of Man (real ISO: IM)
        "name": "Isle of Man",
        "slug": "im",
        "continent": "europe-west",
        "parent_iso": "GB",
        "wikidata_qid": "Q9676",
        "osm_relation_id": 62269,
    },
    "GG": {  # Guernsey (real ISO: GG)
        "name": "Guernsey",
        "slug": "gg",
        "continent": "europe-west",
        "parent_iso": "GB",
        "wikidata_qid": "Q423",
        "osm_relation_id": 270747,
    },
    "JE": {  # Jersey (real ISO: JE)
        "name": "Jersey",
        "slug": "je",
        "continent": "europe-west",
        "parent_iso": "GB",
        "wikidata_qid": "Q270747",
        "osm_relation_id": 270747,
    },
}
```

### Public API

```python
from core.services.planet_init.non_sovereign_territories import (
    is_non_sovereign_synthetic_iso,
    resolve_non_sovereign_iso,
    synthetic_iso_to_info,
    get_all_synthetic_isos,
)

# Check if code is synthetic
is_non_sovereign_synthetic_iso("WL")  # True
is_non_sovereign_synthetic_iso("GB")  # False

# Resolve name/slug to synthetic ISO
resolve_non_sovereign_iso("wales")      # "WL"
resolve_non_sovereign_iso("scotland")   # "XS"
resolve_non_sovereign_iso("im")         # "IM"
resolve_non_sovereign_iso("gg")         # "GG"
resolve_non_sovereign_iso("je")         # "JE"

# Get metadata
synthetic_iso_to_info("WL")  # {name, slug, continent, parent_iso, wikidata_qid, osm_relation_id}
```

---

## 4. Integration Points

### 4.1 ISO Resolution Flow (API → Pipeline)

```python
# backend/api/views/pipeline_start.py: WorldKGPipelineV2StartView.post()

def resolve_iso(country_name):
    # Tier 1: CountryPipelineProfile.canonical_name__iexact
    profile = CountryPipelineProfile.objects.filter(canonical_name__iexact=country_name).first()
    if profile: return profile.iso_code

    # Tier 2: resolve_iso_code()
    from core.services.planet_init.osm_wikidata_resolver import resolve_iso_code
    iso = resolve_iso_code(country_name)
    if iso: return iso

    # Tier 3: EligibleCountry lookup
    from core.models import EligibleCountry
    ec = EligibleCountry.objects.filter(country_name__iexact=country_name).first()
    if ec: return ec.iso_code

    return None
```

**`resolve_iso_code()`** now checks non-sovereign registry:
```python
# backend/core/services/planet_init/country_iso.py

def resolve_iso_code(name):
    # ... existing logic ...

    # Check non-sovereign territories
    try:
        from core.services.planet_init.non_sovereign_territories import resolve_non_sovereign_iso
        synthetic_iso = resolve_non_sovereign_iso(name)
        if synthetic_iso:
            return synthetic_iso
    except Exception:
        pass

    # ... legacy name maps ...
```

---

### 4.2 `CountryEnvelope.from_db()`: Synthetic ISO Handling

```python
# backend/pipeline/envelopes.py

@classmethod
def from_db(cls, iso_code: str) -> "CountryEnvelope":
    # Detect synthetic ISO
    if is_non_sovereign_synthetic_iso(iso_code):
        iso_upper = iso_code.upper()
        info = synthetic_iso_to_info(iso_upper)
        if info:
            # Create synthetic CountryPipelineProfile on-the-fly
            profile, _ = CountryPipelineProfile.objects.get_or_create(
                iso2=iso_upper,
                defaults={
                    "canonical_name": info["name"],
                    "canonical_slug": info["slug"],
                    "continent": info["continent"],
                    "osm_relation_id": info["osm_relation_id"],
                    "wikidata_qid": info["wikidata_qid"],
                    "parent_iso": info["parent_iso"],
                }
            )
            return cls.from_profile(profile)

    # ... normal sovereign country flow ...
```

---

### 4.3 `preprocess_snapshot()`: Extraction

When `SnapshotExtractionService` fails with "not found in mapping":

```python
# backend/pipeline/tasks/helper.py

def preprocess_snapshot(cfg, logger):
    try:
        return SnapshotExtractionService.run_pipeline(cfg)
    except Exception as exc:
        if "not found in mapping" in str(exc) and is_non_sovereign_synthetic_iso(cfg.iso):
            # Non-sovereign territories bypass the sovereign-only hierarchy.
            # Extraction uses the territory's OSM relation ID directly.
            ...
        raise
```

The previous `_preprocess_synthetic_territory` fallback has been removed. The helper now uses `SnapshotExtractionService` for all extraction paths. Non-sovereign territories that fail the hierarchy lookup are handled by the `CountryEnvelope.from_db()` synthetic ISO branch, which creates a `CountryPipelineProfile` on-the-fly with the territory's OSM relation ID.

---

### 4.4 `scan_embeddings`: Auto-Populate ISO Codes

```python
# backend/core/management/commands/scan_embeddings.py

def _build_targets_from_config():
    cfg = load_embedding_splits_config()
    for split in cfg.get("splits", []):
        for target in split.get("targets", []):
            slug = target["slug"]  # e.g., "wales", "im", "gg"
            iso = resolve_non_sovereign_iso(slug)  # Returns "WL", "IM", "GG"
            # Creates/updates EligibleCountry with iso_code=iso
```

---

### 4.5 Split Execution: `EmbeddingSpatialSplitService`

```python
# backend/core/services/snapshot/embedding_spatial_split_service.py

def load_embedding_splits_config():
    """Reads embedding_splits from overrides.json via load_overrides()."""
    overrides = load_overrides()  # Loads settings.OVERRIDES_JSON_PATH
    return overrides.get("embedding_splits", {"splits": [], "merges": []})
```

Run splits:
```bash
docker compose exec backend python manage.py preprocess_embeddings
```

---

## 5. Running the Pipeline

### Clean End-to-End Test

```bash
# 1. Clean generated files
rm -rf /media/.../OSM/embeddings/europe/scotland/
rm -rf /media/.../OSM/embeddings/europe/england/
rm -rf /media/.../OSM/embeddings/europe/wales/
rm -rf /media/.../OSM/embeddings/asia/malaysia/
rm -rf /media/.../OSM/embeddings/asia/singapore/
rm -rf /media/.../OSM/embeddings/asia/brunei/
rm -rf /media/.../OSM/embeddings/north-america/us/

# 2. Clear DB
docker compose exec backend python manage.py shell -c "
from core.models import EligibleCountry
EligibleCountry.objects.all().delete()
"

# 3. Run splits (GB + MSB)
docker compose exec backend python manage.py preprocess_embeddings

# 4. Validate
docker compose exec backend python manage.py scan_embeddings
```

### Expected Output

```
Scotland  → READY ✓ location
England   → READY ✓ location
Wales     → READY ✓ location
Malaysia  → READY ✓ location
Singapore → READY ✓ location
Brunei    → READY ✓ location
US        → READY ✓ location
Im        → NO_EMBEDDINGS   (source TSV not available)
Gg        → NO_EMBEDDINGS   (source TSV not available)
Je        → NO_EMBEDDINGS   (source TSV not available)
```

---

## 6. Adding New Non-Sovereign Territories

### Step 1: Add to `non_sovereign_territories.py`

```python
"XX": {  # New territory
    "name": "Territory Name",
    "slug": "xx",  # lowercase ISO or mnemonic
    "continent": "europe-west",
    "parent_iso": "GB",
    "wikidata_qid": "QXXXXX",
    "osm_relation_id": 123456,
},
```

### Step 2: Add Split Config to `overrides.json`

```json
{
  "source_tsv": "europe/xx-location/xx-location.tsv.gz",
  "continent": "europe",
  "continent_pbf": "europe",
  "targets": [
    {"slug": "xx", "poly": "europe/territory.poly", "output": "xx-location.tsv.gz"}
  ],
  "note": "Source TSV availability status"
}
```

### Step 3: Ensure Files Exist

| File | Location |
|------|----------|
| `.poly` boundary | `/app/data/osm_polygon_files/europe/territory.poly` |
| Source TSV | `COLD_STORAGE_BASE_DIR/embeddings/europe/xx-location/xx-location.tsv.gz` |

---

## 7. Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Synthetic ISO codes** (WL, XS, EN) | No ISO 3166-1 collision; internal-only identifiers |
| **Real ISO for Crown Dependencies** (IM, GG, JE) | They have official ISO codes; no synthetic needed |
| **Lowercase slugs in split config** | Matches filesystem conventions (`im-location.tsv.gz`) |
| **Unified `overrides.json`** | Single source of truth for splits, merges, edge cases |
| **Registry in Python** | Fast lookups, type safety, reverse mappings |
| **Synthetic ISO handling in `CountryEnvelope.from_db()`** | Creates `CountryPipelineProfile` on-the-fly, bypassing sovereign-only hierarchy |

---

## 8. Troubleshooting

| Issue | Resolution |
|-------|------------|
| "not found in mapping" for Wales/Scotland/England | `CountryEnvelope.from_db()` creates a synthetic `CountryPipelineProfile` via the non-sovereign registry |
| `scan_embeddings` shows NO_EMBEDDINGS for IM/GG/JE | Source TSV not in GeoVectors dataset; check `overrides.json` note |
| Split validation fails | Ensure `.poly` file exists at `POLYGON_FILES_DIR/europe/...` |
| ISO resolution fails | Check `resolve_non_sovereign_iso()` for name/slug matches |

---

## 9. Files Summary

| File | Path |
|------|------|
| Non-sovereign registry + API | `backend/core/services/planet_init/non_sovereign_territories.py` |
| Split execution | `backend/core/services/snapshot/embedding_spatial_split_service.py` |
| Loads overrides.json | `backend/core/services/snapshot/country_override_service.py` |
| Runs splits (management command) | `backend/core/management/commands/preprocess_embeddings.py` |
| Validates + populates DB (command) | `backend/core/management/commands/scan_embeddings.py` |
| `CountryEnvelope.from_db()` (synthetic ISO) | `backend/pipeline/envelopes.py` |
| `preprocess_snapshot()` | `backend/pipeline/tasks/helper.py` |
| `resolve_iso_code()` | `backend/core/services/planet_init/country_iso.py` |
| Pipeline start view (3-tier ISO resolution) | `backend/api/views/pipeline_start.py` |
| Central config (splits, merges, overrides) | `settings.OVERRIDES_JSON_PATH` (env var, default `COLD_STORAGE_BASE_DIR/overrides.json`) |
