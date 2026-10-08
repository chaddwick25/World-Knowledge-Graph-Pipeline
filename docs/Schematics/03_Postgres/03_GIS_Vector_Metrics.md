# GIS, Vector, and Metrics: The Unified Postgres Stack

> **Focus:** PostgreSQL serves three roles at once. PostGIS for spatial
> indexing (GiST), pgvector for embedding storage and ANN search (HNSW,
> design only, see §3 status), and DB-backed metrics tracking
> (`PipelineRun`/`SnapshotJob`/`PipelineLogEntry`).
>
> **Key idea:** one Postgres stack replaces a dedicated vector DB plus a
> separate spatial DB plus a separate metrics store. The rows that hold the
> vectors also hold the geometry and the metadata, so relational filters,
> spatial filters, and vector similarity search run in one query.

---

## 1. The Three Roles

**Role 1: PostGIS (spatial)**
- Extension: `postgis-3`
- Column: `geom` (`geometry(Point, 4326)`)
- Index: GiST (R-tree over geometry)
- Queries:
  - bbox: `geom && ST_MakeEnvelope(min_lon, min_lat, ...)`
  - KNN: `ORDER BY geom <-> ST_MakePoint(lon, lat) LIMIT 50`
  - within: `geom__within=polygon`
- Used by: entity loading, NLE encoder, inductive encoding, search

**Role 2: pgvector (embeddings)**
- Extension: `pgvector`
- Columns:
  - `gv_tags_embedding` (VECTOR 300), FastText semantic
  - `gv_nle_embedding` (VECTOR 100), DeepWalk spatial
  - `static_embedding` (VECTOR 400), fused for ANN
- Index: HNSW (`vector_cosine_ops`, m=16, ef_construction=128)
- Queries: ANN, `ORDER BY static_embedding <=> query_vec LIMIT 50`
- Used by: semantic search, hybrid search, MapQA executor

**Role 3: Metrics tracking (Django models)**
- DB: default (port 5432)
- Models:
  - `PipelineRun`, run status, current_stage, stage_metrics
  - `SnapshotJob`, (country_code, snapshot_date, status)
  - `PipelineLogEntry`, step-level structured log entries
  - `TaskResult`, Celery task state (django-celery-results)
- Used by: frontend progress panels, dashboard, API endpoints

---

## 2. PostGIS: Spatial Indexing

### 2.1 GiST Index on `geom`

```sql
CREATE INDEX idx_..._geom ON embeddings_..._cu USING gist (geom);
```

The GiST (Generalized Search Tree) index implements an R-tree over the
`geometry(Point, 4326)` column. It provides:

| Query Type | SQL | Speed | Used By |
|---|---|---|---|
| Bounding box | `geom && ST_MakeEnvelope(...)` | O(log N) | Entity loading by country bbox |
| KNN | `ORDER BY geom <-> ST_MakePoint(lon, lat) LIMIT 50` | O(log N + k) | NLE encoder, inductive encoding |
| Within polygon | `geom__within=polygon` | O(log N) | Subgraph entity loading, subdivision search |

### 2.2 After Partitioning

With per-country leaf partitions, the GiST index on each leaf is stronger.
Partition pruning means the GiST only scans the relevant country's entities:

```
Without partitioning:
  GiST scans 3M rows, post-filters by country → slow

With partitioning:
  Partition prune to embeddings_2025_12_31_cu (190K rows)
  GiST scans only 190K rows → fast
```

### 2.3 PostGIS in the NLE Encoder

The NLE encoder (`NLEModel.encode_coords`) uses the GiST KNN operator `<->`
to find the 50 nearest entities:

```sql
SELECT osm_key,
       public.st_distance(location::public.geography, point::public.geography)
FROM   {table_name}
WHERE  public.st_distance(location::public.geography, point::public.geography) > 0
ORDER BY location OPERATOR(public.<->) point
LIMIT 50;
```

This is the database-level equivalent of the BallTree k-NN query. It works
at runtime without loading entities into memory.

### 2.4 PostGIS in the MapQA Executor

