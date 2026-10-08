# Mathematical Formula Reference: The Formula Base and the Group-Theory Layer

> **Status**: new (2026-09-19)
>
> **Purpose**: one pass over every formula in `docs/Schematics/`. Part 1 is
> the formula base: every formula the schematics document, organized by
> source doc, byte-identical to the originals. Part 2 is the
> group-theoretic reading: for each formula family, the group acting, the
> invariant preserved, the equivariance statement, with symbols and
> explanation.
>
> **Why**: the pipeline is a chain of structure-preserving maps (OSM entity
> → k-NN graph → eigenbasis → factor tables → runtime inner product). Each
> stage preserves something under a transformation group. Part 1 makes the
> formulas checkable in one place. Part 2 names the structure-preservation
> pattern, which is what the formulas have in common.
>
> **Companion docs**:
> - `docs/Schematics/THE_GAUGE_AS_CONTRACT.md`, the gauge frame for the
>   spectral/transport layer (rotors, motors, commutativity as contract)
> - `docs/Schematics/05_Learned_Layer/09_Factor_Table_Math_Reference.md`,
>   the math the factor tables implement
> - `docs/plans/later-stages/MATHEMATICAL_PATTERN_LIBRARY_SEED_CARDS.md`,
>   the invariant-first pattern library

---

## 1. Intent (decision in one line)

Part 1 collects the formulas so the math is checkable in one place. Part 2
names the group that acts at each stage and the invariant that survives,
so the "structure-preserving" property is stated, not felt.

---

## 2. Notation

Shared symbols for both parts. Part 2 defines the group-theoretic terms in
§4.1.

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
| `S_n` | symmetric group on n labels |
| `O(d)`, `SO(d)` | orthogonal group, rotation group in d dimensions |
| `E(d)` | Euclidean group, `R^d ⋊ O(d)` |
| `Z₂` | the two-element group `{+1, −1}` |
| `Spin(n)` | double cover of `SO(n)` (rotors) |
| `G/H` | homogeneous space (orbits of `H` in `G`) |
| `Spec(L)` | eigenvalue multiset of `L` |

---

## 3. Part 1: The Formula Base

Every formula below is quoted from its source schematic. Formula text is
byte-identical to the original; the Source column names the doc and
section.

### 3.1 Encoder: `01_Encoder/`

#### 3.1.1 GV-Tags: token centroid + L2 normalization
[01_Two_Axis_vs_Single_Axis_Encoding.md §3, §8](01_Encoder/01_Two_Axis_vs_Single_Axis_Encoding.md)

| Formula | Meaning |
|---|---|
| `e = np.mean(vectors, axis=0)` | unweighted arithmetic mean (centroid) of per-token FastText vectors, equal weight per token |
| `ê = e / np.linalg.norm(e)` | in-tree L2 normalization, norm ≈ 1.0, so cosine = dot product (required by `vector_cosine_ops`) |
| `static_embedding (400D) = concatenate(gv_tags_embedding(300D), gv_nle_embedding(100D))` | fused embedding, only populated when both axes present |
| `cosine distance ∈ [0, 2]`, `similarity = 1 − distance` | pgvector cosine semantics |

#### 3.1.2 The two IDW formulas (do not conflate)
[03_BallTree_Pickle_Generation.md §4](01_Encoder/03_BallTree_Pickle_Generation.md)

| Formula | Role |
|---|---|
| `w'(d) = max(1/ln(max(d, 1.1)), e)` | training formula (DeepWalk transition weights, graph construction, inductive `_damped_weight`). `e ≈ 2.718` floor keeps the transition matrix connected |
| `dist = np.log(1 + (1 / (n[1] / 1000)))` | inference formula (`NLEModel.encode_coords`, GeoVectors paper §3.4), `n[1]` = meters → km |
| `enc = np.sum(vectors, axis=0) / dist_sum` | normalized weighted mean of the 50 nearest GV-NLE vectors |
| `distances_km = distances_rad × 6371.0` | haversine radians → km |

### 3.2 Postgres: `03_Postgres/`

