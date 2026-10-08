# Wikidata Enrichment Flow: Entry Script → Step 1 → Step 2

> **Focus:** the cross-step data-flow narrative. How the Docker entry script
> loads WorldKG ontology classes into Redis, how Step 1 upserts OSM entities
> with tags but no `wkg_class`, and how Step 2 uses the Redis ontology cache
> + Wikidata SPARQL to enrich entities and candidates via SQL joins.
>
> **Key idea:** Wikidata classes are loaded during entry-script
> initialization (not per-pipeline-run). Step 1 upserts entities with raw
> tags. Step 2 enriches those entities using the ontology cache via a SQL
> join, reusing existing relational patterns instead of per-entity Redis
> lookups in a Python loop.

---

## 1. The Cross-Step Flow

**Docker entrypoint (`init_planet`, step 12: `enrich_worldkg_classes`):**
- `WorldKG_Ontolgy.ttl` (on disk)
- `WorldKGOntologyLoader.load_from_ttl()` parses `rdfs:subClassOf` +
  `owl:equivalentClass` → ontology dict:
  `{(osm_key, osm_value): class, depth, ...}`
- `WorldKGOntologyService.load_ontology_from_dict()`:
  - Redis SET `worldkg:class:{key}:{value}` = `{class, depth, ...}`
  - Redis SET `worldkg:wikidata_to_wkg` = `{QID: class, ...}`
- Ontology is now in Redis, persistent across pipeline runs.

**Step 1: Embed OSM entities (Celery task: `step_1_embed_osm_entities`):**
1. **Preprocess snapshot**: `SnapshotExtractionService` → `PbfFile` +
   `Snapshot` rows.
2. **Stream PBF → encode → upsert**: `read_from_snapshot()` →
   `BatchCollector` → SQL COPY. `OsmEntity` rows created with `osm_id`,
   `tags` (JSONB), `geom`, `gv_tags_embedding`, `wkg_class = NULL`
   (NOT YET ENRICHED).
3. **Post-processing: `enrich_worldkg_classes()`**:
   `sql_enrich_region()` (uses the Redis ontology cache) →
   `_load_ontology_to_temp_table()` (Redis → Postgres temp table) →
   `UPDATE...FROM` join → `OsmEntity.wkg_class` populated via
   `jsonb_each_text`.
4. **Post-processing: `compute_entropy()`**: Shannon entropy of the
   `wkg_class` distribution. Entropy gate blocks if entropy < 1.5
   (semantically uniform, likely a bad extract).

**Step 2: Harvest Wikidata candidates (Celery task: `step_2_harvest`):**
1. `WikidataCandidateService.enrich_wkg_class()`: live SPARQL
   `?entity wdt:P31/wdt:P279* ?typeUri`; maps `?typeUri` → `wkg_class` using
   the Redis `worldkg:wikidata_to_wkg` map (loaded in the entry script);
   enriches CANDIDATES (not entities) with `wkg_class`.
2. Candidates now carry `wkg_class` for IGEA type-matching (Step 3), as
   `PrecomputedLinkCandidate` rows with `wkg_class` populated.

---

## 2. Why This Cross-Step Pattern Saves Time

### 2.1 Ontology Loaded Once (Not Per-Run)

The WorldKG ontology TTL is ~2 MB and contains ~10,000 class definitions.
Loading and parsing it takes ~5 seconds. Per-pipeline-run loading would make
every country run pay that cost. Loading it once during `init_planet` (entry
script) keeps the ontology in Redis for all subsequent pipeline runs, with
no per-run TTL parsing.

### 2.2 SQL Join Instead of Python Loop

`sql_enrich_region()` (Step 1c) uses a single `UPDATE...FROM` join to enrich
all entities in one SQL round-trip. ~10x faster than the legacy
`batch_enrich_region()` Python loop that queried Redis per entity:

| Method | Ireland (2.47M entities) | Approach |
|---|---|---|
| `batch_enrich_region()` | ~60 min | Python ThreadPoolExecutor, per-entity Redis queries |
| `sql_enrich_region()` | 2 min 14 sec | Single SQL join with temp table |

