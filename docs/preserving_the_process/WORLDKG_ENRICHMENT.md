# Preserving the WorldKG Enrichment Architecture

How the semantic enrichment methodology from the original [WorldKG](https://arxiv.org/abs/2304.09503) research paper (Dsouza et al., CIKM 2021) was integrated into the Django pipeline.

The enrichment pipeline assigns WorldKG ontology classes to OSM entities, populates superclass chains, links Wikidata class URIs, and computes class distributions for entropy gating. It runs as the final phase of Step 1 (embedding), after all entities have been upserted into the `OsmEntity` table.

---

## 1. Two-Mode Enrichment: SPARQL vs Local Prediction

### The Original Implementation
The WorldKG paper publishes a SPARQL endpoint at `https://www.worldkg.org/sparql` where OSM entities are typed via `rdf:type wkgs:{ClassName}` assertions, linked back to OSM via `wkgs:osmLink osmn:{osm_id}`, and aligned to Wikidata via `owl:equivalentClass` on the class level.

### The Updated Implementation
The pipeline supports both online and offline enrichment modes, with offline as the default for pipeline runs:

* **Mechanism:** `WorldKGEnrichmentService` (in `semantic_search/services/worldkg_enrichment_service.py`) provides three paths:
  1. **SPARQL mode** (`use_sparql=True`): Queries the live WorldKG SPARQL endpoint for per-entity `rdf:type` assertions. More accurate but slow (~1 req/entity).
  2. **SQL-side enrichment** (default for pipeline runs): `sql_enrich_region()` loads the ontology into a Postgres temp table and runs a single `UPDATE...FROM` join. ~10x faster than the Python loop. Ireland (2.47M entities) enriched in 2 min 14 sec.
  3. **Python local prediction mode** (fallback): `batch_enrich_region()` uses an in-memory inverted index with `ThreadPoolExecutor`. Used when the ontology index is not built or for manual `enrich_worldkg_classes` management command runs.
* **Transformation:** The SQL mode eliminates both network dependency and the Python per-entity loop (GIL bottleneck). The TTL ontology is loaded once into Redis (`WorldKGOntologyService`), then `_ensure_index()` builds the in-memory indexes, which are then copied into a Postgres temp table (`tmp_wkg_ontology`) for the join:
  - `_key_value_index`: `(osm_key, osm_value) → wkgs:Class` (e.g., `("amenity", "restaurant") → "wkgs:Restaurant"`)
  - `_key_class_index`: `osm_key → wkgs:Class` (fallback when value doesn't match)
  - `_class_superclasses`: `wkgs:Class → [wkgs:SuperClass, ...]` (hierarchy chain)
  - `_class_depths`: `wkgs:Class → int` (0=WKGObject, 1=key class, 2=value subclass)
  - `_class_wikidata`: `wkgs:Class → Wikidata class URI` (from `owl:equivalentClass`)

**WorldKG RDF Schema (preserved from paper):**
```
wkgs:  = http://www.worldkg.org/schema/        (classes + properties)
wkg:   = http://www.worldkg.org/resource/       (entity instance URIs)
osmn:  = https://www.openstreetmap.org/node/    (OSM node links)
```

**Fields populated on `OsmEntity`:**
| Field | Example | Description |
|-------|---------|-------------|
| `wkg_class` | `wkgs:Restaurant` | Primary WorldKG class |
| `wkg_superclasses` | `['wkgs:Amenity', 'wkgs:WKGObject']` | Full superclass chain |
| `wikidata_uri` | `http://www.wikidata.org/entity/Q486972` | Wikidata class URI (from NCA/ontology) |
| `wkg_depth` | `0` / `1` / `2` | 0=WKGObject, 1=key class, 2=value subclass |
| `wkg_type_key` | `amenity` | OSM key that asserted the type |
| `wkg_type_value` | `restaurant` | OSM value that asserted the type |
| `wkg_enriched_at` | `2026-08-09T12:43:18Z` | Timestamp of enrichment batch |

**Code Reference:** `backend/semantic_search/services/worldkg_enrichment_service.py`
```python
class WorldKGEnrichmentService:
    def enrich_entity(self, entity: OsmEntity, use_sparql: bool = False) -> Dict:
        if use_sparql:
            result = self._enrich_via_sparql(entity)
            if result:
                return result
            # Fallback to local if SPARQL fails
        # Local tag-based prediction
        result = self._enrich_via_local_tags(entity)
        return result

    def _enrich_via_local_tags(self, entity: OsmEntity) -> Optional[Dict]:
        wkg_class = self.ontology.predict_class_from_tags(entity.tags)
        superclasses = self.ontology._class_superclasses.get(wkg_class, [])
        depth = self.ontology._class_depths.get(wkg_class, 0)
        wikidata_uri = self.ontology._class_wikidata.get(wkg_class)
        return {
            'wkg_class': wkg_class,
            'wkg_superclasses': superclasses,
            'wikidata_uri': wikidata_uri,
            'wkg_depth': depth,
            'method': 'local'
        }
```

---

## 2. Batch Enrichment with Spatial Scoping

### The Pipeline Integration
Enrichment runs as the final phase of Step 1 (`embedding_service.py`), after all OSM entities have been upserted into the `OsmEntity` table. The call chain is:

```
Step 1 (step_1_embed.py)
  → EmbeddingService.run()
    → read_from_snapshot()  ← upserts entities into OsmEntity
    → enrich_worldkg_classes(cfg, logger)  ← pipeline/tasks/helper.py
      → WorldKGEnrichmentService.sql_enrich_region(region=cfg.iso, poly_file=poly_path)
    → compute_entropy(cfg, logger)  ← uses enriched class distribution
```

### Spatial Filtering
`sql_enrich_region` resolves a bounding box to scope which entities get enriched:

1. **Explicit poly file** (if provided), parsed via `parse_poly_bbox()` for exact geometry
2. **Country bbox**, resolved via `resolve_country_bbox(region)` from `OsmBoundary` DB or `CountryPipelineProfile.country_relations_payload["bbox"]`

The bbox is applied as `ST_Within(e.geom, ST_MakeEnvelope(...))` in the SQL WHERE clause. This ensures only entities within the country's spatial extent are enriched, critical for countries that share embedding files (e.g., GB/UK) or have overlapping bounding boxes.

**Important:** Enrichment uses the **country-level** bbox, not individual subgraph bboxes. Even when `has_subgraphs=True`, enrichment processes all entities within the country boundary in a single pass. Subgraph bboxes are only used at query time (subdivision search) and for GV-NLE training fan-out.

### SQL-Side Enrichment (Default Path)
The `sql_enrich_region()` method replaces the previous Python loop with a single SQL operation:

1. **Load ontology into temp table**: `_load_ontology_to_temp_table()` copies the in-memory ontology index into a Postgres temp table (`tmp_wkg_ontology`) with one row per `(osm_key, osm_value, class_name, depth, superclasses, wikidata_uri, type_key, type_value)` mapping.
2. **Resolve leaf table**: `_resolve_leaf_table()` finds the country's leaf partition (e.g., `embeddings_2025_12_31_ie`), falling back to the root table if partitioning hasn't happened.
3. **Single UPDATE...FROM join**: Expands each entity's tags JSONB via `jsonb_each_text()`, joins against `tmp_wkg_ontology`, picks the deepest matching class via `ROW_NUMBER() OVER (PARTITION BY e.id ORDER BY o.depth DESC)`, and updates `wkg_class`, `wkg_superclasses`, `wikidata_uri`, `wkg_depth`, `wkg_type_key`, `wkg_type_value`, `wkg_enriched_at` in one round-trip.
4. **`skip_enriched=True`**, only processes entities with `wkg_class IS NULL` (idempotent re-runs).

**Performance**: ~10x faster than the Python `ThreadPoolExecutor` path because:
- No Python per-entity loop (GIL bottleneck eliminated)
- PostgreSQL parallel query can use multiple cores
- Single round-trip for the UPDATE instead of `bulk_update` batches

**Benchmark**: Ireland (2,474,478 entities), 1,909,858 enriched in 2 min 14 sec. The old Python path would have taken ~60+ minutes.

**Code Reference:** `backend/semantic_search/services/worldkg_enrichment_service.py`
```python
def sql_enrich_region(self, region=None, poly_file=None, snapshot_id=None,
                      skip_enriched=True, limit=None):
    # 1. Load ontology into temp table
    self._load_ontology_to_temp_table(cursor)
    # 2. Resolve leaf table
    leaf = self._resolve_leaf_table(cursor, snapshot_id, region)
    # 3. Single UPDATE...FROM join
    update_sql = f"""
        WITH matched AS (
            SELECT e.id AS entity_id, o.class_name, o.depth, ...
                   ROW_NUMBER() OVER (PARTITION BY e.id ORDER BY o.depth DESC) AS rn
            FROM {leaf} e
            CROSS JOIN LATERAL jsonb_each_text(e.tags::jsonb) AS t(osm_key, osm_value)
            JOIN tmp_wkg_ontology o ON o.osm_key = t.osm_key
                 AND (o.osm_value = t.osm_value OR o.osm_value IS NULL)
            WHERE {where_sql}
        )
        UPDATE {leaf} e SET wkg_class = m.class_name, ...
        FROM matched m WHERE e.id = m.entity_id AND m.rn = 1
    """
    cursor.execute(update_sql, update_params)
```

### Python Batch Enrichment (Fallback Path)
The `batch_enrich_region()` method remains as a fallback when the ontology index is not built. It uses `ThreadPoolExecutor` with configurable worker count (`enrichment.workers` in `pipeline/hyperparams.yaml` (default 2)):

- `query.iterator(chunk_size=batch_size)`, server-side cursor for memory efficiency
- `OsmEntity.objects.using('vectors').bulk_update()` per batch, partial progress is preserved if the connection drops
- `skip_enriched=True`, skips entities that already have `wkg_class` populated (idempotent re-runs)

---

## 3. Wikidata Candidate Harvest (Step 2: Upstream of IGEA)

### The Two-Phase Harvest
Before IGEA can align OSM↔Wikidata entities, it needs Wikidata candidates with `wkg_class` populated. This is done in Step 2 (`harvest_wikidata_candidates`) using a two-phase SPARQL approach:

**Phase 1: Spatial harvest (`harvest_by_bbox` / `harvest_by_country`):**
- Uses `wikibase:box` SPARQL service to find all entities with `wdt:P625` (coordinate location) within the country bbox
- Paginated (2000 per page) with 1.1s delay between pages
- Returns raw candidates with `wikidata_uri`, `lat`, `lon`, `label`, but **no wkg_class**

**Phase 2: Class enrichment (`enrich_wkg_class`):**
- Batched P31/P279* lookup: `?entity wdt:P31/wdt:P279* ?typeUri`
- Second `VALUES` clause restricts `?typeUri` to the ~45 known WorldKG ontology types (from NCA reverse map)
- Maps Wikidata type URI → `wkgs:` class via `_wikidata_to_wkg` reverse map
- **URI normalization**: The ontology TTL stores Wikidata URIs as `http://www.wikidata.org/wiki/Q...` (with `/wiki/`), but Wikidata's SPARQL endpoint returns entity URIs as `http://www.wikidata.org/entity/Q...` (with `/entity/`). `_build_reverse_nca_map()` normalizes `/wiki/` to `/entity/` so the dict keys match the SPARQL response. Without this fix, 0% of candidates map even though Wikidata returns valid P31/P279* rows (e.g., Belize: 715 rows returned, 0 mapped before fix, 462 mapped after).
- **Batch size: 100 QIDs per request** (reduced from 500 to avoid Wikidata's URL length limit)
- **Uses POST** (not GET) to avoid 414/431 errors from URL length limits
- Failed batches (502/429 from Wikidata throttling) are caught and skipped: partial enrichment still feeds IGEA

**Code Reference:** `backend/worldkg_nca/services/wikidata_service.py`
```python
def enrich_wkg_class(self, candidates, batch_size=100):
    # Build VALUES clause from NCA reverse map (~45 known ontology types)
    type_values_clause = self._build_type_values_clause()
    for i in range(0, len(uris), batch_size):
        batch_uris = uris[i:i + batch_size]
        values_clause = " ".join(f"wd:{qid}" for qid in batch_uris)
        query = f"""
SELECT ?entity ?typeUri WHERE {{
  VALUES ?entity {{ {values_clause} }}
  VALUES ?typeUri {{ {type_values_clause} }}
  ?entity wdt:P31/wdt:P279* ?typeUri .
}}
"""
        # POST to avoid Wikidata's URL length limit (~8KB for GET)
        response = requests.post(
            WIKIDATA_SPARQL_ENDPOINT,
            data={"query": query, "format": "json"},
            headers={"User-Agent": WIKIDATA_USER_AGENT,
                     "Accept": "application/sparql-results+json"},
            timeout=SPARQL_TIMEOUT_S,
        )
```

### Why Two Phases?
Adding P31/P279* to the spatial `wikibase:box` query would force Wikidata to JOIN the ~500M-row statement table on top of the spatial result set, causing 504 Gateway Timeout on country-sized bboxes. The two-phase approach lets Wikidata use a hash join (O(batch_size)) per batch instead of a full P31 table scan.

### USLP Independence from IGEA

USLP (Step 4) runs independently of IGEA acceptance. The previous gate that skipped USLP when IGEA accepted 0 links has been removed: `step_4_uslp.py` no longer checks `total_accepted` or `igea_accepted`. Step 3 still emits IGEA acceptance counts in the envelope state, but Step 4 ignores them.

This means the Wikidata harvest + class enrichment in Step 2 feeds IGEA (Step 3) and USLP (Step 4) independently. If `enrich_wkg_class` fails on all batches (as happened with the 414/431 URL-too-long bug), IGEA gets candidates with no `wkg_class` and cannot align them, but USLP still runs using its own OSM tag-based class fallback.

---

## 4. Entropy Gate (Post-Enrichment)

After enrichment, `compute_entropy()` (in `pipeline/tasks/helper.py`) computes the Shannon entropy of the `wkg_class` distribution across all enriched entities in the country. This serves as a semantic diversity check:

- **High entropy** → diverse class distribution (restaurants, highways, parks, etc.) → pipeline continues
- **Low entropy (0 < H < 1.5)** → semantically uniform (e.g., all entities mapped to `wkgs:Building`) → pipeline blocks (likely ontology mapping failure)
- **Zero entropy (H = 0)** → all entities have the same class → pipeline continues (guard: the `> 0.0` check in `step_1_embed.py` means H=0 does NOT block; this handles the single-class edge case without false-blocking)
- **No entities** → first run, no data yet → pipeline continues (guard)

The entropy gate can be bypassed with `hyperparam_overrides={"min_entropy": 0.0}` for debugging.

---

## 5. NCA (Neural Class Alignment): The Learned Mapping Layer

### Relationship to Enrichment
The enrichment described in Sections 1-2 uses **static ontology lookup** (TTL → inverted index → tag→class). NCA provides a **learned** alternative that trains a neural model on IGEA-aligned entities to predict tag→class mappings.

### Lifecycle (runs in Step 3, after IGEA)
1. `build_vocabularies(linked_entities)`: extract tag keys/values and Wikidata classes from IGEA-accepted pairs
2. `prepare_training_data(linked_entities)`: create multi-hot tag vectors and class labels
3. `train()`: fit the neural model (BiLSTM cross-attention)
4. `extract_mappings(threshold)`: probe model to get `(tag, class, confidence)` triples
5. `apply_mappings(osm_entities)`: assign `wkg_class` to entities using learned mappings

### The Reverse Map (Used by Wikidata Harvest)
NCA also produces a **reverse map** (`_wikidata_to_wkg`) that maps Wikidata type URIs → `wkgs:` classes. This reverse map is used by `enrich_wkg_class` in Step 2 to map harvested Wikidata candidates to WorldKG classes. Without NCA having been trained at least once, the reverse map is empty and `enrich_wkg_class` cannot map any candidates.

**Bootstrapping:** On a fresh database, the NCA reverse map is built from the initial WorldKG ontology TTL load (`enrich_worldkg_classes --load-ontology`), which provides ~34 static Wikidata→wkgs mappings. The neural NCA model (trained in Step 3) can expand this with learned mappings over time.

---

## 6. Class Distribution Query (Post-Enrichment)

`get_class_distribution()` (in `worldkg_enrichment_service.py`) aggregates entity counts by `wkg_class` for a given region/snapshot. Used by:
- The entropy gate (Step 1)
- Frontend augmented data panels (`AugmentedDataSummaryView`)

Spatial scoping mirrors `batch_enrich_region`: `resolve_country_bbox(region)` → `geom__within` filter. Snapshot scoping via `source_snapshot_id` when provided.

---

## 7. Operational Notes

### Ontology Loading
```bash
# Load from TTL (primary: provides static mappings + superclass hierarchy)
python manage.py enrich_worldkg_classes --load-from-ttl /path/to/WorldKG_Ontology.ttl

# Load from JSON sample (fallback for development)
python manage.py enrich_worldkg_classes --load-ontology ../data/worldkg_ontology_sample.json
```

The TTL is cached in Redis (DB 2) via `WorldKGOntologyService`. After a Redis flush, reload with:
```bash
redis-cli -n 2 FLUSHDB  # clears ontology cache
python manage.py enrich_worldkg_classes --load-from-ttl /path/to/WorldKG_Ontology.ttl
```

### SPARQL Rate Limiting
- Wikidata SPARQL: ~1 req/s for anonymous clients. `REQUEST_DELAY_S = 1.1` between paginated requests.
- WorldKG SPARQL: 10s timeout per entity (SPARQL mode is slow: use only for single-entity lookups, not bulk).
- `enrich_wkg_class` batch failures (502/429) are caught and skipped. Partial enrichment is expected for large countries under Wikidata throttling.

### Idempotency
- `batch_enrich_region(skip_enriched=True)`: only processes entities with `wkg_class IS NULL`. Safe to re-run.
- `enrich_wkg_class`: modifies candidates in-place. Re-running harvest + enrichment overwrites previous results.

### Database Routing
- `OsmEntity` lives in the `vectors` database (pgvector). All enrichment queries use `OsmEntity.objects.using('vectors')`.
- `SubgraphProfile`, `CountryPipelineProfile`, `OsmBoundary` live in the `default` database.
- The ontology cache lives in Redis (DB 2).

---

## 8. Cross-References

| Topic | Document |
|-------|----------|
| IGEA alignment (consumes enriched candidates) | `docs/preserving_the_process/IGEA.md` |
| USLP spatial link prediction (runs independently of IGEA) | `docs/preserving_the_process/USLP.md` |
| GeoVectors embeddings (Step 1 upstream) | `docs/preserving_the_process/GEOVECTORS.md` |
| OSM2KG class mapping | `docs/preserving_the_process/OSM2KG.md` |
| QID-centric data model | `docs/Schematics/WorkKG_Primities.md` §9 |
| Bounding box resolution | `docs/Schematics/WorkKG_Primities.md` §7 |
| WorldKG CDD architecture | `docs/preserving_the_process/WORLDKG_CDD_ARCHITECTURE.md` |
| Two-axis class space (semantic vs spatial) | `docs/preserving_the_process/USLP.md` §4 |