[01_Vector_Storage_and_Partitioning.md](03_Postgres/01_Vector_Storage_and_Partitioning.md),
[03_GIS_Vector_Metrics.md §3.4](03_Postgres/03_GIS_Vector_Metrics.md)

| Formula | Meaning |
|---|---|
| GiST bbox `O(log N)`, GiST KNN `O(log N + k)` | spatial index bounds |
| HNSW `O(log N)` per query, ~150 bytes/row (m=16 pointers) | ANN graph structure, vectors live in the heap |
| `cosine distance ∈ [0, 2]`, `similarity = 1 − distance` | vector similarity semantics |

### 3.3 Learned Layer: `05_Learned_Layer/`

#### 3.3.1 The k-NN graph and its weights
[09_Factor_Table_Math_Reference.md §2](05_Learned_Layer/09_Factor_Table_Math_Reference.md)

| Formula | Meaning |
|---|---|
| `k = 50` | neighbors per node (GeoVectors paper §3.2) |
| `w'(d) = max( 1 / ln( max(d_km, 1.1) ),  e )` | log-damped inverse distance, floor at Euler's number ≈ 2.718 |
| asymmetric → undirected via max-weight canonicalization | `(i, j)` dedup keeps `max(w_ij, w_ji)` (NetworkX semantics) |
| k-NN memory: `N × k` edges | `O(N × k)`; 400M entities × 50 = 20B edges ≈ 800 GB, the reason for subdivision fan-out |

#### 3.3.2 Laplacians and eigendecomposition
[09_Factor_Table_Math_Reference.md §3](05_Learned_Layer/09_Factor_Table_Math_Reference.md),
[04_Graph_Spectral_Drift.md §2.2](05_Learned_Layer/04_Graph_Spectral_Drift.md)

| Formula | Meaning |
|---|---|
| `L_norm = I − D^{−1/2} W D^{−1/2}` | normalized Laplacian, eigenvalues `λ ∈ [0, 2]`, spectral fingerprint |
| `L_comb = D − W` | combinatorial Laplacian, Dirichlet energy, heat kernel |
| `L Φ = Φ Λ,  ΦᵀΦ = I` | orthonormal eigendecomposition; drop trivial `λ₀ = 0`, keep `λ₁..λ_K` ascending |
| `λ₂` = algebraic connectivity (Fiedler value) | how well-connected the graph is |
| `λ_K − λ₂` | "spectral gap" (a spread; hardening Phase 7 renames) |
| `φ₂` = Fiedler vector | the slowest mode, a smooth coordinate over the graph |

#### 3.3.3 Graph signals and diffusion
[09_Factor_Table_Math_Reference.md §3.3](05_Learned_Layer/09_Factor_Table_Math_Reference.md),
[04_Graph_Spectral_Drift.md §2.3, §2.4](05_Learned_Layer/04_Graph_Spectral_Drift.md)

| Formula | Meaning |
|---|---|
| `s(node) = class index of wkg_class(node)` | scalar class signal per node |
| `sᵀLs  = Σ_edges w_ij (s_i − s_j)²` | Dirichlet energy (combinatorial L, PSD ⇒ ≥ 0); low = classes cluster spatially |
| `sᵢ(Ls)ᵢ` | per-node contribution, stored as `dirichlet_contrib` |
| `(L + μI)⁻¹ s` | regularized inverse diffusion, `μ > 0` |
| `u(t) = e^{−tL} · δ_source` | heat kernel diffusion from a source; mass-conserving `Σᵢ u(t)ᵢ = 1` |

#### 3.3.4 The runtime factorization: heat kernel as one pgvector query
[09_Factor_Table_Math_Reference.md §4](05_Learned_Layer/09_Factor_Table_Math_Reference.md)

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
[09_Factor_Table_Math_Reference.md §5.1](05_Learned_Layer/09_Factor_Table_Math_Reference.md)

| Formula | Meaning |
|---|---|
| `Q = (1/2m) Σ_ij [A_ij − d_i d_j / 2m] δ(c_i, c_j)` | greedy modularity maximization; `δ` = 1 when nodes share a community; `Q ∈ [−0.5, 1]` |