See [SQL Join Enrichment](../03_Postgres/02_SQL_Join_Enrichment.md) for the
full SQL schematic.

### 2.3 Step 2 Reuses the Same Ontology Cache

Step 2 (Wikidata harvest) needs to map Wikidata QIDs to WorldKG classes for
IGEA type-matching. Instead of re-loading the ontology or re-querying
Wikidata for class definitions, it reuses the same Redis
`worldkg:wikidata_to_wkg` map loaded during `init_planet`. The principle:
load the ontology once, then reuse it across Steps 1 and 2 via different
access patterns (SQL join vs. Redis lookup).

---

## 3. The Two Enrichment Targets

| Target | Step | Method | Source |
|---|---|---|---|
| `OsmEntity.wkg_class` | Step 1c | `sql_enrich_region()`, SQL join with temp table | Redis ontology cache |
| `PrecomputedLinkCandidate.wkg_class` | Step 2 | `WikidataCandidateService.enrich_wkg_class()`, SPARQL + Redis map | Wikidata SPARQL + Redis `wikidata_to_wkg` map |

Step 1 enriches **entities** (OSM rows in the vectors DB). Step 2 enriches
**candidates** (Wikidata harvest results for IGEA alignment). Both use the
same ontology cache but different access patterns.

---

## 4. Wikidata URI Normalization

**Source:** `worldkg_nca/services/wikidata_service.py` (`_build_reverse_nca_map`)

Ontology URIs from the TTL use the format
`http://www.wikidata.org/wiki/Q...`, but the Wikidata SPARQL endpoint
returns `http://www.wikidata.org/entity/Q...`. Without normalization,
`enrich_wkg_class` returns 0 mappings even though Wikidata returns valid
P31/P279* rows.

The fix: `_build_reverse_nca_map()` normalizes ontology URIs from
`/wiki/Q...` to `/entity/Q...` to match SPARQL responses.

---

## 5. SPARQL Retry Semantics

Both `wikidata_candidate_service.py` (semantic_search) and
`wikidata_service.py` (worldkg_nca) inherit `SPARQLRetryMixin`
(`semantic_search.utils.sparql_mixin`):

- Retries on 429/502/503/Timeout with exponential backoff
- `SPARQL_MAX_RETRIES = 1`, 2s base delay
- `semantic_search` service: 10s timeout
- `worldkg_nca` service: 30s timeout
- Failed batches are logged and skipped; partial enrichment is preferred
  over blocking
- Batch size: 100 QIDs per POST request (avoids URL length limits)

---

## 6. Failure Modes

| Failure | Effect |
|---|---|
| TTL file missing (entry script) | No ontology in Redis; `sql_enrich_region` falls back to `batch_enrich_region` which also fails → `wkg_class = NULL` |
| Redis down | Same as above: `wkg_class = NULL`; Step 2 SPARQL still works but QID→class mapping fails |
| Wikidata SPARQL timeout (Step 2) | Retries once (2s backoff); failed batches logged and skipped; partial enrichment preferred |
| Entropy gate blocks (Step 1d) | `EntropyGateBlocked` raised; pipeline stops for this country; `SnapshotJob.status = FAILED` |

A transient failure can leave `wkg_class = NULL` for some entities, but it
cannot corrupt the ontology or break the pipeline. Step 2 and Step 3 handle
`wkg_class = NULL` gracefully.

---

## 7. Invariants

- The WorldKG ontology is loaded once during `init_planet` (entry script),
  not per-pipeline-run.
- Step 1 upserts entities with `wkg_class = NULL`; enrichment happens in
  Step 1c post-processing.
- `sql_enrich_region()` is the default enrichment path;
  `batch_enrich_region()` is the fallback when the ontology index is not
  built.
- Step 2 enriches **candidates** (not entities) with `wkg_class` using the
  same Redis ontology cache.
- Wikidata URIs must be normalized from `/wiki/Q...` to `/entity/Q...` to
  match SPARQL responses.
- SPARQL retries are best-effort; partial enrichment is preferred over
  blocking.
