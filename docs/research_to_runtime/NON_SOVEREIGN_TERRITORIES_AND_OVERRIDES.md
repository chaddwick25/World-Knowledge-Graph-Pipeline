***REMOVED*** Non-Sovereign Territories & JSON Overrides Architecture

***REMOVED******REMOVED*** Overview

This document describes how the pipeline handles **non-sovereign territories** (Wales, Scotland, England, Crown Dependencies) and **configuration overrides** through a unified JSON-driven architecture. The system uses two primary files:

1. **`overrides.json`**: Central configuration for country mappings, embedding splits/merges, and edge cases
2. **`non_sovereign_territories.py`**: Python registry for synthetic ISO codes (internal use)

---

***REMOVED******REMOVED*** 1. File Locations

| File | Path | Purpose |
|------|------|---------|
| `overrides.json` | `settings.OVERRIDES_JSON_PATH` (env var, default `COLD_STORAGE_BASE_DIR/overrides.json`) | Cold storage, country overrides, embedding splits/merges, shared embeddings |
| `non_sovereign_territories.py` | `backend/core/services/planet_init/non_sovereign_territories.py` | Python registry for synthetic ISO codes |

---

***REMOVED******REMOVED*** 2. `overrides.json` Structure

```json
{
  "country_iso_overrides": { },
  "embedding_edge_cases": { },
  "embedding_splits": { "splits": [], "merges": [] },
  "continents": { }
}
```

***REMOVED******REMOVED******REMOVED*** 2.1 `country_iso_overrides`

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

***REMOVED******REMOVED******REMOVED*** 2.2 `embedding_edge_cases`

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

***REMOVED******REMOVED******REMOVED*** 2.3 `embedding_splits`: **Core Split/Merge Configuration**

Defines **spatial splits** (one TSV → many) and **merges** (many TSVs → one).

> **Boot-time validation (2026-09-11)**: `python manage.py verify_paths`
> loads `OVERRIDES_JSON_PATH` and checks every
> `embedding_splits.splits[].targets[].poly` against
> `POLYGON_FILES_DIR/overrides/` then root (the same resolution order as
> `EmbeddingSpatialSplitService.resolve_poly_path`), plus split `output`
> parent dirs (auto-created). Missing polys are ✗ errors; `--strict` exits 1.
> A broken overrides.json is now caught at boot, not mid-pipeline.

***REMOVED******REMOVED******REMOVED******REMOVED*** Splits

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

***REMOVED******REMOVED*** 3. Non-Sovereign Territories Registry

**File**: `backend/core/services/planet_init/non_sovereign_territories.py`

Maps territories without official ISO 3166-1 codes to **synthetic 2-letter codes**.

***REMOVED******REMOVED******REMOVED*** Registry

```python
NON_SOVEREIGN_TERRITORIES = {
    "WL": {  ***REMOVED*** Wales
        "name": "Wales",
        "slug": "wales",
        "continent": "europe",
        "parent_iso": "GB",
        "wikidata_qid": "Q25",
        "osm_relation_id": 58437,
    },
    "XS": {  ***REMOVED*** Scotland
        "name": "Scotland",
        "slug": "scotland",
        "continent": "europe",
        "parent_iso": "GB",
        "wikidata_qid": "Q22",
        "osm_relation_id": 58446,
    },
    "EN": {  ***REMOVED*** England
        "name": "England",
        "slug": "england",
        "continent": "europe",
        "parent_iso": "GB",
        "wikidata_qid": "Q21",
        "osm_relation_id": 58447,
    },
    "IM": {  ***REMOVED*** Isle of Man (real ISO: IM)
        "name": "Isle of Man",
        "slug": "im",
        "continent": "europe-west",
        "parent_iso": "GB",
        "wikidata_qid": "Q9676",
        "osm_relation_id": 62269,
    },
    "GG": {  ***REMOVED*** Guernsey (real ISO: GG)
        "name": "Guernsey",
        "slug": "gg",
        "continent": "europe-west",
        "parent_iso": "GB",
        "wikidata_qid": "Q423",
        "osm_relation_id": 270747,
    },
    "JE": {  ***REMOVED*** Jersey (real ISO: JE)
        "name": "Jersey",
        "slug": "je",
        "continent": "europe-west",
        "parent_iso": "GB",
        "wikidata_qid": "Q270747",
        "osm_relation_id": 270747,
    },
}
```

***REMOVED******REMOVED******REMOVED*** Public API

