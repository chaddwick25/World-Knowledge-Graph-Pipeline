# Subdivision Batch Training: Admin Boundaries as Parallel Work Units

> **Focus:** how administrative subdivisions are used as boundaries for
> batch training the GV-NLE model in Step 5, why the split exists (k-NN
> graph O(N²) memory), and how Celery chords synchronize the fan-out.
>
> **Key idea:** subdivisions are **parallel training work units**, not
> storage partitions. The k-NN graph construction is O(N²) in memory. A
> 400M-entity country (Australia) would not fit in RAM. Splitting by admin
> boundary keeps each BallTree small enough to fit in RAM while preserving
> local geographic structure. Storage partitioning (per-country leaf) and
> training fan-out (per-subdivision) are distinct concepts.

---

## 1. The Problem: k-NN Graph Memory

```
k-NN graph construction:
  Build BallTree(N entities) → O(N log N) build
  Query N × k=50 neighbors   → O(N log N) total
  Memory: N × k edges         → O(N × k)

For Cuba (190K entities):
  190K × 50 = 9.5M edges → ~380 MB (manageable)

For Australia (400M entities):
  400M × 50 = 20B edges → ~800 GB (does NOT fit in RAM)
```

The BallTree itself is O(N) in memory, but the k-NN graph (N × k edges) is
the bottleneck. A single BallTree for a large country would exhaust RAM
during graph construction.

---

## 2. The Solution: Admin Boundary Split

Australia (400M entities, continuous geographic space), one subgraph per
admin region, each with its own BallTree(k=50) → DeepWalk → vectors:

- Subgraph: New South Wales
- Subgraph: Victoria
- Subgraph: Queensland
- Subgraph: Western Australia
- Subgraph: South Australia
- Subgraph: Tasmania
- Subgraph: ... (parallel workers, CPU cores)

Why subdivisions? k-NN graph construction is O(N²) in memory. Splitting by
admin boundary keeps each BallTree small enough to fit in RAM while
preserving local geographic structure.

### 2.1 Why Admin Boundaries (Not Grid Cells)

- **Preserves local geographic structure**: entities within a subdivision
  are geographically clustered, so the k-NN graph captures meaningful
  spatial relationships.
- **Aligns with the Wikidata admin_level hierarchy**:
  `admin_level=4/6/8` subdivisions are already in the DB as
  `SubgraphProfile` rows.
