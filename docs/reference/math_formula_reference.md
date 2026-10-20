# Mathematical Formula Reference

> **Status**: new (2026-09-19)
>
> **Purpose**: one pass over every formula in the app docs. Part 1 is the
> formula base: every formula the docs record, organized by source doc,
> byte-identical to the originals. Part 2 says, in plain terms, what stays
> true about each formula family and which checks enforce it.
>
> **Why**: the pipeline is a chain of math (OSM entity → k-NN graph →
> eigenbasis → factor tables → runtime inner product). At each stage,
> some property has to hold or the next stage's numbers are wrong. Part 1
> makes the formulas checkable in one place. Part 2 names the property and
> the check.
>
> **Companion docs**:
> - `../semantic_search/factor_table_math_reference.md`,
>   the math the factor tables implement

---

## 1. Intent (decision in one line)

Part 1 collects the formulas so the math is checkable in one place. Part 2
states what stays true at each stage and where that property is checked in
code or tests.

---

## 2. Notation

| Symbol | Meaning |
|---|---|
| `A` | adjacency matrix (weighted, symmetrized) |
| `D` | degree matrix, `D = diag(deg)`, `deg(i) = Σ_j A_ij` |
| `W` | weight matrix (k-NN edge weights) |
| `L` | graph Laplacian (normalized or combinatorial, per context) |
| `Φ`, `Λ` | eigenvectors, eigenvalues: `L Φ = Φ Λ` |
| `λ_k`, `φ_k` | k-th eigenvalue, k-th eigenvector |
| `λ₂` | Fiedler value (algebraic connectivity) |
| `s` | graph signal (WorldKG class index per node) |
| `δ_anchor` | unit impulse at the anchor node |
| `t` | heat-kernel diffusion time |
| `C` | functional-map transport matrix |
| `d`, `d_km` | haversine distance, in km |
| `‖·‖`, `‖·‖_F` | Euclidean norm, Frobenius norm |
| `⟨·,·⟩` | inner product |
| `Spec(L)` | eigenvalue multiset of `L` |
| `P` | a permutation matrix (relabels the nodes) |

---

## 3. Part 1: The Formula Base

Every formula below is quoted from its source schematic. Formula text is
byte-identical to the original; the Source column names the doc and
section.

### 3.1 Encoder: `geovectors_encoder/`

#### 3.1.1 GV-Tags: token centroid + L2 normalization
[../geovectors_encoder/two_axis_vs_single_axis_encoding.md §3, §8](../geovectors_encoder/two_axis_vs_single_axis_encoding.md)

| Formula | Meaning |
|---|---|
| `e = np.mean(vectors, axis=0)` | unweighted arithmetic mean (centroid) of per-token FastText vectors, equal weight per token |
| `ê = e / np.linalg.norm(e)` | in-tree L2 normalization, norm ≈ 1.0, so cosine = dot product (required by `vector_cosine_ops`) |
| `static_embedding (400D) = concatenate(gv_tags_embedding(300D), gv_nle_embedding(100D))` | fused embedding, only populated when both axes present |
| `cosine distance ∈ [0, 2]`, `similarity = 1 − distance` | pgvector cosine semantics |

#### 3.1.2 The two IDW formulas (do not conflate)
[../geovectors_encoder/balltree_pickle_generation.md §4](../geovectors_encoder/balltree_pickle_generation.md)

| Formula | Role |
|---|---|
| `w'(d) = max(1/ln(max(d, 1.1)), e)` | training formula (DeepWalk transition weights, graph construction, inductive `_damped_weight`). `e ≈ 2.718` floor keeps the transition matrix connected |
| `dist = np.log(1 + (1 / (n[1] / 1000)))` | inference formula (`NLEModel.encode_coords`, GeoVectors paper §3.4), `n[1]` = meters → km |
| `enc = np.sum(vectors, axis=0) / dist_sum` | normalized weighted mean of the 50 nearest GV-NLE vectors |
| `distances_km = distances_rad × 6371.0` | haversine radians → km |

### 3.2 Postgres

[../geovectors_encoder/vector_storage_partitioning.md](../geovectors_encoder/vector_storage_partitioning.md),
[../worldkg_nca/gis_vector_metrics.md §3.4](../worldkg_nca/gis_vector_metrics.md)

