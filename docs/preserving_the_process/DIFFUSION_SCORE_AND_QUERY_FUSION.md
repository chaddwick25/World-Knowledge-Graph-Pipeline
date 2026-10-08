# Diffusion Score and Query Fusion

> **Status**: new doc (2026-09-28). Records the provenance of the query-time
> fusion score, the diffusion term's math and implementation, and the
> Laplacian choice behind it. Complements `docs/issues/HNSW_RELEVANCE_AND_USLP_RERANK_DESIGN.md`
> (exact-search decision) and `docs/Schematics/05_Learned_Layer/09_Factor_Table_Math_Reference.md`
> (full formula reference with the worked heat-kernel example).

## 1. Provenance map

The runtime re-rank sums four terms:

```
combined_score = diffusion_score + name_score + geo_score + uslp_boost
```

| Term | Source | Paper-grounded | Ref |
|---|---|---|---|
| `geo_score` | USLP tri-space geographic component, template-aware `1 - d_cluster/d_max` | Yes, USLP paper (Mann et al. 2023 §3.3) | `docs/Schematics/06_IGEA_USLP/02_USLP_Spatial_Link_Prediction.md` §13.3 |
| `uslp_boost` | +0.5 for entities that are predicted USLP link tails from the anchor | Yes, USLP paper | same |
| `diffusion_score` | graph heat kernel `e^{-tL} δ_anchor` in the Laplacian eigenbasis | No USLP paper. Spectral domain (Spectral Maps paper, learned layer) | this doc, §3-4 |
| `name_score` | FastText cosine between the OBJECT span and each entity's `name`, clamped [0,1]; 0.0 for nameless entities and for category queries (no `name_query`) | No paper. Deterministic query-time computation | this doc, §6 |
| `combined_score` (the fusion) | the pipeline's own linear combination | No paper | this doc, §6 |

The reason to record this: the fusion is the pipeline's own design, and the
diffusion term is the least documented piece of it.

## 2. The graph and the Laplacian choice (Step 5c, batch time)

Step 5c builds a k-NN graph from entity locations (haversine distance,
k=50, log-inverse-distance edge weights `w = max(1/ln(max(d_km, 1.1)), e)`),
then computes the Laplacian eigendecomposition.

**The stored factorization uses the normalized Laplacian:**

```
L = I - D^{-1/2} W D^{-1/2}        [GRAPH_REP:Eq 3.2]
```

- All eigenvalues ∈ [0, 2].
- Kept: the k smallest eigenvalues, k=128 (`EIGEN_LOADING_DIM = 128`,
  `worldkg_nca/models.py:329`), or k=64 for large countries
  (`step_5c_graph_spectral/__init__.py:76`).
- Solvers: GPU `torch.lobpcg`, CPU shift-invert randomized SVD, or
  `scipy.sparse.linalg.eigsh`, auto-selected by graph size and GPU
  availability (`spectral_analysis_service.py:254-261`).

**The combinatorial Laplacian appears in only one place: the graph-path
heat kernel.** `compute_heat_kernel` uses `L = D - W`
(`spectral_analysis_service.py:297`), the mass-preserving choice, with
documented invariants: conservation `Σᵢ u(t)ᵢ = 1`, non-negativity,
identity `u(0) = δ_source`, steady state uniform on the connected
component (`:274-278`). That path (`expm_multiply(-tL, δ)`, O(N) per time
step) serves event diffusion at multiple `t` values. It is not the path
that feeds `combined_score`.

**The invariant difference matters.** The two kernels are different
operators. The normalized kernel does not conserve mass and converges to a
degree-weighted steady state (amplitude ∝ √d_i); the combinatorial kernel
conserves mass and converges to uniform. Any invariant statement about the
factor-table path must use the normalized-kernel facts, not the
combinatorial docstring.

