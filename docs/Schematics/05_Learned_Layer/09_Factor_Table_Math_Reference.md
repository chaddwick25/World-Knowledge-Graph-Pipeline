# Factor-Table Mathematics: From k-NN Graph to Runtime pgvector

> **Purpose:** the complete mathematical reference for the combinatorial-
> graph layer: how the in-memory k-NN graph becomes Laplacian eigenbases
> (batch), how those factor into flat `factor_*` tables, and how runtime
> queries evaluate the factorized heat kernel as a **single pgvector inner
> product**. Every formula here is what the code implements (docstrings
> verified), not a paraphrase.
>
> **Companion docs:**
> - `docs/Schematics/05_Learned_Layer/05_Factor_Node_Runtime_Joins.md`, the batch-then-serve architecture
> - `docs/Schematics/05_Learned_Layer/04_Graph_Spectral_Drift.md`, temporal drift
> - `docs/plans/next-stage/GRAPH_SPECTRAL_FEEDBACK_HARDENING_PLAN.md`, hardening plan (this layer's known gaps)
> - `docs/plans/completed/DIRECT_PATH_ENRICHMENT_PLAN.md`, `getEntityContext` (the runtime tool that consumes these tables, §6)
> - `docs/plans/completed/FACTOR_NODE_RUNTIME_JOINS_PLAN.md`, original architecture
>
> **Code map:**
> - Graph build: `backend/semantic_search/services/knn_graph_service.py`
> - Eigendecomposition: `backend/semantic_search/services/spectral_analysis_service.py`
> - Factor writes: `backend/semantic_search/services/factor_node_writer.py`
> - Runtime resolution: `backend/semantic_search/services/factor_resolution_service.py`
> - Transport matrices: `backend/semantic_search/services/subgraph_transport_service.py`
> - Models: `backend/worldkg_nca/models.py`, `backend/semantic_search/models.py`
> - Pipeline: `backend/pipeline/tasks/country_pipeline_steps/step_5c_graph_spectral/`, `step_5d_*.py`

---

## 1. The Two Decompositions That Make the Whole Design Work

The architecture is a chain of three mathematical moves, each one converting
an expensive object into something storable and cheap to evaluate:

| Move | Transformation | Result |
|---|---|---|
| Move 1: spatial data → weighted graph | OSM entities (lat, lon, tags) → k-NN graph | `A`, `D`, `W` (batch) |
| Move 2: graph → Laplacian → eigendecomposition | `L = I − D^{−1/2} W D^{−1/2}`, `L = Φ Λ Φᵀ` | `λ₁..λ_K` + `Φ` (batch); fingerprint (default DB, eigenvalues λ); factor table (vectors DB, φ per node, 128D) |
| Move 3: Laplacian → factorized heat kernel → one inner product per node | `e^{−tL} = Φ · e^{−tΛ} · Φᵀ` | `w = e^{−tλ} ⊙ φ(anchor)` (numpy, K ≤ 128); φ(node) rows (pgvector column); ONE SQL: `eigen_loadings <#> :w` ORDER BY ... ASC |

**Why this is the "factorization":** the matrix exponential of the
Laplacian (the object you'd otherwise apply with scipy at request time)
never needs to be built. Its eigen-decomposition `e^{−tL} = Φe^{−tΛ}Φᵀ`
splits it into a scalar diagonal (λ's, one row per region) and a per-node
matrix (φ rows, one row per entity). Applying `e^{−tL}` to a delta signal
becomes a **dot product between stored rows**, which is exactly what
pgvector is for.

---

## 2. Stage 1: The k-NN Graph (the Input Combinatorial Object)

`KNNGraphService` (`knn_graph_service.py`). Nodes are OSM entities; edges
are geographic proximity.

### 2.1 Construction Math

```
nodes  = OSM entities with geom, keyed by osm_id (no osm_type, graph keyspace is osm_id only)
k      = 50  (GeoVectors paper §3.2)
metric = haversine on (lat, lon) via sklearn NearestNeighbors(metric='haversine', algorithm='ball_tree')
distances_km = distances_rad × 6371.0

edge weight (GeoVectors TRAINING formula):
    w'(d) = max( 1 / ln( max(d_km, 1.1) ),  e )
             log-damped inverse distance    floor at Euler's number ≈ 2.718
        (1.1 km clamp avoids ln(1)=0)  (keeps transition matrix connected)
```

The graph is **asymmetric** (i's k-nearest may not include j). At
materialization it is made undirected with **max-weight canonicalization**:
each `(i, j)` pair is deduplicated keeping `max(w_ij, w_ji)`, matching
NetworkX semantics exactly (`SparseGraph._canonical_edges`).

### 2.2 Two Representations

| Representation | When | Memory (IE: 2.47M nodes / 73.8M edges) |
|---|---|---|
| `networkx.Graph` (dict-of-dicts) | small graphs, tests, GraphML debug artifact | ~15 GB |
| `SparseGraph` (COO: `node_ids`, `edge_index`, `edge_weight`, `class_map`) | Step 5c spectral path | ~1.5 GB |

`SparseGraph` exposes the adjacency lazily as scipy CSR
(`to_csr(symmetrize=True)`) and both Laplacians (§3).

---

## 3. Stage 2: Laplacians and the Eigendecomposition

### 3.1 The Two Laplacians and Their Roles

```
Adjacency:          A  (CSR, symmetrized, weighted)
Degree matrix:      D = diag(deg),   deg(i) = Σ_j A_ij

Normalized Laplacian   L_norm = I − D^{−1/2} W D^{−1/2}
   → spectral fingerprint, eigenvalues λ ∈ [0, 2]      [GRAPH_REP:Eq 3.2]

Combinatorial Laplacian L_comb = D − W
   → Dirichlet energy sᵀLs, heat kernel, per-node sᵢ(Ls)ᵢ
```

### 3.2 Eigendecomposition

```
L Φ = Φ Λ,  ΦᵀΦ = I   (orthonormal)
request k+1 smallest, drop the trivial λ₀ = 0, keep λ₁..λ_K sorted ascending

λ₂            = algebraic connectivity (Fiedler value), how well-connected the graph is
λ_K − λ₂      = "spectral gap" (really a spread; hardening plan Phase 7 renames)
φ₂            = Fiedler vector, the "slowest" mode, a smooth coordinate over the graph
```

Solver routing (all in `spectral_analysis_service.py`):

```
n < 10K or no CUDA            →  CPU eigsh(which='SM')            (fallback)
10K ≤ n < 5M and CUDA free    →  GPU torch.lobpcg (block eigensolver, no LU)
n ≥ 5M                        →  CPU shift-invert randomized SVD
                                   rational filter f(λ) = 1/(λ + σ) via CG + AMG
                                   (Chebyshev polynomial filters failed on clustered spectra)
```

### 3.3 The Graph Signal (WorldKG Classes as a Node Function)

```
s(node) = class index of wkg_class(node)          (scalar encoding, hardening plan Phase 3)
Dirichlet energy:  sᵀLs  = Σ_edges w_ij (s_i − s_j)²     (combinatorial L, PSD ⇒ ≥ 0)
per-node:          sᵢ(Ls)ᵢ                               (stored as dirichlet_contrib)
diffusion:         (L + μI)⁻¹ s                           (regularized inverse, μ > 0)
```

---

## 4. The Runtime Factorization: Heat Kernel as One pgvector Query

### 4.1 Derivation

Heat-kernel diffusion from an anchor node (event diffusion, "what's near X"):

```
u(t) = e^{−tL} · δ_anchor

Eigendecompose the matrix exponential:
e^{−tL} = Φ · e^{−tΛ} · Φᵀ  =  Σ_k e^{−t·λ_k} · φ_k · φ_kᵀ

Apply to δ_anchor:
u(t) = Σ_k e^{−t·λ_k} · φ_k(anchor) · φ_k

Per-node score:
score(node) = Σ_k e^{−t·λ_k} · φ_k(anchor) · φ_k(node)  =  < w, φ(node) >
                with  w_k = e^{−t·λ_k} · φ_k(anchor)
```

The **two halves of the product come from two different tables**:

1. Anchor `osm_id` → `SELECT eigen_loadings FROM factor_spectral_node_metric
   WHERE osm_id = :anchor` → φ(anchor) ∈ R^128 (vectors DB)
2. Region + snapshot → `SELECT eigenvalues FROM
   semantic_search_graphspectralfingerprint` → λ₁..λ_K (default DB)
2b. **Eigenbasis coherence** (hardening Phase 1): the anchor row's
   `fingerprint_id` must equal the fingerprint's UUID. Mismatch →
   `diffusion_rank` returns None → PostGIS fallback (never wrong scores).
   NULL `fingerprint_id` (pre-migration rows) → lenient by default with an
   `unverified_eigenbasis` trace note; `FACTOR_EIGENBASIS_STRICT=True`
   upgrades NULL to a mismatch.
3. `w = zeros(128); w[:k] = exp(−t · λ) * φ_anchor[:k]`
4. Candidates → `SELECT osm_id, (eigen_loadings <#> :w) AS neg_ip FROM
   factor_spectral_node_metric WHERE snapshot_id = :snap AND country_code =
   :cc [AND subgraph_slug = :slug] [AND osm_id IN (:candidates)] ORDER BY
   neg_ip ASC LIMIT :top_k`
5. `score = −neg_ip` (pgvector `<#>` = NEGATIVE inner product, so ASC order
   = descending score)

### 4.2 Worked Numeric Example (illustrative, K = 4 for readability)

Take anchor `osm_id = 12345` (a cafe) with stored loadings and fingerprint
eigenvalues at `t = 1.0`:

```
λ        = [0.02, 0.11, 0.29, 0.63]          (normalized Laplacian, ascending)
φ(anchor)= [0.44, −0.31, 0.52, 0.18]         (stored row of Φ)

w_k = e^{−t·λ_k} · φ_k(anchor):
  e^{−0.02}=0.980 → w₁ = 0.980·0.44  = 0.431
  e^{−0.11}=0.896 → w₂ = 0.896·(−0.31)= −0.278
  e^{−0.29}=0.748 → w₃ = 0.748·0.52  = 0.389
  e^{−0.63}=0.533 → w₄ = 0.533·0.18  = 0.096

Two candidate nodes:
  φ(A) = [0.40, −0.25, 0.30, 0.10]   → score = 0.431·0.40 + (−0.278)(−0.25) + 0.389·0.30 + 0.096·0.10
                                      = 0.172 + 0.070 + 0.117 + 0.010 = 0.368
  φ(B) = [0.10,  0.20, −0.35, 0.45]  → score = 0.043 − 0.056 − 0.136 + 0.043 = −0.106

ORDER BY eigen_loadings <#> :w ASC  →  A first (0.368), B later (−0.106)
```

Read the structure: the decay `e^{−tλ}` damps **high-frequency** components
first. A matches the anchor on the smooth low-λ modes and wins. Diffusion
rank ≈ "who is on the same smooth spatial structure as the anchor".

Provenance and design rationale (Laplacian choice, invariant differences,
fusion with USLP scores, PCA future work): see
`docs/preserving_the_process/DIFFUSION_SCORE_AND_QUERY_FUSION.md`.

### 4.3 Why Zero-Padding to 128 Is Safe

Large countries compute K=64, small ones K=128. Both are padded to
`EIGEN_LOADING_DIM = 128` with trailing zeros. Zeros contribute nothing to
the inner product, so `<#>` results are identical across the two cases. The
drift code takes `min(K_from, K_to)` the same way.

---

## 5. Table-by-Table Math Reference

All `factor_*` tables live on the **vectors** DB (join `OsmEntity` without
FDW). The fingerprint and drift rows live on the **default** DB behind the
`Snapshot` FK.

| Batch write | Runtime read |
|---|---|
| Step 5c → `factor_spectral_node_metric` (`SpectralNodeMetric`) | `diffusion_rank` (pgvector `<#>`), `community_summary` (GROUP BY), `resolve_metrics` (metrics join) |
| Step 5c → `semantic_search_graphspectralfingerprint` (`GraphSpectralFingerprint`) | `_get_eigenvalues` (λ → w), drift computation |
| Step 5c Phase B → `factor_subgraph_transport` (`SubgraphTransport`) | `transport_loadings` (numpy k×k), then `<#>` in target |
| Step 5d → `factor_drift_node_metric` (`DriftNodeMetric`) | temporal drift joins |
| cmd → `factor_amenity_embedding` (`AmenityEmbedding`) | `amenity_embedding` (equality lookup), feeds `<=>` cosine vs `gv_tags_embedding` |
| Step 5d → `semantic_search_graphspectraldrift` (`GraphSpectralDrift`) | drift / forecast APIs |

### 5.1 `factor_spectral_node_metric` → `SpectralNodeMetric`

Key: `(snapshot_id, country_code, subgraph_slug, osm_id)`. **This table *is*
the graph at runtime**, one row per materialized factor node.

| Column | Math | Runtime use |
|---|---|---|
| `eigen_loadings` (VectorField 128) | φ(node) = row of Φ, zero-padded to 128 | **operand of `<#>`** |
| `fiedler_component` | φ₂(node) = `eigen_loadings[0]` (duplicated for cheap scalar ops) | scalar sort/filter |
| `louvain_community` | Louvain: greedy modularity max, `Q = (1/2m) Σ_ij [A_ij − d_i d_j / 2m] δ(c_i, c_j)` | `GROUP BY` |
| `dirichlet_contrib` | `sᵢ·(Ls)ᵢ` (combinatorial L) | per-node energy |
| `degree` | deg(i) = Σ_j A_ij (dedup) | structural join |
| `clustering_coeff` | local transitivity (NULL ≥ 200K nodes) | display |
| `component_id` / `component_size` | connected components of A | structural context |

Runtime SQL (the heart of the system, `diffusion_rank`):

```sql
SELECT osm_id, (eigen_loadings <#> :w) AS neg_ip
FROM factor_spectral_node_metric
WHERE snapshot_id = %(snap)s AND country_code = %(cc)s
  [AND subgraph_slug = %(slug)s]
  [AND osm_id IN %(candidates)s]
ORDER BY neg_ip ASC
LIMIT %(top_k)s;
```

`<#>` is **exact** negative inner product, no ANN/HNSW involved here
(that's why Step 1 doesn't maintain HNSW on the `gv_*` columns; the
`static_embedding` HNSW is a separate path).

### 5.2 `semantic_search_graphspectralfingerprint` → `GraphSpectralFingerprint`

Key: `(region, snapshot)`, region = country code or subgraph slug. Carries
the **scalar half of the factorization**.

| Column | Math |
|---|---|
| `eigenvalues` (JSON) | λ₁..λ_K, ascending, ∈ [0, 2] |
| `algebraic_connectivity` | λ₂ |
| `spectral_gap` | λ_K − λ₂ |
| `signal_smoothness` | sᵀLs (class-index signal; hardening Phase 3 → one-hot trace) |
| `fiedler_vector` | φ₂ sampled ≤ 10k entries |
| `node_count` / `edge_count` / `k_eigenvalues` | graph dimensions |

Runtime is an ORM fetch (`_get_eigenvalues`), never pgvector. The values
become the `e^{−tλ}` decay in `w`.

### 5.3 `factor_subgraph_transport` → `SubgraphTransport`

Key: `(snapshot_id, country_code, subgraph_from, subgraph_to)`. Only for
≥ 5M-node countries split into per-subgraph eigenbases. Full math in §7.

### 5.4 `factor_drift_node_metric` → `DriftNodeMetric`

Key: `(country_code, snapshot_from_id, snapshot_to_id, osm_id)`. Written by
Step 5d from two snapshots' factor rows over **shared nodes only**.

| Column | Math |
|---|---|
| `fiedler_delta` | φ₂(to) − φ₂(from) after sign alignment: `sign = −1` if `Σ_nodes φ_from·φ_to < 0` per component (hardening Phase 2 → block Procrustes) |
| `loading_drift` | cosine distance `1 − <a,b>/(‖a‖‖b‖)` of aligned loading rows ∈ [0, 2] |
| `community_changed` | raw Louvain ID inequality (hardening Phase 2 → ARI < threshold) |
| `degree_delta` | deg(t) − deg(t−1) |

No pgvector at runtime, scalar comparisons for temporal queries.

### 5.5 `factor_amenity_embedding` → `AmenityEmbedding`

300-D L2-normalized FastText per lowercased amenity vocabulary string. This
table is a **dictionary**: access is an equality lookup, and its output
participates in pgvector **cosine** (`<=>`) against entity
`gv_tags_embedding` in the executor's semantic amenity tier:

```
amenity text --> SELECT embedding FROM factor_amenity_embedding WHERE amenity_text = :q
     hit  --> np array → cosine vs gv_tags_embedding (pgvector <=>, semantic tier)
     miss --> runtime FastTextEmbeddingService.calculate_text_embedding (open vocabulary)
```

### 5.6 `semantic_search_graphspectraldrift` → `GraphSpectralDrift`

Key: `(region, snapshot_from, snapshot_to)`. Pure numpy/JSON, no pgvector.

| Column | Math |
|---|---|
| `spectral_distance` | `‖λ_t − λ_{t−1}‖₂` (zero-padded; eigenvalues are graph invariants, no node alignment) |
| `connectivity_delta` | Δλ₂ |
| `spectral_gap_delta` | Δ(λ_K − λ₂) |
| `fiedler_drift` | cosine distance of Fiedler vectors over the common node set ∈ [0, 2] |
| `smoothness_delta` / ratio | Δ(sᵀLs) / ratio |
| `drift_magnitude` | low < 0.1 < medium < 0.5 < high < 1.0 < extreme |
| `forecast_eigenvalues` / `forecast_confidence` | ARIMA / exp-smoothing forecast + 1.96σ intervals |
| `changepoint_detected` | CUSUM on the eigenvalue series |

---

## 6. Calling the Factor Tables at Runtime: `getEntityContext`

The enrichment layer consumes these tables through
`EntityContextService.get_context` (from
`docs/plans/completed/DIRECT_PATH_ENRICHMENT_PLAN.md`), the deterministic
replacement for LLM tool selection:

1. `QueryExecutorService.execute(parsed)` → results (osm_ids)
2. `EntityContextService.get_context(osm_ids, country, snapshot, template)`,
   3 SQL queries:
   - `uslp_links` → `SpatialTripletScore` (relational)
   - `communities` → `FactorResolutionService.resolve_metrics` →
     `factor_spectral_node_metric` (structural)
   - `class_distribution` → `OsmEntity.wkg_class GROUP BY` (semantic)
3. `LLMService.chat_stream()` (one synthesis call, grounded by context)

```python
from semantic_search.services.entity_context_service import EntityContextService

context = EntityContextService.get_context(
    osm_ids, country_code, snapshot_date,
    template=template, trace=trace,
)
# context = {
#   "uslp_links":   [{"head_osm_id", "relation", "tail_osm_id", "normalized_score"}, ...],
#   "communities":  {osm_id: {"fiedler_component", "louvain_community", "degree",
#                             "clustering_coeff", "component_id", "component_size", ...}},
#   "class_distribution": [{"wkg_class", "count"}, ...],
# }
```

### 6.1 Template → Context-Source Selection

| Template | USLP links | Communities | Class dist | Factor tables touched |
|---|---|---|---|---|
| #1 FILTER-AGGREGATE-MEASURE | ✓ | ✓ | ✓ | `resolve_metrics` (spectral node metric) |
| #2 OBJECT-FIELD-MEASURE | ✓ | ✓ | — | `resolve_metrics` |
| #4 GEOCODE-BATCH-COMPARE | ✓ | ✓ | ✓ | `resolve_metrics` |
| #5 LOCATION-BEARING-CLASSIFY | — | — | ✓ | `OsmEntity.wkg_class` |
| #8 PLACE-ATTRIBUTE-QUERY | ✓ | — | ✓ | `diffusion_rank` + `resolve_metrics` |
| #11-14 graph/spectral | — | ✓ | — | `diffusion_rank` / `community_summary` |

### 6.2 Direct Factor-Table Calls (executor + enrichment)

```python
# ---- factor_spectral_node_metric ----
# heat kernel diffusion (PLACE-ATTRIBUTE / EVENT-DIFFUSION):
ranked = FactorResolutionService().diffusion_rank(
    anchor_osm_id=12345, t=1.0,
    snapshot_id="2025_12_31", country_code="BZ",
    candidate_osm_ids=candidates, limit=20, trace=trace,
)   # one pgvector <#> query (SQL in §5.1)

# structural metrics for getEntityContext's "communities" source:
metrics = FactorResolutionService().resolve_metrics(
    osm_ids, snapshot_id, country_code,
)   # {osm_id: {fiedler_component, louvain_community, degree, ...}}

# community summaries (COMMUNITY-DETECT):
summary = FactorResolutionService().community_summary(
    snapshot_id, country_code, wkg_class="wkgs:Cafe", trace=trace,
)   # SELECT louvain_community, COUNT(*) ... GROUP BY louvain_community

# ---- factor_amenity_embedding ----
emb = FactorResolutionService().amenity_embedding("cafe")   # 300-D array or None
# None → runtime FastText fallback (open-vocabulary OBJECT extraction)

# ---- factor_subgraph_transport (≥ 5M-node countries) ----
from semantic_search.services.subgraph_transport_service import (
    SubgraphTransportService,
)
loadings_b = SubgraphTransportService.transport_loadings(
    loadings_a, C,                       # C = stored k×k matrix (JSON)
)   # numpy k×k multiply → then <#> in subgraph B
```

### 6.3 What the Synthesis LLM Receives

```
Question: "Which cafes are within 50km of Belize City?"
Template: FILTER-AGGREGATE-MEASURE (#1)
Primary result: 18 cafes (osm_ids ...)

Context:
  USLP spatial links: 5 cafes predicted co-located with hotels (within_50m_of)…
  Community structure: 18 cafes → 3 communities; largest (11) in city center;
                       degree 22–48, Fiedler components cluster downtown…
  Class distribution: 14 wkgs:Cafe, 3 wkgs:FastFoodRestaurant, 1 wkgs:Bar

Synthesize a grounded answer (3 sentences, 80 words max)…
```

Every number in that prompt came from a row in §5's tables. No model
inference, no graph load, ~50 ms.

---

## 7. Cross-Subgraph Transport: the Math for Big Countries

For countries ≥ `SUBDIVISION_NODE_THRESHOLD` (5M), each subgraph is solved
independently, so each has its **own eigenbasis**. Transport matrices bridge
them (functional maps, Ovsjanikov 2012).

**Batch (Step 5c Phase B), per adjacent pair (A, B):**

```
F_A, F_B = shared buffer-zone entities' loadings in each basis   (m × k)

C_{A→B} = argmin_C  ‖F_B − F_A·Cᵀ‖²  +  λ‖CΛ_A − Λ_B C‖²
              descriptor term            Laplacian commutativity term

Descriptor OLS:        Cᵀ = lstsq(F_A, F_B)   →  C = F_Bᵀ F_A (F_Aᵀ F_A)⁻¹
Commutativity term is DIAGONAL in vec(C):
    ‖CΛ_A − Λ_B C‖² = Σ_{i,j} (λ_a[i] − λ_b[j])² · C[i,j]²
⇒ regularized solve = k independent k×k systems:
    (F_AᵀF_A + λ·diag((λ_a[i] − λ_b[j])²)) · c_j = (F_AᵀF_B)[:, j]

fit_residual           = ‖F_B − F_A Cᵀ‖_F / ‖F_B‖_F
commutativity_residual = ‖CΛ_A − Λ_B C‖_F / ‖Λ_A‖_F
(min 16 shared entities, else the pair is skipped)
```

**Runtime (anchor in A, candidates in B):**

1. Anchor in subgraph A: `SELECT eigen_loadings` → φ(anchor) in A's basis
2. `loadings_b = C · loadings_a` (numpy matvec; C = stored k×k matrix from
   `factor_subgraph_transport`, JSON, with residuals)
3. `w_target[k] = e^{−t·λ_k^B} · loadings_b[k]` (re-decay in B's eigenvalue
   space; λ^B from B's fingerprint)
4. `SELECT osm_id, (eigen_loadings <#> :w_target) AS neg_ip FROM
   factor_spectral_node_metric WHERE subgraph_slug = 'B' ... ORDER BY neg_ip
   ASC`
5. Merge + re-rank A and B results (hardening Phase 4 normalizes scores)

Why not compare loadings across subgraphs directly? Eigenbases are
unrelated (rotation + scale per subgraph). `C` is the learned basis change,
and its `fit_residual`/`commutativity_residual` are the quality
certificates stored alongside.

---

## 8. Temporal Drift: the Pair-Wise Math (Step 5d)

```
per-node (factor_drift_node_metric):
   sign alignment:  dots_k = Σ_nodes φ_from[:,k]·φ_to[:,k];  sign_k = −1 if dots_k < 0
                    (hardening Phase 2: block-wise orthogonal Procrustes for near-degenerate λ)
   fiedler_delta   = φ₂(to) − φ₂(from)
   loading_drift   = 1 − <a,b>/(‖a‖‖b‖)   ∈ [0, 2]
   community_changed = id_from ≠ id_to     (hardening Phase 2: ARI < 0.8)
   degree_delta    = deg(t) − deg(t−1)

aggregate (semantic_search_graphspectraldrift):
   spectral_distance = ‖λ_t − λ_{t−1}‖₂        (λ's are graph invariants, no node alignment)
   connectivity_delta = λ₂(t) − λ₂(t−1)
   spectral_gap_delta = (λ_K − λ₂)(t) − (λ_K − λ₂)(t−1)
   fiedler_drift      = cosine distance over common node set
   smoothness_delta   = (sᵀLs)(t) − (sᵀLs)(t−1)
   forecast           = ARIMA / exp-smoothing on the eigenvalue series + 1.96σ bands
   changepoint        = CUSUM on the series
```

---

## 9. Invariants to Protect (from the Test Suites)

```
λ ∈ [0, 2], sorted ascending, non-negative            (normalized Laplacian)
ΦᵀΦ ≈ I                                               (orthonormality)
eigen_loadings rows are coordinates in ONE snapshot's basis, never mix snapshots
score = <w, φ>  computed with <#> (negative IP) ⇒ ORDER BY ASC = rank by score
loading_drift, fiedler_drift ∈ [0, 2]                 (cosine distance)
tr(sᵀLs) ≥ 0;  = 0 for constant signal on connected graph
modularity Q ∈ [−0.5, 1]
zero-padding to EIGEN_LOADING_DIM = 128 is shared padding — inner products stay valid
```

---

## 10. Known Hardening Gaps (see the Hardening Plan)

| Area | Gap | Plan phase |
|---|---|---|
| Fingerprint ↔ factor rows | written non-atomically → stale-basis mismatch risk on reruns | Phase 1 |
| Drift alignment | sign flips only; fails on near-degenerate spectra | Phase 2 |
| `community_changed` | raw Louvain ID comparison, not partition similarity | Phase 2 |
| Signal encoding | class-index ordering-dependent `sᵀLs` / `dirichlet_contrib` | Phase 3 |
| Cross-subgraph merge | raw score re-rank across eigenbases | Phase 4 |
| Amenity classes | hardcoded 10-term dict in runtime path | Phase 5 |
| Graph persistence | k-NN graph rebuilt from scratch every run | Phase 6 |
| `spectral_gap` naming | λ_K − λ₂ is a spread, not the classic gap | Phase 7 |
