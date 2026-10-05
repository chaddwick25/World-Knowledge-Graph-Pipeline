# SQL Join Enrichment: Ontology → Temp Table → UPDATE...FROM

> **Focus:** how `sql_enrich_region()` uses a Postgres temp table +
> `jsonb_each_text` + `UPDATE...FROM` join with `ROW_NUMBER()` to enrich
> OSM entities with WorldKG ontology classes. 10x faster than the Python
> `ThreadPoolExecutor` loop it replaced.
>
> **Key idea:** a SQL join reuses existing Postgres patterns. The ontology
> loads once into a temp table, then a single `UPDATE...FROM` join expands
> entity tags via `jsonb_each_text` and picks the deepest (most specific)
> match per entity.

---

## 1. The Enrichment Problem

After Step 1 upserts OSM entities into `OsmEntity` (with `tags` JSONB but no
`wkg_class`), the pipeline needs to assign each entity a WorldKG ontology
class based on its tags. The WorldKG ontology maps `(osm_key, osm_value)`
pairs to `wkg_class` names with an associated depth (specificity).

```
OsmEntity row:
  osm_id: 123456
  tags: {"amenity": "pub", "name": "The Red Lion", "building": "yes"}
  wkg_class: NULL  ← needs to be populated

WorldKG ontology (in Redis, loaded from TTL):
  (amenity, pub)      → class: Amenity,      depth: 3
  (building, yes)     → class: Building,     depth: 1
  (amenity, *)        → class: Amenity,      depth: 2  (wildcard)

Desired result:
  wkg_class = "Amenity"      (deepest match: depth 3)
  wkg_depth = 3
```

---

## 2. The Two Paths

### 2.1 Legacy Python Path (slow)

`batch_enrich_region()` parallelizes tag prediction across
`ENRICHMENT_WORKERS` (code default 2) threads via `ThreadPoolExecutor`. For
each entity, it queries the Redis ontology cache for each tag key/value
pair, picks the deepest match, and `bulk_update`s the entity.

- Uses threads (not multiprocessing) because Celery worker processes are
  daemonic and Python forbids daemonic processes from spawning children.
- Batch size 5000 (5x fewer `bulk_update` DB calls than the original 1000).
- For Ireland (2.47M entities): ~60 minutes.

### 2.2 SQL Join Path (fast, current default)

`sql_enrich_region()` loads the ontology into a Postgres temp table, then
runs a single `UPDATE...FROM` join. For Ireland (2.47M entities): **2 min
14 sec**, ~10x faster.

**Source:** <ref_file file="backend/semantic_search/services/worldkg_enrichment_service.py" /> (`sql_enrich_region` moved to `semantic_search/services/worldkg_enrichment_sql.py` 2026-09-25)

**Called from:** `pipeline/tasks/helper.py` in `enrich_worldkg_classes()`

The SQL path is the default; it falls back to `batch_enrich_region()` if the
ontology index is not built.

---

## 3. The SQL Join, Step by Step

### 3.1 Load Ontology into Temp Table

```python
self._load_ontology_to_temp_table(cursor)
```

Creates a temp table `tmp_wkg_ontology` with columns:
- `osm_key`, e.g. "amenity"
- `osm_value`, e.g. "pub" (or NULL for wildcard)
- `class_name`, e.g. "Amenity"
- `depth`, specificity (higher = more specific)
- `superclasses`, hierarchy path
- `wikidata_uri`, Wikidata QID equivalent
- `type_key`, `type_value`, type assertion metadata

The temp table is session-scoped, dropped automatically when the connection
closes. Loaded once per `sql_enrich_region()` call.

### 3.2 The UPDATE...FROM Join

```sql
WITH matched AS (
    SELECT
        e.id AS entity_id,
        o.class_name,
        o.depth,
        o.superclasses,
        o.wikidata_uri,
        o.type_key,
        o.type_value,
        ROW_NUMBER() OVER (
            PARTITION BY e.id
            ORDER BY o.depth DESC NULLS LAST
        ) AS rn
    FROM {leaf} e
    CROSS JOIN LATERAL jsonb_each_text(e.tags::jsonb) AS t(osm_key, osm_value)
    JOIN tmp_wkg_ontology o
      ON o.osm_key = t.osm_key
     AND (o.osm_value = t.osm_value OR o.osm_value IS NULL)
    WHERE {where_sql}
)
UPDATE {leaf} e
SET
    wkg_class = m.class_name,
    wkg_superclasses = m.superclasses,
    wikidata_uri = m.wikidata_uri,
    wkg_depth = m.depth,
    wkg_type_key = m.type_key,
    wkg_type_value = m.type_value,
    wkg_enriched_at = %s
FROM matched m
WHERE e.id = m.entity_id AND m.rn = 1
```

