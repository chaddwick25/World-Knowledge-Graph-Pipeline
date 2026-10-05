# Preserving the OSM-to-Built-Form Transfer Technique

How the measured building context from the GlobalBuildingAtlas (GBA, TUM)
combines with the pipeline's global OSM-derived embedding stack to make
built-form computable anywhere and anywhen, and how the transport-matrix
formalism already built for subgraph functional maps makes that transfer
rigorous. Originated 2026-09-10 from the GBA Africa integration plan
(`docs/plans/next-stage/GLOBAL_BUILDING_ATLAS_INTEGRATION_PLAN.md`) and
notebook 08 (Doppelganger Physical Agent, `08_gba_physical_agent_dev.ipynb`).

---

## 1. The Problem

GBA enriches African pipeline countries with measured building context:
height at the entity point, footprint area, local density (BCR), FAR proxy,
shape complexity, nearest-building distance, materialized as a
`BuiltEnvironmentMetric` factor table (Step 5f, Phase 1 on Cape Verde).

Two properties of the dataset define the design space:

- **Heights are model projections.** GBA.Height is an ML prediction from
  PlanetScope imagery, trained on LiDAR nDSMs from developed countries.
  The ESSD paper states no LiDAR reference data exists in Africa, so
  African heights are extrapolations, unvalidated on that continent.
- **The dataset is frozen.** One-shot release, OSM ~2024/2025 vintage. A
  2017 snapshot would receive 2024 buildings. No temporal axis, no drift
  artifacts (unlike the AEF satellite layers).

GBA answers "what is physically built here" for the countries that get
measured. The question that follows: how does that signal leave Africa?

## 2. The Fusion: Measured Atlas × Global Substrate

The homogeneous global substrate already exists in the repo: the embedding
stack. GV-Tags (300D, "what you are") and GV-NLE (100D, "where you are")
exist for ~182 of 191 countries, all 54 African ones included.

| Substrate | Coverage | Temporal | What it captures |
|---|---|---|---|
| GBA built context (measured) | Africa Phase 1; global in principle | Static, 2024/2025 one-shot | Physical form: height, FAR, BCR, morphology |
| OSM-derived embeddings + tag profiles (gv_tags, gv_nle) | ~182 countries, all of Africa | Snapshot-pinned, daily/monthly | Semantic + graph position, per snapshot |

The asymmetry is the opportunity: GBA is rich but frozen; OSM is thin (no
heights) but temporal and universal. The novel artifact is a learned mapping
between them.

```
     GBA (measured)                      OSM stack (global)
     height, BCR, FAR, morphology        gv_tags 300D, gv_nle 100D
     frozen, 2024/2025 one-shot          snapshot-pinned, ~182 countries
              \                                   /
               \                                 /
                v                               v
          +-------------------------------------------+
          |           learned mapping E (W1)          |
          |  train: OSM features -> GBA built metrics |
          +-------------------------------------------+
                          |
                          v
          global built-form factor (GV-BF)
          any country, any snapshot
```

## 3. Four Workflows the Fusion Enables

| # | Workflow | Mechanism | Novel question it answers | Repo fit |
|---|----------|-----------|---------------------------|----------|
| W1 | **Learned built-form layer** | Train per-entity/regional OSM feature vector (gv_tags + tag profile + density covariates) → GBA built metrics (Africa labels). Apply the mapping to any country/snapshot with embeddings. Artifact: a global predicted built-form factor ("GV-BF" in repo naming) | "What is the built form of a place that has no measured height data, at any point in time?" | Learned-layer infra exists (GV-NLE, USLP, parser training) |
| W2 | **Morphology twins (Doppelganger)** | Notebook-08 PCA axes trained on GBA-measured regions; project OSM-only regions into that space via the same feature representation | "Which cities globally are physically analogous to Westlands, Nairobi?" — cross-continent analog search | Notebook 08 is a working prototype |
| W3 | **Built-vs-labeled discrepancy loop** | Compare GBA measured built context vs class-conditional expectations from OSM tags; high discrepancy = stale OSM, mapping gap, or reclassification candidate | "Which entities are physically inconsistent with their labels?" | Feeds the enrichment machinery (`enrich_with_places`, WorldKG classes) |
| W4 | **Temporal built-form projection** | Apply W1's mapping to a historical snapshot's own OSM features | "What did this neighborhood's built form look like in 2018?" | Subsumed by W1; the snapshot stack already exists |

