# Vector Storage & Partitioning: pgvector + PostGIS

> **Focus:** PostgreSQL is the unified storage stack for embeddings. The
> two-DB topology (default + vectors), the `OsmEntity` partitioned table
> schema, the HNSW index lifecycle, and the per-leaf index strategy.
>
> **Key idea:** Postgres handles GIS, vector support, and metrics in one
> stack. `semantic_search_osmentity` is partitioned
> `PARTITION BY LIST (snapshot_id)` → `LIST (country_code)`, so the partition
> IS the spatial filter. Partition pruning does the work of a bbox scan, a
> 23x speedup.

---

## 1. Database Topology

### 1.1 Two PostgreSQL instances

| Alias | Engine | Port | Purpose | Docker image |
|---|---|---|---|---|
| `default` | PostGIS | 5432 | Orchestration models (`PipelineRun`, `SnapshotJob`, `CountryPipelineProfile`, `PartitionRegistry`, etc.) | `postgis/postgis` |
| `vectors` | PostGIS + pgvector | 5433 | `OsmEntity` (embeddings + partitions), `PrecomputedLinkCandidate`, IGEA/graph models | Custom `postgres-vectors.Dockerfile` (PostgreSQL 15 + `postgresql-15-pgvector` + `postgresql-15-postgis-3`) |

**Source:** <ref_file file="backend/backend/settings.py" />

### 1.2 Vectors DB server tuning

The `postgres-vectors` container is tuned for large HNSW index builds and
bulk upserts:

```
shm_size: 24g
command:
  -c synchronous_commit=off
  -c maintenance_work_mem=16GB
  -c max_wal_size=32GB
  -c checkpoint_timeout=1h
  -c shared_buffers=32GB
  -c work_mem=128MB
  -c effective_cache_size=96GB
  -c random_page_cost=1.1
  -c effective_io_concurrency=200
  -c max_parallel_maintenance_workers=4
```

`/dev/shm` is 24 GB because the previous 1 GB cap caused "No space left on
device" during parallel `CREATE INDEX ... USING hnsw`.

### 1.3 Database routers

**Source:** <ref_file file="backend/backend/database_router.py" />

Three routers in priority order:
1. **`ShardRouter`**, currently a no-op. Intended for future continent-based
   sharding. Returns `None`, falls through.
2. **`AppRouter`**, routes `core`, `osmsnapshot` apps to `default`.
3. **`VectorDBRouter`**, routes `vectors`, `igea`, `worldkg_nca` apps to
   `vectors`. `allow_migrate` ensures these apps' tables only exist on
   `vectors`.

`OsmEntity` (app label `worldkg_nca`) automatically reads/writes to the
`vectors` DB without explicit `.using('vectors')` calls.

---

## 2. The `semantic_search_osmentity` Table

### 2.1 Django model

**Source:** <ref_file file="backend/worldkg_nca/models.py" />

```python
class OsmEntity(models.Model):
    db_table = 'semantic_search_osmentity'
    unique_together = [['osm_type', 'osm_id', 'gv_tags_version',
                        'snapshot_id', 'country_code']]
```

Key columns:

| Column | Type | Purpose |
|---|---|---|
| `id` | `BIGINT` (sequence) | Surrogate PK |
| `osm_type` | `VARCHAR(10)` | `node` / `way` / `relation` |
| `osm_id` | `BIGINT` | OSM element ID |
| `tags` | `JSONB` | Raw OSM tags |
| `gv_tags_embedding` | `VECTOR(300)` | FastText semantic ("what you are") |
| `gv_nle_embedding` | `VECTOR(100)` | DeepWalk spatial ("where you are") |
| `static_embedding` | `VECTOR(400)` | Concatenated GV-Tags + GV-NLE for ANN |
| `wkg_class` | `VARCHAR(200)` | WorldKG ontology class |
| `wkg_superclasses` | `VARCHAR(200)[]` | WorldKG hierarchy path |
| `geom` | `geometry(Point, 4326)` | PostGIS point (lat/lon) |
| `snapshot_id` | `VARCHAR(20)` | `YYYY_MM_DD`, PARTITION KEY |
| `country_code` | `VARCHAR(3)` | ISO code, PARTITION KEY |
| `source_snapshot_id` | `UUID` | Cross-DB FK to `osmsnapshot.Snapshot` |

### 2.2 The Two Snapshot Fields (Critical Distinction)

| | `source_snapshot_id` | `snapshot_id` |
|---|---|---|
| Type | UUIDField | CharField |
| Source | `Snapshot.id` | derived from UUID |
| DB | default (port 5432) | vectors (port 5433) |
| Purpose | cross-DB FK reference | PARTITION KEY |
| Example | `a1b2c3d4-...` | `'2025_12_31'` |