#### 3.3.6 Cross-subgraph transport (functional maps)
[09_Factor_Table_Math_Reference.md §7](05_Learned_Layer/09_Factor_Table_Math_Reference.md)

| Formula | Meaning |
|---|---|
| `C_{A→B} = argmin_C  ‖F_B − F_A·Cᵀ‖²  +  λ‖CΛ_A − Λ_B C‖²` | descriptor term + Laplacian commutativity term; `F_A, F_B` = shared buffer-zone entities' loadings (`m × k`) |
| `Cᵀ = lstsq(F_A, F_B)   →  C = F_Bᵀ F_A (F_Aᵀ F_A)⁻¹` | descriptor OLS |
| `‖CΛ_A − Λ_B C‖² = Σ_{i,j} (λ_a[i] − λ_b[j])² · C[i,j]²` | commutativity term is diagonal in `vec(C)` |
| `(F_AᵀF_A + λ·diag((λ_a[i] − λ_b[j])²)) · c_j = (F_AᵀF_B)[:, j]` | regularized solve = k independent k×k systems |
| `fit_residual = ‖F_B − F_A Cᵀ‖_F / ‖F_B‖_F` | descriptor quality certificate |
| `commutativity_residual = ‖CΛ_A − Λ_B C‖_F / ‖Λ_A‖_F` | geometry-respect quality certificate; min 16 shared entities or the pair is skipped |

#### 3.3.7 Temporal drift (Step 5d)
[09_Factor_Table_Math_Reference.md §8](05_Learned_Layer/09_Factor_Table_Math_Reference.md),
[04_Graph_Spectral_Drift.md §3.1, §3.2](05_Learned_Layer/04_Graph_Spectral_Drift.md)

| Formula | Meaning |
|---|---|
| `spectral_distance = ‖λ_t − λ_{t−1}‖₂` | eigenvalue L2 difference; eigenvalues are graph invariants, no node alignment |
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
[04_Graph_Spectral_Drift.md §4](05_Learned_Layer/04_Graph_Spectral_Drift.md)