- **Natural query scope**: users search by subdivision ("restaurants in
  Havana"), so training by subdivision aligns with query patterns.
- **Cross-boundary entities**: entities near a boundary may have neighbors
  in the adjacent subdivision. The k-NN graph for each subdivision includes
  a buffer zone (entities within `buffer_km` of the boundary) to preserve
  cross-boundary edges.

---

## 3. Subdivisions vs Storage Partitions: Critical Distinction

| | Storage partitioning (Postgres) | Training fan-out (Celery) |
|---|---|---|
| Level | per-country leaf | per-subdivision |
| Purpose | query isolation | memory-bounded training |
| Key | `(snapshot_id, country_code)` | `SubgraphProfile.slug` |
| Index | HNSW on static_embedding (design, not built 2026-09-09) | BallTree (in-memory, ephemeral) |
| Example | `embeddings_2025_12_31_cu` | havana, santiago, camagüey |

A country with 5 subdivisions has:
- 1 storage partition (`embeddings_2025_12_31_cu`)
- 5 training fan-out units (havana, santiago, ...)
- All 5 fan-out units write to the SAME storage partition

Storage partitioning is about **query isolation**. Each country's entities
live in a separate leaf partition. Training fan-out is about
**memory-bounded computation**: each subdivision's BallTree fits in RAM.
They are orthogonal concepts.

---

## 4. Step 5: The GV-NLE Training Flow

**`train_gv_nle` (Celery task):**

1. **Rehydrate subgraphs from DB** (`CountryEnvelope.from_db`):
   `SubgraphProfile.objects.filter(country_profile=profile)` → enriches with
   `subgraph_pbf_path`, `subgraph_poly_path`, etc.
2. **has_subgraphs?**
   - NO → train at country level (single BallTree)
   - YES → fan out to per-subgraph training

**Per-subgraph training (chord header, `.si()` signatures):**

`_train_subgraph_gv_nle(subgraph_envelope)`:
1. Load entities from PostGIS (GiST index, partition pruning):
   `OsmEntity.objects.filter(country_code=cfg.iso,
   snapshot_id=cfg.snapshot_date, geom__within=subgraph_polygon)`
2. Build BallTree in-memory (sklearn, haversine):
   `NearestNeighbors(n_neighbors=51, metric='haversine',
   algorithm='ball_tree', n_jobs=-1)`
3. k-NN graph + IDW damped weights: `w'(d) = max(1/ln(max(d, 1.1)), e)`
4. Weighted DeepWalk (Word2Vec Skip-gram) → 100D GV-NLE embedding per entity
5. GPU slot lock (`fcntl.flock` on `/tmp/gpu_training.lock`): only one
   worker trains at a time (16 GB GPU)
6. Upsert GV-NLE vectors to `OsmEntity.gv_nle_embedding`

**Chord callback (`step_5b_finalize`):**
1. Aggregate subgraph results
2. `compute_static_embeddings` (400D fused)
3. `create_static_embedding_hnsw_index` (HNSW on 400D)

> **Status note (2026-09-09):** steps 2-3 of the callback are pending. The
> fused `static_embedding` is NULL for all rows and no `static_embedding`
> HNSW index exists (see `docs/plans/FUSED_400D_ANN_DEDUP_PLAN.md`).

---

## 5. GPU Slot Locks

GV-NLE training (DeepWalk) is GPU-accelerated via PyTorch. With 4 prefork
workers, all 4 could load PyTorch models onto the GPU concurrently, causing
CUDA OOM on a 16 GB GPU.

**Fix:** `fcntl.flock` on `/tmp/gpu_training.lock` in
`_train_subgraph_gv_nle`:
- Only one worker trains at a time
- Others block until the lock is released
- `GpuSlotLock` queues subgraphs when the GPU is busy

### 5.1 GPU Configuration

| Env Var | Default | Purpose |
|---|---|---|
| `GV_NLE_GPU_DEVICES` | `cuda:0,cuda:1` | Comma-separated GPU device list |
| `GV_NLE_GPU_CONCURRENCY` | `1,1` | Per-GPU concurrency limits |

Set `gv_nle.gpu_concurrency` to `2,1` in `pipeline/hyperparams.yaml` only for countries with uniformly small
subgraphs (<100K entities). Large IE subgraphs (kildare: 847K, leitrim: 1M)
cause CUDA OOM at concurrency=2 on a 16 GB GPU.

---

## 6. Celery Chord Synchronization

Step 5 uses a **chord** to synchronize subgraph fan-out:

```python
chord(
    header=[sub_train_subgraph_gv_nle.si(sg_env) for sg_env in subgraphs],
    body=step_5b_finalize.s(cfg)
)
```

- Header tasks use `.si()` (immutable signatures) to prevent Celery from
  passing inherited positional args.
- The chord callback (`step_5b_finalize`) fires only when ALL header tasks
  complete.
- The callback aggregates results and runs `compute_static_embeddings` +
  `create_static_embedding_hnsw_index`.

See [Celery Control Plane](../02_Celery/01_Celery_Control_Plane.md) for the
full chord mechanics and `PatchedDatabaseBackend` polling.

### 6.1 Subgraph Rehydration (Fresh-DB Fix)

On a fresh DB, `has_subgraphs=False` at canvas dispatch time because subgraph
poly files have not been generated yet. Step 5 now **rehydrates subgraphs
from DB at task start** (via `CountryEnvelope.from_db()`). When subgraphs
are found after rehydration, the task **self-dispatches per-subgraph work
inline** instead of relying on the canvas chord.

---

## 7. The Relationship to Semantic Search

Step 5 trains GV-NLE (100D spatial embeddings). These are concatenated with
GV-Tags (300D semantic) to form the 400D `static_embedding` used for ANN
search:

- **Step 5 output**: `gv_nle_embedding` (100D) per entity
- **Post-Step 5**:
  - `compute_static_embeddings`: `static_embedding = concatenate(
    gv_tags_embedding, gv_nle_embedding)`, a 400D fused vector
  - `create_static_embedding_hnsw_index`: HNSW index on `static_embedding`
    (`vector_cosine_ops`, m=16)
- **Semantic search**: `SELECT ... ORDER BY static_embedding <=> query_vec
  LIMIT 50`, HNSW ANN for O(log N)

> **Status note (2026-09-09):** the fused embedding and its HNSW index are
> not built. Runtime search uses exact `+ 0` scans + USLP re-rank.

**Important distinction:** GV-NLE is the **spatial** axis (where something
is). Semantic search uses the **fused** 400D embedding (what + where). The
subdivision fan-out trains GV-NLE; the fused embedding is what semantic
search actually queries.

---

## 8. Invariants

- Subdivisions are **training work units**, not storage partitions.
- Storage partitioning (per-country leaf) and training fan-out
  (per-subdivision) are distinct concepts.
- The BallTree is rebuilt in-memory per subgraph, not persisted.
- GPU slot locks (`fcntl.flock`) prevent CUDA OOM on single-GPU machines.
- Chord header tasks MUST use `.si()` (immutable signatures).
- Step 5 rehydrates subgraphs from DB at task start (fresh-DB fix).
- `static_embedding` (400D) is only populated after both GV-Tags + GV-NLE
  are present.
