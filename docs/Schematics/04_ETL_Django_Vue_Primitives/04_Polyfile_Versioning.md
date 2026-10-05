# Polyfile Versioning: Border Geometry vs Snapshot Date

> **Focus:** how polyfiles (`.poly` boundary files) are generated and
> versioned across the pipeline. The inconsistency between country-level
> (static) and subgraph-level (timestamped) polyfiles, and whether the
> border geometry inside timestamped polyfiles corresponds to the snapshot
> date.
>
> **Key idea:** subgraph polyfiles ARE versioned by snapshot date and their
> border geometry IS extracted from the time-filtered snapshot PBF, so
> borders correspond to the date. Country-level polyfiles are NOT versioned.
> They are generated from the planet PBF (current borders) and saved to a
> static path. `resolve_country_bbox()` expects timestamped country-level
> polyfiles as a fallback, but `SnapshotExtractionService` never generates
> them. This is a known gap.

---

## 1. What a Polyfile Is

A `.poly` file is an OSM-format polygon file that describes a boundary as a
list of longitude/latitude coordinates. `osmium extract -p` uses it to clip
a larger PBF to a geographic region. The pipeline uses polyfiles at two
levels:

**Country-level poly:**
- Used by `SnapshotExtractionService` to clip continent PBF → country PBF
- Source: Geofabrik download (static) OR generated from planet PBF
- Path: `{POLYGON_FILES_DIR}/{continent}/{country}.poly` (STATIC)

**Subgraph-level poly:**
- Used by `SubgraphPbfService` to clip country snapshot PBF → subgraph PBF
- Source: generated from the timestamped snapshot PBF
- Path: `{extractions}/{continent}/{country}/subgraphs/{subgraph}/{subgraph}_{date}.osm.poly` (VERSIONED)

---

## 2. The Snapshot PBF Creation Flow (Correctly Time-Filtered)

**Source:** <ref_file file="backend/core/services/snapshot/snapshot_extraction_service.py" />

1. `osmium extract --polygon --with-history`: continent PBF (history) +
   country poly → country history PBF
2. `osmium time-filter`: country history PBF @ `{snapshot_date}T23:59:59Z`
   → timestamped snapshot PBF (`{country}_{snapshot_date}.osm.pbf`)

The snapshot PBF contains ALL OSM entities (including border relations) as
of that timestamp. Borders in this file ARE correct for the snapshot date.

<ref_snippet file="backend/core/services/snapshot/snapshot_extraction_service.py" lines="164-169" />

The `osmium time-filter` command flattens a history file to a point in time.
The snapshot PBF contains every OSM entity (including admin boundary
relations) **as it was at that timestamp**. This is correct.

**The problem is what happens next with polyfiles.**

---

## 3. Subgraph Polyfiles: Versioned AND Date-Accurate ✓

**Source:** <ref_file file="backend/core/services/snapshot/subgraph_pbf_service.py" />

1. `source_pbf = regional_path_service.get_single_snapshot_pbf_path(
   continent, country, snapshot_date)` → the timestamped snapshot PBF
2. `_extract_relation_pbf(source_pbf, relation_id, output_pbf)`: extracts
   the subgraph relation from the TIME-FILTERED snapshot PBF. Border
   geometry IS from the snapshot date.
3. `generate_high_res_poly(output_pbf, relation_id, output_poly)`: generates
   the poly from the extracted subgraph PBF. Border geometry in the polyfile
   = border as of the snapshot date.
4. Output: `{subgraph}_{snapshot_date}.osm.poly`. Filename IS timestamped,
   geometry IS date-accurate.

<ref_snippet file="backend/core/services/snapshot/subgraph_pbf_service.py" lines="214-265" />

**Verdict:** subgraph polyfiles are both **versioned** (timestamped
filename) and **date-accurate** (geometry from the time-filtered snapshot
PBF). Borders correspond to the snapshot date. ✓

---

## 4. Country-Level Polyfiles: Static AND NOT Date-Accurate ✗

**Source:** <ref_file file="backend/core/services/snapshot/snapshot_extraction_service.py" />

