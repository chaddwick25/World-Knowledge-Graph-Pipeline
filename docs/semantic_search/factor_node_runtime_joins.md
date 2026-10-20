# Factor-Node Runtime Joins: Batch Compute, Runtime Serve

> **Architecture principle** (Chip Huyen, *Designing Machine Learning
> Systems*, Ch. 6 "Model Inference", Batch vs. Online Prediction):
>
> *"The simplest way to put a model into production is to **precompute all
> possible predictions** it could ever make and cache them. When a request
> comes in, you just look up the relevant prediction. This is batch
> prediction. It's the most efficient form of inference, no computation at
> request time at all."*
>
> This schematic documents that pattern applied to MapQA's spectral
> operators. The "model" is the Laplacian eigendecomposition + heat kernel
> diffusion + Louvain community detection. The "predictions" are per-entity
> eigen-loadings, community assignments, and Dirichlet contributions. The
> batch job (Step 5c) computes them once per snapshot and writes them to
> flat pgvector tables. The runtime path (`FactorResolutionService`) is pure
> SQL: it serves the precomputed values via indexed joins and pgvector `<#>`
> lookups. No NetworkX, SciPy, sklearn, or GPU at request time.

---

## 1. The Batch-Then-Serve Architecture

The core insight, following Huyen's batch prediction pattern: **move all
expensive computation to the batch pipeline, and make runtime a lookup**.

**Batch phase (pipeline, hours):**
- Step 5: GV-NLE training (GPU) → 100D spatial embeddings
- Step 5c: graph spectral analysis
  - Build k-NN graph (COO), ~1.5 GB SparseGraph (was 15 GB NetworkX)
  - GPU eigendecomposition, `torch.lobpcg`, ~8 min for IE k=64 (was
    OOM-killed)
  - networkit Louvain (C++), ~10 min for IE 2.47M nodes (was OOM-killed)
  - `FactorNodeWriter` → `factor_spectral_node_metric` (2.47M rows)

**Serve phase (request, ms):**
- MapQA query arrives → `FactorResolutionService` (pure SQL, no graph ops)
  - `check_availability()`: `SELECT EXISTS` (G4 gate)
  - `diffusion_rank()`: `ORDER BY eigen_loadings <#> :w` (one pgvector
    query = heat kernel)
  - `community_summary()`: `GROUP BY louvain_community`
  - `amenity_embedding()`: `SELECT embedding WHERE text = :q`
- Trace: `{"step": "factor_join", ...}`

**Why batch, not online?** (Huyen §6.1.3, when batch prediction works):

| Criterion | MapQA spectral operators | Batch-friendly? |
|---|---|---|
| Prediction latency requirement | < 1 second (user-facing query) | Yes, lookup is ms |
| Input space size | Finite: ~2.5M entities per country | Yes, precompute all |
| Prediction freshness | Per-snapshot (monthly cadence) | Yes, recompute on snapshot |
| Compute cost per prediction | High: full eigendecomposition + diffusion | Yes, amortize to batch |
| Prediction frequency | Many queries per entity per snapshot | Yes, reuse across queries |

Every criterion favors batch. The eigendecomposition takes 8 minutes on
GPU; doing it per-request would be 8 minutes per query. Precomputing and
serving from tables makes it 8 minutes **total**, then every query is a
sub-second SQL lookup.

---

## 2. The Batch Pipeline (Step 5c)

**Source:** <ref_file file="backend/pipeline/tasks/country_pipeline_steps/step_5c_graph_spectral/" />

### 2.1 Pipeline Flow

1. **5c.pre: process hygiene.** `gc.collect()` + guarded
   `torch.cuda.empty_cache()` (clears Step 5 GV-NLE residual memory).
2. **5c.0: build k-NN graph.** `KNNGraphService.build_sparse_graph()` →
   `SparseGraph` (COO: node_ids, edge_index, edge_weight), ~1.5 GB for IE
   (was ~15 GB NetworkX dict-of-dicts).
