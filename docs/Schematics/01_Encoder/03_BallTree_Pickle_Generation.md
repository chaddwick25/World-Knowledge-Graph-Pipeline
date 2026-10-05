# BallTree Pickle Generation: PostGIS GiST → k-NN Graph → DeepWalk

> **Focus:** how the `wdw.pickle` (DeepWalk model) is generated from
> GeoVectors TSVs, how the BallTree is used in-memory for k-NN graph
> construction and inductive encoding, and how the PostGIS GiST index
> provides the spatial backbone for both.
>
> **Key idea:** there are two spatial indexing layers. PostGIS GiST
> (persistent, database-level) and sklearn BallTree (ephemeral, in-memory).
> The pickle (`wdw.pickle`) stores the DeepWalk model embeddings, not the
> BallTree itself. The BallTree is rebuilt in-memory from PostGIS-loaded
> entities each time it's needed.

---

## 1. The Two Spatial Indexing Layers

**Layer 1: Postgres GiST (database level, persistent)**
- Index: `idx_..._geom ON geom USING gist`
- What: R-tree over the geometry column (PostGIS)
- Where: lives on the leaf partition (`embeddings_2025_12_31_cu`)
- Purpose:
  - Bounding box queries ("entities within Cuba's bbox"):
    `geom && ST_MakeEnvelope(...)`
  - KNN queries ("50 nearest entities to this point"):
    `ORDER BY geom <-> ST_MakePoint(lon, lat) LIMIT 50`, used by the NLE
    encoder (`encode_coords`) and inductive encoding
- Speed: O(log N) for bbox, O(log N + k) for KNN

**Layer 2: scikit-learn BallTree (in-memory, ephemeral)**
- `NearestNeighbors(metric='haversine', algorithm='ball_tree')`
- What: ball tree over radian coordinates
- Where: in Python memory
- Purpose:
  - Build the k-NN graph for DeepWalk training (Step 5),
    `KNNGraphService.build_knn_graph()`
  - Inductive encoding of unseen entities,
    `InductiveSpatialService.embed_entity()`
- Speed: O(N log N) build, O(log N) per query
- Scale: loaded per-country/subgraph (e.g. 190K for CU)

**Why both?** The BallTree is the right tool for batch k-NN graph
construction (N×k queries in-memory, haversine distance, parallelizable via
`n_jobs=-1`). The GiST index is the right tool for loading entities by
geographic region (bbox/polygon filter) and for runtime spatial queries
(find entities near a point without loading everything into memory).

---

## 2. The wdw.pickle: What It Is and What It Contains

The `wdw.pickle` is **not** a BallTree. It is a `WDWStore`, a dict-like
container mapping `{osm_type}_{osm_id}` → embedding vector, produced by the
DeepWalk (Word2Vec Skip-gram) training in Step 5. It is the pre-trained NLE
model that `NLEModel.load_indexes()` loads for GV-NLE inference (the
two-axis Step-1 encoding path that consumed it was removed 2026-09-10 — see
`docs/issues/TICKET_REMOVE_DUAL_ENCODER.md`).

### 2.1 Pickle Generation from TSVs

**Source:** <ref_file file="backend/geovectors_encoder/services/geovectors_service.py" />

`GeoVectorsEncoderService.generate_subgraph_pickle()` (line 189) generates
the pickle from the pre-trained GeoVectors TSV files:

<ref_snippet file="backend/geovectors_encoder/services/geovectors_service.py" lines="264-309" />

```
GeoVectors TSV (e.g. cuba-location.tsv.gz)
  ↓  gzip.open(tsv_path, 'rt')
  ↓  for each line: parse osm_type_code, osm_id, vector
  ↓  map 'n'/'w'/'r' → 'node'/'way'/'relation'
  ↓  key = f"{osm_type}_{osm_id}"
  ↓  wdw_store[key] = vector
  ↓
WDWStore (in-memory dict: {key → 100D vector})
  ↓
Persist:
  - wdw.pickle          (legacy pickle format, backward compat)
  - wdw_embeddings.npy  (NumPy array, optimized loading)
  - wdw_keys.json       (JSON key index, optimized loading)
```

The pickle is stored at:
`{OSM_WIKIDATA_EXTRACTIONS_DIR}/{continent}/{country}/pickle/wdw.pickle`

