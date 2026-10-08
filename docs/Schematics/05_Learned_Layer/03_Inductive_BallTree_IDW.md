# Inductive BallTree + IDW: Encoding Unseen Entities

> **Focus:** how BallTree and logarithmic inverse-distance weighting (IDW)
> are used to inductively encode unseen entities from PostGIS spatial
> indexes, and how this relates to pickle generation and semantic search.
>
> **Key idea:** when an OSM entity is extracted from a new temporal
> snapshot, it has tags (so GV-Tags is computable via FastText) but **no
> GV-NLE** (DeepWalk training on the full graph is too expensive for every
> snapshot). The inductive service builds a BallTree from PostGIS-loaded
> entities (using the GiST index for the initial load), queries the k=50
> nearest trained entities, and computes a proximity-weighted mean of their
> GV-NLE vectors using the damped IDW formula.

---

## 1. The Inductive Bridge

**New entity (from a new snapshot):**
- `osm_id: 999999`
- `tags: {"amenity": "restaurant", "name": "New Place"}`
- `geom: POINT(-82.36 23.13)`
- `gv_tags_embedding: [300D vector]` ← FastText, computed immediately
- `gv_nle_embedding: NULL` ← NO DeepWalk training yet
- `static_embedding: NULL` ← needs both axes

**Inductive spatial service:**
1. Load trained entities from PostGIS (GiST index): `OsmEntity.objects.
   filter(country_code='CU', snapshot_id='2025_12_31',
   gv_nle_embedding__isnull=False)`. GiST prunes to the country bbox;
   partition pruning routes to the leaf.
2. Build BallTree in-memory (sklearn, haversine):
   `BallTree(np.radians(pool_coords), metric='haversine')`
3. Query k=50 nearest trained entities:
   `dists_rad, indices = tree.query(np.radians([[lat, lon]]), k=50)`;
   `dists_km = dists_rad[0] * 6371.0`
4. IDW damped weights: `w'(d) = max(1/ln(max(d_km, 1.1)), e)`
5. Weighted mean of 50 nearest GV-NLE vectors:
   `provisional_nle = sum(w_i * nle_i) / sum(w_i)`, L2-normalized
6. Concatenate → 400D `static_embedding`:
   `static_embedding = concatenate(gv_tags, provisional_nle)`

**Source:** <ref_file file="backend/semantic_search/services/inductive_spatial_service.py" />

---

## 2. Why Inductive Encoding?

DeepWalk training (Step 5) is expensive:
- Build BallTree from all entities
- Construct k-NN graph with IDW weights
- Run weighted random walks
- Train Word2Vec Skip-gram over walk sequences
- GPU-accelerated (PyTorch)
- Per-subdivision fan-out for memory

Running Step 5 for every new temporal snapshot would be prohibitively
expensive. Instead, the inductive service computes a **provisional** GV-NLE
for new entities by interpolating from the nearest trained entities.

### 2.1 The Trade-Off

| Approach | Cost | Quality |
|---|---|---|
| Full DeepWalk retraining (Step 5) | High (GPU, hours) | Authoritative |
| Inductive BallTree + IDW | Low (CPU, seconds) | Provisional (good approximation) |

The inductive encoding is a **good approximation** because:
- The k-NN graph captures local spatial structure
- IDW damped weights preserve the distance-decay relationship
- The 50 nearest trained entities provide a rich neighborhood signal
- L2 normalization keeps the provisional vector in the same space as the
  trained vectors

---

## 3. The IDW Damped Weight Formula

The IDW (Inverse Distance Weighting) damped weight formula is the
architectural keystone that connects the spatial axis to the semantic axis:

```python
weights = np.maximum(weights, 1.1)          # clamp minimum distance to 1.1km
weights = np.maximum(1.0 / np.log(weights), np.e)  # w' = max(1/ln(d), e)
```

### 3.1 What the Formula Means

- `d` = haversine distance in kilometers between two OSM entities
- Clamp to 1.1: prevents `ln(1) = 0` (division by zero). Entities closer
  than 1.1km get maximum weight.
- `ln(d)`: logarithmic damping, the weight drops off logarithmically with
  distance, not linearly. Preserves meaningful weights at medium distances
  while preventing extreme weights at very close range.
- `max(..., e)`: floor at e ≈ 2.718. Even the farthest entities retain a
  non-zero weight, so the k-NN graph stays fully connected.

### 3.2 The Four Usage Sites

The IDW formula appears in four places in the codebase, all of which must
stay in sync:

| Location | File | Purpose |
|---|---|---|
| Training (Numba JIT) | `geovectors_encoder/core/graph.py:26-33` | DeepWalk GV-NLE training |
| k-NN graph construction | `semantic_search/services/knn_graph_service.py:117` (`build_knn_graph`) | Building the k-NN graph edges |
| DeepWalk service (damping) | `semantic_search/services/deepwalk_service.py:148-164` | Applies damping to pre-computed graph |
| Inductive inference | `semantic_search/services/inductive_spatial_service.py:109-113` | Provisional GV-NLE for new entities |

### 3.3 NLE Encoder Variant (Original GeoVectors Formula)

`NLEModel.encode_coords()` uses a slightly different variant that matches
the original GeoVectors paper:

```python
dist = np.log(1 + (1 / (n[1] / 1000)))  # n[1] in meters → km
```