```
                         built-form transfer E
                                  |
              +-------------------+-------------------+
              |                   |                   |
              v                   v                   v
         +---------+        +----------+        +---------+
         |   W1    |        |    W2    |        |   W3    |
         | learned |        |morphology|        | built-vs|
         |  layer  |        |  twins   |        | labeled |
         +----+----+        +----------+        +---------+
              |             (Doppelganger)      (enrichment loop)
              |
              +-- W4: temporal projection = W1 applied to old snapshots
```

## 4. Why W1 Is the Priority: the Vintage Fix

GBA is 2024/2025; a 2017 snapshot receives 2024 buildings. W1 fixes that by
construction: the learned mapping is applied to *that snapshot's own OSM
features*, so built context becomes self-consistent with the snapshot date.
Built-form for any country, any snapshot, from data the pipeline already
has.

The deeper insight: the measured Atlas is the training set; the embeddings
make it computable everywhere and everywhen.

## 5. The Two Transport Problems

One definition covers both problems. Two operators solve them.

### 5.1 Definition: the circular chart

Transport is defined by a loop. A signal in one domain is described into a
feature space, transported to the other domain, and compared. The loop
must commute: the same result comes out of either path around the circle.

```
  domain A (signal x_A)  --- tau_{A->B} --->  domain B (signal x_B)
       |                                          |
       | describe_A                               | describe_B
       v                                          v
  domain A (features z_A)  --- C_{A->B} --->  domain B (features z_B)

  the loop commutes:  describe_B o tau  =  C_{A->B} o describe_A
```

Two problems, one circle. They differ only in what `describe` means:

| | Problem 1: spectral diffusion | Problem 2: built-form transfer |
|---|---|---|
| describe | express loadings in the subgraph eigenbasis | map OSM features to built-form |
| tau / C | functional map (analytic) | learned projection E (W1) |
| Status | ✅ solved, BZ-validated 2026-08-20 | ⚠️ planned, draft 2026-09-10 |

### 5.2 Problem 1: cross-subgraph spectral diffusion (✅ solved 2026-08-20)

#### 5.2.1 The gauge problem: one thing, two languages

Each subgraph has its own eigenbasis (its own Laplacian, its own
eigenvectors). Loadings are coordinates in that basis. Adjacent subgraphs
use different coordinate systems for the same world, so the same entity's
loadings are not comparable across subgraphs: one thing, two languages.

```
  subgraph A                          subgraph B
  nodes a1..aN                        nodes b1..bM
  eigenbasis Phi_A ("English")        eigenbasis Phi_B ("French")

  the same real entity c sits in both buffer zones.
  c has loadings f_A(c) in A's basis and f_B(c) in B's basis.
  same building, two coordinate representations.
```

Bronstein et al. call this the gauge problem (§4.5); the fix is parallel
transport. Ovsjanikov et al. 2012 give the discrete analogue: a k×k
functional map that transports functions between spectral bases. Pegoraro
et al. 2023 show the map survives non-isomorphic subgraphs. The failure
this solves: a diffusion query near the Dublin/Meath border could not
reach Cork via the spectral path, and the executor fell back to PostGIS.

#### 5.2.2 The dictionary: two objective terms

The buffer zones overlap, so shared entities carry loadings in both bases.
They are the bilingual corpus. The functional map is the dictionary.