**Eigenvalue as frequency.** The eigenvalue is the mode's "price": the
Dirichlet energy of a graph signal s decomposes as
`sᵀLs = Σ_k λ_k (φ_kᵀs)²`. A unit-amplitude mode k carries energy λ_k.
Keeping the bottom-k eigenvalues keeps the smoothest modes; high-frequency
modes are numerically unstable and decay instantly in the heat kernel
(`e^{-tλ} → 0`).

## 3. The factorization (Step 5c, batch time)

`L = ΦΛΦᵀ`, and the pieces are stored separately:

- `φ_k(node)`, the node's loading on mode k: stored per row in
  `factor_spectral_node_metric.eigen_loadings` (128-dim).
- `λ_k`, the eigenvalues: stored per fingerprint in
  `GraphSpectralFingerprint`.
- Countries ≥ 5M nodes split into subdivisions with buffers (Option A,
  buffered graphs, §5); each subgraph gets its own eigenbasis.

Read-time coherence: `diffusion_rank` refuses to pair a row's loadings with
eigenvalues from a different fingerprint (phase-1 eigenbasis coherence
check, `factor_resolution_service.py:190-222`). A same-snapshot rerun that
failed between the two writes would otherwise pair old loadings with a new
fingerprint and produce wrong scores.

## 4. Runtime factorization of the score

The heat-kernel diffusion score from an anchor at time t:

```
score(node) = Σ_k φ_k(anchor) · e^{-t·λ_k} · φ_k(node)
              └─ excitation ─┘   └─ decay ─┘   └─ target ─┘
```

The move that makes it one query: group the anchor-dependent factors into a
single coefficient vector.

```
w_k = e^{-t·λ_k} · φ_k(anchor)         built once at query time
score(node) = wᵀ φ(node)               one inner product per candidate
```

Implementation (`factor_resolution_service.py`):

1. Look up the anchor's `eigen_loadings` and the fingerprint's eigenvalues.
2. Build `w` (coefficient vector, `:231-237`):
   `w[:k] = exp(-t · λ[:k]) · φ_anchor[:k]`, zero-padded to 128.
3. One pgvector inner-product query over candidate entities
   (`:335-341`): `score = -(eigen_loadings <#> w)`, ordered by descending
   score, filtered at `min_score = 1e-6`, trimmed to `limit` (default 20),
   anchor excluded.
4. Cross-subgraph transport when the candidate set spans subgraphs (§5).

The exponential decay is a diagonal filter in the eigenbasis, a low-pass
filter with cutoff ~1/t: small t keeps the high modes (local detail), large
t keeps only the low modes (global structure). The semigroup property
`H(t1)H(t2) = H(t1+t2)` means decay composes:
`w(t1+t2) = e^{-t2·Λ} · w(t1)`.

**Worked example, normalized Laplacian on a 3-node path A-B-C** (unit
edges, degrees 1-2-1). Eigenpairs: λ = 0, 1, 2 with
`φ1 = (0.5, 0.707, 0.5)`, `φ2 = (0.707, 0, -0.707)`,
`φ3 = (0.5, -0.707, 0.5)`. Anchor B, t = 1:

```
w = (e^0 · 0.707, e^{-1} · 0, e^{-2} · (-0.707)) = (0.707, 0, -0.0957)

score(A) = 0.707·0.5 + (-0.0957)·0.5        = 0.306
score(B) = 0.707·0.707 + (-0.0957)·(-0.707) = 0.568
score(C) = 0.306
```

The anchor keeps the most heat; the two symmetric neighbors tie. Mass sums
to 1.179, not 1: the normalized kernel does not conserve mass. As t → ∞
only the λ = 0 mode survives: `u(∞) = (0.354, 0.500, 0.354)`, with the
center-to-leaf ratio √2 = √(d_B/d_A): the degree-weighted steady state.
The combinatorial kernel on the same graph (λ = 0, 1, 3) would give
`(0.317, 0.367, 0.317)` and a uniform steady state: the Laplacian choice
changes the answer, which is why §2 records it.

## 5. Borders: buffer zones and transport