> **`NLEModel.train()` removed 2026-09-10:** both the reference
> (`GeoVectors-master/NLEModel.py`) and the in-tree
> `backend/geovectors_encoder/core/models/nle.py` used to contain an early
> `return None` inside `train()`, which made the sliding-window + Node2Vec
> training block unreachable dead code — a known upstream bug faithfully
> reproduced in-tree. `train()` only populated the PostGIS table and never
> trained a Node2Vec model; it had **no callers**, so it was removed
> entirely with the two-axis Step-1 path
> (`docs/issues/TICKET_REMOVE_DUAL_ENCODER.md`). The WDW pickle is generated
> by `GeoVectorsEncoderService.generate_subgraph_pickle()`, not by
> `train()`.

### 2.2 NLEModel.load_indexes(): Reloading the Pickle

**Source:** <ref_file file="backend/geovectors_encoder/core/models/nle.py" />

<ref_snippet file="backend/geovectors_encoder/core/models/nle.py" lines="259-270" />

`load_indexes()` loads the `wdw.pickle` into `self.wdw` (the `WDWStore`).
This is the DeepWalk model. Given an OSM key, `self.wdw.predict(key)`
returns the 100D GV-NLE embedding for that entity.

### 2.3 The NLE Encoder Uses PostGIS GiST (Not BallTree) for KNN

When encoding a new entity, `NLEModel.encode_coords()` (line 189) queries
PostGIS for the 50 nearest neighbors using the GiST KNN operator `<->`:

<ref_snippet file="backend/geovectors_encoder/core/models/nle.py" lines="189-242" />

```sql
SELECT osm_key,
       public.st_distance(location::public.geography, point::public.geography)
FROM   {table_name}
WHERE  public.st_distance(location::public.geography, point::public.geography) > 0
ORDER BY location OPERATOR(public.<->) point
LIMIT 50;
```

Then applies the IDW damped weight formula:
```python
dist = np.log(1 + (1 / (n[1] / 1000)))  # n[1] is distance in meters → km
node_enc = self.wdw.predict(other_key)
vectors.append(node_enc * dist)
enc = np.sum(vectors, axis=0) / dist_sum
```

This is the **original GeoVectors encoding formula** (paper Section 3.4):
weighted average of the k=50 nearest entities' DeepWalk embeddings, with
log-inverse distance weighting.

---

## 3. BallTree for k-NN Graph Construction (Step 5 Training)

**Source:** <ref_file file="backend/semantic_search/services/knn_graph_service.py" />

`KNNGraphService.build_knn_graph()` builds the k-NN graph that feeds
DeepWalk training. It uses `NearestNeighbors(ball_tree, haversine)`
in-memory:

<ref_snippet file="backend/semantic_search/services/knn_graph_service.py" lines="162-190" />

```
1. Load entities from PostGIS (GiST index speeds up bbox filter)
   OsmEntity.objects.filter(geom__isnull=False,
                            geom__within=country_polygon)
       ← GiST index prunes to country's bbox

2. Build BallTree in memory (scikit-learn)
   coords_rad = np.radians([[lat, lon] for e in entities])
   nn = NearestNeighbors(n_neighbors=51, metric='haversine',
                         n_jobs=-1, algorithm='ball_tree')
   nn.fit(coords_rad)

3. For each entity → kneighbors() → 50 nearest + distances
   distances_rad, indices = nn.kneighbors(coords_rad)
   distances_km = distances_rad * 6371.0

4. Edge weight: w'(d) = max(1/ln(max(d, 1.1)), e)
   (IDW damped weight — see §4 below)

5. Weighted DeepWalk → 100D GV-NLE embeddings
   Stored in OsmEntity.gv_nle_embedding (vector(100))
```

The BallTree is **not persisted**. It is rebuilt in-memory each time
`build_knn_graph()` or `train_gv_nle` runs. The GiST index makes the DB
load fast; the BallTree makes the N×k neighbor query fast.

---

## 4. The IDW Damped Weight Formula

The IDW (Inverse Distance Weighting) formula appears in **two distinct
variants** that serve different mathematical roles and are **not
interchangeable**.

### 4.1 Training formula: graph construction (DeepWalk transition matrix)