```
  shared entities u1..um  (the border overlap)

  A-side readings                  B-side readings
  f_A(u1) = row of Phi_A           f_B(u1) = row of Phi_B
  ...                              ...
  stack into F_A (m x k)           stack into F_B (m x k)

  fit the dictionary C:

  C = argmin  ||F_B - F_A * C^T||^2  +  lambda * ||C*Lambda_A - Lambda_B*C||^2
              ^^^^^^^^^^^^^^^^^^^       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
              term 1: dictionary fit    term 2: grammar constraint
              (boundary agreement)      (respects graph geometry
                                         beyond the border)
```

The theoretical definition is `C = Φ_Bᵀ S Φ_A`, with S the node-to-node
correspondence on shared entities. The implementation skips building S and
reads the shared rows directly, `f_A(u) = Φ_A[idx_a(u), :]`, then solves
the regularized least squares (`SubgraphTransportService`, functional maps
v2 plan lines 160-200). `fit_residual` and `commutativity_residual` on
`factor_subgraph_transport` record how well each term was satisfied.

Term 1 is the boundary agreement. Term 2 is the part that reaches beyond
it: the dictionary must respect each graph's grammar, not just the sample
sentences. C must approximately commute with both Laplacians, which pulls
the constraint from the border out to the whole graph.

#### 5.2.3 The flow, walked with real numbers

Tiny example: A = {a1, c}, B = {c, b1}, one shared entity c.

```
  A:  a1 -- c        B:  c -- b1        (c is the same building in both)

  both Laplacians:  L = [ 1 -1 ; -1 1 ]
  both eigenbases:  Phi = [ 1/sqrt2  1/sqrt2 ]   Lambda = diag(0, 2)
                          [ 1/sqrt2 -1/sqrt2 ]

  correspondence S maps B's c to A's c:

  C = Phi_B^T * S * Phi_A = [  0.5  -0.5 ]
                            [  0.5  -0.5 ]

  signal on A: heights [1, 3] at (a1, c)

  step 1  express in A's basis:      c_A = Phi_A^T * f = [ 2*sqrt2, -sqrt2 ]
  step 2  transport to B's basis:    c_B = C * c_A       = [ 1.5*sqrt2, 1.5*sqrt2 ]
  step 3  read off B's node values:  f_B = Phi_B * c_B   = [ 3, 0 ]  at (c, b1)
```

Read the result: the shared node c gets 3, exactly its measured value in
A. The unknown node b1 gets 0, neutral, because no information reached it.
Transport is anchored by the border: it carries known values across
faithfully and stays agnostic where it has no evidence. More shared
entities, better dictionary, less neutrality. (Both graphs are identical
here, so term 2 is degenerate; it binds when A and B differ, the real
case.)

#### 5.2.4 Runtime flow

One matrix-vector multiply per crossing, then the existing pgvector path:

```
  query anchor in subgraph A
        |
        v
  eigen-loadings c_A (128-dim)
        |
        |  c_B = C_{A->B} * c_A     (128x128 x 128 = 16K FLOPs)
        v
  loadings in B's basis
        |
        v
  pgvector <#> against B's factor rows
        |
        v
  ranked candidates across the border
```

### 5.3 Problem 2: built-form transfer (⚠️ planned 2026-09-10)

#### 5.3.1 Measured vs unmeasured

GBA measures built form in some regions (Africa Phase 1). OSM features
exist everywhere. Problem 1 transported between two graph eigenbases;
Problem 2 transports between OSM-feature space and built-form space. Same
circle, different `describe`: E maps OSM features to built-form
coordinates, learned from the measured regions.

```
  x (OSM feature vector, global + homogeneous)
  |
  |  E  (learned mapping W1)
  v
  x_hat (built-form imputed, height included)
  |
  |  z = Psi^T * Sigma^-1 * (x_hat - mu)
  v
  z (morphology coordinates, measured reference basis)
  height always loads on the axes
```