| Formula | Meaning |
|---|---|
| GiST bbox `O(log N)`, GiST KNN `O(log N + k)` | spatial index bounds |
| HNSW `O(log N)` per query, ~150 bytes/row (m=16 pointers) | ANN graph structure, vectors live in the heap |
| `cosine distance ∈ [0, 2]`, `similarity = 1 − distance` | vector similarity semantics |

### 3.3 Learned Layer: `semantic_search/`

#### 3.3.1 The k-NN graph and its weights
[../semantic_search/factor_table_math_reference.md §2](../semantic_search/factor_table_math_reference.md)

| Formula | Meaning |
|---|---|
| `k = 50` | neighbors per node (GeoVectors paper §3.2) |
| `w'(d) = max( 1 / ln( max(d_km, 1.1) ),  e )` | log-damped inverse distance, floor at Euler's number ≈ 2.718 |
| asymmetric → undirected via max-weight canonicalization | `(i, j)` dedup keeps `max(w_ij, w_ji)` (NetworkX semantics) |
| k-NN memory: `N × k` edges | `O(N × k)`; 400M entities × 50 = 20B edges ≈ 800 GB, the reason for subdivision fan-out |

#### 3.3.2 Laplacians and eigendecomposition
[../semantic_search/factor_table_math_reference.md §3](../semantic_search/factor_table_math_reference.md),
[../semantic_search/graph_spectral_drift.md §2.2](../semantic_search/graph_spectral_drift.md)

| Formula | Meaning |
|---|---|
| `L_norm = I − D^{−1/2} W D^{−1/2}` | normalized Laplacian, eigenvalues `λ ∈ [0, 2]`, spectral fingerprint |
| `L_comb = D − W` | combinatorial Laplacian, Dirichlet energy, heat kernel |
| `L Φ = Φ Λ,  ΦᵀΦ = I` | orthonormal eigendecomposition; drop trivial `λ₀ = 0`, keep `λ₁..λ_K` ascending |
| `λ₂` = algebraic connectivity (Fiedler value) | how well-connected the graph is |
| `λ_K − λ₂` | "spectral gap" (a spread; hardening Phase 7 renames) |
| `φ₂` = Fiedler vector | the slowest mode, a smooth coordinate over the graph |

#### 3.3.3 Graph signals and diffusion
[../semantic_search/factor_table_math_reference.md §3.3](../semantic_search/factor_table_math_reference.md),
[../semantic_search/graph_spectral_drift.md §2.3, §2.4](../semantic_search/graph_spectral_drift.md)

| Formula | Meaning |
|---|---|
| `s(node) = class index of wkg_class(node)` | scalar class signal per node |
| `sᵀLs  = Σ_edges w_ij (s_i − s_j)²` | Dirichlet energy (combinatorial L, PSD ⇒ ≥ 0); low = classes cluster spatially |
| `sᵢ(Ls)ᵢ` | per-node contribution, stored as `dirichlet_contrib` |
| `(L + μI)⁻¹ s` | regularized inverse diffusion, `μ > 0` |
| `u(t) = e^{−tL} · δ_source` | heat kernel diffusion from a source; mass-conserving `Σᵢ u(t)ᵢ = 1` |

#### 3.3.4 The runtime factorization: heat kernel as one pgvector query
[../semantic_search/factor_table_math_reference.md §4](../semantic_search/factor_table_math_reference.md)

| Formula | Meaning |
|---|---|
| `e^{−tL} = Φ · e^{−tΛ} · Φᵀ  =  Σ_k e^{−t·λ_k} · φ_k · φ_kᵀ` | eigendecompose the matrix exponential, never build it |
| `u(t) = Σ_k e^{−t·λ_k} · φ_k(anchor) · φ_k` | apply to the anchor delta |
| `score(node) = Σ_k e^{−t·λ_k} · φ_k(anchor) · φ_k(node)  =  < w, φ(node) >` | per-node score is an inner product |
| `w_k = e^{−t·λ_k} · φ_k(anchor)` | the weight vector; one pgvector `<#>` (negative inner product), `ORDER BY neg_ip ASC` |
| zero-padding to `EIGEN_LOADING_DIM = 128` | trailing zeros contribute nothing to the inner product |

