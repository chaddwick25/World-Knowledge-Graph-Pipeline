# Slug Gate & RDF Namespace Alignment: the Two "Alignment" Layers of Planet Init

> **Focus:** how the pipeline aligns *filesystem paths* (planet → continent →
> country slug resolution) and *RDF namespaces* (WorldKG 1.0 vs the legacy
> GeoVectors v2 / bogus `schema.worldkg.org` URIs). Both layers are "aligned
> well enough to run" by `init_planet`, but each has produced production
> incidents (NL Step-1 crash 2026-08-26; 0-class namespace mismatch) because
> multiple code paths derived identifiers independently instead of reading
> one gate.
>
> **Key idea:** there is exactly ONE gate for country paths, the
> `canonical_slug` field on `CountryPipelineProfile` (patched per-country via
> `overrides.json`). There is exactly ONE truth for the RDF schema, the
> `@prefix wkgs:` declaration in the on-disk WorldKG 1.0 TTLs. Every service
> that derives a slug or a namespace from a *different* source (e.g. from
> `canonical_name`, or from `http://schema.worldkg.org/`) is a bug.

---

## 1. The Country Slug Gate (planet → continent → country paths)

### 1.1 The Three Path Families and Their Slug Sources

| Path family | Slug source | Convention | NL example |
|---|---|---|---|
| TSV embeddings (`EMBEDDINGS_ROOT/...`) | Geofabrik filename slug | hyphens | `netherlands-location.tsv.gz` |
| OSM extraction dirs (`OSM_WIKIDATA_EXTRACTIONS_DIR/...`) | `regional_path_service.get_country_dir()` → **normalizes to underscores** | underscores | `.../europe/netherlands/temporal_snapshots/` |
| Poly files (`POLYGON_FILES_DIR`) | Geofabrik dir slug | hyphens | `europe/netherlands.poly` |

### 1.2 How the Gate Is Built (init_planet Chain)

1. `country_relation_resolver.sync()`
   (`core/services/planet_init/country_relation_resolver.py`):
   - `geofabrik_index_service.get_iso_to_slug_map()`, ISO → {slug, name,
     parent, pbf_url}. Last-write-wins on duplicate ISO codes, the source of
     the VU/MH bugs (see §1.5).
   - `sparql_country_relation_service`, ISO → {name, relation_id, uri}
   - `slug = geo_data['slug'].replace('_', '-')` (Geofabrik slug)
2. `import_country_relations`
   (`api/management/commands/import_country_relations.py`)
3. `prebuild_worldkg_structure`
   (`core/management/commands/prebuild_worldkg_structure.py`):
   - `CountryPipelineProfile.canonical_slug = slug` ← **THE GATE**
   - `CountryPipelineProfile.canonical_name = SPARQL name` ← the TRAP
     (see §1.4)
   - `embedding_slug` / `embedding_root_path = slug`
4. `prebuild_country_paths`
   (`core/management/commands/prebuild_country_paths.py`): TSV match by
   `canonical_slug`; `osm_slug = override or canonical_slug`
5. `sync_geovectors_metadata`
   (`geovectors_encoder/.../sync_geovectors_metadata.py`)

### 1.3 `overrides.json`: the Per-Country Patch List (the Gate's Escape Hatch)

Location: `settings.OVERRIDES_JSON_PATH` (host:
`/media/thanos/f5d95b0c-9c60-433c-a76f-3c12175d4f27/OSM/overrides.json`, NOT
in the repo). Sections:

- **`country_iso_overrides`**: `{ISO: {country_slug, embedding_slug,
  poly_slug}}`. `country_slug` gates OSM/poly paths, `embedding_slug` gates
  TSV matching. Current entries (2026-08-26): BS, ZA, GB, VU, IE.
- **`embedding_edge_cases`**: shared location/tag borrow rules (Poland ←
  europe-east location, Germany split node/way views, Indonesia/Japan shared
  tags). Consumed by `prebuild_country_paths._resolve_country_paths()`.