#### 5.3.2 Height always rides in the components

The PCA basis Ψ is defined on the measured domain (GBA regions, height in
the training matrix), so every axis carries a height loading by
construction. For OSM-only regions, height is not observed; it enters
through the transfer E (W1) first:

```
z = Ψᵀ Σ⁻¹ (x − μ)
```

applied to the imputed feature vector, not the raw OSM vector. Height is
always in the components because the components live in the measured
domain; the transfer E is what carries new points into that domain.

The notebook-08 PCA loadings make the property visible. Both axes carry
height, on different loadings:

```
  PCA loadings (notebook 08, Boston / Cambridge / Somerville)

  PC1 - built density & verticality vs footprint fragmentation
    far_proxy              0.502  ########
    mean_height            0.499  ########
    avg_shape_complexity  -0.485  ########     <- fragmentation pole
    bcr                    0.429  #######
    height_variance        0.239  ####
    max_height            -0.152  ###          <- fragmentation pole

    reads as: dense, uniformly tall blocks vs fragmented footprints.
    query:    "find dense built-up blocks"

  PC2 - height dispersion vs footprint compactness
    max_height             0.653  ###########
    height_variance        0.604  ##########
    bcr                   -0.369  ######      <- compactness pole
    avg_shape_complexity  -0.208  ###         <- compactness pole
    mean_height            0.133  ##
    far_proxy             -0.109  ##          <- compactness pole

    reads as: a few towers over uniform blocks vs low compact blocks.
    query:    "find high-rise districts"
```

#### 5.3.3 The seal as training signal

`docs/plans/DESCRIPTOR_COMMUTATIVITY_REGULARIZER_PLAN.md` (proposed, not
implemented) makes the loop a regularizer: descriptors commute with
transport. The built-form coordinates are a descriptor family, so it
applies verbatim:

```
E_B ∘ τ_{A→B}  =  C_{A→B} ∘ E_A
transport-then-describe  ==  describe-then-transport
```

Both paths around the circle must agree:

```
  region A (x_A)  --- tau_{A->B} --->  region B (x_B)
       |                                   |
       | E_A                               | E_B
       v                                   v
  region A (z_A)  --- C_{A->B} --->  region B (z_B)

  the loop commutes:  E_B o tau  =  C_{A->B} o E_A
```

At the shared entity c: A measured its height (3, via GBA). One path
carries that 3 across the border into B. The other path asks E_B to
predict c's height from OSM features alone. If E_B predicts 5, the loop
is broken, and the violation is a training loss that pushes E_B toward 3.
Measured labels travel across borders and constrain each local E.

The seal does double duty. For pairs with a border, it is a consistency
check. For pairs without one (two countries share no buffer zone), it is
the training signal itself: train C and E on every bordered pair, then
apply the learned regularity to the borderless pairs. Structural
similarity replaces physical proximity. That is what carries the signal
from measured Africa to the rest of the world.

### 5.4 Both solutions on one axis

The two problems are the same circle with different `describe` maps.
Side by side:

| | Functional maps (existing) | Morphology transfer (W2) |
|---|---|---|
| Basis | Φ_A, Φ_B: spectral bases of adjacent subgraphs | Ψ: PCA axes anchored by measured GBA regions |
| Domain | Both sides are peer subgraphs | Anchor (measured) ↔ target (OSM-only) |
| Direction | Push reference coordinates out to peers | Pull new data into the reference |
| Operator | C: k×k basis-to-basis | E: R^d → R^k feature-to-coordinates |

```
  functional maps (existing)          morphology transfer (W2)
  ---------------------------         -------------------------
  peer -> peer, push-out              data -> anchor, pull-in

  Phi_A --C_AB--> Phi_B               x --E--> Psi
  eigenbasis A -> eigenbasis B        OSM features -> measured basis

  same signal, adjacent graphs        new signal, reference basis
  horizontal transport                reverse direction (centripetal)
```