| Formula | Meaning |
|---|---|
| `p_r = v_r · x` | project embeddings to 1D with random unit vectors; 100 projections, seed 42 |
| 1D Wasserstein (Earth Mover's Distance) per projection, averaged | approximates true W₂ in high dimensions |
| `F = α · exp(-λ_sem · W_sem) + (1-α) · exp(-λ_spat · W_spat)` | freshness score; `α = 0.5`, `λ_sem = λ_spat = 5.0`; `F ∈ [0, 1]`, 1 = identical |

#### 3.3.9 Spectral filtering: Chebyshev vs Lanczos
[06_Spectral_Filtering_Chebyshev_vs_Lanczos.md §1, §2](05_Learned_Layer/06_Spectral_Filtering_Chebyshev_vs_Lanczos.md)

| Formula | Meaning |
|---|---|
| `f(L) · x = U · f(Λ) · Uᵀ · x` | filtering a signal by the Laplacian = multiplying its spectral components by `f(λ)` |
| `f(L) ≈ Σₖ cₖ Tₖ(L̃)` | polynomial approximation, no eigenvectors needed |
| `L̃ = 2L/λ_max - I` | scaled Laplacian, eigenvalues in [−1, 1] |
| `T₀(L̃) x = x`, `T₁(L̃) x = L̃ x`, `Tₖ(L̃) x = 2 L̃ T_{k-1}(L̃) x - T_{k-2}(L̃) x` | three-term recursion, sparse mat-vec only |
| `α = t · λ_max / 2`, `f(λ) = exp(-tλ)` | heat-kernel filter coefficients; `λ_max ≈ 1.30` for IE (not bipartite) |

#### 3.3.10 MapQA geographic scoring and routing
[07_MapQA_Geographic_Scoring_and_Routing.md §1](05_Learned_Layer/07_MapQA_Geographic_Scoring_and_Routing.md),
[01_MapQA_Parser_Executor.md §3](05_Learned_Layer/01_MapQA_Parser_Executor.md)

| Formula | Meaning |
|---|---|
| `geo_score = 1 - d_cluster / d_max` | USLP geographic score (Mann et al. 2023 §3.3); P4 geohash (~39 km cells) default, raw haversine when the user gives an exact radius |
| `combined_score = diffusion_score + name_score + geo_score + uslp_boost` | re-rank; `name_score` = FastText cosine between the OBJECT span and entity `name` (proper-name queries only, else 0.0); USLP signal boost `+0.5` for predicted link tails (normalized score ≥ 0.7) |
| tag match boost `+1.0` | exact tag value match ranks above semantic near-match |

#### 3.3.11 Visualization kernels
[05_EXTERNAL_CONCEPTS_DAG_VISUALIZATION.md §4](05_Learned_Layer/05_EXTERNAL_CONCEPTS_DAG_VISUALIZATION.md)

| Formula | Meaning |
|---|---|
| `ŝ(x) = Σ_i w_i(x) · z_i ,   w_i(x) = 1 / d(x, x_i)^p` | generic IDW interpolation (the post's form of the pipeline's IDW) |
| `K(x, x') = exp(-||x - x'||² / 2σ²)` | Gaussian RBF kernel |
| `ρ(x) = Σ_i K(x, x_i)` | RBF density surface over result entity positions |

### 3.4 IGEA + USLP: `06_IGEA_USLP/`

#### 3.4.1 USLP tri-space scoring
[02_USLP_Spatial_Link_Prediction.md §3](06_IGEA_USLP/02_USLP_Spatial_Link_Prediction.md)

| Formula | Meaning |
|---|---|
| `geo_sim = 1 - d / d_max` | geographic; `d` = haversine(head, tail), `d_max` = per-cluster max; `[0, 1]` |
| `name_sim = cosine(FastText(literal), FastText(candidate_name))` | semantic name similarity; `[−1, 1]` |
| `class_sim = cosine(FastText(relation_text), FastText(candidate_class))` | ontological compatibility; `[−1, 1]` |
| `unnormalized = geo + name + class` | `[0, 3.0]` |
| `normalized = unnormalized / 3.0` | `[0, 1.0]`; acceptance threshold `0.7` |
| geohash radius: precision 1 → ~5000 km, 3 → ~156 km, 4 → ~39 km | relation-specific search radius |

#### 3.4.2 IGEA alignment
[01_IGEA_Iterative_Alignment.md §3, §7](06_IGEA_USLP/01_IGEA_Iterative_Alignment.md)

| Formula | Meaning |
|---|---|
| `score = cross_attention_model.predict(osm, wikidata)` | per-country cross-attention BiLSTM; fallback `cosine(gv_tags_embedding, candidate_emb)` |
| threshold `0.6`, max distance `2500 m`, max 3 iterations | acceptance, pair filter, loop bound |

### 3.5 Romanizer: `07_Romanizer/`

[01_Deterministic_Cross_Script_Romanization.md §5.1](07_Romanizer/01_Deterministic_Cross_Script_Romanization.md)

| Formula | Meaning |
|---|---|
| NFKD decomposition + strip combining marks (category Mn) | deterministic diacritic stripping |
| `S_xscript = n-gram Jaccard similarity("paris bagueete", "paribagetteu")` | pg_trgm trigram similarity, e.g. 0.38 |
| `S_total = S_name + S_xscript + S_geo + S_class` | combined score, parallel FastText + romanizer paths |

### 3.6 Agent / MCP / LLM: `08_Agent_MCP_LLM/`

[05_LLM_Capacity_Envelope.md §2](08_Agent_MCP_LLM/05_LLM_Capacity_Envelope.md)

| Formula | Meaning |
|---|---|
| `fp16 KV = 2 × 36 layers × 8 KV heads × 128 head_dim × 2 bytes ≈ 144 KiB/token` | KV cache per token; 4096-token slot ≈ 0.55 GB |

### 3.7 The Gauge: `THE_GAUGE_AS_CONTRACT.md`

[THE_GAUGE_AS_CONTRACT.md §2, §3](THE_GAUGE_AS_CONTRACT.md)

| Formula | Meaning |
|---|---|
| `AB = A·B + A∧B` | geometric product = inner (projection) + outer (extension) |
| `R = exp(B/2)` | rotor: a bivector exponentiated, a rotation |
| `M = R(1 + εt)` | motor: rotor × translation, rigid body motion |
| `P = A(AᵀA)⁻¹Aᵀ` → `(v·B)B⁻¹`, `B₁ ∨ B₂` | projection in flat phase vs GA (plane as bivector), regressive product for plane intersection |
| `f = A ∘ σ ∘ B_J ∘ P_{J-1} ∘ ... ∘ P_1 ∘ σ_1 ∘ B_1` | the GDL blueprint: equivariant operator, nonlinearity, coarsening, invariant readout |
| `f(g·x) = f(x)` invariant; `f(g·x) = g·f(x)` equivariant | the two symmetry properties |
| `B(R·f) = R·B(f)`; `A(R·f) = A(f)` | gauge-equivariant filter, gauge-invariant readout |
| `M → RMR⁻¹` | change of frame = rotor conjugation |
| `C = Φ_Bᵀ S Φ_A` | transport operator as the gauge transformation between chart bases |
| `‖CΛ_A - Λ_B C‖ < tol` | the contract check (Seed 4, commutativity residual) |

---

## 4. Part 2: The Group-Theory Layer

Every formula family above is a structure-preserving map: something acts,
something survives. Part 2 names both, with symbols and notation.

### 4.1 The vocabulary

| Symbol | Meaning | Pipeline instance |
|---|---|---|
| `G` | a group: associative product, identity, inverses | `S_n`, `O(3)`, `Z₂`, `Spin(n)`, `E(d)` |
| `g ∈ G` | a group element | a relabeling, a rotation, a sign flip |
| `g·x` | the action of `g` on a point `x` | relabeling a graph, rotating the sphere, flipping an eigenvector |
| `G·x = {g·x : g ∈ G}` | the orbit of `x` | all relabelings of one graph = one isomorphism class |
| `f(g·x) = f(x)` | invariance | `Spec(P A Pᵀ) = Spec(A)` |
| `f(g·x) = g·f(x)` | equivariance (the map commutes with the action) | `L(P A Pᵀ) = P L(A) Pᵀ` |
| `ρ: G → GL(V)` | representation: group elements as linear maps | permutation matrices, rotation matrices, `±1` |
| `T∘ρ₁(g) = ρ₂(g)∘T` | intertwinor: equivariant map between representations | `C Λ_A = Λ_B C` |
| `S_n` | symmetric group on n labels | node relabeling |
| `O(d)`, `SO(d)` | orthogonal group, rotation group | the embedding space's symmetries |
| `E(d) = R^d ⋊ O(d)` | Euclidean group, translations ⋊ rotations | the isometry group of embedding space |
| `Z₂` | `{+1, −1}` | eigenvector sign freedom |
| `Spin(n)` | double cover of `SO(n)` | rotors |
| `G/H` | homogeneous space, orbits of `H` in `G` | `S^{d−1} = O(d)/O(d−1)`, the unit sphere embeddings live on |

The through-line: an equivariant map is a commutation diagram. `f(g·x) =
g·f(x)` says the square commutes. Every "structure-preserving" property in
this repo is one of these squares.

### 4.2 The master table

For each formula family: the group acting, what survives, the equivariance
statement, and the code home. "Home" is the schematic's Source file.

| Formula family | Group | Action | Invariant / equivariance | Home |
|---|---|---|---|---|
| Token centroid (GV-Tags) | `S_T` (token order) | permute tokens | the mean is unchanged | `embedding_service.py` |
| L2 normalization | scale `R_{>0}` | `x → x/‖x‖` | quotient: embeddings live on `S^{d−1} = O(d)/O(d−1)` | `FastTextModel.encode_tags()` |
| Cosine similarity | `O(d)` | rotate both vectors | score unchanged: `f(gx, gy) = f(x, y)` | search, USLP name/class |
| Haversine distance | `O(3)` | rotate the sphere | geodesic distance preserved: `d(g·p, g·q) = d(p, q)` | k-NN, USLP geo, executor |
| IDW weights | `O(3)` | rotate the sphere | weights are radial, depend on `d` only | `graph.py`, `_damped_weight` |
| k-NN graph build | `S_n` | relabel nodes | `A → P A Pᵀ`; `L(P A Pᵀ) = P L(A) Pᵀ` | `knn_graph_service.py` |
| Laplacian spectrum | `S_n` | relabel nodes | `Spec(L)` invariant; eigenvectors permute: `Φ(P A Pᵀ) = P Φ(A)` | `spectral_analysis_service.py` |
| Eigenvector sign | `Z₂` | `φ → −φ` | sign-aligned comparisons are gauge-fixed | `factor_node_writer.py` |
| Heat kernel | `S_n` | relabel nodes | `e^{−tL}` equivariant; the score `⟨w, φ(node)⟩` is invariant | `factor_resolution_service.py` |
| Fiedler value, gap | `S_n` | relabel nodes | `λ₂`, `λ_K − λ₂` invariant | fingerprint rows |
| Dirichlet energy | `S_n` | relabel graph and signal jointly | `sᵀLs` invariant | `graph_signal_service.py` |
| Louvain modularity | `S_n × S_C` | relabel nodes, renumber communities | `Q` invariant | `community_detection_service.py` |
| Functional map | the two manifolds' symmetry | transport between eigenbases | `C Λ_A ≈ Λ_B C`: `C` is an approximate intertwinor | `subgraph_transport_service.py` |
| Spectral drift | `S_n` | relabel nodes | `‖λ_t − λ_{t−1}‖₂` needs no node alignment | `spectral_drift_service.py` |
| Loading drift | `O(k)` | rotate the loading space | cosine distance `1 − ⟨a,b⟩/(‖a‖‖b‖)` invariant | `spectral_drift_service.py` |
| Sliced Wasserstein | `E(d)` | translate + rotate | `W₂(g·X, g·Y) = W₂(X, Y)` | `embedding_drift_service.py` |
| Chebyshev filter | `S_n` | relabel nodes | `f(L)x` equivariant: `f(P A Pᵀ)(P x) = P f(L) x` | spectral filtering layer |
| USLP geo score | `O(3)` | rotate the sphere | `1 − d/d_max` invariant | `spatial_link_prediction.py` |
| GA rotor / motor | `Spin(n)`, `E(3)` | conjugate by `R` | grade preserved; `R(AB)R̃ = (RAR̃)(RBR̃)` | `THE_GAUGE_AS_CONTRACT.md` |
| MapQA bag-of-words | `S_T` (token order) | permute tokens | TF-IDF statistics unchanged | `query_parser_service.py` |

### 4.3 Structural invariant 1: the symmetric group and the spectrum

The pipeline's deepest structural claim is that the eigenvalue map is the
canonical invariant of the relabeling group.

```
Permutation matrix P ∈ S_n acts:        A → P A Pᵀ
Laplacian is equivariant:                L(P A Pᵀ) = P L(A) Pᵀ
Eigenvalues are invariant:               Spec(L(P A Pᵀ)) = Spec(L(A))
Eigenvectors are equivariant:            Φ(P A Pᵀ) = P Φ(A)     (up to Z₂ sign)
```

The relabeling group is the largest symmetry any graph has: the nodes carry
no intrinsic order, so the graph is the orbit `S_n · A`, and every
computation that answers the same question after relabeling is
`S_n`-equivariant by construction.

Why the factor tables work: eigenvalues are the invariant summary (one row
per region), eigenvector rows are the equivariant coordinates (one row per
node), and the runtime score `⟨w, φ(node)⟩` is computed in the relabeled
coordinates consistently, so the score is invariant. The design stores the
invariant and evaluates the equivariant operator as an inner product. This
is why drift needs no node alignment: `‖λ_t − λ_{t−1}‖₂` compares
invariants, and the two snapshots' spectra are in the same coordinate-free
space by construction.

### 4.4 Structural invariant 2: the intertwinor condition

The functional-map objective is the equivariance condition in disguise.

```
C_{A→B} = argmin_C  ‖F_B − F_A·Cᵀ‖²  +  λ‖CΛ_A − Λ_B C‖²
                           fit                commutation
```

If the commutativity term were exactly zero, `C Λ_A = Λ_B C` would be the
representation-theoretic definition of an intertwinor: a map `T` such that
`T∘ρ₁(g) = ρ₂(g)∘T` for every group element `g`. Here the role of the group
action is played by the Laplacian's spectrum: `Λ_A` and `Λ_B` are the
"diagonalized actions" of the two graphs' symmetries, and `C` is the map
that carries one eigenbasis onto the other without breaking the geometry.

The residual is the equivariance defect. `commutativity_residual =
‖CΛ_A − Λ_B C‖_F / ‖Λ_A‖_F` is a number, not a judgment: it measures how
far `C` is from being a true equivariant map, which is exactly what
`THE_GAUGE_AS_CONTRACT.md` calls the gauge contract between compute tiers.

The two residuals are the quality certificates: `fit_residual` answers
"does C map the shared descriptors correctly?", `commutativity_residual`
answers "does C respect the geometry?". Both are stored with the transport
matrix and both are gates, not vibes.

### 4.5 Structural invariant 3: the Z₂ gauge and sign alignment

An eigenvector is defined only up to sign: if `Lφ = λφ` then `L(−φ) =
λ(−φ)`. The sign choice is a gauge, the group is `Z₂`, and comparing
eigenvectors across snapshots or bases without fixing the gauge compares
arbitrary representatives.

The drift writer fixes the gauge per component:

```
dots_k = Σ_nodes φ_from[:,k]·φ_to[:,k]      (per component k, over shared nodes)
sign_k = −1 if dots_k < 0                   (flip the "to" basis)
```

`dots_k < 0` means the two bases chose opposite representatives of the
same `Z₂` orbit; flipping one restores a common representative. This is
gauge fixing: choose one canonical frame per orbit, then compare. The
hardening plan Phase 2 upgrades the per-component `Z₂` fix to block-wise
orthogonal Procrustes, which is the same move for a larger gauge group
(`O(k)` per degenerate block).

### 4.6 Worked example: sign alignment with real numbers

Take one loading row from the Factor Table Math Reference's own example,
`φ_from = [0.44, −0.31, 0.52]`, and a second snapshot's row that is the
same vector up to the `Z₂` action, `φ_to = [−0.43, 0.30, −0.51]`.

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

The two rows are identical up to the `Z₂` action, so after gauge fixing
the drift is zero. The invariant check works: the gauge was the only
difference.

Now a genuinely different row, `φ_to = [0.10, 0.20, −0.35]`:

```
dots_k = 0.44·0.10 + (−0.31)·0.20 + 0.52·(−0.35)
       = 0.044 − 0.062 − 0.182 = −0.200 < 0
⇒ sign_k = −1, aligned φ_to = [−0.10, −0.20, 0.35]

  ⟨a,b⟩ = −0.044 + 0.062 + 0.182 = 0.2000
  ‖b‖   = √(0.0100 + 0.0400 + 0.1225) = 0.4153
  loading_drift = 1 − 0.2000/(0.7484 × 0.4153) = 1 − 0.6435 = 0.357
```

Real drift of 0.36 survives the gauge fix. Zero stays zero, real change
stays real: that is what a checkable invariant buys.

### 4.7 Where the reading is real, and where it is not

The honest boundary, mirroring `THE_GAUGE_AS_CONTRACT.md` §7.

**Real (the structure-preservation is the content):**
- The spectrum under `S_n`: eigenvalues are genuinely the invariant summary
  of the relabeling group, and the drift comparison relies on it.
- The intertwinor condition: `CΛ_A ≈ Λ_B C` is literally an equivariance
  statement, and its residual is the equivariance defect.
- The `Z₂` sign gauge: eigenvector comparison without gauge fixing is
  undefined, and the code fixes it.
- Cosine / L2: `O(d)` and the unit sphere `S^{d−1} = O(d)/O(d−1)` are the
  exact symmetry structure of the embedding space.
- Sliced Wasserstein: `W₂` is genuinely `E(d)`-invariant (translation +
  rotation), which is why it is a well-defined distribution distance.
- Rotors / motors: `Spin(n)` and `E(3)` are the actual groups the GA layer
  represents, per `THE_GAUGE_AS_CONTRACT.md`.

**Decorative (true but not the reason the formula was chosen):**
- Bag-of-words statistics are `S_T`-invariant, but the parser was built as
  a TF-IDF classifier, not as a representation.
- Radial kernels (IDW, RBF) are `O(3)`-invariant by construction, but they
  were chosen for distance-decay, not symmetry.
- The Romanizer's NFKD output is invariant under Unicode normalization
  equivalence classes; the point is determinism, not symmetry.
- KV cache arithmetic is multiplication; no group structure is doing work.

The test for the real column: remove the symmetry and the formula stops
being well-defined (sign comparison, cross-basis transport, snapshot drift,
cosine ranking). The test for the decorative column: the formula still
works, the group was already baked into the construction.

### 4.8 What the three invariants say about the pipeline

- `S_n` invariance is why spectral methods are coordinate-free: the factor
  tables are the invariant summary plus the equivariant coordinates, and
  the runtime path never re-reads the graph.
- The intertwinor condition is why cross-subgraph transport is checkable:
  the residual is a gate, and disagreement between compute tiers is a
  number, not a judgment.
- The `Z₂` gauge is why temporal comparisons are well-defined: sign
  alignment is gauge fixing, and hardening Phase 2 widens the gauge group
  from `Z₂` to `O(k)`.

These are the same pattern the owner's intuition names: equivariance,
Clifford structure, and group action are three dialects of one invariant,
the commutation diagram `f(g·x) = g·f(x)`.

---

## 5. Invariants to protect

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
  Spec(P A Pᵀ) = Spec(A) for every permutation P       (S_n invariance)
  L(P A Pᵀ) = P L(A) Pᵀ                                 (Laplacian equivariance)
  C Λ_A = Λ_B C implies C is an intertwinor             (functional map)
  sign-aligned comparisons are gauge-fixed (Z₂)          (drift)
  cosine and W₂ are O(d) / E(d) invariant                (embedding comparisons)
```

---

## 6. Sources

| Formula base section | Source doc |
|---|---|
| 3.1 | `01_Encoder/01_Two_Axis_vs_Single_Axis_Encoding.md`, `01_Encoder/03_BallTree_Pickle_Generation.md` |
| 3.2 | `03_Postgres/01_Vector_Storage_and_Partitioning.md`, `03_Postgres/03_GIS_Vector_Metrics.md` |
| 3.3.1-3.3.8 | `05_Learned_Layer/09_Factor_Table_Math_Reference.md`, `05_Learned_Layer/04_Graph_Spectral_Drift.md` |
| 3.3.9 | `05_Learned_Layer/06_Spectral_Filtering_Chebyshev_vs_Lanczos.md` |
| 3.3.10 | `05_Learned_Layer/07_MapQA_Geographic_Scoring_and_Routing.md`, `05_Learned_Layer/01_MapQA_Parser_Executor.md` |
| 3.3.11 | `05_Learned_Layer/05_EXTERNAL_CONCEPTS_DAG_VISUALIZATION.md` |
| 3.4 | `06_IGEA_USLP/02_USLP_Spatial_Link_Prediction.md`, `06_IGEA_USLP/01_IGEA_Iterative_Alignment.md` |
| 3.5 | `07_Romanizer/01_Deterministic_Cross_Script_Romanization.md` |
| 3.6 | `08_Agent_MCP_LLM/05_LLM_Capacity_Envelope.md` |
| 3.7 | `THE_GAUGE_AS_CONTRACT.md` |