Cross-subgraph diffusion exists so border communities answer correctly.
Two mechanisms, both batch-time:

- **Subgraph buffers (Option A).** Each subdivision's k-NN graph is built
  from the subgraph polygon plus a buffer (`subgraph_spectral.py`,
  "full buffered" graph). Buffer-only entities are pruned at write time
  (`core_ids`), but they exist in the graph during the solve, so border
  communities straddling two subdivisions are represented in both
  eigenbases.
- **Transport matrices.** Entities present in both buffered graphs are the
  correspondence points for fitting a k×k functional-map matrix C per
  adjacent pair (`transport_matrices.py:22-23`). The fit is a regularized
  least squares over the shared buffer correspondences
  (`np.linalg.lstsq(F_A, F_B)`) with a Laplacian-commutativity
  regularization term (`subgraph_transport_service.py:124-141, 209`).

Runtime flow for a candidate set spanning subgraphs
(`factor_resolution_service.py:416-441`):

1. Transport the anchor's heat-kernel coefficients into the target
   eigenbasis: `w_transport = transport_loadings(w_anchor[:k], C)`.
2. Re-apply the decay with the **target** subgraph's eigenvalues:
   `w_target[k] = e^{-t·λ_k_target} · w_transport[k]`.
3. Run the same pgvector query in the target subgraph.
4. Merge results across subgraphs and re-rank by raw score, trim to limit
   (`:251-276`).

The heat operator commutes with the Laplacian, which is what makes the
coefficients transportable: the eigenbasis is the operator's diagonal
form, and C maps one diagonal form to the adjacent one.

## 6. The fusion and its caveats