The repo's transport is horizontal (peer to peer, push-out). The morphology
transfer is centripetal (data into the anchor basis, pull-in). Same
operator family, reversed information flow. Owner's framing: a reverse
transport jutsu, the Flying Thunder God analogy. A seal is placed on the
target space (the PCA basis anchored by GBA measurements); any point
carrying the matching marker (a homogeneous OSM feature vector) teleports
to it.

The supervision spectrum shows where each operator sits, by how much
correspondence it needs:

| Method | Needs | Training signal | Example |
|---|---|---|---|
| Functional map (analytic) | Shared entities (m pairs) | `‖F_B − F_A Cᵀ‖²` + commutativity | `compute_transport_matrix`, BZ-validated |
| Learned linear C_θ | Same pairs, or spectra only | Boundary loss and/or `‖C_θΛ_A − Λ_B C_θ‖²` | Linear layer, same k×k shape |
| Learned nonlinear | Pairs or spectra | Same losses, higher capacity | MLP on loadings |
| GNN transport | Nothing entity-level | The graph itself | Message passing across structure |

```
  correspondence-based (repo)          learned projection (E / C_theta)

  A --- border overlap --- B           A ----------------------- B
        | shared entities |            no shared entities
        v                v             only: Lambda_A, Lambda_B, structure
  read f_A(u), f_B(u)                 train:  C_theta * Lambda_A
        |                                     = Lambda_B * C_theta
        v                                     (and/or the loop's
  fit C analytically                          E_B o tau = C o E_A)
```

The axis that matters: boundary pairs vs structure only. The functional
map reads bilingual entities at the border. The learned route can be
trained on the commutativity term alone, which needs no shared entities,
just the two spectra. Structural similarity replaces physical proximity.
The linear learned case converges to the analytic C on the same pairs, so
it is a strict generalization; its value is the boundary-free supervision
path and the smooth step into nonlinearity.

## 6. The Architecture

A sheaf-like structure: local descriptions per subgraph (`E_sub`), glued by
the transport matrices the repo already built and validated on BZ. The
commutativity constraint makes the glue rigorous.

```
  subgraph A (measured)       subgraph B (OSM-only)      subgraph C (OSM-only)
  +------------------+        +------------------+       +------------------+
  | nodes, GBA labels |        | nodes, no labels |       | nodes, no labels |
  | E_A (local map)  |        | E_B (local map)  |       | E_C (local map)  |
  +--------+---------+        +--------+---------+       +--------+---------+
           |                           |                          |
           +--------- C_AB -----------+---------- C_BC ----------+
                       transport matrices glue the field

  commutativity at every boundary:
  E_B o tau_{A->B} = C_AB o E_A,  E_C o tau_{B->C} = C_BC o E_B, ...
```

Two consequences of the seal:

1. **E is not one global map.** Each subgraph gets its own `E_sub` (learned
   on its own nodes), and the transport matrices C glue them. The built-form
   field is consistent across subgraph boundaries by construction.
2. **The learned field respects the graph's own topology.** If a node's OSM
   neighborhood maps to built-form z, its transported neighbor's built-form
   must agree. A violation is measurable and becomes a training loss term.

The price of the seal: every `E_sub` is coupled to its neighbors'
transports, so training is a joint solve over the subgraph graph, not
per-subgraph fitting. The BZ-validated transport matrices are the coupling
data that make the solve tractable.

## 7. Honest Caveats (they decide the design)

1. **Circularity is partial**: GBA.Polygon fuses OSM + Microsoft footprints,
   so footprint-derived labels (BCR, FAR) are partly circular with the OSM
   features. GBA.Height is independent (PlanetScope model), so train
   primarily on height; use footprint features as secondary labels only.