Worked numeric example (K = 4, from the source doc): `λ = [0.02, 0.11, 0.29,
0.63]`, `φ(anchor) = [0.44, −0.31, 0.52, 0.18]` at `t = 1.0` gives `w =
[0.431, −0.278, 0.389, 0.096]`; candidate A scores 0.368, candidate B scores
−0.106, A ranks first. The decay `e^{−tλ}` damps high-frequency modes first.

#### 3.3.5 Louvain modularity
[../semantic_search/factor_table_math_reference.md §5.1](../semantic_search/factor_table_math_reference.md)

| Formula | Meaning |
|---|---|
| `Q = (1/2m) Σ_ij [A_ij − d_i d_j / 2m] δ(c_i, c_j)` | greedy modularity maximization; `δ` = 1 when nodes share a community; `Q ∈ [−0.5, 1]` |

#### 3.3.6 Cross-subgraph transport (functional maps)
[../semantic_search/factor_table_math_reference.md §7](../semantic_search/factor_table_math_reference.md)

| Formula | Meaning |
|---|---|
| `C_{A→B} = argmin_C  ‖F_B − F_A·Cᵀ‖²  +  λ‖CΛ_A − Λ_B C‖²` | descriptor term + Laplacian commutativity term; `F_A, F_B` = shared buffer-zone entities' loadings (`m × k`) |
| `Cᵀ = lstsq(F_A, F_B)   →  C = F_Bᵀ F_A (F_Aᵀ F_A)⁻¹` | descriptor OLS |
| `‖CΛ_A − Λ_B C‖² = Σ_{i,j} (λ_a[i] − λ_b[j])² · C[i,j]²` | commutativity term is diagonal in `vec(C)` |
| `(F_AᵀF_A + λ·diag((λ_a[i] − λ_b[j])²)) · c_j = (F_AᵀF_B)[:, j]` | regularized solve = k independent k×k systems |
| `fit_residual = ‖F_B − F_A Cᵀ‖_F / ‖F_B‖_F` | descriptor quality certificate |
| `commutativity_residual = ‖CΛ_A − Λ_B C‖_F / ‖Λ_A‖_F` | geometry-respect quality certificate; min 16 shared entities or the pair is skipped |

#### 3.3.7 Temporal drift (Step 5d)
[../semantic_search/factor_table_math_reference.md §8](../semantic_search/factor_table_math_reference.md),
[../semantic_search/graph_spectral_drift.md §3.1, §3.2](../semantic_search/graph_spectral_drift.md)

| Formula | Meaning |
|---|---|
| `spectral_distance = ‖λ_t − λ_{t−1}‖₂` | eigenvalue L2 difference; eigenvalues don't depend on node order, so no node alignment is needed |
| `connectivity_delta = λ₂(t) − λ₂(t−1)` | Fiedler shift |
| `spectral_gap_delta = (λ_K − λ₂)(t) − (λ_K − λ₂)(t−1)` | gap shift |
| `fiedler_drift = cosine distance over common node set` | Fiedler vector change, `∈ [0, 2]` |
| `smoothness_delta = (sᵀLs)(t) − (sᵀLs)(t−1)` | Dirichlet energy shift |
| `dots_k = Σ_nodes φ_from[:,k]·φ_to[:,k];  sign_k = −1 if dots_k < 0` | per-component sign alignment before comparison |
| `fiedler_delta = φ₂(to) − φ₂(from)` | aligned Fiedler difference |
| `loading_drift = 1 − <a,b>/(‖a‖‖b‖)   ∈ [0, 2]` | cosine distance of aligned loading rows |
| `degree_delta = deg(t) − deg(t−1)` | degree change |
| `drift_magnitude`: `low < 0.1 < medium < 0.5 < high < 1.0 < extreme` | classification thresholds |
| ARIMA(1,1,1), exp-smoothing fallback `α = 0.3`, `1.96σ` intervals, CUSUM | forecast + change-point |

#### 3.3.8 Embedding drift: Sliced Wasserstein Distance + freshness
[../semantic_search/graph_spectral_drift.md §4](../semantic_search/graph_spectral_drift.md)