Bridge: `snapshot_id_from_uuid(uuid) → str` (`worldkg_nca/snapshot_utils.py`)

---

## 3. Partitioning: Active

`semantic_search_osmentity` is a partitioned table:

```
PARTITION BY LIST (snapshot_id)
  → LIST (country_code)
```

Each country gets its own leaf partition:

- `embeddings_partitioned` (root, partitioned by snapshot_id)
  - `embeddings_2025_12_31` (snapshot partition)
    - `embeddings_2025_12_31_cu` (ALL Cuba, 190K rows; no leaf HNSW, exact `+ 0` scans only)
    - `embeddings_2025_12_31_cv` (ALL Cape Verde, 38K)
    - `embeddings_2025_12_31_is` (ALL Iceland, 184K)
    - `embeddings_2025_12_31_jm` (ALL Jamaica, 30K)

### 3.1 Automatic Cutover

The cutover happens automatically on a fresh DB:
1. `create_country_partitions` detects the empty monolith
2. Drops it and recreates as a partitioned table
3. Step 1 calls `create_country_partitions --country XX --skip-data --skip-mv`
   before upserting (creates the leaf partition)
4. Step 6 calls `create_country_partitions --country XX --mv-only` (creates
   MV + HNSW)

No manual cutover or backfill needed on fresh DBs. Verified E2E with JM
(Jamaica) and CV (Cape Verde).

### 3.2 Runtime Partition-Aware Upserts

`vector_storage_service._bulk_upsert()` checks `pg_partitioned_table` at
runtime to select the correct ON CONFLICT target:
- **Monolith**: 3-column `(osm_type, osm_id, gv_tags_version)`
- **Partitioned**: 5-column `(osm_type, osm_id, gv_tags_version, snapshot_id, country_code)`

No setting flag needed. The runtime check is the sole source of truth.

### 3.3 Partition Pruning = Spatial Filter

```
Query: "restaurants in Cuba"
  → Prune to embeddings_2025_12_31_cu (190K rows)
  → exact `+ 0` scan within that leaf (~5ms; HNSW not in the query path)

Without partitioning:
  → Seq scan all 3M rows, filter by country → slow
```

The partition key derives from two fields:

1. **`snapshot_id`**: from `source_snapshot_id` (UUID) via
   `snapshot_id_from_uuid()` → `Snapshot.timestamp` → `'2025_12_31'`
2. **`country_code`**: from `geom && ST_MakeEnvelope(bbox)` where the bbox
   comes from `CountryPipelineProfile` → `'CU'`

The routing key is the pair `(snapshot_id, country_code)`.

---

## 4. HNSW Index: The 400D ANN Index

> **Status (2026-09-09): design, not built.** `static_embedding` is NULL for
> all 46,030,536 rows, `osmentity_static_embedding_hnsw_idx` does not exist,
> and `use_ann` is opt-in `false`. The only HNSW indexes in the DB are the
> per-snapshot MV indexes (`idx_mv_embeddings_*_hnsw`), which no runtime query
> reads. Sections 4.1-4.3 describe the intended build (plan:
> `docs/plans/FUSED_400D_ANN_DEDUP_PLAN.md`).

### 4.1 What HNSW Stores

**HNSW index (150 bytes/row):**
- Layer assignment (which level this node lives on)
- m=16 neighbor pointers per layer
- Distance values to neighbors
- Total: 150 B/row → 434 MB for 3M rows → 90 GB for 600M rows

**Heap table + TOAST (3,435 bytes/row, measured):**
- Main heap row: osm_id, tags (JSONB), geom, metadata (small, fits inline,
  ~200-300 bytes)
- TOAST table: `static_embedding` (400D = 1.6 KB), `gv_tags_embedding`
  (300D = 1.2 KB), `gv_nle_embedding` (100D = 400 B); vectors > 2KB are
  TOASTed out-of-line
- Total: 3,435 B/row → 9.9 GB for 3M → 1.4 TB for 400M (AU)

The HNSW index stores **only the graph structure**: m=16 edges per node,
each edge is a pointer (~8 bytes), so ~128 bytes + overhead = 150 bytes/row.
The actual vector data lives in the Postgres heap table, shared by all
indexes. The HNSW index just points into the heap.

### 4.2 Production Scale (Measured)