Used to assign edge weights on the k-NN graph that feeds DeepWalk training.
The `max(..., e)` floor is essential: no edge has zero weight, so the graph
stays fully connected and random walks never get trapped.

```python
# WeightedDeepWalkGraph._damp_and_row_norm (Numba JIT path)
# Also: knn_graph_service.calculate_edge_weight, inductive_spatial_service._damped_weight
weights = np.maximum(weights, 1.1)          # clamp minimum distance to 1.1 km
weights = np.maximum(1.0 / np.log(weights), np.e)  # w' = max(1/ln(d), e)
```

| Location | File | Purpose |
|---|---|---|
| Training (Numba JIT) | `geovectors_encoder/core/graph.py:26-33` | DeepWalk GV-NLE training |
| k-NN graph construction | `semantic_search/services/knn_graph_service.py:117` (`build_knn_graph`) | Building k-NN graph edges |
| DeepWalk service (damping) | `semantic_search/services/deepwalk_service.py:169-222` | Only for raw-distance graphs (see §4.3) |
| Inductive inference | `semantic_search/services/inductive_spatial_service.py:110-113` | Provisional GV-NLE for new entities |

**What the formula means:**
- `d` = haversine distance in kilometers between two OSM entities
- Clamp to 1.1: prevents `ln(1) = 0` (division by zero); entities closer
  than 1.1 km all receive the same maximum weight.
- `ln(d)`: logarithmic damping, weight drops off logarithmically with
  distance.
- `max(..., e)`: floor at e ≈ 2.718. Even the farthest entities retain a
  non-zero weight, preserving graph connectivity for the stochastic
  transition matrix.
- **Row normalization**: after damping, each node's outgoing edge weights
  sum to 1.0, making the transition matrix stochastic for random walk
  sampling.

### 4.2 Inference formula: NLE encoding (encode_coords)

Used **only** in `NLEModel.encode_coords()` to weight the 50 nearest
pre-trained neighbor embeddings when encoding a new entity's location. This
formula is from GeoVectors paper Section 3.4 (the NLE encoding step):

```python
# NLEModel.encode_coords() — PostGIS 50-NN inference path
# n[1] is st_distance(..., geography) result = distance in meters → divide by 1000 → km
dist = np.log(1 + (1 / (n[1] / 1000)))     # w = ln(1 + 1/d_km)
enc = np.sum(vectors, axis=0) / dist_sum   # normalized weighted mean
```

This formula does **not** need a floor because the weighted mean is
normalized by `dist_sum`; any constant scale factor cancels out. It is
monotonically decreasing with distance and assigns higher weight to closer
neighbors, but it does not enforce graph connectivity (there is no graph; it
is a direct weighted aggregation).

**The two formulas are not interchangeable:**

| Property | Training formula `max(1/ln(max(d,1.1)), e)` | Inference formula `ln(1+1/d)` |
|---|---|---|
| Paper section | §3.2 (GV-NLE graph construction) | §3.4 (NLE encoding) |
| Used in | `_damp_and_row_norm`, `calculate_edge_weight`, `_damped_weight` | `NLEModel.encode_coords()` |
| Floor at `e` | Yes, essential for graph connectivity | No, normalized mean removes scale |
| Returns | Edge weight for stochastic transition matrix | Unnormalized weight for vector aggregation |
| Value at d=1 km | `max(1/ln(1.1), e)` ≈ e | `ln(1+1)` ≈ 0.693 |
| Value at d=5 km | `max(1/ln(5), e)` ≈ e | `ln(1+0.2)` ≈ 0.182 |

### 4.3 In-tree inductive path: deliberate design choice

`InductiveSpatialService._damped_weight()` uses the **training formula**
(§3.2) rather than the paper's §3.4 inference formula. This is a deliberate
in-tree design choice, not an error: the `max(..., e)` floor preserves the
same weight-range distribution as the DeepWalk-trained embedding space. The
normalized weighted mean cancels the scale difference from the floor. See
the `inductive_spatial_service.py` module docstring for the full rationale.

### 4.4 Double-damping guard

`KNNGraphService.calculate_edge_weight()` outputs **pre-damped weights** in
the range [e, ~10.5]. If these are passed to
`WeightedDeepWalkService.apply_damped_weights()`, the formula
`max(1/ln(w+1e-10), e)` maps every weight to `max(≤1, e) = e`, collapsing
all distance information to a constant.