3. **5c.1: Laplacian eigendecomposition.**
   `SpectralAnalysisService.compute_spectral_features(SparseGraph, k)`:
   - GPU `torch.lobpcg` (CUDA available, n ≥ 10K). Block eigensolver, no LU
     factorization. IE k=64: ~8 min on RTX 4070 Ti SUPER, ~4-5 GB VRAM.
   - CPU `eigsh(which='SM')` fallback (no CUDA / small graphs).
   - Returns: eigenvalues, eigenvectors (N×K), node_order.
4. **5c.2: graph signal** (WorldKG class → Dirichlet energy).
   `GraphSignalService.build_class_signal(SparseGraph, class_map)`,
   `signal_smoothness(SparseGraph, signal)` → sᵀLs (combinatorial Laplacian
   from CSR).
5. **5c.3: store `GraphSpectralFingerprint`** (region-level summary):
   eigenvalues, Fiedler vector, λ₂, spectral gap, smoothness.
6. **5c.4: community detection (Louvain).**
   `CommunityDetectionService.detect_communities(SparseGraph)` → networkit
   PLM (parallel C++, ~2 GB, ~10 min for IE). NetworkX Louvain was
   OOM-killed at 61 GB; do not use for large graphs. IE: 308 communities,
   modularity 0.99.
7. **5c.5: write factor rows (the "predictions").**
   `FactorNodeWriter.write_spectral_nodes(SparseGraph, features, ...)` →
   2,477,070 rows → `factor_spectral_node_metric` (`eigen_loadings` 128D
   pgvector, community, degree, etc.). `clustering_coeff` NULL for graphs ≥
   200K nodes.
8. **5c.6: GraphML debug artifact** (deferred, last, non-fatal). Skipped for
   graphs ≥ `GRAPHML_NODE_THRESHOLD` (200K) nodes. `nx.write_graphml`
   builds a 20-40 GB ElementTree → OOM risk. Factor tables are
   authoritative; GraphML is never read at runtime.

### 2.2 Why the Graph Representation Matters

The original implementation used `networkx.Graph` (dict-of-dicts) as the
intermediate representation. For IE's 2.47M nodes / 73.8M edges, this cost
~15 GB, and every consumer (eigensolver, signal service, Louvain, factor
writer) materialized additional copies. The OOM at 58-61 GB was the sum of:
NetworkX graph + GraphML ElementTree + ARPACK workspace + NetworkX Louvain
data structures.

