# Graph Spectral Analysis (Step 5c) + Temporal Drift (Step 5d)

> **Focus:** how the pipeline computes Laplacian spectral features on the
> k-NN graph (Step 5c), uses WorldKG classes as graph signals to measure
> semantic clustering, detects temporal drift between snapshots (Step 5d),
> and forecasts eigenvalues with ARIMA. Also covers embedding drift via
> Sliced Wasserstein Distance.
>
> **Key idea:** the k-NN graph built in Step 5 is not just for DeepWalk. It
> is a **graph substrate** for spectral analysis. The Laplacian eigenvalues
> capture global structure (connectivity, bottlenecks, community structure).
> WorldKG classes encoded as node signals measure whether semantic classes
> cluster spatially (Dirichlet energy). Comparing eigenvalues across
> snapshots detects structural drift without re-reading the full graph.

---

## 1. Where Steps 5c/5d Sit in the Pipeline

**Step 5: Train GV-NLE** (DeepWalk on the k-NN graph) → 100D spatial
embeddings per entity.

**Step 5b: Finalize subgraph NLE** → `compute_static_embeddings` (400D
fused), `create_static_embedding_hnsw_index`.

**Step 5c: Graph spectral analysis** (this doc):
1. Build k-NN graph (SparseGraph COO for large graphs, NetworkX for small)
2. Route: < 5M nodes → country-level GPU LOBPCG; ≥ 5M → subdivision-scoped
   solves
3. Compute top-k Laplacian eigenvalues + Fiedler vector (GPU
   `torch.lobpcg`, CPU `eigsh` fallback)
4. Encode WorldKG classes as graph signals
5. Compute Dirichlet energy (signal smoothness)
6. Louvain community detection (networkit PLM for large graphs)
7. Write per-entity `factor_spectral_node_metric` rows (`subgraph_slug` for
   subdivision, stamped with the fingerprint's UUID — hardening Phase 1
   eigenbasis coherence)
8. Phase B (subdivision only): compute functional-map transport matrices
9. Store `GraphSpectralFingerprint` (one per country or one per subgraph)

**Step 5d: Temporal drift** (this doc):
1. Load current + previous `GraphSpectralFingerprint`
2. Compute spectral distance, Fiedler drift, smoothness delta
3. Classify drift magnitude (low/medium/high/extreme)
4. ARIMA forecast of next-snapshot eigenvalues (if ≥3 snapshots)
5. CUSUM change-point detection
6. Store `GraphSpectralDrift`

**Step 6: Mark search-ready** → create materialized view + HNSW index.

Both steps are **non-fatal** (failures are logged, the pipeline continues).
Step 5d is **conditional**, only runs when ≥2 snapshots exist.

### 1.1 Conceptual Grounding: Kuhn's Core Concepts

The graph spectral analysis and temporal drift operations are where the
pipeline uses the WorldKG Project's artifacts to deliver the geospatial
reasoning capabilities described in Kuhn's core concepts. The spectral
eigenbasis, community structure, and heat kernel diffusion provide the
computational substrate for Kuhn's **Network** concept (connectivity,
diffusion spread), **Event** concept (change detection via temporal drift),
and **Field** concept (continuous attribute propagation via the Laplacian).
These are materialized as factor-node tables that the GUI queries at
request time through the MapQA templating system.

For large countries (≥ 5M nodes), the spectral solve is split across
subdivisions, with functional-map transport matrices bridging adjacent
subgraph eigenbases. This keeps each solve small enough to fit on a single
GPU while preserving cross-subgraph diffusion at runtime. The routing is
automatic: small countries get one global solve, large countries get
subdivision-scoped solves with transport.

---

## 2. Step 5c: Graph Spectral Analysis

### 2.1 The Graph Substrate

**Source:** <ref_file file="backend/semantic_search/services/knn_graph_service.py" />

Two graph builders, selected by Step 5c routing:

- `KNNGraphService.build_graph()`, a weighted `networkx.Graph` for small
  countries (< 5M nodes). Loads OSM entities from the vectors DB (filtered
  by `country_code` + `snapshot_id`), builds k-NN edges using haversine +
  log-damped IDW weights, populates `wkg_class` node attributes.
- `KNNGraphService.build_sparse_graph()`, a PyG-style COO `SparseGraph`
  (`node_ids` / `edge_index` / `edge_weight`) for large graphs (~1.5 GB for
  IE's 2.47M nodes vs ~15 GB NetworkX). Used by the country-level GPU LOBPCG
  path and the subdivision path.
- `KNNGraphService.build_sparse_graph_for_subgraph()`, filters entities by
  subgraph polygon/bbox with a buffer zone for correct boundary k-NN
  behavior. With `return_core_ids=True` (Option A), returns
  `(SparseGraph, core_ids)`: the graph includes core + buffer entities, but
  factor rows are written for core entities only. Shared buffer entities
  get exact eigen-loadings for transport matrix computation.

### 2.2 Laplacian Spectral Features

**Source:** <ref_file file="backend/semantic_search/services/spectral_analysis_service.py" />

```python
def compute_spectral_features(self, G, k: int = 128) -> dict:
```

**Routing** (automatic, based on live `OsmEntity` count,
`SUBDIVISION_NODE_THRESHOLD = 5_000_000`):
- **< 5M nodes**: country-level GPU LOBPCG, one global eigenbasis, one solve
- **≥ 5M nodes with subgraphs**: subdivision-scoped GPU solves +
  functional-map transport matrices (Phase B). Each subgraph solved
  independently.
- **≥ 5M nodes without subgraphs**: country-level fallback with warning

**Solver selection** (per solve):
1. GPU `torch.lobpcg` when CUDA is available and VRAM is sufficient
   (`_check_gpu_vram` estimates needed VRAM with a 20% safety margin)
2. CPU `scipy.sparse.linalg.eigsh(which='SM')` fallback when no CUDA or
   insufficient VRAM
3. CPU shift-invert (`_shift_invert_solve`) for the ≥5M country-level
   fallback

**Methodology:**
1. Compute normalized Laplacian: `L = I - D^{-1/2} W D^{-1/2}`
2. Eigendecomposition (GPU LOBPCG or CPU eigsh)
3. Drop the trivial λ₀=0 eigenvalue
4. Sort eigenvalues ascending
5. Extract the Fiedler vector (2nd eigenvector)

**GPU resource management:**
- VRAM pre-check before each solve (`_check_gpu_vram`)
- Aggressive cleanup between subgraphs: `torch.cuda.synchronize()` +
  `empty_cache()` × 2 + `gc.collect()`
- `PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:128` in the worker env
- Sequential solves only (no parallel GPU batching)

**What it produces:**
- `eigenvalues`: top-k non-trivial eigenvalues
- `fiedler_vector`: 2nd eigenvector (truncated to 10k samples for storage)
- `algebraic_connectivity`: λ₂ (Fiedler value, 0 if disconnected)
- `spectral_gap`: λₖ - λ₂
- `node_count`, `edge_count`

### 2.3 Graph Signals: WorldKG Classes as Signals

**Source:** <ref_file file="backend/semantic_search/services/graph_signal_service.py" />

WorldKG classes are encoded as **graph signals**, one value per node:

```
Node: OSM entity (restaurant, school, park, ...)
Signal: wkg_class index (0, 1, 2, ..., C-1)

Class signal:     s[i] = class_index(entity_i)
One-hot signal:   S[i, c] = 1 if entity_i has class c, else 0
```

**Dirichlet energy** measures how well classes cluster spatially:

```
E(s) = sᵀ L s = Σ_{(i,j)∈E} w_{ij} (s_i - s_j)²
```

- **Low energy** → classes cluster spatially (nearby entities have the same
  class)
- **High energy** → classes are mixed (nearby entities have different
  classes)

This is the **semantic-structural coupling** metric. It tells whether the
spatial graph structure aligns with the semantic class structure.

### 2.4 Heat Kernel Diffusion

**Source:** `spectral_analysis_service.py` (`compute_heat_kernel`)

```python
def compute_heat_kernel(self, G, source_node, t_values) -> dict:
```

Computes heat kernel diffusion: `u(t) = e^{-tL} · δ_source`

- Uses `scipy.sparse.linalg.expm_multiply` for the sparse matrix exponential
- Models how "information" diffuses from a source entity through the graph
- Used by the EVENT-DIFFUSION MapQA template ("how does an event at X
  spread?")
- Mass-conserving: `Σᵢ u(t)ᵢ = 1` for all t

### 2.5 Community Detection

**Source:** <ref_file file="backend/semantic_search/services/community_detection_service.py" />

**networkit PLM** (parallel C++) for `SparseGraph` when networkit is
installed (`networkit==10.1`); NetworkX Louvain fallback for small
`networkx.Graph` inputs. NetworkX Louvain was OOM-killed at 61 GB RSS on IE
(2.47M nodes). Do not use it for large graphs.

- **Louvain method**, greedy modularity optimization
- Reveals local cluster structure (complementary to spectral's global view)
- Optional class filtering: `filter_communities_by_class()` returns only
  communities where a target class is dominant
- Produces: `communities`, `modularity` (Q score), `node_to_community`
  mapping

### 2.6 The GraphSpectralFingerprint Model

**Source:** `backend/semantic_search/models.py`

```python
class GraphSpectralFingerprint(models.Model):
    id = UUIDField(primary_key=True)
    region = CharField(max_length=10)           # ISO country code
    snapshot = FK to osmsnapshot.Snapshot
    eigenvalues = JSONField()                    # top-k non-trivial eigenvalues
    fiedler_vector = JSONField()                 # truncated to 10k samples
    algebraic_connectivity = FloatField()        # λ₂
    spectral_gap = FloatField()                  # λₖ - λ₂
    signal_smoothness = FloatField()             # Dirichlet energy sᵀLs
    node_count = IntegerField()
    edge_count = IntegerField()
    k_eigenvalues = IntegerField(default=128)
    created_at = DateTimeField(auto_now_add=True)
    unique_together = (region, snapshot)
```

One row per region+snapshot (country-level path), or one row per
subgraph+snapshot (subdivision path). The unique constraint ensures
idempotency: re-running Step 5c for the same snapshot does not create
duplicates.

---

## 3. Step 5d: Temporal Drift

### 3.1 Spectral Drift Metrics

**Source:** <ref_file file="backend/semantic_search/services/spectral_drift_service.py" />

Comparing `GraphSpectralFingerprint` from two snapshots:

| Metric | Formula | Meaning |
|---|---|---|
| `spectral_distance` | `‖λ_t - λ_{t-1}‖₂` | L2 norm of eigenvalue difference |
| `connectivity_delta` | `Δλ₂` | Shift in algebraic connectivity |
| `spectral_gap_delta` | `Δ(λₖ - λ₂)` | Shift in spectral gap |
| `fiedler_drift` | `cosine_distance(v_t, v_{t-1})` | Fiedler vector change |
| `smoothness_delta` | `Δ(sᵀLs)` | Dirichlet energy shift |

Shorter eigenvalue vectors are zero-padded for comparison.

### 3.2 Drift Classification

```python
def classify_drift_magnitude(self, spectral_distance: float) -> str:
    if spectral_distance < 0.1: return "low"
    if spectral_distance < 0.5: return "medium"
    if spectral_distance < 1.0: return "high"
    return "extreme"
```

### 3.3 ARIMA Forecasting

**Source:** <ref_file file="backend/semantic_search/services/spectral_forecast_service.py" />

```python
def forecast_eigenvalues(self, eigenvalue_series) -> (forecast, confidence):
```

- **ARIMA(1,1,1)** per eigenvalue column when ≥3 snapshots and statsmodels
  are available
- **Exponential smoothing** fallback (α=0.3, most recent gets highest
  weight)
- Forecasts clamped to non-negative (eigenvalues of a PSD matrix)
- Confidence: 1.96σ half-widths per eigenvalue

### 3.4 CUSUM Change-Point Detection

```python
def detect_changepoints(self, spectral_distance_series) -> dict:
```

- Cumulative sum method on the spectral distance time series
- Returns changepoint index and magnitude
- Flags structural changes (e.g. a new subdivision added, a border change)

### 3.5 The GraphSpectralDrift Model

```python
class GraphSpectralDrift(models.Model):
    id = UUIDField(primary_key=True)
    region = CharField(max_length=10)
    snapshot_from = FK to osmsnapshot.Snapshot
    snapshot_to = FK to osmsnapshot.Snapshot
    spectral_distance = FloatField()
    connectivity_delta = FloatField()
    spectral_gap_delta = FloatField()
    fiedler_drift = FloatField()
    smoothness_delta = FloatField()
    drift_magnitude = CharField()                # low/medium/high/extreme
    forecast_eigenvalues = JSONField(null=True)  # ARIMA forecast
    forecast_confidence = JSONField(null=True)   # 1.96σ intervals
    changepoint_detected = BooleanField()
    created_at = DateTimeField(auto_now_add=True)
    unique_together = (region, snapshot_from, snapshot_to)
```

---

## 4. Embedding Drift (Management Command)

**Source:** <ref_file file="backend/semantic_search/services/embedding_drift_service.py" />

Unlike spectral drift (which compares graph structure), embedding drift
compares the **distribution of embeddings** between snapshots.

### 4.1 Sliced Wasserstein Distance (SWD)

1. Generate R random projection vectors on the unit sphere
2. Project high-dimensional embeddings to 1D: `p_r = v_r · x`
3. Compute 1D Wasserstein distance (Earth Mover's Distance) per projection
4. Average across all R projections

- Default: 100 projections, seed=42 for reproducibility
- Approximates the true Wasserstein distance in high dimensions
- Computed separately for semantic (300D GV-Tags) and spatial (100D GV-NLE)

### 4.2 Freshness Score

```
F = α · exp(-λ_sem · W_sem) + (1-α) · exp(-λ_spat · W_spat)
```

- `W_sem`, `W_spat`: Sliced Wasserstein Distance for semantic/spatial
- `α = 0.5` (equal weight to both axes)
- `λ_sem = λ_spat = 5.0` (decay rate)
- Range: [0, 1] where 1 = identical (no drift)

### 4.3 Subdivision-Level Drift

`compute_country_drift()` computes drift at both:
- **Country level**: all entities in the country
- **Subdivision level**: per `SubgraphProfile` bbox

This reveals **where** drift is concentrated, e.g. a new development in one
subdivision may cause high drift there while the rest of the country is
stable.

### 4.4 Command Interface

```bash
python manage.py compute_embedding_drift \
    --country BZ \
    --snapshot-from 2025_12_31_baseline \
    --snapshot-to 2025_12_31 \
    --num-projections 100 \
    --mock-missing  # generate synthetic embeddings if missing
```

---

## 5. New MapQA Templates (4 new, 9 total)

**Source:** <ref_file file="backend/worldkg_nca/views/graph.py" />

Four new MapQA templates extend the parser/executor:

| # | Template | Endpoint | What it does |
|---|---|---|---|
| 11 | SPECTRAL-ANALYSIS | `/api/nca/spectral-query/` | Returns eigenvalues, λ₂, spectral gap, signal smoothness for a region |
| 12 | TEMPORAL-DRIFT | `/api/nca/temporal-query/` | Returns spectral drift between snapshots |
| 13 | COMMUNITY-DETECT | `/api/nca/community-query/` | Louvain communities + optional class filtering |
| 14 | EVENT-DIFFUSION | `/api/nca/event-diffusion-query/` | Heat kernel diffusion from a source entity |

The executor uses the **factor-table path** (SQL + pgvector `<#>`) via
`FactorResolutionService`, with no NetworkX/SciPy graph loading at request
time. The legacy runtime graph path (GraphML → NetworkX → SciPy) has been
removed. See `05_Factor_Node_Runtime_Joins.md` for the factor-table
architecture and cross-subgraph transport via functional maps.

---

## 6. Canvas Integration

**Source:** `backend/pipeline/canvas.py`

Steps 5c/5d are wired into the canvas after Step 5/5b and before Step 6:

```python
# Step 5c (graph & spectral analysis) + Step 5d (temporal drift) run
# after Step 5/5b and before Step 6.  Both are non-fatal / conditional —
# their task bodies log warnings and no-op when prerequisites are missing
```

- Step index 5.7 → `step_5c_graph_spectral_analysis`
- Step index 5.8 → `step_5d_temporal_drift`
- Both added to `_get_step_tasks()`, `_run_eager()`, and `_run_async()`
- Both non-fatal: failures logged, pipeline continues to Step 6

---

## 7. Testing

49 unit tests across 6 test files (no DB needed):

| Test File | Tests | Coverage |
|---|---|---|
| `test_spectral_analysis_service.py` | 14 | Eigenvalue properties, analytical solutions, heat kernel |
| `test_spectral_drift_service.py` | 9 | Drift metrics, symmetry, padding, classification |
| `test_spectral_forecast_service.py` | 8 | ARIMA, exp smoothing, confidence, CUSUM |
| `test_graph_signal_service.py` | 8 | Dirichlet energy, class signals, diffusion |
| `test_community_detection_service.py` | 7 | Partition, modularity, class filtering |
| `test_embedding_drift_service.py` | 3 | SWD properties, freshness score |

```bash
# All spectral + drift tests
docker compose exec backend python -m pytest \
    tests/unit/test_spectral_analysis_service.py \
    tests/unit/test_spectral_drift_service.py \
    tests/unit/test_spectral_forecast_service.py \
    tests/unit/test_graph_signal_service.py \
    tests/unit/test_community_detection_service.py \
    tests/unit/test_embedding_drift_service.py \
    -v --reuse-db
```

---

## 8. Invariants

- Steps 5c/5d run after Step 5/5b and before Step 6.
- Both are non-fatal. Failures are logged, the pipeline continues.
- Step 5d is conditional, only runs when ≥2 snapshots exist.
- Normalized Laplacian eigenvalues ∈ [0, 2].
- Spectral distance is non-negative, symmetric, zero for identical
  snapshots.
- Forecast eigenvalues are non-negative (clamped, PSD property).
- Dirichlet energy is non-negative, zero for a constant signal.
- SWD is non-negative, symmetric, zero for identical distributions.
- Freshness score ∈ [0, 1] where 1 = identical.
- `GraphSpectralFingerprint` unique on (region, snapshot).
- `GraphSpectralDrift` unique on (region, snapshot_from, snapshot_to).
- The k-NN graph for spectral analysis uses the same haversine + IDW
  weights as DeepWalk. Same graph substrate, different analysis.
- **Step 5c routing**: < 5M nodes → country-level; ≥ 5M → subdivision +
  functional maps. Threshold: `SUBDIVISION_NODE_THRESHOLD = 5_000_000`.
- **Country-level factor rows**: `subgraph_slug=NULL`. **Subdivision factor
  rows**: `subgraph_slug` set, core entities only.
- **Eigen-loadings from different subgraph bases are not directly
  comparable** without transport matrices (`factor_subgraph_transport`).
- **Runtime does not load NetworkX/SciPy graphs**. The factor-table path is
  authoritative (`FACTOR_NODE_TABLES_ENABLED` defaults `true`).
- **GPU solves are sequential**, no parallel GPU batching. VRAM pre-check
  gates GPU selection; CPU fallback when insufficient.