The production `train_gv_nle` management command correctly sets
`apply_damping=False` to avoid this. `WeightedDeepWalkService` now defaults
to `apply_damping=False`, and `apply_damped_weights()` raises a `ValueError`
if called with pre-damped input.

---

## 5. The Full Pickle Generation Flow (Step 5)

**Step 5: GV-NLE training**

1. **Load entities from PostGIS.** `OsmEntity.objects.using('vectors')`
   `.filter(country_code=cfg.iso, snapshot_id=cfg.snapshot_date,
   geom__isnull=False)`. GiST index prunes to the country bbox; partition
   pruning routes to the leaf partition.
2. **Build BallTree in-memory.** `coords_rad = np.radians([[lat, lon] for e
   in entities])`; `NearestNeighbors(n_neighbors=51, metric='haversine',
   algorithm='ball_tree', n_jobs=-1).fit(coords_rad)`.
3. **k-NN graph + IDW damped weights.** `distances, indices =
   nn.kneighbors(coords_rad)`; per edge `w' = max(1/ln(max(d_km, 1.1)), e)`;
   row-normalize into a stochastic transition matrix.
4. **Weighted DeepWalk.** Random walks on the IDW-weighted k-NN graph;
   Word2Vec Skip-gram over walk sequences; 100D GV-NLE embedding per entity.
5. **Upsert + pickle persistence.** `OsmEntity.gv_nle_embedding` ← 100D
   vectors (SQL COPY); `wdw.pickle` ← WDWStore (DeepWalk model, for
   `NLEModel.reload`); `compute_static_embeddings` → 400D `static_embedding`;
   `create_static_embedding_hnsw_index` → HNSW on 400D.

> **Verified 2026-08-31, re-verified 2026-09-09:** the last two steps are
> **pending**. `static_embedding` is NULL for all 46,030,536 rows and no
> `static_embedding` HNSW index exists. See
> [01_Two_Axis_vs_Single_Axis_Encoding.md §7](01_Two_Axis_vs_Single_Axis_Encoding.md).

---

## 6. Subgraph Fan-Out for BallTree Memory

k-NN graph construction is O(N²) in memory. The BallTree for a 400M-entity
country (Australia) would not fit in RAM. The pipeline splits countries into
**subgraphs** (administrative subdivisions), each with its own BallTree:

- Cuba (continuous geographic space)
  - Subgraph: Havana, BallTree(k=50) → DeepWalk → vectors
  - Subgraph: Santiago, BallTree(k=50) → DeepWalk → vectors
  - Subgraph: Camagüey, BallTree(k=50) → DeepWalk → vectors
  - Subgraph: ..., parallel workers, CPU cores

Why subdivisions? k-NN graph construction is O(N²) in memory. Splitting by
admin boundary keeps each BallTree small enough to fit in RAM while
preserving local geographic structure.

See [Subdivision Batch Training](../05_Learned_Layer/02_Subdivision_Batch_Training.md)
for the full fan-out architecture.

---

## 7. The Inductive Bridge: BallTree for Unseen Entities

**Source:** <ref_file file="backend/semantic_search/services/inductive_spatial_service.py" />

When an OSM entity is extracted from a new temporal snapshot, it has tags
(so GV-Tags is computable via FastText) but **no GV-NLE** (DeepWalk training
on the full graph is too expensive for every snapshot). The inductive
service computes a provisional GV-NLE via BallTree + IDW:

<ref_snippet file="backend/semantic_search/services/inductive_spatial_service.py" lines="171-174" />

```
New entity at (lat, lon) — has GV-Tags but no GV-NLE
  ↓
Load trained entities from PostGIS (GiST index)
  ↓
Build BallTree in-memory (sklearn, haversine metric)
  ↓
BallTree.query(lat, lon, k=50) → 50 nearest + distances
  ↓
IDW damped weights: w' = max(1/ln(max(dist_km, 1.1)), e)
  ↓
Weighted mean of 50 nearest GV-NLE vectors
  ↓
L2-normalized 100D provisional GV-NLE
  ↓
Concatenate [300D GV-Tags | 100D GV-NLE] → 400D static_embedding
```

See [Inductive BallTree IDW](../05_Learned_Layer/03_Inductive_BallTree_IDW.md)
for the full inductive encoding schematic.