The `SparseGraph` COO representation (Huyen §3.2.3, "data format matters
for ML systems") reduces the graph to three numpy arrays (~1.5 GB).
Consumers build scipy CSR matrices lazily and only when needed. The NetworkX
graph is never materialized for large countries.

<ref_file file="backend/semantic_search/services/knn_graph_service.py" />

### 2.3 Why GPU lobpcg, Not scipy eigsh

The eigendecomposition is the most expensive batch computation. Three
approaches were tested:

| Solver | IE (2.47M nodes, k=64) | Memory | Verdict |
|---|---|---|---|
| `scipy eigsh(which='SM')` | hours (estimated) | unbounded ARPACK workspace | Too slow |
| `scipy eigsh(sigma=1e-6, which='LM')` (shift-invert) | 4 min, then **failed** | SuperLU `gstrf` error | **Fails**, LU factorization can't handle the 2.47M × 2.47M near-singular matrix |
| `torch.lobpcg` (GPU) | **8 min 20 sec** | ~4-5 GB VRAM | **Works**, block eigensolver, no factorization |

LOBPCG (Locally Optimal Block Preconditioned Conjugate Gradient) is a block
method: it computes k eigenvalues simultaneously using only matrix-vector
products. No LU factorization, no shift-invert, no singularity issues. The
GPU parallelism makes it fast enough for a batch step.

<ref_file file="backend/semantic_search/services/spectral_analysis_service.py" />

### 2.4 Why networkit, Not NetworkX Louvain

NetworkX's `louvain_communities` is pure Python. For IE's 2.47M nodes it
allocated ~25+ GB of Python objects (dicts, sets, lists for the modularity
optimization) and was OOM-killed at 61 GB RSS, after the eigendecomposition
had already succeeded and the fingerprint was stored.

networkit's PLM (Parallel Louvain Method) is a C++ implementation with
OpenMP parallelism. Same algorithm, same results, ~2 GB memory, ~10 minutes
for IE. `CommunityDetectionService` dispatches to networkit for `SparseGraph`
inputs and falls back to NetworkX for `networkx.Graph` inputs (tests, small
graphs).

<ref_file file="backend/semantic_search/services/community_detection_service.py" />

---

## 3. The Three Factor Tables (the "Cached Predictions")

**Source:** <ref_file file="backend/worldkg_nca/models.py" />

All three tables live in the `worldkg_nca` app → `vectors` database (via
`VectorDBRouter`), colocated with `OsmEntity` for runtime joins. Wide-per-
family schema (not EAV) because keys, cadences, and vector dimensions
differ between metric families.

### 3.1 SpectralNodeMetric: `factor_spectral_node_metric`

One row per `(snapshot_id, country_code, osm_id)`. Written by Step 5c. This
is the "prediction cache": each row holds the precomputed eigen-loadings
and structural metrics for one entity.

| Column | Type | Batch source |
|---|---|---|
| `snapshot_id` | CharField(20) | Partition key (YYYY_MM_DD) |
| `country_code` | CharField(3) | Subpartition key |
| `osm_id` | BigInteger | k-NN graph node key |
| `fingerprint_id` | UUID (NULL) | GraphSpectralFingerprint.id whose eigenbasis produced the loadings (Phase 1 coherence; UUID, not FK — fingerprint lives on the default DB) |
| `eigen_loadings` | VECTOR(128) | Φ row from GPU lobpcg |
| `fiedler_component` | Float | φ₂(node), scalar copy |
| `louvain_community` | Integer | networkit PLM partition |
| `dirichlet_contrib` | Float | s_i·(Ls)_i (combinatorial L) |
| `degree` | Integer | SparseGraph.degrees (bincount) |
| `clustering_coeff` | Float (NULL) | NULL for graphs ≥ 200K nodes |
| `component_id` | Integer | scipy connected_components |
| `component_size` | Integer | same |

UNIQUE: (snapshot_id, country_code, osm_id). INDEX:
(snapshot_id, country_code, louvain_community),
(snapshot_id, country_code, subgraph_slug), fingerprint_id.

### 3.2 DriftNodeMetric: `factor_drift_node_metric`

One row per `(country_code, snapshot_from_id, snapshot_to_id, osm_id)`.
Written by Step 5d. The "temporal prediction cache": precomputed per-entity
drift between snapshot pairs.

### 3.3 AmenityEmbedding: `factor_amenity_embedding`

One row per amenity vocabulary term (621 rows). Written by
`compute_amenity_embeddings` (init_planet step). Precomputed FastText
embeddings, removes the last runtime sklearn/FastText import.

---

## 4. The Serve Path (Runtime, SQL-Only)

**Source:** <ref_file file="backend/semantic_search/services/factor_resolution_service.py" />

The runtime path is the "serve" half of Huyen's batch-then-serve pattern.
No computation, only indexed lookups and one pgvector inner-product query.
The expensive graph operations (eigendecomposition, community detection,
Dirichlet energy) happened in the batch phase and are never repeated at
request time.

### 4.1 FactorResolutionService Methods

| Method | SQL pattern (what the "serve" does) |
|---|---|
| `check_availability()` | `SELECT osm_id FROM factor_spectral_node_metric WHERE ... AND osm_id IN (...)` → G4 data availability gate (batch produced?) |
| `resolve_metrics()` | `SELECT ... FROM factor_spectral_node_metric WHERE snapshot_id=... AND country_code=...` → serve precomputed per-node metrics |
| `diffusion_rank()` | `SELECT osm_id, (eigen_loadings <#> :w) AS neg_ip FROM factor_spectral_node_metric WHERE ... ORDER BY neg_ip ASC LIMIT :limit` → serve heat kernel diffusion (one query) |
| `community_summary()` | `SELECT louvain_community, count(*), ... GROUP BY louvain_community` → serve community aggregation |
| `amenity_embedding()` | `SELECT embedding FROM factor_amenity_embedding WHERE amenity_text = ...` → serve precomputed FastText embedding |

### 4.2 Heat Kernel Diffusion as One pgvector Query

This is the key transformation that makes batch-then-serve work for
spectral operators. The heat kernel score:

```
score(node) = Σ_k e^{-t·λ_k} · φ_k(anchor) · φ_k(node)
```

is an inner product between the anchor's eigen-loadings (scaled by
`e^{-t·λ_k}`) and each candidate's eigen-loadings. The batch phase
precomputed all `φ_k(node)` values and stored them as 128D pgvector rows.
At serve time:

**Batch (Step 5c, once per snapshot):**
- GPU lobpcg computes Φ (N × K eigenvector matrix)
- `φ_k(anchor)` → row in `factor_spectral_node_metric`
- `φ_k(node)` → row for every node
- `λ_k` → `GraphSpectralFingerprint`

**Serve (per query, ms):**
- Fetch the anchor's eigen-loadings (one index lookup)
- Compute `w_k = e^{-t·λ_k} · φ_k(anchor)` (K=64 numpy ops, microseconds)
- `SELECT osm_id, (eigen_loadings <#> :w) AS neg_ip FROM
  factor_spectral_node_metric WHERE snapshot_id = :snap AND country_code =
  :cc ORDER BY neg_ip ASC LIMIT :limit` → ranked results (ms)

The entire heat kernel diffusion, which was a scipy sparse matrix
exponential over the full graph per request, is now one pgvector `<#>`
(negative inner product) query. This is Huyen's batch prediction pattern in
its purest form: the "prediction" (diffusion ranking) is fully determined
by the precomputed eigen-loadings and eigenvalues; the serve path just
computes a weight vector and lets the vector index do the ranking.

### 4.3 Executor Integration

| Template | Serve path (factor tables authoritative) |
|---|---|
| EVENT-DIFFUSION | `diffusion_rank()`, pgvector `<#>` per t |
| PLACE-ATTRIBUTE-QUERY | `diffusion_rank()` + amenity filter |
| COMMUNITY-DETECT | `community_summary()`, SQL GROUP BY |
| DISTANCE / RADIUS | PostGIS ST_DWithin (no factor coverage) |
| Amenity FastText lookup | `factor_amenity_embedding` first, then FT |

### 4.4 Feature Flag

| Flag | Default | Effect |
|---|---|---|
| `FACTOR_NODE_TABLES_ENABLED` | `true` | Factor-table path is authoritative. Set to `false` only for debugging. |

`FACTOR_NODE_TABLES_SHADOW` has been removed. The legacy graph path is
gone, so there is nothing to shadow-compare against.

---

## 5. The Batch Writers

### 5.1 FactorNodeWriter: Spectral Nodes

**Source:** <ref_file file="backend/semantic_search/services/factor_node_writer.py" />

`write_spectral_nodes()` accepts `SparseGraph` or `networkx.Graph`. Per
node i (osm_id = node_order[i]):
- `eigen_loadings` = eigenvectors[i, :K] (zero-padded to 128)
- `fiedler_component` = eigen_loadings[0] (φ₂)
- `louvain_community` = node_to_community[osm_id]
- `dirichlet_contrib` = s_i · (Ls)_i (combinatorial L)
- `degree` = SparseGraph.degrees[i] (or G.degree for nx)
- `clustering_coeff` = NULL (graphs ≥ 200K) or nx.clustering
- `component_id`, `component_size` = scipy connected_components
- bulk_create row

Written in 5k-row ORM batches → `factor_spectral_node_metric`.

### 5.2 FactorNodeWriter: Drift Nodes

<ref_snippet file="backend/semantic_search/services/factor_node_writer.py" lines="144-200" />

### 5.3 compute_amenity_embeddings Command

**Source:** <ref_file file="backend/semantic_search/management/commands/compute_amenity_embeddings.py" />

`init_planet` step "compute_amenity_embeddings":
1. Load FastText cc.en.300.bin model
2. Iterate the MapQA amenity vocabulary (621 terms):
   - `embedding = fasttext.get_word_vector(amenity_text)`
   - L2-normalize: `embedding /= ‖embedding‖`
   - upsert into `factor_amenity_embedding`
3. Registered before "finalize" in the init_planet step list

---

## 6. Database Routing

**Source:** <ref_file file="backend/backend/database_router.py" />