| Formula | Meaning |
|---|---|
| `p_r = v_r · x` | project embeddings to 1D with random unit vectors; 100 projections, seed 42 |
| 1D Wasserstein (Earth Mover's Distance) per projection, averaged | approximates true W₂ in high dimensions |
| `F = α · exp(-λ_sem · W_sem) + (1-α) · exp(-λ_spat · W_spat)` | freshness score; `α = 0.5`, `λ_sem = λ_spat = 5.0`; `F ∈ [0, 1]`, 1 = identical |

#### 3.3.9 Spectral filtering: Chebyshev vs Lanczos
[../semantic_search/spectral_filtering_chebyshev_vs_lanczos.md §1, §2](../semantic_search/spectral_filtering_chebyshev_vs_lanczos.md)

| Formula | Meaning |
|---|---|
| `f(L) · x = U · f(Λ) · Uᵀ · x` | filtering a signal by the Laplacian = multiplying its spectral components by `f(λ)` |
| `f(L) ≈ Σₖ cₖ Tₖ(L̃)` | polynomial approximation, no eigenvectors needed |
| `L̃ = 2L/λ_max - I` | scaled Laplacian, eigenvalues in [−1, 1] |
| `T₀(L̃) x = x`, `T₁(L̃) x = L̃ x`, `Tₖ(L̃) x = 2 L̃ T_{k-1}(L̃) x - T_{k-2}(L̃) x` | three-term recursion, sparse mat-vec only |
| `α = t · λ_max / 2`, `f(λ) = exp(-tλ)` | heat-kernel filter coefficients; `λ_max ≈ 1.30` for IE (not bipartite) |

#### 3.3.10 MapQA geographic scoring and routing
[../semantic_search/mapqa_geographic_scoring_and_routing.md §1](../semantic_search/mapqa_geographic_scoring_and_routing.md),
[../semantic_search/mapqa_parser_executor.md §3](../semantic_search/mapqa_parser_executor.md)

| Formula | Meaning |
|---|---|
| `geo_score = 1 - d_cluster / d_max` | USLP geographic score (Mann et al. 2023 §3.3); P4 geohash (~39 km cells) default, raw haversine when the user gives an exact radius |
| `combined_score = diffusion_score + name_score + geo_score + uslp_boost` | re-rank; `name_score` = FastText cosine between the OBJECT span and entity `name` (proper-name queries only, else 0.0); USLP signal boost `+0.5` for predicted link tails (normalized score ≥ 0.7) |
| tag match boost `+1.0` | exact tag value match ranks above semantic near-match |

#### 3.3.11 Visualization kernels
[../semantic_search/external_concepts_dag_visualization.md §4](../semantic_search/external_concepts_dag_visualization.md)

| Formula | Meaning |
|---|---|
| `ŝ(x) = Σ_i w_i(x) · z_i ,   w_i(x) = 1 / d(x, x_i)^p` | generic IDW interpolation (the post's form of the pipeline's IDW) |
| `K(x, x') = exp(-||x - x'||² / 2σ²)` | Gaussian RBF kernel |
| `ρ(x) = Σ_i K(x, x_i)` | RBF density surface over result entity positions |

### 3.4 IGEA + USLP: `igea/`

#### 3.4.1 USLP tri-space scoring
[../igea/uslp_spatial_link_prediction.md §3](../igea/uslp_spatial_link_prediction.md)

| Formula | Meaning |
|---|---|
| `geo_sim = 1 - d / d_max` | geographic; `d` = haversine(head, tail), `d_max` = per-cluster max; `[0, 1]` |
| `name_sim = cosine(FastText(literal), FastText(candidate_name))` | semantic name similarity; `[−1, 1]` |
| `class_sim = cosine(FastText(relation_text), FastText(candidate_class))` | ontological compatibility; `[−1, 1]` |
| `unnormalized = geo + name + class` | `[0, 3.0]` |
| `normalized = unnormalized / 3.0` | `[0, 1.0]`; acceptance threshold `0.7` |
| geohash radius: precision 1 → ~5000 km, 3 → ~156 km, 4 → ~39 km | relation-specific search radius |

#### 3.4.2 IGEA alignment
[../igea/igea_iterative_alignment.md §3, §7](../igea/igea_iterative_alignment.md)

| Formula | Meaning |
|---|---|
| `score = cross_attention_model.predict(osm, wikidata)` | per-country cross-attention BiLSTM; fallback `cosine(gv_tags_embedding, candidate_emb)` |
| threshold `0.6`, max distance `2500 m`, max 3 iterations | acceptance, pair filter, loop bound |

### 3.5 Romanizer: `semantic_search/`

[../semantic_search/deterministic_cross_script_romanization.md §5.1](../semantic_search/deterministic_cross_script_romanization.md)

| Formula | Meaning |
|---|---|
| NFKD decomposition + strip combining marks (category Mn) | deterministic diacritic stripping |
| `S_xscript = n-gram Jaccard similarity("paris bagueete", "paribagetteu")` | pg_trgm trigram similarity, e.g. 0.38 |
| `S_total = S_name + S_xscript + S_geo + S_class` | combined score, parallel FastText + romanizer paths |

### 3.6 Agent / MCP / LLM: `platform/`

[../platform/llm_capacity_envelope.md §2](../platform/llm_capacity_envelope.md)

| Formula | Meaning |
|---|---|
| `fp16 KV = 2 × 36 layers × 8 KV heads × 128 head_dim × 2 bytes ≈ 144 KiB/token` | KV cache per token; 4096-token slot ≈ 0.55 GB |

---

## 4. Part 2: What stays true, and what the checks catch

Every formula family above survives some change untouched — node
relabeling, rotation, sign flips. This part says which change, in plain
terms, and where the check lives in code.

### 4.1 The master table

| Formula family | What stays true | Code home |
|---|---|---|
| Token centroid (GV-Tags) | token order doesn't change the mean | `embedding_service.py` |
| L2 normalization | rescaling doesn't change direction; embeddings sit on the unit sphere, so cosine = dot | `FastTextModel.encode_tags()` |
| Cosine similarity | rotating both vectors together leaves the score unchanged | search, USLP name/class |
| Haversine distance | distance between two points doesn't depend on the coordinate frame | k-NN, USLP geo, executor |
| IDW weights | weights depend on distance only | `graph.py`, `_damped_weight` |
| k-NN graph build | relabeling nodes relabels the Laplacian the same way: `L(P A Pᵀ) = P L(A) Pᵀ` | `knn_graph_service.py` |
| Laplacian spectrum | eigenvalues don't depend on node order: `Spec(P A Pᵀ) = Spec(A)`; eigenvectors relabel with the nodes | `spectral_analysis_service.py` |
| Eigenvector sign | if `φ` is an eigenvector so is `−φ`; comparisons must align signs first | `factor_node_writer.py` |
| Heat kernel | the score `⟨w, φ(node)⟩` is the same however the nodes are numbered | `factor_resolution_service.py` |
| Fiedler value, gap | `λ₂` and `λ_K − λ₂` are properties of the graph, not its ordering | fingerprint rows |
| Dirichlet energy | `sᵀLs` doesn't depend on node numbering | `graph_signal_service.py` |
| Louvain modularity | `Q` doesn't depend on node or community numbering | `community_detection_service.py` |
| Functional map | `C Λ_A ≈ Λ_B C`: carrying one eigenbasis to the other must not scramble the spectra | `subgraph_transport_service.py` |
| Spectral drift | `‖λ_t − λ_{t−1}‖₂` needs no node alignment between snapshots | `spectral_drift_service.py` |
| Loading drift | cosine distance of sign-aligned rows is unaffected by basis rotation | `spectral_drift_service.py` |
| Sliced Wasserstein | translating or rotating both point clouds leaves `W₂` unchanged | `embedding_drift_service.py` |
| Chebyshev filter | `f(L)x` gives the same answer under any node ordering | spectral filtering layer |
| USLP geo score | depends only on geodesic distance | `spatial_link_prediction.py` |
| MapQA bag-of-words | word order doesn't change TF-IDF statistics | `query_parser_service.py` |

### 4.2 Why the factor tables work

The pipeline's deepest property is that eigenvalues don't depend on how
the nodes are numbered. If `P` is a permutation matrix relabeling the
nodes:

```
adjacency relabels:          A → P A Pᵀ
Laplacian relabels too:      L(P A Pᵀ) = P L(A) Pᵀ
eigenvalues unchanged:       Spec(L(P A Pᵀ)) = Spec(L(A))
eigenvectors relabel:        Φ(P A Pᵀ) = P Φ(A)     (up to ± sign)
```

The nodes carry no intrinsic order, so any computation that answers the
same question after relabeling is safe to store. Eigenvalues are the
per-region summary (one row each); eigenvector rows are the per-node
coordinates; and the runtime score `⟨w, φ(node)⟩` is an inner product over
those coordinates, so it comes out the same however the nodes were
numbered. This is also why drift needs no node alignment:
`‖λ_t − λ_{t−1}‖₂` compares two spectra directly, and both snapshots
live in the same order-free space.

### 4.3 Why cross-subgraph transport is checkable

The functional-map objective is a spectrum-consistency condition:

```
C_{A→B} = argmin_C  ‖F_B − F_A·Cᵀ‖²  +  λ‖CΛ_A − Λ_B C‖²
                           fit              spectrum match
```

If the second term were exactly zero, `C Λ_A = Λ_B C` would say the
transport matrix maps one eigenbasis onto the other without disturbing
the spectra — `C` carries each eigenvalue's subspace to the matching one.
The residual `commutativity_residual = ‖CΛ_A − Λ_B C‖_F / ‖Λ_A‖_F`
measures how far `C` is from doing that exactly — a number, not a
judgment.

The two residuals are the quality certificates: `fit_residual` answers
"does `C` map the shared descriptors correctly?", `commutativity_residual`
answers "does `C` respect the spectra?". Both are stored with the
transport matrix and both are gates, not vibes.

### 4.4 Sign alignment before comparison

An eigenvector is defined only up to sign: if `Lφ = λφ` then `L(−φ) =
λ(−φ)`. Comparing eigenvectors across snapshots without aligning signs
compares arbitrary choices — the two runs picked opposite but equally
valid representatives.

The drift writer aligns signs per component:

```
dots_k = Σ_nodes φ_from[:,k]·φ_to[:,k]      (per component k, over shared nodes)
sign_k = −1 if dots_k < 0                   (flip the "to" basis)
```

`dots_k < 0` means the two runs chose opposite signs for the same
component; flipping one puts them on the same footing. The hardening plan
Phase 2 upgrades the per-component sign fix to block-wise orthogonal
Procrustes, the same move for a whole degenerate block at once.

### 4.5 Worked example: sign alignment with real numbers

Take one loading row from the Factor Table Math Reference's own example,
`φ_from = [0.44, −0.31, 0.52]`, and a second snapshot's row that is the
same vector with the sign flipped, `φ_to = [−0.43, 0.30, −0.51]`.

```
dots_k = 0.44·(−0.43) + (−0.31)·0.30 + 0.52·(−0.51)
       = −0.1892 − 0.0930 − 0.2652 = −0.5474 < 0
⇒ sign_k = −1, aligned φ_to = [0.43, −0.30, 0.51]

loading_drift = 1 − ⟨a,b⟩/(‖a‖‖b‖)
  ⟨a,b⟩       = 0.44·0.43 + 0.31·0.30 + 0.52·0.51 = 0.5474
  ‖a‖         = √(0.1936 + 0.0961 + 0.2704) = 0.7484
  ‖b‖         = √(0.1849 + 0.0900 + 0.2601) = 0.7314
  loading_drift = 1 − 0.5474/(0.7484 × 0.7314) ≈ 1 − 1.000 = 0.000
```

The two rows are identical up to sign, so after alignment the drift is
zero. The check works: the sign choice was the only difference.

Now a genuinely different row, `φ_to = [0.10, 0.20, −0.35]`:

```
dots_k = 0.44·0.10 + (−0.31)·0.20 + 0.52·(−0.35)
       = 0.044 − 0.062 − 0.182 = −0.200 < 0
⇒ sign_k = −1, aligned φ_to = [−0.10, −0.20, 0.35]

  ⟨a,b⟩ = −0.044 + 0.062 + 0.182 = 0.2000
  ‖b‖   = √(0.0100 + 0.0400 + 0.1225) = 0.4153
  loading_drift = 1 − 0.2000/(0.7484 × 0.4153) = 1 − 0.6435 = 0.357
```

Real drift of 0.36 survives the sign alignment. Zero stays zero, real
change stays real: that is what a checkable property buys.

### 4.6 Which properties are load-bearing

The honest boundary between math the design relies on and math that is
merely true.

**Load-bearing (the property is why the design works):**
- Eigenvalues don't depend on node order — the drift comparison relies on
  it.
- `CΛ_A ≈ Λ_B C` — the transport residual is a measurable quality gate.
- Eigenvector sign — comparison without sign alignment is undefined, and
  the code aligns it.
- Cosine / L2 — rotating the embedding space leaves them unchanged, which
  is what makes scores comparable.
- Sliced Wasserstein — unchanged under translation and rotation, which is
  what makes it a well-defined distance between embedding distributions.

**True but incidental (not why the formula was chosen):**
- TF-IDF statistics ignore word order — but the parser was built as a
  classifier, not because of that.
- IDW / RBF kernels depend only on distance — chosen for distance-decay,
  not for that property.
- NFKD output is deterministic under Unicode normalization — the point is
  determinism.
- KV cache arithmetic is multiplication — nothing deeper doing work.

The test for the load-bearing column: remove the property and the formula
stops being well-defined (sign comparison, cross-basis transport,
snapshot drift, cosine ranking). For the incidental column: the formula
still works.

### 4.7 What this means for the pipeline

- Eigenvalues being order-free is why spectral methods are
  coordinate-free: the factor tables store the summary plus per-node
  coordinates, and the runtime path never re-reads the graph.
- The transport residual is why cross-subgraph transport is checkable:
  disagreement between compute tiers is a number, not a judgment.
- Sign alignment is why temporal comparisons are well-defined; hardening
  Phase 2 extends the per-component fix to whole degenerate blocks.

---

## 5. Checks to protect

Checkable statements derived from both parts.

```
Part 1:
  λ ∈ [0, 2], sorted ascending, non-negative            (normalized Laplacian)
  ΦᵀΦ ≈ I                                               (orthonormality)
  score = <w, φ> computed with <#> (negative IP)        (ORDER BY ASC = rank by score)
  loading_drift, fiedler_drift ∈ [0, 2]                 (cosine distance)
  modularity Q ∈ [−0.5, 1]
  tr(sᵀLs) ≥ 0; = 0 for constant signal on connected graph
  zero-padding to 128 is shared padding                 (inner products stay valid)
  normalized = (geo + name + class) / 3.0 ∈ [0, 1]      (USLP)
  F ∈ [0, 1], 1 = identical                             (freshness)

Part 2:
  Spec(P A Pᵀ) = Spec(A) for every permutation P        (eigenvalues are order-free)
  L(P A Pᵀ) = P L(A) Pᵀ                                 (Laplacian follows relabeling)
  C Λ_A = Λ_B C  ⇒  transport preserves the spectra     (functional map)
  eigenvector signs aligned before comparison            (drift)
  cosine and W₂ unchanged under rotation/translation     (embedding comparisons)
```

---

## 6. Sources

| Formula base section | Source doc |
|---|---|
| 3.1 | `../geovectors_encoder/two_axis_vs_single_axis_encoding.md`, `../geovectors_encoder/balltree_pickle_generation.md` |
| 3.2 | `../geovectors_encoder/vector_storage_partitioning.md`, `../worldkg_nca/gis_vector_metrics.md` |
| 3.3.1-3.3.8 | `../semantic_search/factor_table_math_reference.md`, `../semantic_search/graph_spectral_drift.md` |
| 3.3.9 | `../semantic_search/spectral_filtering_chebyshev_vs_lanczos.md` |
| 3.3.10 | `../semantic_search/mapqa_geographic_scoring_and_routing.md`, `../semantic_search/mapqa_parser_executor.md` |
| 3.3.11 | `../semantic_search/external_concepts_dag_visualization.md` |
| 3.4 | `../igea/uslp_spatial_link_prediction.md`, `../igea/igea_iterative_alignment.md` |
| 3.5 | `../semantic_search/deterministic_cross_script_romanization.md` |
| 3.6 | `../platform/llm_capacity_envelope.md` |