The MapQA executor uses PostGIS for spatial operations:
- `ST_DWithin`, radius-based filtering ("amenities within 50m of X")
- `Distance` (GisDistance annotation), ordering by distance ("nearest X to Y")

Both use the GiST index for O(log N + k) lookups.

---

## 3. pgvector: Embedding Storage + ANN

> **Status (2026-09-09): the HNSW-on-`static_embedding` index and the
> query-time ANN flow in §3.2-3.3 are design, not live.** `static_embedding`
> is NULL for all 46,030,536 rows, no index exists, `use_ann` is opt-in
> `false`. Runtime search is exact (`+ 0`) + USLP re-rank (see
> `docs/plans/FUSED_400D_ANN_DEDUP_PLAN.md` and
> `docs/issues/HNSW_RELEVANCE_AND_USLP_RERANK_DESIGN.md`).

### 3.1 Three Vector Columns

| Column | Dim | Source | Index |
|---|---|---|---|
| `gv_tags_embedding` | 300 | FastText (semantic) | (fused into static_embedding) |
| `gv_nle_embedding` | 100 | DeepWalk (spatial) | (fused into static_embedding) |
| `static_embedding` | 400 | concatenate(GV-Tags, GV-NLE) | HNSW planned, not built (2026-09-09) |

### 3.2 HNSW Index

```sql
CREATE INDEX ON embeddings_..._cu
  USING hnsw (static_embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 128);
```

- **150 bytes/row** (graph edges only, m=16 pointers per node)
- **O(log N) per query**, no training or re-clustering needed
- **Incrementally updatable**, no retraining when data changes
- **Works at 600M rows**, 90 GB index fits on a 96GB+ machine

### 3.3 Query-Time ANN

```sql
SELECT osm_id, tags, wkg_class, geom,
       static_embedding <=> %s::vector AS distance
FROM semantic_search_osmentity
WHERE geom @ ST_MakeEnvelope(...)           ← spatial boundary (GiST)
ORDER BY static_embedding <=> %s::vector    ← HNSW probe on 400D
LIMIT 50;
```

Both indexes serve one query:
- GiST prunes to the spatial boundary (USLP relation scope)
- HNSW probes the 400D fused embedding for semantic similarity

### 3.4 Cosine Distance Semantics

- Distance range: [0, 2]
- Similarity = 1 - distance
- L2 normalization: GV-Tags embeddings must have norm ≈ 1.0
- `vector_cosine_ops` operator class for HNSW

---

## 4. Metrics Tracking: DB-Backed Pipeline Observability

### 4.1 Three Layers

| Layer | Model | DB | Role |
|---|---|---|---|
| Run-level state | `PipelineRun` | default | Authoritative run status (PENDING/RUNNING/COMPLETED/FAILED), `current_stage`, `completed_stages`, `stage_metrics` |
| Snapshot-level state | `SnapshotJob` | default | `(country_code, snapshot_date, status)`, gates pipeline start (409 if RUNNING) |
| Step-level audit | `PipelineLogEntry` | default | Queryable structured log entries per step with `metadata` JSONField |
| Celery audit | `TaskResult` | default | Celery's internal task state machine. 48h expiry. |

### 4.2 SnapshotJob: Pipeline Status Gate

**Source:** `osmsnapshot/models.py`

```python
class SnapshotJob(models.Model):
    country_code = CharField(max_length=3)
    snapshot_date = CharField(max_length=20)  # YYYY_MM_DD
    status = CharField(...)  # PENDING/RUNNING/COMPLETED/FAILED
    pipeline_run_id = FK(PipelineRun)
    started_at, completed_at, error_message
    # Indexed on (country_code, snapshot_date, status)
```

- Pipeline start view gates on `SnapshotJob`: 409 if RUNNING, `force=true`
  to re-run COMPLETED.
- Step 6 + canvas `on_failure` update `SnapshotJob.status`.
- REST: `/api/snapshot-jobs/<iso>/` (dates), `SnapshotJobStatusView`,
  `SnapshotJobResultsView` (joins `TaskResult` for step-level detail).