The factor models are in `worldkg_nca` (not `semantic_search`) because
`VectorDBRouter` routes `worldkg_nca` to the `vectors` database. This
colocates them with `OsmEntity` for runtime joins.

| App | Database | Why |
|---|---|---|
| `worldkg_nca` (factor models, `OsmEntity`) | vectors | pgvector + PostGIS + colocated with `OsmEntity` for runtime joins |
| `semantic_search` (executor, resolver) | default | App logic, no factor tables here |
| `core`, `osmsnapshot` | default | Pipeline tracking, snapshot identity |

---

## 7. IE E2E Verification (2026-08-19)

The batch-then-serve architecture was verified end-to-end on Ireland
(IE/2025_12_31), the country that originally OOM-killed the pipeline.

### 7.1 Batch Phase Results

```
Run ID: 0818cccb-a371-4d94-a1ef-ab62d007e2d4
Total time: 53 min 28 sec

  11:56  Step 5c started
  12:13  k-NN graph loaded (2,477,073 entities from vectors DB)
  12:33  k-NN graph built (2,477,070 nodes, 123,853,500 edges)
  12:34  build_sparse_graph → COO representation (~1.5 GB)
  12:34  Computing Laplacian eigendecomposition (k=64)
  12:34  GPU lobpcg: n=2477070 k=65 device=cuda:0
  12:39  Stored GraphSpectralFingerprint (λ₂ ≈ -4.3e-9, gap=1.19e-4)
         ↑ eigendecomposition: 8 min 20 sec on GPU
  12:40  FactorNodeWriter: skipping clustering (2.47M ≥ 200K threshold)
  12:50  Wrote 2,477,070 SpectralNodeMetric rows (308 communities, Q=0.99)
         ↑ community detection + factor write: ~10 min
  12:50  Skipping GraphML (2.47M ≥ 200K threshold)
  12:50  Step 5c complete

Peak worker RSS: ~27 GB (previously OOM-killed at 58–61 GB)
GPU memory: freed to 0 after lobpcg completion
Kernel OOM events: none
```

### 7.2 Serve Phase Verification

```sql
-- Factor rows exist (the "predictions" are cached)
SELECT count(*) FROM factor_spectral_node_metric
WHERE country_code='IE' AND snapshot_id='2025_12_31';
-- → 2,477,070

-- Fingerprint exists (carries eigenvalues for diffusion query)
SELECT region, eigenvalues, algebraic_connectivity, node_count
FROM semantic_search_graphspectralfingerprint
WHERE region='IE';
-- → 1 row, 64 eigenvalues, node_count=2477070

-- Communities exist (Louvain partition)
SELECT louvain_community, count(*)
FROM factor_spectral_node_metric
WHERE country_code='IE' AND snapshot_id='2025_12_31'
GROUP BY louvain_community
ORDER BY count(*) DESC
LIMIT 5;
-- → 308 communities, largest ~50K nodes
```

### 7.3 Before vs. After

| Metric | Before (OOM-killed) | After (batch-then-serve) |
|---|---|---|
| Graph representation | NetworkX dict-of-dicts (~15 GB) | SparseGraph COO (~1.5 GB) |
| Eigensolver | scipy eigsh (OOM / SuperLU failure) | GPU torch.lobpcg (8 min 20 sec) |
| Community detection | NetworkX Louvain (OOM at 61 GB) | networkit PLM (~10 min, ~2 GB) |
| GraphML serialization | nx.write_graphml (OOM at 58 GB) | Skipped (≥ 200K nodes) |
| Factor rows written | 0 (process killed) | 2,477,070 |
| Fingerprint stored | No | Yes (64 eigenvalues) |
| Peak RSS | 58-61 GB (OOM-killed) | ~27 GB |
| Runtime diffusion query | N/A (no factor rows) | One pgvector `<#>` query (ms) |

---

## 8. Country-Level vs Subdivision + Functional Maps

Step 5c routes between two modes based on the country's entity count
(`SUBDIVISION_NODE_THRESHOLD = 5_000_000`):

- **< 5M nodes** → country-level GPU LOBPCG (one global eigenbasis)
- **≥ 5M nodes** → subdivision-scoped GPU solves + functional-map transport
- **≥ 5M but no subgraphs** → country-level fallback with warning
- **Node count indeterminate** → legacy `has_subgraphs` routing

