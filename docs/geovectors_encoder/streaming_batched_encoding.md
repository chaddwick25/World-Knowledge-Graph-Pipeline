# Streaming Batched Encoding: PBF → Bounded Memory

> **Focus:** SAX-style PBF ingestion via `pyosmium.SimpleHandler`, chunked
> DB flush, and bounded parallel encode/upsert fan-out via `BatchCollector`.
>
> **Key idea:** a planet PBF is ~70 GB; a large country (Norway, US) is
> 1-3 GB. Peak worker memory must stay bounded by one chunk, not by file
> size. The streaming pattern makes processing large countries possible on a
> 4 GB Celery worker.

---

## 1. Why Streaming

The pipeline ingests the full planet PBF and per-country snapshot PBFs.
Loading any of these fully into memory is fatal on a 4 GB Celery worker.
Every PBF is treated as a **stream**: `pyosmium` reads the file sequentially
from disk and fires SAX callbacks (`node()`, `way()`, `relation()`) per
element. Handlers buffer a fixed number of records and flush before the
buffer grows unbounded.

**Primitives:**
- Planet / country PBF (raw OSM, on disk) → osmium `SimpleHandler` (SAX
  streaming) → Parquet / DB sink

**Streaming contract (Handler):**
- `node(n)` / `way(w)` / `relation(r)` append to in-memory lists (ids, lats,
  lons, tags, ...)
- flush when `len(buf) >= batch_size`
- flush = Parquet `write_table` or enqueue to the upsert queue

**Infrastructure:**
- PostgreSQL (`OsmEntity`): SQL COPY upsert, `source_snapshot_id`
  provenance. The Parquet/`GraphExtract` sink was removed 2026-09-09
  (dead-code cleanup, see §4).

---

## 2. The Streaming Handler Contract

Every PBF consumer subclasses `osmium.SimpleHandler` and overrides one or
more of `node()`, `way()`, `relation()`. The contract:

1. **`__init__`** opens the sink (Parquet writers, a flush callback, or a
   `BatchCollector`) and initializes fixed-size buffers.
2. **`node` / `way` / `relation`** append to the in-memory buffer and call
   `_flush_*()` when `len(buf) >= batch_size`.
3. **`_flush_*()`** writes the buffer to the sink and resets it to empty.
   Peak memory is one batch.
4. **`close_files()` / `flush_all()`** does a final flush and closes the sink.
5. **`apply_file(path, locations=...)`** drives the stream. Osmium reads the
   PBF from disk, fires callbacks, and never materializes the whole file.

`locations=True` tells osmium to populate node coordinates (needed for any
spatial handler); `locations=False` skips coordinate loading entirely and
saves memory for handlers that only need node/way refs.

---

## 3. Handler Inventory

| Handler | File | Sink | batch_size | locations | Purpose |
|---|---|---|---|---|---|
| `AssetExtractor` (removed 2026-09-09) | `core/services/snapshot/asset_extractor.py` | Parquet (nodes/edges/tags) | 500,000 | True | GraphExtract substrate, dead code |
| `StreamingOsmExtractor` (removed 2026-09-09) | `semantic_search/management/commands/extract_osm_embeddings.py` | flush callback (DB upsert) | configurable | True | Offline entity extraction, command deleted |
| `NodeHandler` | `core/services/snapshot/embedding_shapely_splitter.py` (Pass 1) | numpy arrays + shapely STRtree | 2,000,000 (query chunk) | True | Subgraph spatial assignment for nodes |
| `WayRelationHandler` | `core/services/snapshot/embedding_shapely_splitter.py` (Pass 2) | `node_assign` dict | streaming | False | Subgraph assignment for ways + relations |
| `AssignmentHandler` | `core/services/snapshot/embedding_spatial_split_service.py` | per-region buffers | streaming | True | Spatial split for multi-country TSVs |
| `BoundingBoxHandler` | `core/services/snapshot/pbf_bounding_box_service.py` | bbox accumulator | streaming | True | Compute PBF bounding box |
| `SampleHandler` | `geovectors_encoder/core/util.py` | `BatchCollector` (writer) | 20,000 | True | GeoVectors training-sample extraction |