- Frontend year selector reads `SnapshotJob` records for snapshot date badges.

### 4.3 PipelineRun: Run-Level State

```python
class PipelineRun(models.Model):
    status = CharField(...)  # PENDING/RUNNING/COMPLETED/FAILED
    current_stage = CharField(...)
    completed_stages = JSONField(...)
    stage_metrics = JSONField(...)  # counts, durations, quality checks
```

- `start_stage()` / `complete_stage()` / `mark_failed()` methods
- `stage_metrics` stores per-stage counts, durations, basic quality checks
- Consumed by frontend progress panels and dashboard

### 4.4 PipelineLogEntry: Step-Level Audit

```python
class PipelineLogEntry(models.Model):
    pipeline_run = FK(PipelineRun, null=True)
    country_code = CharField(...)  # denormalized for shard routing
    continent = CharField(...)     # denormalized for shard routing
    step_name = CharField(...)
    level = CharField(...)  # start/complete/warning/error
    metadata = JSONField(...)
    timestamp = DateTimeField(auto_now_add=True)
```

- Queryable structured log, `filter(country_code=..., step_name=..., level=...)`
- `metadata` JSONField stores step-specific context (entity counts,
  durations, error details)
- Denormalized `country_code` + `continent` for future `ShardRouter` routing
  without a JOIN

### 4.5 USLP Dashboard: aggregate() + Case/When

**Source:** `igea/services/augmented_data_service.py`

USLP predictions are persisted as `SpatialTripletScore` rows. The dashboard
retrieves and aggregates them in a single SQL round-trip:

```python
result = accepted_qs.aggregate(
    total=Count('id'),
    geo_dominant=Count(
        Case(When(geo_score__gt=F('name_score') + F('topo_score'), then=1))
    ),
    name_dominant=Count(
        Case(When(name_score__gt=F('geo_score') + F('topo_score'), then=1))
    ),
    class_dominant=Count(
        Case(When(topo_score__gt=F('geo_score') + F('name_score'), then=1))
    ),
    b0=Count(Case(When(normalized_score__gte=0.0, normalized_score__lt=0.2, then=1))),
    # ... b1-b4 histogram buckets ...
    avg_confidence=Avg('normalized_score'),
)
```

`F()` expressions and `Case/When` keep the arithmetic and counting inside
the SQL engine. Only the scalar aggregates return over the wire, not every
row.

---

## 5. The Unified Query: All Three Roles in One SQL

The hybrid search endpoint demonstrates all three roles working together:

```sql
SELECT osm_id, tags, wkg_class, geom,
       static_embedding <=> %s::vector AS distance
FROM semantic_search_osmentity
WHERE snapshot_id = %s                    ← partition pruning (metrics)
  AND country_code = %s                   ← partition pruning (metrics)
  AND wkg_class = %s                      ← relational filter (metadata)
  AND geom @ ST_MakeEnvelope(...)         ← spatial filter (PostGIS GiST)
ORDER BY static_embedding <=> %s::vector  ← ANN search (pgvector HNSW)
LIMIT 50;
```

This single query uses:
1. **Partition pruning** (snapshot_id + country_code), routes to the leaf
2. **Relational filter** (wkg_class), narrows by ontology class
3. **Spatial filter** (GiST), narrows by bounding box
4. **ANN search** (HNSW), finds semantically similar entities

A dedicated vector DB plus a separate spatial DB plus a separate metadata
store would need a three-phase query with cross-DB joins. Postgres does it
in one pass.

---

## 6. Invariants

- The vectors DB must have both `pgvector` and `PostGIS` extensions.
- GiST index on `geom` is required for the NLE encoder's KNN query.
- HNSW index on `static_embedding` is built after Step 5 (not during bulk
  load).
- `PipelineLogEntry` has denormalized `country_code` + `continent` for
  future shard routing without a JOIN.
- `SnapshotJob` gates pipeline start, 409 if RUNNING, `force=true` to
  re-run COMPLETED.
- USLP dashboard uses `aggregate()` + `Case/When` for single-round-trip
  metrics.