`combined_score = diffusion_score + name_score + geo_score + uslp_boost`,
re-sorted descending (`query_executor_service/geo_uslp.py`). The re-rank
runs on FILTER-AGGREGATE-MEASURE (#1) and PLACE-ATTRIBUTE-QUERY (#8);
wiring into the remaining trained templates is open.

- **`diffusion_score` is non-zero only for heat-kernel-built pools.**
  PLACE-ATTRIBUTE-QUERY (#8) sets it per result
  (`query_executor_service/service.py:376`); EVENT-DIFFUSION (#14)
  computes it at multiple t. The other trained templates (radius count,
  distance, bearing) leave it 0.0, so their re-rank is name + geo + uslp.
- **`name_score` fires only for proper-name OBJECTs.** The caller decides
  name-vs-category via `_resolve_amenity_tag` (alias → ontology → tag →
  fuzzy correction): a non-resolvable OBJECT span is a proper name and
  gets `name_score` = FastText cosine against each entity's `name`
  (exact brands ≈ 1.0, nameless = 0.0). Category queries ("cafes") pass
  no `name_query` and behavior is unchanged.
- **Name-first pool (2026-09-28).** Proper-name pools are built name-first:
  a trigram tier on `name_romanized` (index-assisted `%%`, sim ≥ 0.4,
  fragment guard, romanizer for cross-script) is merged into the FastText
  pool before the radius filter. The embedding pool is name-blind for
  out-of-vocabulary brands ("juici patties" admitted 1 entity; the name
  tier found 35). See `spatial_search.py` `_search_by_name` / `_merge_pools`.
- **Scale mismatch, no renormalization.** `diffusion_score` is a raw
  inner product (filtered at `min_score = 1e-6`); `name_score` and
  `geo_score` are clamped to [0,1]; `uslp_boost` is {0, +0.5}. The fusion
  adds them as-is.
- **Role split.** The USLP pair "gathers broadly, orders precisely"
  (relevance via exact search, ordering via proven criteria). The
  diffusion term is the spectral ordering signal: it enters when the
  factor tables built the pool, and it carries the graph-structure
  evidence (Kuhn's Network concept) that embedding distance alone lacks.
  The `name_score` term is the identity signal: it pins exact-name
  entities to the top of proper-name queries regardless of distance.

## 7. Modes and PCA: not implemented

**Status: not implemented (2026-09-28).** No PCA call exists in the
backend. The only PCA in the repo is notebook 08's urban-morphology
feature PCA (`notebooks/08_gba_physical_agent_dev.ipynb`,
`OSM_TO_BUILT_FORM.md` "PCA basis Ψ"), which operates on measured building
features, not on graph modes. The pattern library classifies PCA as the
covariance-operator pattern (`MATHEMATICAL_PATTERN_LIBRARY_SEED_CARDS.md`,
variance structure → axis-aligned, ranked), a sibling of the spectral
pattern, not the same operator.

The workflow is built for this extension, because the runtime is already
factorized: `score = wᵀφ(node)` with `w = filter(λ) ⊙ φ(anchor)`. The
exponential is one diagonal filter; any learned mode weighting is another
diagonal filter in the same eigenbasis. The runtime inner product does not
change. Three directions, in increasing distance from the current code:

1. **Mode-impact analysis (analysis only, all inputs already stored).**
   For a query signal s (e.g. an amenity-class indicator over nodes), the
   energy per mode is `(φ_kᵀs)²` and the Dirichlet energy weights it by
   `λ_k`. A PCA-style breakdown answers "which modes carry this answer":
   the graph analog of explained variance. No new runtime path; loadings
   and eigenvalues are already in `factor_spectral_node_metric` and
   `GraphSpectralFingerprint`.
2. **Data-driven filter (PCA-learned mode weights).** Learn mode weights
   from a corpus of graph signals (amenity class indicators, real query
   footprints from the parser). The top principal components of the
   signal corpus, expressed in the eigenbasis, give a ranked mode
   ordering; keeping the top r is the analog of PCA's component
   truncation. Runtime unchanged: `w = filter ⊙ φ(anchor)`, one pgvector
   inner product. This replaces `e^{-tλ}` with a learned diagonal filter
   and is the natural reading of "different modes in PCA with this
   workflow".
3. **Semantic-axes PCA on embeddings.** Standard covariance PCA on entity
   embeddings (GV-Tags, or the dormant fused 400D `static_embedding`)
   yields semantic principal axes, orthogonal to the graph modes. This is
   the pattern-library covariance-operator card, and the natural carrier
   for the `static_embedding` ANN path if that fusion is ever materialized
   (`docs/plans/FUSED_400D_ANN_DEDUP_PLAN.md`, open, not implemented).

**The discipline constraint.** Any learned filter is a learned component
and must pass the same relevance test that killed HNSW ANN: a cafe query
must return cafes, not `highway=service`
(`HNSW_RELEVANCE_AND_USLP_RERANK_DESIGN.md` §1). A data-driven mode
weight would be validated against the exact-search baseline before it can
replace `e^{-tλ}`. The blocker is the relevance discipline, not compute:
the factorization is 128-dim and batch-time.

## 8. Cross-references

| Topic | Document |
|---|---|
| Exact-search decision, USLP re-rank, HNSW failure | `docs/issues/HNSW_RELEVANCE_AND_USLP_RERANK_DESIGN.md` |
| Full formula reference, worked heat-kernel example | `docs/Schematics/05_Learned_Layer/09_Factor_Table_Math_Reference.md` |
| Runtime diffusion row, factor-table joins | `docs/Schematics/05_Learned_Layer/05_Factor_Node_Runtime_Joins.md` |
| Subdivision functional maps plan | `docs/plans/SUBGRAPH_FUNCTIONAL_MAPS_PLAN_v2.md` |
| Network concept realization | `docs/preserving_the_process/KUHN_CORE_CONCEPTS.md` §6-7 |
| USLP tri-space scoring, §13.3 consumption | `docs/Schematics/06_IGEA_USLP/02_USLP_Spatial_Link_Prediction.md` |
| PCA as covariance-operator pattern | `docs/plans/later-stages/MATHEMATICAL_PATTERN_LIBRARY_SEED_CARDS.md` |