```python
from core.services.planet_init.non_sovereign_territories import (
    is_non_sovereign_synthetic_iso,
    resolve_non_sovereign_iso,
    synthetic_iso_to_info,
    get_all_synthetic_isos,
)

***REMOVED*** Check if code is synthetic
is_non_sovereign_synthetic_iso("WL")  ***REMOVED*** True
is_non_sovereign_synthetic_iso("GB")  ***REMOVED*** False

***REMOVED*** Resolve name/slug to synthetic ISO
resolve_non_sovereign_iso("wales")      ***REMOVED*** "WL"
resolve_non_sovereign_iso("scotland")   ***REMOVED*** "XS"
resolve_non_sovereign_iso("im")         ***REMOVED*** "IM"
resolve_non_sovereign_iso("gg")         ***REMOVED*** "GG"
resolve_non_sovereign_iso("je")         ***REMOVED*** "JE"

***REMOVED*** Get metadata
synthetic_iso_to_info("WL")  ***REMOVED*** {name, slug, continent, parent_iso, wikidata_qid, osm_relation_id}
```

---

***REMOVED******REMOVED*** 4. Integration Points

***REMOVED******REMOVED******REMOVED*** 4.1 ISO Resolution Flow (API → Pipeline)

```python
***REMOVED*** backend/api/views/pipeline_start.py: WorldKGPipelineV2StartView.post()

def resolve_iso(country_name):
    ***REMOVED*** Tier 1: CountryPipelineProfile.canonical_name__iexact
    profile = CountryPipelineProfile.objects.filter(canonical_name__iexact=country_name).first()
    if profile: return profile.iso_code

    ***REMOVED*** Tier 2: resolve_iso_code()
    from core.services.planet_init.osm_wikidata_resolver import resolve_iso_code
    iso = resolve_iso_code(country_name)
    if iso: return iso

    ***REMOVED*** Tier 3: EligibleCountry lookup
    from core.models import EligibleCountry
    ec = EligibleCountry.objects.filter(country_name__iexact=country_name).first()
    if ec: return ec.iso_code

    return None
```

**`resolve_iso_code()`** now checks non-sovereign registry:
```python
***REMOVED*** backend/core/services/planet_init/country_iso.py

def resolve_iso_code(name):
    ***REMOVED*** ... existing logic ...

    ***REMOVED*** Check non-sovereign territories
    try:
        from core.services.planet_init.non_sovereign_territories import resolve_non_sovereign_iso
        synthetic_iso = resolve_non_sovereign_iso(name)
        if synthetic_iso:
            return synthetic_iso
    except Exception:
        pass

    ***REMOVED*** ... legacy name maps ...
```

---

***REMOVED******REMOVED******REMOVED*** 4.2 `CountryEnvelope.from_db()`: Synthetic ISO Handling

```python
***REMOVED*** backend/pipeline/envelopes.py

@classmethod
def from_db(cls, iso_code: str) -> "CountryEnvelope":
    ***REMOVED*** Detect synthetic ISO
    if is_non_sovereign_synthetic_iso(iso_code):
        iso_upper = iso_code.upper()
        info = synthetic_iso_to_info(iso_upper)
        if info:
            ***REMOVED*** Create synthetic CountryPipelineProfile on-the-fly
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

    ***REMOVED*** ... normal sovereign country flow ...
```

---

***REMOVED******REMOVED******REMOVED*** 4.3 `preprocess_snapshot()`: Extraction

When `SnapshotExtractionService` fails with "not found in mapping":

```python
***REMOVED*** backend/pipeline/tasks/helper.py

def preprocess_snapshot(cfg, logger):
    try:
        return SnapshotExtractionService.run_pipeline(cfg)
    except Exception as exc:
        if "not found in mapping" in str(exc) and is_non_sovereign_synthetic_iso(cfg.iso):
            ***REMOVED*** Non-sovereign territories bypass the sovereign-only hierarchy.
            ***REMOVED*** Extraction uses the territory's OSM relation ID directly.
            ...
        raise
```

The previous `_preprocess_synthetic_territory` fallback has been removed. The helper now uses `SnapshotExtractionService` for all extraction paths. Non-sovereign territories that fail the hierarchy lookup are handled by the `CountryEnvelope.from_db()` synthetic ISO branch, which creates a `CountryPipelineProfile` on-the-fly with the territory's OSM relation ID.

---

***REMOVED******REMOVED******REMOVED*** 4.4 `scan_embeddings`: Auto-Populate ISO Codes