| Country | Entities | HNSW Index | Build Time | Verdict |
|---|---|---|---|---|
| Jamaica (JM) | 30K | 4.5 MB | ~1 min | HNSW alone |
| Cape Verde (CV) | 38K | 5.7 MB | ~1 min | HNSW alone |
| Iceland (IS) | 184K | 27.6 MB | ~5 min | HNSW alone |
| Cuba (CU) | 190K | 28.5 MB | ~5 min | HNSW alone |
| Canada (CA) | ~200M | 30 GB | ~2.1h | HNSW viable |
| Australia (AU) | ~400M | 60 GB | ~4.2h | HNSW viable |
| USA (US) | ~600M | 90 GB | ~6.4h | HNSW viable |

### 4.3 Index Lifecycle

- **Step 1**: calls `create_country_partitions --skip-data --skip-mv`,
  creates the leaf partition with **no HNSW index** (nothing to drop, nothing
  to rebuild during bulk load).
- **After Step 5 (not yet run, 2026-09-09)**: `compute_static_embeddings`
  populates the 400D `static_embedding` column (concatenates GV-Tags +
  GV-NLE; current both-axes coverage: 2,160,420 of 46,030,536 rows, 4.7%).
- **After Step 5 (not yet run)**: `create_static_embedding_hnsw_index` builds
  the HNSW index on `static_embedding` with `m=16, ef_construction=128`.
- **Step 6**: calls `create_country_partitions --mv-only`, creates the
  materialized view + HNSW. **The MV is an artifact: no runtime query reads
  it** (runtime search uses `+ 0` exact scans on the parent/leaf partitions).

The `_drop_vector_indexes` method and `DROP_INDEXES_DURING_LOAD` env var
have been **removed**. A fresh leaf partition has no HNSW during bulk load,
so there's nothing to drop.

---

## 5. Bulk Upsert Optimizations

**Source:** <ref_file file="backend/geovectors_encoder/services/vector_storage_service.py" />

`vector_storage_service._bulk_upsert()` uses:
- **Reusable UNLOGGED staging table**, created once per `VectorStorageService`
  instance, `TRUNCATE`'d between batches (not CREATE/DROP). Writes no WAL.
- **Reusable staging index** on `(osm_type, osm_id)`, built once after the
  first COPY, survives TRUNCATE.
- **Direct leaf-partition insert**, bypasses partition routing overhead
  (~3-5s per 20K batch).
- **`SET LOCAL synchronous_commit = off`** per batch transaction, replayable
  batches, safe to lose the last unflushed batch on crash.
- **Deterministic lock order**, `ORDER BY osm_type, osm_id` in merge SELECT
  (deadlock defense).

---

## 6. GiST Index: The Spatial Backbone

```
idx_..._geom ON geom USING gist
```

The PostGIS GiST index on the `geom` column provides:
- **Bounding box queries**: `geom && ST_MakeEnvelope(...)`, used by
  `backfill_partition_keys`
- **KNN queries**: `ORDER BY geom <-> ST_MakePoint(lon, lat) LIMIT 50`, used
  by the NLE encoder (`encode_coords`) and inductive encoding
- **Speed**: O(log N) for bbox, O(log N + k) for KNN

After partitioning, the GiST index on each leaf partition is even more
powerful. Partition pruning means the GiST only scans the relevant country's
entities, not the whole planet.

---

## 7. Why pgvector on PostgreSQL (Not a Dedicated Vector DB)

A dedicated vector database (Milvus, Qdrant, Weaviate) would not
dramatically improve ingestion throughput. The I/O ceiling doesn't go away.
The codebase has architectural reasons to stay on pgvector:

- **PostGIS integration**: the NLE encoder's 50-NN geographic query uses
  `st_distance` + `<->` operator. Moving vectors to a separate DB would
  split the spatial query from the vector storage.
- **Cross-database joins**: the search API filters by `snapshot_id`,
  `country_code`, `wkg_class`, and `geom` *before* vector similarity search.
  Those relational filters are on the same rows that hold the vectors.
- **Partitioning**: the per-country leaf partition scheme gives per-country
  query isolation. That's PostgreSQL native partitioning; a dedicated vector
  DB would need its own sharding strategy.

---

## 8. Invariants

- The vectors database must have both `pgvector` and `PostGIS` extensions
  enabled.
- `static_embedding` is only populated when both GV-Tags + GV-NLE are present
  (run `compute_static_embeddings` after Step 5).
- The HNSW index on `static_embedding` is built after bulk load, not during
  (no per-INSERT graph maintenance).
- `source_snapshot_id` (UUID) is a cross-DB reference to
  `osmsnapshot.Snapshot`; `snapshot_id` (VARCHAR) is the partition key.
- On a fresh DB, the first country's Step 1 converts the empty monolith to a
  partitioned table automatically, no manual cutover.