---

## 8. Data Flow Summary

```
GeoVectors TSV files (pre-trained, per country)
  ↓
generate_subgraph_pickle() → wdw.pickle (WDWStore: {osm_key → 100D vector})
  ↓
NLEModel.load_indexes() → self.wdw (in-memory DeepWalk model)
  ↓
NLEModel.encode_coords(lat, lon)
  - PostGIS GiST KNN: SELECT ... ORDER BY geom <-> point LIMIT 50
  - IDW weights: ln(1 + 1/dist_km)
  - weighted mean of wdw.predict(other_key) for 50 neighbors
  ↓
100D GV-NLE embedding

SEPARATELY (Step 5 training):
PostGIS GiST → load entities → BallTree(haversine) → k-NN graph
  → IDW damped weights → DeepWalk → 100D GV-NLE → wdw.pickle

SEPARATELY (inductive, unseen entities):
PostGIS GiST → load trained entities → BallTree(haversine)
  → query k=50 → IDW damped weights → weighted mean → 100D provisional
```

### 8.1 The wdw.pickle Lifecycle: Producers and Consumers

The pickle is **shared infrastructure**, not the property of any single
path. Two producers create it; three consumers read it.

**Producers (2):**
- `generate_subgraph_pickle()`: `location.tsv.gz` (Zenodo) → `WDWStore` →
  `wdw.pickle` + `wdw_embeddings.npy` + `wdw_keys.json`
- Step 5 `train_gv_nle`: k-NN graph → DeepWalk → DB bulk write + subgraph
  pickles

**Consumers (3):**
- Two-axis writer (Step 1): **dormant today**, needs a country-level pickle
  (0 exist on disk)
- Single-pass Phase 2 (legacy): **latent**, country pickle → subgraph pickle
  fallback (falls back to IE subgraph pickles)
- Inductive path (query time): **active** (new entities), BallTree + IDW
  from pickle

**Verified state (2026-08-31):** all 197 countries have `has_embeddings =
t` (gate 1 passes) but **zero** country-level `wdw.pickle` files exist on
disk (gate 2 fails), so the two-axis Step-1 writer never runs. Ireland's 104
pickles live at `pickles/{subgraph}/wdw.pickle`, a path the country-envelope
resolution (`pickle/wdw.pickle`) never checks (`pickles/` vs `pickle/`). The
active GV-NLE routes are Step 5 (direct DB bulk write) and the inductive
query-time path.

---

## 9. Invariants

- The `wdw.pickle` stores the DeepWalk model (WDWStore), **not** a BallTree.
  The BallTree is always rebuilt in-memory from PostGIS-loaded entities.
- **Two IDW formulas, two distinct roles, do not conflate:**
  - Training formula `max(1/ln(max(d, 1.1)), e)`, used in `graph.py`,
    `knn_graph_service.calculate_edge_weight`,
    `inductive_spatial_service._damped_weight`. Must stay in sync across
    those three sites.
  - Inference formula `ln(1 + 1/d)`, used **only** in
    `NLEModel.encode_coords()`. Do **not** replace with the training
    formula; the two serve different roles (see §4.2 comparison table).
- **Double-damping guard:** `WeightedDeepWalkService` defaults to
  `apply_damping=False`. Never call `apply_damped_weights()` on a graph
  whose weights came from `calculate_edge_weight()`. Pre-damped weights ≥ e
  collapse to e under the second application, destroying all distance
  information. `apply_damped_weights()` now raises `ValueError` if called
  with pre-damped input.
- The NLE encoder's PostGIS KNN query (`<->` operator) depends on the GiST
  index being present on the leaf partition's `geom` column.
- The BallTree is built per subgraph (admin boundary) to keep memory
  bounded.
- `generate_subgraph_pickle()` is idempotent, skips if the country has no TSV
  or no mapping in `country_relations.json`.
- `NLEModel.train()` was **removed 2026-09-10** — an early `return None` made
  the Node2Vec training block unreachable dead code and the method had zero
  callers (see `docs/issues/resolved/TICKET_REMOVE_DUAL_ENCODER.md`). The wdw
  pickle is produced by `GeoVectorsEncoderService.generate_subgraph_pickle()`,
  never by `train()`.