---

## 4. AssetExtractor: Removed 2026-09-09

`AssetExtractor` was the canonical streaming handler. It extracted nodes,
edges, and tags from a snapshot PBF into three Parquet files that became the
`GraphExtract` substrate. It was removed with the `GraphExtract` model in
the dead-code cleanup of 2026-09-09: nothing in the pipeline created
`GraphExtract` rows or invoked the extractor.

Its lessons still apply to the surviving handlers:

- **Flush = one Parquet row group.** Every 500,000 nodes (or edges, or
  tags) the buffer converts to a PyArrow `Table`, written as a new row
  group, and resets. Peak memory is one batch of three Python lists (~12 MB
  for 500K nodes), not the full PBF.
- **Way centroid bug.** An earlier version appended way IDs + centroids as
  pseudo-nodes into `nodes.parquet`, polluting the table with fake entries
  whose `osm_id` was a way ID. The fix: never write way centroids. If way
  centroid coordinates are needed for GNN features, compute them downstream
  from `edges.parquet` + `nodes.parquet`.
- **Invalid location handling.** `osmium.InvalidLocationError` is caught
  per-node so one bad node cannot desync the parallel arrays or abort the
  pass. The `try` wraps the location access **before** touching the arrays.

---

## 5. The Step 1 Production Path: BatchCollector

**Source:** <ref_file file="backend/geovectors_encoder/services/batch_collector.py" />
**Called by:** <ref_file file="backend/geovectors_encoder/core/util.py" /> (`read_from_snapshot`)

The Step 1 production path streams the PBF via `SampleHandler` (another
`SimpleHandler` subclass) with `chunk_size = 20,000`. Instead of writing to
a file, it pushes records into a `BatchCollector`, a **drop-in writer
replacement** that fans out to parallel encoding workers.

### 5.1 Bounded Queue + Single Upsert Worker

```
PBF (on disk)
  ↓  osmium reads sequentially
SampleHandler.node(n)          ← SAX callback per element
  ↓  append to in-memory record list
  ↓  every 20,000 nodes:
writer.storage.flush()         ← pushes one batch to BatchCollector
  ↓  bounded upsert_queue (maxsize = workers × depth)
encoding_worker (N threads)    ← FastText + NLE encode, release GIL
  ↓  encoded batch
upsert_worker (1 thread)       ← SQL COPY into OsmEntity (vectors DB)
  ↓
PostgreSQL (vectors, port 5433)
```

### 5.2 Why a Single Upsert Worker

The single upsert worker eliminates PostgreSQL lock contention on the leaf
partition's unique index. The regression that motivated it: 8 workers each
running `INSERT ... ON CONFLICT` concurrently into the same leaf partition
caused lock contention (Option A, abandoned). The current design (Approach
B / Option C) uses N encoding threads + 1 upsert thread. Encoding is
parallel, upsert is serialized.

### 5.3 BatchCollector Contract (do not break)

`read_from_snapshot` and the `itertools.chain(w_data, r_data)` loop in
`EmbeddingService.run` depend on `BatchCollector` being a drop-in writer:

- `BatchCollector` exposes `add_line(record)` and `storage` (self), so it
  replaces `DBOnlyWriter` / `DualEncodingWriter` without changes.
- `storage.flush()` is a no-op when the buffer is empty (`SampleHandler`
  calls it every `chunk_size` nodes; empty batches must not be enqueued).
- `finish(n_workers)` flushes any partial batch and enqueues one sentinel
  per worker so every consumer thread terminates exactly once.
- `counts` / `total` reflect every record handed to `add_line`, grouped by
  OSM type (`node` / `way` / `relation`).