2. **Mapping completeness skew**: rural Africa OSM is sparse, DE/NL is
   dense. The feature vector must be completeness-robust (presence-based
   features, tag distributions normalized by entity count, road density) or
   the transfer learns "OSM density" instead of "physical form". Include
   mapping density as an explicit covariate.
3. **Transfer validity is testable immediately**: the 13 snapshot countries
   in the live DB (IE, MX, KR, NL, CU, GT, JM, LK, IS, CY, MC, BZ + CV) all
   have GBA coverage. Compute measured built-form there, compare against W1
   predictions. Held-out validation across continents, not just Africa.
4. **Tail problem**: African heights cluster at 1–2 stories; a mean-loss
   model under-predicts high-rises, which is exactly where the interesting
   questions live. Quantile-aware or log-target loss.
5. **Joint-solve coupling**: the commutativity constraint couples all
   `E_sub`; per-subgraph fitting is not the training regime.
6. **The gauge must be absorbed, not avoided.** Eigenbases carry per-solve
   sign and ordering ambiguity. The analytic C absorbs it in the fit. A
   learned layer must see the actual (arbitrary-signed) bases as input or
   work on gauge-invariant features. This is the same "per-LOBPCG sign
   ambiguity" the functional maps plan lists as part of the problem.
7. **The linear learned case buys little on its own.** Its value is the
   boundary-free supervision path and the smooth interpolation into
   nonlinearity, not a better C on the same pairs.
8. **Hallucination risk**: where structures genuinely differ (city subgraph
   vs rural subgraph), a structure-only transport can produce confident
   nonsense. `commutativity_residual` on `factor_subgraph_transport` is the
   canary: it measures exactly this.

## 8. Recommendation and Next Steps

W1 first (the enabler, fixes the vintage flaw), W2 second (the genuinely
novel workflow, prototyped in notebook 08), W3 as a cheap enrichment loop
on the side. W4 is W1 applied temporally, no separate work.

The validation loop is free: the 13 snapshot countries are a ready
cross-continent test set.

Next artifact: `docs/plans/next-stage/OSM_TO_BUILT_FORM_TRANSFER_PLAN.md`,
with W1 + W2, the four caveats and the commutativity constraint as gates.

## 9. Source References

- GBA integration plan: `docs/plans/next-stage/GLOBAL_BUILDING_ATLAS_INTEGRATION_PLAN.md`
- Parallel factor family: `docs/plans/next-stage/SATELLITE_EMBEDDING_INTEGRATION_PLAN.md`
- Prior exploration: `notebooks/07_global_building_atlas_learning.ipynb`,
  `notebooks/08_gba_physical_agent_dev.ipynb` (PhysicalAgent, PCA loadings)
- Transport matrices: `docs/plans/completed/SUBGRAPH_FUNCTIONAL_MAPS_PLAN_v2.md`
  (C = Φ_Bᵀ S Φ_A, line 104; regularized objective, line 179; BZ-validated
  2026-08-20)
- Descriptor commutativity: `docs/plans/DESCRIPTOR_COMMUTATIVITY_REGULARIZER_PLAN.md`
  (proposed, not implemented)
- Factor-node architecture: `docs/plans/completed/FACTOR_NODE_RUNTIME_JOINS_PLAN.md`
- GBA dataset: mediaTUM DOI 10.14459/2025mp1782307; arXiv 2506.04106;
  ESSD 17-6647-2025
- Gauge problem: Bronstein et al., "Geometric Deep Learning: Grids, Groups,
  Graphs, Geodesics, and Gauges" §4.5
- Functional maps: Ovsjanikov et al. 2012, "Functional Maps: A Flexible
  Representation of Maps Between Shapes"
- Spectral maps for subgraphs: Pegoraro et al. 2023, "Spectral Maps for
  Learning on Subgraphs"
- Transport implementation: functional maps v2 plan lines 160-200
  (`SubgraphTransportService.compute_transport_matrix`,
  `transport_loadings`)