`_resolve_poly_file()` resolution order:
1. Caller-supplied `poly_file_path`, if provided
2. `OsmBoundary` DB lookup (`polygon_file`), static, from
   `generate_osm_boundaries`
3. Static on-disk `{country}.poly`, static, no timestamp
4. Generate from the PLANET PBF, NOT from the timestamped snapshot PBF:
   `generate_high_res_poly(planet_pbf, osm_relation_id, generated)`.
   `planet_pbf` is the 155GB history file (current state), so the border
   geometry is the CURRENT border, not the snapshot-date border. Output
   path `{country}.poly` is STATIC, no timestamp.

<ref_snippet file="backend/core/services/snapshot/snapshot_extraction_service.py" lines="282-297" />

**Verdict:** country-level polyfiles are **static** (no timestamp in the
filename) AND **not date-accurate** (geometry from the planet PBF, not the
time-filtered snapshot PBF). Borders do NOT correspond to the snapshot
date. ✗

---

## 5. The Design Intent vs Implementation Gap

### 5.1 `resolve_country_bbox()` Expects Timestamped Polyfiles

**Source:** `backend/core/services/planet_init/country_bbox.py`

The bbox resolver has 5 fallback layers. Layer 4 expects a timestamped
polyfile in the `temporal_snapshots` directory:

1. Explicit `poly_file_path` (caller-supplied), works
2. `OsmBoundary` DB (high-precision bbox), works (static)
3. `CountryPipelineProfile.country_relations_payload`, works (static)
4. Runtime snapshot `.osm.poly` (filesystem fallback), **expects a
   timestamped poly** at
   `{extractions}/{continent}/{country}/temporal_snapshots/{country}_{date}.osm.poly`,
   **NEVER GENERATED by `SnapshotExtractionService`**
5. Vectors DB extent fallback (`ST_Extent(geom)`), works (NaN guard needed)

Layer 4 looks for:
- `{snap_dir}/{pbf_path.name}.replace(".pbf", ".poly")`
- `{snap_dir}/{country}_{date}.osm.poly`

But `SnapshotExtractionService` never writes a polyfile to the
`temporal_snapshots` directory. The field
`CountryPipelineProfile.snapshot_poly_path` exists for this purpose but is
typically NULL.

### 5.2 The `snapshot_poly_path` Field

**Source:** `backend/core/models/country_profile.py`

```python
snapshot_poly_path = models.CharField(
    max_length=2048,
    null=True,
    blank=True,
    help_text="Resolved absolute path to the temporal snapshot .osm.poly",
)
```

This field was designed to hold the timestamped country-level polyfile
path. `prebuild_country_paths` looks for a timestamped polyfile on disk:

```python
snap_poly_candidate = snap_dir / f"{osm_slug}_{snapshot_date}.osm.poly"
if snap_poly_candidate.exists():
    snap_poly = str(snap_poly_candidate)
```

But since `SnapshotExtractionService` never generates this file, the field
stays NULL and the fallback never fires.

---

## 6. How Geofabrik Handles Polyfile Versioning

Geofabrik's `.poly` files are **static by design**:

- They represent the **clipping boundary** (a buffer around the country),
  not the data snapshot.
- The boundary changes rarely, only when Geofabrik updates their clipping
  polygons (via GitHub PRs to `geofabrik/polygons_download.geofabrik.de`).
- PBF files ARE timestamped (`denmark-170101.osm.pbf`), but `.poly` files
  are not.
- This is correct for Geofabrik's use case: the clipping boundary is stable,
  the data changes.

**Our pipeline has a different problem.** We generate our own polyfiles
from OSM relation boundaries (via `osmium getid` + `osmium export`), and
those boundaries **can change between snapshots** when OSM contributors edit
admin boundaries. So our generated polyfiles should be versioned, even if
Geofabrik's downloaded ones don't need to be.

### 6.1 Geofabrik Downloaded Polyfiles (Static, Correct)

**Source:** `backend/core/services/planet_init/geofabrik_poly_service.py`

- Downloaded once via `download_all_geofabrik_polygons()`
- Skips if the file already exists
- Static path: `{sanitized_full_id}.poly` (no timestamp)
- These are clipping boundaries, not admin boundaries, stable by design