This is `ln(1 + 1/d)`, a log-inverse distance weight. It produces the same
monotonically-decreasing-with-distance behavior as `max(1/ln(d), e)` but
without the floor clamping. Both are valid IDW formulations. The choice
depends on whether you need the graph-connectivity guarantee (the
`max(..., e)` floor) or the paper-exact formula.

---

## 4. The BallTree: In-Memory, Ephemeral

<ref_snippet file="backend/semantic_search/services/inductive_spatial_service.py" lines="171-174" />

```python
from sklearn.neighbors import BallTree
self._tree = BallTree(np.radians(self._pool_coords), metric='haversine')
```

The BallTree is:
- **In-memory**, built from PostGIS-loaded entities, not from a pickle
- **Ephemeral**, rebuilt each time `InductiveSpatialService` is initialized
- **Per-country**, loaded for one country's entities (partition pruning
  keeps the pool size manageable)
- **Haversine metric**, radian coordinates for great-circle distance

### 4.1 BallTree vs PostGIS GiST KNN

Both can answer "find the k nearest entities to this point":

| Method | Where | Speed | Use Case |
|---|---|---|---|
| PostGIS GiST KNN (`<->`) | Database | O(log N + k) per query | NLE encoder (per-entity, runtime) |
| BallTree (sklearn) | In-memory | O(log N) per query | Batch k-NN graph construction, inductive encoding |

The BallTree is faster for batch queries (N × k queries in-memory). The
GiST KNN is better for single runtime queries (no need to load entities
into memory first).

---

## 5. The Pickle Connection

The `wdw.pickle` (DeepWalk model) and the BallTree serve different purposes:

**`wdw.pickle` (persisted):**
- `WDWStore`: {osm_key → 100D GV-NLE vector}
- Loaded by `NLEModel.load_indexes()`
- Used by `NLEModel.encode_coords()` to predict GV-NLE for known entities

**BallTree (in-memory, ephemeral):**
- Built from PostGIS-loaded entity coordinates
- Used to find k=50 nearest neighbors
- Combined with `wdw.pickle` (or trained `gv_nle_embedding`) for IDW

The inductive service does **not** use the `wdw.pickle` directly. It uses
the `gv_nle_embedding` column from PostGIS (populated by Step 5 training,
which used the DeepWalk model). The BallTree finds the nearest neighbors;
the `gv_nle_embedding` column provides the vectors to weight.

---

## 6. The Full Inductive Flow

1. New entity arrives (new snapshot, has GV-Tags, no GV-NLE).
2. `InductiveSpatialService.initialize(country_code, snapshot_id)`:
   - Query PostGIS for trained entities (`gv_nle_embedding IS NOT NULL`);
     GiST prunes to the country bbox, partition pruning routes to the leaf
   - Extract coords + `gv_nle_embedding` vectors
   - Build `BallTree(np.radians(coords), metric='haversine')`
3. `InductiveSpatialService.embed_entity(lat, lon, gv_tags_vector)`:
   - `BallTree.query(np.radians([[lat, lon]]), k=50)` → 50 nearest indices
     + distances (radians)
   - Convert to km: `dists_rad * 6371.0`
   - IDW damped weights: `w'(d) = max(1/ln(max(d, 1.1)), e)`
   - Weighted mean: `provisional_nle = sum(w_i * nle_i) / sum(w_i)`
   - L2-normalize `provisional_nle`
   - Concatenate: `static_embedding = [gv_tags | provisional_nle]` (400D)
4. Upsert to PostGIS: `OsmEntity.gv_nle_embedding = provisional_nle`
   (100D); `OsmEntity.static_embedding = fused` (400D).
5. Semantic search can now find this entity:
   `SELECT ... ORDER BY static_embedding <=> query_vec LIMIT 50`.

> **Status note (2026-09-09):** the fused `static_embedding` and its HNSW
> index are not built. Runtime search uses exact `+ 0` scans + USLP re-rank.

---

## 7. Relationship to the Learned Layer

**Training (Step 5, expensive, per-snapshot):**
PostGIS GiST → load entities → BallTree → k-NN graph → IDW damped weights →
DeepWalk → 100D GV-NLE → `wdw.pickle` → `compute_static_embeddings` → 400D
`static_embedding` → `create_static_embedding_hnsw_index` → HNSW

**Inductive (runtime, per-new-entity):**
PostGIS GiST → load trained entities → BallTree → query k=50 → IDW damped
weights → weighted mean → 100D provisional → concatenate → 400D
`static_embedding`

**Semantic search (runtime, per-query):**
HNSW on `static_embedding` → O(log N) ANN → PostGIS spatial filter (GiST) →
relational filter (`wkg_class`) → results

The inductive path is the bridge between training (expensive, per-snapshot)
and search (runtime, per-query). It lets new entities participate in
semantic search without retraining the full DeepWalk model.

---

## 8. Invariants

- The BallTree is in-memory and ephemeral, rebuilt from PostGIS-loaded
  entities each time.
- The IDW formula `max(1/ln(max(d, 1.1)), e)` must stay in sync across all
  four usage sites.
- The inductive encoding is **provisional**. It approximates GV-NLE for new
  entities without retraining DeepWalk.
- `static_embedding` (400D) is only populated when both GV-Tags + GV-NLE
  (or provisional NLE) are present.
- The BallTree uses haversine distance (radian coordinates) for
  great-circle accuracy.
- L2 normalization keeps the provisional vector in the same space as the
  trained vectors.