- **`continents`**: empty.

Workflow: `prebuild_country_paths --report` lists profiles with no TSV match
→ add `country_slug`/`embedding_slug` entries → re-run
`init_planet --step sync_hierarchy` (or `prebuild_country_paths`).
Module-level cache in `country_override_service` (`clear_cache()` to
invalidate).

### 1.4 The NL Incident (2026-08-26): Two Bugs, One Crash

1. **Gate violation**: `CountryEnvelope.from_db` resolved
   `snapshot_pbf_path` from `canonical_slug` (`netherlands`), but
   `SnapshotExtractionService.extract_country_snapshot()` derived its output
   slug from `normalize_country_slug(canonical_name)` →
   `kingdom_of_the_netherlands` (no NL override existed). The PBF was
   extracted to the wrong directory and Step 1 crashed: `Open failed for
   '.../europe/netherlands/...'`.
2. **Silent masking**: `preprocess_snapshot()` (`pipeline/tasks/helper.py`)
   tried to repair `cfg.snapshot_pbf_path` after extraction, but
   `CountryEnvelope` is `@dataclasses.dataclass(frozen=True)`. The assignment
   raised `AttributeError` swallowed by a bare `except Exception:`.

**Fixes (implemented 2026-08-26):**
- `extract_country_snapshot()` gained an optional `country_slug` parameter
  (the gate). `preprocess_snapshot` passes `cfg.slug`;
  `get_country_slug(iso, slug)` still applies overrides on top. Legacy
  callers (`preprocess_country`, `country_search_views`) unchanged.
- `preprocess_snapshot()` now **returns** a replaced envelope
  (`dataclasses.replace`), no more in-place mutation, no bare except. Caller:
  `step_1_embed_osm_entities` (`env = preprocess_snapshot(env,
  logger=logger)`).
- Regression tests: `backend/tests/unit/test_snapshot_slug_gate.py`
  (8 tests).

**Scope:** 44 countries have `get_country_slug(iso,
normalize_country_slug(canonical_name)) != canonical_slug` (verified against
the DB 2026-08-26). The code fix covers all 44 at once; `overrides.json`
only patches one at a time. The full list is in `.devin/rules/06-notes.md`
§6.7.2.

### 1.5 Geofabrik Index Data Bugs (verified against `/app/data/geofabrik_index.json`)

`get_iso_to_slug_map()` is **last-write-wins** over duplicate ISO codes:

| ISO | Bug | Fix direction (not yet implemented) |
|---|---|---|
| VU | assigned to SIX features (vanuatu, wallis-et-futuna, tokelau, polynesie-francaise, ile-de-clipperton, american-oceania) → resolves to `wallis-et-futuna` | preferred-slug policy or correction map |
| MH | appears on BOTH `marshall-islands` and `pitcairn-islands` → resolves to `pitcairn-islands` | preferred-slug policy |
| DO/HT, SN/GM, AE+gcc, IE+NI | grouped Geofabrik extracts (one PBF/TSV, multiple ISO) — only GB + MY/SG/BN have TSV splitting (`EmbeddingSplitService`) | extend splitter to DO/HT, SN/GM |
| GF (and French territories) | parent=`france` (subregion, not continent) → `continent_name=None` | walk up to continent |
| PR/VI | nested slugs `us/puerto-rico` break `{slug}-location.tsv.gz` filename parsing | flatten + alias |

---

## 2. RDF Namespace Alignment (WorldKG 1.0 vs GeoVectors v2)

### 2.1 The Ground Truth

The on-disk WorldKG 1.0 dumps under `settings.WORLDKG_ONTOLOGY_PATH`
(`/app/data/OSM-PBF-FILES/world_kg_ontology/`) declare:

```turtle
# WorldKG_Ontolgy.ttl
@prefix wkgs: <http://www.worldkg.org/schema/> .     ← classes + properties
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix dbo: <http://dbpedia.org/ontology/> .

# planet_osm.ttl (instance data)
@prefix wkg: <http://www.worldkg.org/resource/> .    ← entity URIs
@prefix wkgs: <http://www.worldkg.org/schema/> .
@prefix osmn: <https://www.openstreetmap.org/node/> .
@prefix geo: <http://www.opengis.net/ont/geosparql#> .
@prefix sf: <http://www.opengis.net/ont/sf#> .
```

Verified 2026-08-26: parsing the ontology TTL yields **1171 classes** under
`http://www.worldkg.org/schema/` and **0** under both
`http://schema.worldkg.org/` and the GeoVectors v2 namespace.

### 2.2 Paper Figure 2 (GeoVectors v2, arXiv:2108.13092) vs WorldKG 1.0

| Prefix | Paper Fig 2 (GeoVectors v2 corpus) | WorldKG 1.0 (this project's data) |
|---|---|---|
| resource | `geovec:` `http://geovectors.l3s.uni-hannover.de/resource/` (`geovec:v2_n_240109189`) | `wkg:` `http://www.worldkg.org/resource/` (`wkg:10`) |
| schema | `geovec-s:` `http://geovectors.l3s.uni-hannover.de/schema/` (`EmbeddedSpatialThing`) | `wkgs:` `http://www.worldkg.org/schema/` (`wkgs:Village`) |
| geometry | `geo:` `http://www.w3.org/2003/01/geo/wgs84_pos#` (`latitude`/`longitude`) | `geo:` `http://www.opengis.net/ont/geosparql#` (`asWKT` via `wkgs:spatialObject`) |
| OSM link | `prov:hadPrimarySource <https://www.openstreetmap.org/node/{id}>` | `wkgs:osmLink osmn:{id}` |
| node type | `lgd:Node` (LinkedGeoData) | (dropped) |
| Wikidata | `owl:sameAs` on the entity | `owl:equivalentClass` on the class (NCA) |
| dcterms/owl/xsd/rdfs | standard | standard (identical) |

**Interpretation:** the paper describes the older GeoVectors v2 corpus. This
project mirrors the successor **WorldKG 1.0** (Dsouza et al., CIKM 2021).
The namespace evolution is `geovectors.l3s.uni-hannover.de/...` →
`www.worldkg.org/...`. Code must follow the **data**, not the paper.

### 2.3 The `schema.worldkg.org` Bug (fixed 2026-08-26)

Two modules hardcoded `WKGS_BASE_URI = "http://schema.worldkg.org/"`, a
namespace that exists in NEITHER the paper NOR the data:

| Module | Before | After |
|---|---|---|
| `worldkg_nca/services/ontology_loader.py` | `http://www.worldkg.org/schema/` ✅ (was already correct) | unchanged |
| `semantic_search/services/worldkg_enrichment_service.py` | `http://schema.worldkg.org/` ❌ | `http://www.worldkg.org/schema/` |
| `worldkg_nca/services/triples_service.py` | `http://schema.worldkg.org/` ❌ | `http://www.worldkg.org/schema/` |

Also fixed: `WKG_BASE_URI` (`http://www.worldkg.org/` →
`http://www.worldkg.org/resource/`, the entity namespace; it was unused dead
code).

**Impact today is masked** because the SPARQL endpoint
`https://www.worldkg.org/sparql` is **dead** (301 → Plone marketing page at
`https://www.dsis.uni-bonn.de/worldkg/de`). All SPARQL enrichment fails and
falls back to local tag prediction
(`WorldKGEnrichmentService._enrich_via_local_tags`). If the endpoint ever
returns, the queries will now be namespace-correct.

**Regression tests:** `backend/tests/unit/test_worldkg_namespaces.py`
(4 tests) locks: (1) TTL declares the canonical prefix, (2) `load_from_ttl`
parses ≥1000 classes incl. `wkgs:WKGObject`/`wkgs:Amenity`/
`wkgs:Restaurant`, (3) bogus + GeoVectors namespaces match 0 classes,
(4) all three modules agree on `WKGS_BASE_URI` and `WKG_BASE_URI`.

---

## 3. Sources & URLs (the Canonical Reference List)

### Datasets / Corpora

| Item | URL |
|---|---|
| WorldKG portal | https://www.worldkg.org/ |
| WorldKG GitHub (CreateTriples.py, ontology JSON) | https://github.com/alishiba14/WorldKG-Knowledge-Graph |
| WorldKG ontology TTL (Zenodo record) | https://zenodo.org/record/4953986 |
| WorldKG SPARQL endpoint (**currently dead**, 301 → dsis.uni-bonn.de) | https://www.worldkg.org/sparql |
| GeoVectors corpus data page (paper's embeddings) | https://geovectors.l3s.uni-hannover.de/data |
| GeoVectors paper (Fig. 2 namespaces) | `papers/WorldKG/GeoVectors: A Linked Open Corpus of OpenStreetMap.pdf`, Tempelmeier, Gottschalk, Demidova, arXiv:2108.13092v1, 30 Aug 2021, L3S / Leibniz Universität Hannover |
| Geofabrik index (ISO → slug, source of VU/MH bugs) | https://download.geofabrik.de/index-v1.json |
| Wikidata SPARQL endpoint | https://query.wikidata.org/sparql |
| LinkedGeoData ontology (paper's `lgd:`) | http://linkedgeodata.org/meta/ |
| Basic Geo (WGS84) vocabulary (paper's `geo:`) | https://www.w3.org/2003/01/geo/wgs84_pos |
| PROV ontology (paper's `prov:`) | https://www.w3.org/ns/prov# |
| GeoSPARQL ontology (WorldKG 1.0 geometry) | http://www.opengis.net/ont/geosparql# |

### Local Ground Truth (files that must be treated as authoritative)

| Item | Path |
|---|---|
| `overrides.json` (slug gate patch list) | `settings.OVERRIDES_JSON_PATH` (host: `/media/thanos/f5d95b0c-9c60-433c-a76f-3c12175d4f27/OSM/overrides.json`) |
| `CountryPipelineProfile.canonical_slug` | THE gate, DB column, set by `prebuild_worldkg_structure` |
| WorldKG ontology TTL | `settings.WORLDKG_ONTOLOGY_PATH` (`/app/data/OSM-PBF-FILES/world_kg_ontology/WorldKG_Ontolgy.ttl`) |
| WorldKG instance TTLs | `/app/data/OSM-PBF-FILES/world_kg_ontology/planet_osm.ttl`, `void.ttl` |
| Geofabrik index cache | `/app/data/geofabrik_index.json` |
| Country relations (merged Geofabrik + SPARQL) | `/app/data/country_relations.json` |

### Code Invariants (do not break)

1. **Country slug**: every service that needs a country slug for a path MUST
   derive it from `canonical_slug` (via `get_country_slug(iso,
   canonical_slug)`), never from `normalize_country_slug(canonical_name)`.
2. **Envelope mutation**: `CountryEnvelope`/`CountryPaths` are frozen
   dataclasses. Use `dataclasses.replace` and reassign, never in-place
   attribute assignment.
3. **Namespaces**: `WKGS_BASE_URI` = `http://www.worldkg.org/schema/` and
   `WKG_BASE_URI` = `http://www.worldkg.org/resource/` everywhere. Run
   `backend/tests/unit/test_worldkg_namespaces.py` after any RDF/ontology
   change.
4. **Init steps**: after editing `overrides.json`, re-run
   `init_planet --step sync_hierarchy` (or `prebuild_country_paths`) to
   propagate. The override cache is module-level in
   `country_override_service`.