### 3.3 How It Works

1. **`jsonb_each_text(e.tags::jsonb)`** expands each entity's tags JSONB
   into key-value rows. One entity with 5 tags → 5 rows.
2. **`JOIN tmp_wkg_ontology`** matches each `(osm_key, osm_value)` pair
   against the ontology. The `OR o.osm_value IS NULL` clause handles
   wildcard ontology entries (e.g. `(amenity, *)` matches any amenity value).
3. **`ROW_NUMBER() OVER (PARTITION BY e.id ORDER BY o.depth DESC)`** ranks
   matches per entity by depth (specificity). `rn = 1` is the deepest (most
   specific) match.
4. **`UPDATE...FROM matched m WHERE e.id = m.entity_id AND m.rn = 1`**
   updates each entity with only its deepest match.

### 3.4 Why This Is Fast

- **Single SQL round-trip**, no Python loop, no per-entity Redis queries,
  no `bulk_update` batches.
- **Postgres does the join natively**, the hash join or nested loop is
  optimized by the query planner using the temp table's statistics.
- **`jsonb_each_text` is a built-in**, Postgres expands JSONB natively in C,
  not in Python.
- **`ROW_NUMBER()` is a window function**, Postgres computes the ranking in
  a single pass over the matched rows.

---

## 4. Cross-Step Data Flow

The SQL join enrichment is part of a cross-step flow that connects the
entry script, Step 1, and Step 2:

1. **Docker entrypoint (`init_planet`)**, Step 12: `enrich_worldkg_classes
   --load-from-ttl`
   - `WorldKGOntologyLoader.load_from_ttl()` parses `WorldKG_Ontolgy.ttl`
     (`rdfs:subClassOf` + `owl:equivalentClass`)
   - `WorldKGOntologyService.load_ontology_from_dict()` fills the Redis cache
     (`worldkg:class:*` keys)
2. **Step 1: Embed OSM Entities**
   - `read_from_snapshot()` → `BatchCollector` → SQL COPY upsert; `OsmEntity`
     rows created with tags JSONB, `wkg_class = NULL`
   - `enrich_worldkg_classes()` (post-processing after upsert) →
     `sql_enrich_region()` → `_load_ontology_to_temp_table()` (reads Redis
     cache) → `UPDATE...FROM` join → `OsmEntity.wkg_class` populated via
     `jsonb_each_text` join
3. **Step 2: Harvest Wikidata Candidates**
   - `WikidataCandidateService.enrich_wkg_class()` runs live SPARQL
     (`?entity wdt:P31/wdt:P279* ?typeUri`), maps `?typeUri` → `wkg_class`
     via the Redis `_wikidata_to_wkg` map, enriches candidates (not entities)
   - Candidates now carry `wkg_class` for IGEA type-matching (Step 3)

See [Wikidata Enrichment Flow](../04_ETL_Django_Vue_Primitives/03_Wikidata_Enrichment_Flow.md)
for the full cross-step narrative.

---

## 5. Failure-Mode Separation

| Layer | Failure | Effect |
|---|---|---|
| TTL load (entry script) | File missing / parse error | No ontology in Redis; `sql_enrich_region` falls back to `batch_enrich_region` which also fails → `wkg_class = NULL` |
| Redis cache | Redis down / key missing | Same as above: `sql_enrich_region` falls back, `batch_enrich_region` fails → `wkg_class = NULL` |
| Temp table load | Connection error | `sql_enrich_region` raises; Step 1 catches and continues (enrichment is best-effort) |
| UPDATE...FROM join | Lock contention | Single transaction; either commits or rolls back atomically |

A transient failure can leave `wkg_class = NULL` for some entities, but it
cannot corrupt the ontology or break the pipeline. Step 2 (Wikidata harvest)
and Step 3 (IGEA) handle `wkg_class = NULL` gracefully.

---

## 6. Invariants

- The SQL path is the default; `batch_enrich_region()` is the fallback when
  the ontology index is not built.
- The temp table is session-scoped, no cleanup needed.
- `ROW_NUMBER() ... ORDER BY depth DESC` ensures each entity gets the
  **deepest** (most specific) ontology match.
- Wildcard ontology entries (`osm_value IS NULL`) match any value for that
  key, but lose to specific entries on depth ranking.
- `wkg_enriched_at` timestamp tracks when each entity was last enriched.