Both paths are sequential (one solve at a time) and automatic (routing is
based on a live `OsmEntity` count query at runtime). Both are non-fatal.

### 8.1 Current Country Routing

| Country | Entity count | Route |
|---|---|---|
| CV (Cabo Verde) | ~212K | Country-level GPU |
| BZ (Belize) | ~308K | Country-level GPU |
| JM (Jamaica) | ~1.1M | Country-level GPU |
| IE (Ireland) | ~9.7M | Subdivision + functional maps |
| CA (Canada) | ~53M | Subdivision + functional maps |

### 8.2 Artifact Comparison

| Artifact | Country-level (< 5M) | Subdivision + functional maps (≥ 5M) |
|---|---|---|
| Graph input | `build_sparse_graph` (country-wide), all entities in one graph | `build_sparse_graph_for_subgraph` (per subgraph + buffer zone) |
| Laplacian | One `L = I - D⁻¹ᐟ²WD⁻¹ᐟ²` (N × N) | N Laplacians (one per subgraph, buffered) |
| Solves | GPU LOBPCG, k=128 eigenvectors, one solve | GPU LOBPCG per subgraph (k=128, clamped to n//20), VRAM-checked → CPU eigsh fallback |
| Eigenbasis | Φ: N × 128, one global eigenbasis | Φ_A, Φ_B, ...: per-subgraph eigenbases |
| Phase B | — | Functional-map transport: for each adjacent subgraph pair, `C = argmin ‖F_B - F_A Cᵀ‖² + λ‖CΛ_A - Λ_B C‖²`; shared buffer-zone entities are the correspondences |
| `factor_spectral_node_metric` | One row per entity, `subgraph_slug=NULL` | One row per CORE entity per subgraph, `subgraph_slug="belize_district"` etc., eigen_loadings in the subgraph's LOCAL eigenbasis |
| Fingerprint | One row per country (`region="BZ"`), unique (region, snapshot) | One row per subgraph (`region="belize_district"`), unique (region, snapshot) |
| `factor_subgraph_transport` | ❌ NOT PRODUCED | One row per adjacent (directed) pair: `subgraph_from`, `subgraph_to`, `transport_matrix` (128×128 JSON), `k_dim`, `shared_entity_count`, `fit_residual`, `commutativity_residual`, unique (snapshot, country, from, to) |
| GraphML (debug) | Only if < 200K nodes, not read at runtime | Only if < 200K nodes per subgraph, not read at runtime |
| Runtime diffusion | `score(node) = Σ_k e^{-tλ_k} · φ_k(anchor) · φ_k(node)`, one pgvector `<#>` query over all entities (`subgraph_slug IS NULL`) | 1. Query the anchor's subgraph; 2. pgvector `<#>` in that subgraph; 3. load transport matrices to adjacents; 4. transport heat-kernel coefficients; 5. query adjacent subgraphs; 6. merge + re-rank |
| Eigenbasis comparability | Global, comparable | Local per subgraph, comparable via C matrices |

### 8.3 Key Differences Summary

| Aspect | Country-Level (< 5M) | Subdivision + Functional Maps (≥ 5M) |
|---|---|---|
| **Graphs** | 1 country-wide k-NN graph | N subgraph k-NN graphs (with buffer zones) |
| **Solves** | 1 GPU LOBPCG solve | N GPU LOBPCG solves (VRAM-checked, CPU fallback) |
| **Eigenbasis** | One global Φ (N × 128) | N local Φ_A, Φ_B, ... (each n_i × 128) |
| **Factor rows** | All entities, `subgraph_slug=NULL` | Core entities only, `subgraph_slug="district_name"` |
| **Fingerprints** | 1 row (region=country code) | N rows (region=subgraph slug) |
| **Transport matrices** | None | N×(adjacent pairs) rows in `factor_subgraph_transport` |
| **Runtime diffusion** | Single pgvector `<#>` query | Subgraph-scoped query + transport to adjacents |
| **Cross-region diffusion** | Natural (one eigenbasis) | Via C matrix multiplication |

The eigen-loadings in `factor_spectral_node_metric` are **not directly
comparable** between the two paths. Country-level loadings are in the global
eigenbasis; subdivision loadings are in each subgraph's local eigenbasis.
The transport matrices bridge this gap at runtime.

### 8.4 GPU Resource Management

Both paths use GPU LOBPCG with the following resource management:
- **VRAM pre-check** (`_check_gpu_vram`): estimates needed VRAM (sparse
  matrix + eigenvectors + 3× workspace + 2GB overhead) and compares against
  `torch.cuda.mem_get_info()`. Falls back to CPU `eigsh` if insufficient
  (20% safety margin).
- **Aggressive cleanup**: `torch.cuda.synchronize()` + `empty_cache()` +
  `gc.collect()` before and after each solve, and between subgraphs.
  `empty_cache()` is called twice (first frees cached blocks, second
  defrags).
- **`PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:128`**: limits the CUDA
  allocator split size to prevent fragmentation across many sequential
  solves. Set in the worker container environment.
- **CPU-side tensor cleanup**: conversion tensors (`indices`, `values`,
  `L_coo`) are `del`'d after moving to GPU.

### 8.5 BZ Subdivision Validation (2026-08-20)

Belize (308K entities, 5 districts) was run through the forced subdivision
path to validate the full pipeline end-to-end:

```
Run ID: 4c1d9520-62cf-4e3f-aac2-a07d7db60e9d
Total time: 138.3 seconds

Phase A — Subgraph solves (5/5 completed, 0 failures):
  belize_district:    27,025 nodes,  7,885 core rows, GPU LOBPCG, 16.8s
  cayo_district:      27,407 nodes, 13,423 core rows, GPU LOBPCG, 11.9s
  corozal_district:   small,         2,933 core rows, CPU eigsh,    0.3s
  orange_walk_district: small,       2,640 core rows, GPU LOBPCG,  11.4s
  stann_creek_district: small,       5,521 core rows, GPU LOBPCG,  11.0s

  Total factor rows written: 32,402 (core entities only)

Phase B — Transport matrices (10/10 adjacent pairs computed):
  belize → cayo:      shared=20,021  fit=0.2160  comm=35.13
  belize → corozal:   shared=7,270   fit=0.7928  comm=1074.97
  belize → orange_walk: shared=20,335 fit=0.3366  comm=109.75
  belize → stann_creek: shared=18,467 fit=0.3710  comm=2933.95
  cayo → corozal:     shared=1,585   fit=0.6523  comm=1066.43
  cayo → orange_walk: shared=18,016  fit=0.2774  comm=87.99
  cayo → stann_creek: shared=20,956  fit=0.3084  comm=29.83
  corozal → orange_walk: shared=7,536 fit=0.1513 comm=0.05
  corozal → stann_creek: shared=632  fit=0.1131  comm=12.24
  orange_walk → stann_creek: shared=12,597 fit=0.1613 comm=180.72

  Mean fit residual: 0.338
  Min shared count: 632
```

VRAM check logged `15.3 GB free, ~2.1 GB needed`. The pre-check is active
and passing. No OOM, no fragmentation issues.

---

## 9. Testing

**Source:** <ref_file file="backend/tests/unit/test_factor_node_tables.py" />

### 9.1 Factor-Node Tests (16 tests)

| Test | Coverage |
|---|---|
| test_write_spectral_nodes_match_eigenbasis | Eigenbasis mapping (node_order) |
| test_spectral_node_structural_fields | Degree, clustering, component |
| test_spectral_node_idempotent | Re-run doesn't duplicate rows |
| test_dirichlet_contrib_combinatorial_laplacian | Matches GraphSignalService |
| test_drift_sign_aligned | Eigenvector sign alignment |
| test_drift_changed_detection | community_changed + degree_delta |
| test_factor_availability | G4 check_availability |
| test_factor_metric_resolution | resolve_metrics |
| test_community_summary | community_summary aggregation |
| test_diffusion_rank_pgvector | Heat kernel <#> vs numpy ref |
| test_diffusion_rank_candidate_filter | Positive-score threshold |
| test_diffusion_rank_missing_anchor | Returns None when anchor absent |
| test_amenity_embedding_lookup | amenity_embedding retrieval |
| test_compute_amenity_embeddings_command | Command writes 621 rows |
| test_executor_event_diffusion_table_path | EVENT-DIFFUSION with flag on |
| test_executor_event_diffusion_flag_off_unchanged | Flag off → graph path unchanged |

### 9.2 SparseGraph + Step 5c Tests (15 tests)

**Source:** <ref_file file="backend/tests/unit/test_sparse_graph.py" />

| Test | Coverage |
|---|---|
| test_from_nx_to_nx_round_trip | COO ↔ NetworkX preservation |
| test_degrees_match_networkx | Degree computation equivalence |
| test_normalized_laplacian_matches_networkx | Normalized L equivalence |
| test_combinatorial_laplacian_matches_networkx | Combinatorial L equivalence |
| test_components_match_networkx | Connected components equivalence |
| test_max_weight_dedup | Parallel k-NN edges → max weight |
| test_spectral_features_match_networkx_path | Spectral on SparseGraph == nx |
| test_signal_smoothness_matches_networkx_path | Dirichlet energy equivalence |
| test_class_signal_matches_networkx_path | Class signal equivalence |
| test_community_detection_sparse_matches_nx | networkit == NetworkX Louvain |
| test_empty_sparse_graph | Empty graph → empty results |
| test_serialize_graph_skipped_for_large_graph | GraphML skip guard |
| test_serialize_graph_written_for_small_graph | Small graphs still serialized |
| test_serialize_graph_threshold_boundary | Boundary case (== threshold) |
| test_serialize_graph_no_graph_artifact_dir | Missing GRAPH_ARTIFACT_DIR |

```bash
# All tests (40+ across spectral, sparse graph, community, factor, step 5c)
docker compose exec backend python -m pytest \
    tests/unit/test_sparse_graph.py \
    tests/unit/test_spectral_analysis_service.py \
    tests/unit/test_step_5c_graph_spectral.py \
    tests/unit/test_community_detection_service.py \
    tests/unit/test_factor_node_tables.py \
    -v --reuse-db
```

---

## 10. Relationship to the Spatial-Agent Paper

The batch-then-serve pattern maps directly onto the paper's factorization
step (§3.3). The paper's factor nodes are transient computation artifacts;
this implementation **persists** them as queryable table rows.

| Paper concept | Implementation (batch-then-serve) |
|---|---|
| Factorization (concept graph → factor graph) | Batch writers materialize factor nodes as table rows (the "predictions") |
| Factor nodes (supplementary parameters) | `SpectralNodeMetric`, `DriftNodeMetric`, `AmenityEmbedding` |
| G4 data availability | `check_availability()`, `SELECT EXISTS` (was the factor produced by batch?) |
| SUPPORT resolution | `diffusion_rank()`, pgvector `<#>` (serve the precomputed diffusion) |
| COND resolution | `community_summary()` + amenity lookup |
| Grounded execution (Σ state, F trace) | Executor trace + `factor_join` steps (Algorithm 1's F) |
| GeoFlow constraints (G1-G5) | Parser validates G2; G1/G3/G5 by construction; G4 via `check_availability` |

---

## 11. Invariants

- **Batch computes, runtime serves.** No NetworkX, SciPy, sklearn, or GPU
  at request time. All expensive computation is in Step 5c/5d (batch).
- Factor tables live on the `vectors` DB (colocated with `OsmEntity`).
- `eigen_loadings` is always 128-dimensional (zero-padded for K<128).
- Eigenvectors are snapshot-relative. Never mix eigenbases across snapshots.
- Drift requires sign alignment before comparison.
- `osm_type` is not stored (k-NN graph keyspace is `osm_id` only).
- `dirichlet_contrib` uses the combinatorial Laplacian (matching
  `GraphSignalService`).
- `FACTOR_NODE_TABLES_ENABLED` defaults `true`. The table path is
  authoritative.
- Factor rows must be generated by running Step 5c/5d. Existing snapshots
  may have no rows until recomputed.
- GraphML is skipped for graphs ≥ 200K nodes (debug artifact only).
- `clustering_coeff` is NULL for graphs ≥ 200K nodes.
- Physical partitioning is deferred; composite indexes are used now.
- Test DBs need `vector` + `postgis` extensions in both databases.