### 5.4 Thread-Safety

- `FastTextModel` and `NLEModel` are shared across encoding threads. FastText
  inference releases the GIL; `NLEModel.encode_coords` issues a PostGIS KNN
  query via `DjangoPostgresDB.get_pool_connection()`, which returns the
  *calling thread's* Django connection. Each worker gets its own psycopg2
  connection automatically.
- Encoding threads do **no** database I/O. They only encode and push encoded
  batches onto the `upsert_queue`.
- A single `upsert_worker` thread owns the only `VectorStorageService`
  instances. This eliminates PostgreSQL lock contention.
- The upsert thread closes its Django connections in `finally` via
  `connections.close_all()` (no request lifecycle in Celery threads).

### 5.5 Sentinel-Based Termination + ErrorBox

- One `SENTINEL` object per worker guarantees clean shutdown without
  deadlocks on a full bounded queue.
- `ErrorBox`, a thread-safe single-slot container, lets the first worker
  exception propagate without all workers coordinating.

---

## 6. The Two-Pass Subgraph Splitter

**Source:** <ref_file file="backend/core/services/snapshot/embedding_shapely_splitter.py" />

`embedding_shapely_splitter` is the most memory-conscious handler. It
assigns OSM entities to subgraph regions (e.g. US states, GB countries) via
shapely STRtree containment. Two streaming passes:

### 6.1 Pass 1: Node → Region (locations=True, chunked STRtree)

`NodeHandler` streams nodes with `locations=True` into numpy arrays
(`osm_ids`, `lons`, `lats`). Even though it buffers all node coordinates,
the spatial containment test runs in **2M-node chunks** to bound the STRtree
query memory.

### 6.2 Pass 2: Way/Relation → Region (locations=False)

`WayRelationHandler` streams ways + relations with `locations=False`. Only
node/way refs are needed, not coordinates, so coordinates are never
materialized in this pass. Each way's nodes are looked up in the
`node_assign` map built in Pass 1. This is the single biggest memory saving
in the splitter.

---

## 7. OOM-Prevention Technique Summary

| Technique | Where | Effect |
|---|---|---|
| SAX-style `SimpleHandler` streaming | All handlers | PBF never fully loaded; peak memory = one batch |
| `locations=False` second pass | `embedding_shapely_splitter` Pass 2 | No coordinate materialization for way/relation pass |
| Chunked shapely STRtree queries (chunk_size=2M) | `embedding_shapely_splitter` Pass 1 | Bounded spatial-index memory |
| Bounded `upsert_queue` + single upsert worker | `BatchCollector` | Bounded encode/upsert memory, no PG lock contention |
| `chunk_size=20k` for `read_from_snapshot` | `geovectors_encoder/core/util.py` | Small batches for the parallel fan-out |
| `InvalidLocationError` caught per-node | `AssetExtractor.node` (pattern kept) | One bad node can't desync buffers or abort the pass |
| Sentinel-based worker termination | `BatchCollector.finish` | Clean shutdown without deadlocks on a full bounded queue |
| `max_dict_size` cap on dependency dicts | `SampleHandler` way_nodes / relation_nodes | Unbounded relation-member dicts trimmed at 100k entries |

The common thread: **never hold the whole PBF in memory**. Stream via
osmium's SAX API, buffer in fixed-size chunks, and flush to disk (Parquet)
or to a bounded queue (DB upsert) before the buffer grows unbounded.

---

## 8. The Pre-Streaming Regression (and Why It Was Replaced)

The upstream `GeoVectors-master` reference implementation used the same
`osmium.SimpleHandler` SAX API but **did not flush in chunks**.
`SampleHandler` accumulated every tagged node, way, and relation into
in-memory lists/dicts for the entire PBF pass, then `read_from_snapshot`
deep-copied all three collections before resolving way/relation coordinate
dependencies.