### 6.2 Generated Polyfiles (Should Be Versioned, Currently Not)

**Source:** `backend/core/services/snapshot/pbf_bounding_box_service.py`

`generate_high_res_poly(pbf_file, relation_id, output_poly)`:
1. `osmium getid --with-history --add-referenced` extracts the relation
   from `pbf_file`
2. `osmium export -f geojson` converts to GeoJSON
3. Parses GeoJSON → writes the `.poly` file

The method itself is **path-agnostic**. Versioning depends on the output
path provided by the caller:
- `SubgraphPbfService` provides a timestamped path → versioned ✓
- `SnapshotExtractionService` provides a static path → not versioned ✗

---

## 7. The Inconsistency Summary

| Polyfile Type | Filename Versioned? | Geometry Date-Accurate? | Source PBF |
|---|---|---|---|
| Geofabrik download (country) | NO (static) | N/A (clipping boundary, not admin) | Geofabrik server |
| Generated country-level | NO (static) | NO (from planet PBF, current) | Planet PBF (155GB history) |
| Generated subgraph-level | YES (timestamped) | YES (from time-filtered snapshot PBF) | Timestamped snapshot PBF |
| `snapshot_poly_path` field | Designed for YES | Would be YES if generated | N/A, field is NULL |

---

## 8. Recommended Fix

To make country-level polyfiles date-accurate and versioned:

1. **In `SnapshotExtractionService`**, after the time-filter step, generate
   a country-level polyfile from the **timestamped snapshot PBF** (not from
   the planet PBF):

   ```python
   # After osmium time-filter produces snapshot_pbf_path:
   timestamped_poly = (
       Path(snapshot_pbf_path).parent
       / f"{country_slug}_{snapshot_date}.osm.poly"
   )
   pbf_bounding_box_service.generate_high_res_poly(
       str(snapshot_pbf_path),  # ← time-filtered, NOT planet PBF
       osm_relation_id,
       str(timestamped_poly),
   )
   ```

2. **Populate `CountryPipelineProfile.snapshot_poly_path`** with the
   timestamped path during `prebuild_country_paths`.

3. **Update `resolve_country_bbox()`** to prioritize the timestamped
   polyfile (layer 4) over the static one (layer 3) when a `snapshot_date`
   is provided.

This would:
- Give country-level polyfiles that reflect borders as of the snapshot date
- Make `resolve_country_bbox()` layer 4 work as designed
- Align country-level polyfiles with subgraph-level polyfiles (both
  versioned + date-accurate)

---

## 9. Why This Matters

Admin boundaries in OSM change over time:
- Countries redraw provincial/state borders
- New municipalities are created
- Boundaries are refined with better GPS data
- Disputed territories change

If we use a static (current) polyfile to clip a historical snapshot PBF, we
may:
- **Include entities** that weren't in the country at the snapshot date
  (the border has since expanded)
- **Exclude entities** that were in the country at the snapshot date (the
  border has since contracted)
- **Misassign entities** to the wrong subgraph (subgraph borders have
  changed)

The subgraph-level polyfiles already handle this correctly (geometry from
the time-filtered snapshot PBF). The country-level polyfiles should do the
same.

---

## 10. Invariants

- Subgraph polyfiles are versioned AND date-accurate (geometry from the
  time-filtered snapshot PBF).
- Country-level polyfiles are static AND NOT date-accurate (geometry from
  the planet PBF).
- `resolve_country_bbox()` layer 4 expects timestamped country-level
  polyfiles but they're never generated. Known gap.
- `CountryPipelineProfile.snapshot_poly_path` exists for timestamped
  polyfiles but is typically NULL.
- Geofabrik's `.poly` files are static by design (clipping boundary, not
  admin boundary). Correct for their use case, not for ours.
- `generate_high_res_poly()` is path-agnostic. Versioning depends on the
  caller's output path.
- The `osmium time-filter` step correctly flattens the snapshot PBF to a
  point in time. The issue is only that no polyfile is extracted from it at
  the country level.