```python
***REMOVED*** backend/core/management/commands/scan_embeddings.py

def _build_targets_from_config():
    cfg = load_embedding_splits_config()
    for split in cfg.get("splits", []):
        for target in split.get("targets", []):
            slug = target["slug"]  ***REMOVED*** e.g., "wales", "im", "gg"
            iso = resolve_non_sovereign_iso(slug)  ***REMOVED*** Returns "WL", "IM", "GG"
            ***REMOVED*** Creates/updates EligibleCountry with iso_code=iso
```

---

***REMOVED******REMOVED******REMOVED*** 4.5 Split Execution: `EmbeddingSpatialSplitService`

```python
***REMOVED*** backend/core/services/snapshot/embedding_spatial_split_service.py

def load_embedding_splits_config():
    """Reads embedding_splits from overrides.json via load_overrides()."""
    overrides = load_overrides()  ***REMOVED*** Loads settings.OVERRIDES_JSON_PATH
    return overrides.get("embedding_splits", {"splits": [], "merges": []})
```

Run splits:
```bash
docker compose exec backend python manage.py preprocess_embeddings
```

---

***REMOVED******REMOVED*** 5. Running the Pipeline

***REMOVED******REMOVED******REMOVED*** Clean End-to-End Test

```bash
***REMOVED*** 1. Clean generated files
rm -rf /media/.../OSM/embeddings/europe/scotland/
rm -rf /media/.../OSM/embeddings/europe/england/
rm -rf /media/.../OSM/embeddings/europe/wales/
rm -rf /media/.../OSM/embeddings/asia/malaysia/
rm -rf /media/.../OSM/embeddings/asia/singapore/
rm -rf /media/.../OSM/embeddings/asia/brunei/
rm -rf /media/.../OSM/embeddings/north-america/us/

***REMOVED*** 2. Clear DB
docker compose exec backend python manage.py shell -c "
from core.models import EligibleCountry
EligibleCountry.objects.all().delete()
"

***REMOVED*** 3. Run splits (GB + MSB)
docker compose exec backend python manage.py preprocess_embeddings

***REMOVED*** 4. Validate
docker compose exec backend python manage.py scan_embeddings
```

***REMOVED******REMOVED******REMOVED*** Expected Output

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

***REMOVED******REMOVED*** 6. Adding New Non-Sovereign Territories

***REMOVED******REMOVED******REMOVED*** Step 1: Add to `non_sovereign_territories.py`

```python
"XX": {  ***REMOVED*** New territory
    "name": "Territory Name",
    "slug": "xx",  ***REMOVED*** lowercase ISO or mnemonic
    "continent": "europe-west",
    "parent_iso": "GB",
    "wikidata_qid": "QXXXXX",
    "osm_relation_id": 123456,
},
```

***REMOVED******REMOVED******REMOVED*** Step 2: Add Split Config to `overrides.json`

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

***REMOVED******REMOVED******REMOVED*** Step 3: Ensure Files Exist

| File | Location |
|------|----------|
| `.poly` boundary | `/app/data/osm_polygon_files/europe/territory.poly` |
| Source TSV | `COLD_STORAGE_BASE_DIR/embeddings/europe/xx-location/xx-location.tsv.gz` |

---

***REMOVED******REMOVED*** 7. Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Synthetic ISO codes** (WL, XS, EN) | No ISO 3166-1 collision; internal-only identifiers |
| **Real ISO for Crown Dependencies** (IM, GG, JE) | They have official ISO codes; no synthetic needed |
| **Lowercase slugs in split config** | Matches filesystem conventions (`im-location.tsv.gz`) |
| **Unified `overrides.json`** | Single source of truth for splits, merges, edge cases |
| **Registry in Python** | Fast lookups, type safety, reverse mappings |
| **Synthetic ISO handling in `CountryEnvelope.from_db()`** | Creates `CountryPipelineProfile` on-the-fly, bypassing sovereign-only hierarchy |

---

***REMOVED******REMOVED*** 8. Troubleshooting

| Issue | Resolution |
|-------|------------|
| "not found in mapping" for Wales/Scotland/England | `CountryEnvelope.from_db()` creates a synthetic `CountryPipelineProfile` via the non-sovereign registry |
| `scan_embeddings` shows NO_EMBEDDINGS for IM/GG/JE | Source TSV not in GeoVectors dataset; check `overrides.json` note |
| Split validation fails | Ensure `.poly` file exists at `POLYGON_FILES_DIR/europe/...` |
| ISO resolution fails | Check `resolve_non_sovereign_iso()` for name/slug matches |

---

***REMOVED******REMOVED*** 9. Files Summary

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