This worked for small countries (Cuba, Iceland) but had three fatal problems
at planet scale:

1. **Peak memory = O(PBF size).** A 1-3 GB country PBF (Norway, Australia)
   produced tens of millions of records held in Python lists/dicts
   simultaneously. On a 4 GB Celery worker, this was an instant OOM.
2. **`deepcopy` doubled the memory footprint.** After the PBF pass,
   `read_from_snapshot` did `deepcopy(sample_handler.sampled_nodes)` etc.
   to avoid mutating the handler's state during dependency resolution.
3. **No backpressure.** `DependencyGeomHandler` also deep-copied the
   dependency dicts, so each pass doubled those dicts too.

### The Streaming Rewrite

The in-tree `geovectors_encoder/core/util.py` is a streaming rewrite with
three changes that fixed the OOM:

| Change | Upstream (old) | In-tree (new) | Effect |
|---|---|---|---|
| **Chunked flush** | `sampled_nodes.append(record)`, never flushed | `writer.add_line(record)` + `writer.storage.flush()` every 20k nodes | Peak buffer = 20k records, not O(PBF) |
| **No `deepcopy`** | `deepcopy(sample_handler.sampled_nodes)` after pass | Writer drains inline during the pass; no post-pass copy | Memory footprint halved |
| **`max_dict_size` cap** | `way_nodes` / `relation_nodes` grow unbounded | `if len(self.way_nodes) > self.max_dict_size: trim to last 50k` | Bounded dependency dicts |

---

## 9. Throughput Ceiling

The streaming rewrite solved the OOM problem. A separate question is whether
upsert throughput can be improved. After benchmarking, the current
production defaults (`parallel_upsert.workers=1` in `pipeline/hyperparams.yaml`,
`PARALLEL_UPSERT_CHUNK_SIZE=20000`) are already at the I/O-bound ceiling for
the FastText-only encoding path. **Verified 2026-08-31:** `.env` confirms
`parallel_upsert.workers=1`, the live configuration, not just a documented
default.

### Benchmark (Belize, 307,937 rows)

| config | workers | chunk_sz | wall_clock_s | rows/sec | vs base |
|---|---|---|---|---|---|
| baseline | 1 | 20000 | 177.7 | 1733 | +0% |
| 2w_60k | 2 | 60000 | 181.5 | 1697 | -2% |
| 1w_60k | 1 | 60000 | 202.1 | 1524 | -12% |

- 2 workers + 60k batch: -2%. Parallel encoding almost exactly offset the
  60k batch penalty; no net gain.
- 1 worker + 60k batch: -12%. Tripling the batch size alone was
  significantly slower. Larger SQL COPY transactions hold locks longer and
  generate larger WAL flushes.

The bottleneck is disk I/O on SQL COPY, not encoding. FastText encoding is
only ~1s per 20K batch. The only scenario where parallelism would pay off is
the NLE two-axis path (per-entity PostGIS 50-NN query + Node2Vec inference,
10-50x more expensive than FastText).

---

## 10. Why pgvector on PostgreSQL (Not a Dedicated Vector DB)

A dedicated vector database (Milvus, Qdrant, Weaviate) would not
dramatically improve ingestion throughput. The I/O ceiling doesn't go away.
Reasons to stay on pgvector:

- **PostGIS integration**: the NLE encoder's 50-NN geographic query uses
  `st_distance` + `<->` operator. Moving vectors to a separate DB would split
  the spatial query from the vector storage.
- **Cross-database joins**: the search API filters by `snapshot_id`,
  `country_code`, `wkg_class`, and `geom` *before* vector similarity search.
  Those relational filters are on the same rows that hold the vectors.
- **Partitioning**: the per-country leaf partition scheme gives per-country
  query isolation. That's PostgreSQL native partitioning; a dedicated vector
  DB would need its own sharding strategy.

The throughput ceiling is a disk I/O constraint, not a pgvector constraint.
