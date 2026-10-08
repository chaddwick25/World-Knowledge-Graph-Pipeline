# The Gauge as Contract

> **Status**: Draft (2026-09-13)
> **Origin**: owner direction 2026-09-13. GA is the representational
>   phase change that makes gauge transformations first-class. The
>   gauge is the contract between compute tiers.
> **Grounded in**:
>   - Bronstein et al., *Geometric Deep Learning: Grids, Groups,
>     Graphs, Geodesics, and Gauges* (2021), §3.5 Blueprint (B, σ, P, A),
>     §2 Optimization, gauge symmetry on manifolds
>   - Dorst, *Geometric Algebra for Computer Science* (GA4CS), the
>     primitive for the phase change: rotors, motors, the geometric
>     product, grade structure
>   - `docs/plans/later-stages/MATHEMATICAL_PATTERN_LIBRARY_SEED_CARDS.md`
>     (the seed cards, the repo's invariant-first pattern library)
>   - `docs/plans/later-stages/GDL_BLUEPRINT_VISION_PROJECTS_PLAN.md`
>     (the three vision-graph projects, the GDL blueprint instantiation)
>   - `~/.gemini/antigravity/scratch/data-science-math/GEOMETRIC_GNN_GA_GUIDELINE.md`
>     (GGNN expressivity bridged to GA, Dorst GA4CS)
>   - `~/.gemini/antigravity/scratch/data-science-math/ARCHITECTURE_PART_2_GA4CS.md`
>     (PyTorch GA upgrade, hand-rolled Clifford tensor ops on the K80)
>   - `~/.gemini/antigravity/scratch/data-science-math/GAO_BRIDGE_COHEN_DORST.md`
>     (Cohen linear algebra ↔ Dorst GA bridge)
>   - `ganja.js` (`/home/thanos/projects/ganja_js`), Geometric Algebra
>     code generator for JavaScript, MIT, enkimute

---

## 1. Intent (decision in one line)

The gauge is the contract between compute tiers. Geometric algebra is
the representational phase change that makes gauge transformations
first-class objects, which makes the contract checkable.

## 2. The phase change: flat vectors to multivectors

A flat vector representation carries magnitude and direction in a
fixed basis. A multivector representation carries magnitude,
direction, area, volume, orientation, and rigid body motion in a
single algebraic object. The lifting is inductive: every flat vector
is a grade-1 multivector, so everything representable before is
representable after. What changes is what becomes first-class.

| Representation | Objects | Operations | Gauge structure |
|---------------|---------|------------|-----------------|
| Flat vectors (current) | points, feature vectors | dot product, addition | none (basis is fixed) |
| Multivectors (GA) | points, lines, planes, volumes, rotors, motors | geometric product (fuses inner + outer), wedge, vee | rotors and motors are gauge transformations |

The phase change is not "vectors become multivectors." It is "gauge
transformations become representable as objects." In the flat phase, a
rotation is a matrix you apply. In the multivector phase, a rotation is
a rotor `R` you can compose, invert, interpolate, and estimate from
data. The gauge transformation is no longer an operation on the
representation; it is an element of the representation.

### 2.1 The grade ladder

GA organizes objects by grade. Each grade is a different geometric
primitive, and the geometric product moves between grades.

```
grade 0: scalar        (a number, e.g. an eigenvalue)
grade 1: vector        (a point, an edge, a direction)
grade 2: bivector      (an oriented area, a plane element)
grade 3: trivector     (an oriented volume)
  ...
grade n: pseudoscalar  (the unit volume of the space)

geometric product:  AB = A·B + A∧B
  inner product A·B   drops to the lower grade (projection)
  outer product A∧B   lifts to the higher grade (extension)

rotor R = exp(B/2)    a bivector B exponentiated, a rotation
motor M = R(1 + εt)  a rotor times a translation, a rigid body motion
```

The grade ladder is the inductive structure. Grade 1 (vectors) is the
flat-vector phase. Grades 0 through n, with the geometric product
moving between them, is the multivector phase. The lifting adds the
higher grades and the product that connects them.

### 2.2 What the phase change buys

| Operation | Flat phase | Multivector phase |
|-----------|-----------|-------------------|
| Rotation | 3×3 matrix, 9 numbers, gimbal lock | rotor `R`, 4 numbers (quaternion), no gimbal lock |
| Rigid body motion | 4×4 matrix, 16 numbers | motor `M`, 8 numbers (dual quaternion) |
| Projection onto a plane | matrix `P = A(AᵀA)⁻¹Aᵀ` | `(v·B)B⁻¹`, B is the plane as a bivector |
| Intersection of two planes | cross product, coordinate-dependent | `B₁ ∨ B₂`, the regressive product, coordinate-free |
| Gauge transformation | apply a matrix | compose with a rotor/motor |

The multivector phase is more compact, coordinate-free, and composable.
The gauge transformation is an object, not an operation.

## 3. Gauge theory: three levels of symmetry

Bronstein's framework (2021 §3.5) generalizes in three steps. Each step
makes a different kind of symmetry representable.

| Level | Symmetry | What becomes representable | Blueprint block |
|-------|----------|---------------------------|-----------------|
| 1. Global | group G acts on the whole domain | invariance (f(g·x) = f(x)), equivariance (f(g·x) = g·f(x)) | B, A |
| 2. Scale separation | coarsening P commutes with G | multiscale structure | P |
| 3. Gauge | local frame at each point, arbitrary choice | gauge-equivariant filters (B(R·f) = R·B(f)), gauge-invariant readouts (A(R·f) = A(f)) | B, A on manifolds |

### 3.1 Level 1: global symmetry

A domain has a global symmetry group G. An image has translation
symmetry (G = translations). A sphere has rotation symmetry (G = SO(3)).
A graph has permutation symmetry (G = symmetric group).

Two properties a function f can have:
- **Invariant**: f(g·x) = f(x). Image classification: a cat is a cat
  anywhere in the image.
- **Equivariant**: f(g·x) = g·f(x). Image segmentation: the mask moves
  with the image.

A CNN works because convolution is translation-equivariant. The filter
commutes with the shift. This is the Erlangen Programme applied to
neural networks: geometry is the study of invariants under a group.

### 3.2 Level 2: scale separation

Real signals have multiscale structure. The blueprint applies an
equivariant operator B, then a coarsening P, repeatedly:

```
f = A ∘ σ ∘ B_J ∘ P_{J-1} ∘ ... ∘ P_1 ∘ σ_1 ∘ B_1
```

The P block (pooling, decimation, coarsening) must commute with the
symmetry. CNN max-pool commutes with translation. Graph coarsening
commutes with permutation. This gives the blueprint (B, σ, P, A):
equivariant operator, nonlinearity, coarsening, invariant readout.

### 3.3 Level 3: gauge symmetry

On a curved manifold (or a graph with no global coordinate system),
there is no global symmetry group G. You cannot translate "by 50
pixels" on a sphere; there is no global notion of "50 pixels in
direction x."

What exists is a local frame at each point, a local coordinate chart.
The choice of frame is arbitrary. You can rotate the frame at point p
independently of the frame at point q. This arbitrary local choice is a
gauge. Changing the frame is a gauge transformation.

The physics analogy is direct: in general relativity, you choose
coordinates locally on the manifold; in gauge theory (Yang-Mills,
electromagnetism), you choose a local phase or frame for the field. The
physics must not depend on the arbitrary choice. The math must be gauge
invariant: the answer does not change when you change gauge.

For deep learning: if a filter operates on features defined in a local
frame, and the frame choice is arbitrary, the filter must produce the
same answer regardless of which frame was picked.

- At each point p, a tangent plane with a frame (e₁, e₂).
- A feature is a vector in that frame: f = f₁·e₁ + f₂·e₂.
- Rotate the frame by R: the frame becomes (R·e₁, R·e₂), the feature
  becomes (R·f₁, R·f₂).
- A filter B that is gauge-equivariant satisfies: B(R·f) = R·B(f). The
  filter output rotates the same way the input did.
- A readout A that is gauge-invariant satisfies: A(R·f) = A(f). The
  classification does not care about the frame orientation.

### 3.4 Why GA enters at level 3

In GA, the gauge transformation at a point is a rotor `R`. The
gauge-equivariant filter is the geometric product with a multivector
operator. The gauge-invariant readout is the grade-0 projection (the
scalar part). The math is the same as Bronstein's; GA makes the objects
concrete.

| Bronstein (level 3) | GA realization |
|---------------------|----------------|
| gauge transformation at p | rotor R ∈ Spin(n) |
| gauge-equivariant filter B | geometric product with a multivector operator |
| gauge-invariant readout A | grade-0 projection ⟨M⟩₀ |
| local frame at p | orthonormal basis vectors e₁, e₂, ... |
| feature in local frame | multivector M = Σ grade components |
| change of frame | M → RMR⁻¹ (rotor conjugation) |

The gauge transformation is rotor conjugation. The invariant is the
scalar part. This is why GA is the natural language for level 3: the
gauge transformation is a first-class object (a rotor), not an
operation (a matrix application).

## 4. The seed cards as gauge-theoretic operations

Each seed card names an invariant. Read through the gauge frame, each
invariant is a statement about gauge consistency.

| Seed | Invariant | Gauge interpretation | GA operation |
|------|-----------|---------------------|--------------|
| 1 (Transport) | loop commutes: describe_B ∘ τ = C ∘ describe_A | C is the gauge transformation between two local frames (subgraph spectral bases) | transport operator as multivector map |
| 2 (Maps as operators) | representation invariance: C acts the same in any basis | C is a gauge-equivariant linear operator | operator C as a multivector linear map |
| 3 (Descriptor consistency) | C ∘ d_A = d_B | descriptors commute through the map; descriptor values are gauge-invariant | least-squares rotor fit (ganja.js `example_complex_least_squares.html`) |
| 4 (Structure commutation) | CΛ_A ≈ Λ_B C | the map respects the local geometry (the gauge) | commutation residual is gauge inconsistency |
| 5 (Canonicalization) | canonical(canonical(x)) = canonical(x) | gauge fixing: choose one canonical frame | rotor estimation (orthogonal Procrustes, ganja.js `example_ga3d_rotor_estimation.html`) |
| 6 (Iterative refinement) | fixed point | gauge converges to a stable frame | iterative rotor refinement |
| 7 (Extend-decompose-buffer) | boundary fidelity | gauge consistency at the buffer overlap | transition functions at chart boundaries |
| 8 (Factorize-then-evaluate) | factorization commutes with evaluation | the factorized form is gauge-equivalent to the original | factor tables as gauge-invariant artifacts |
| 8a (Diagonalizing) | operator becomes coordinate-wise | spectral decomposition IS the gauge choice; eigenvalues are gauge-invariant scalars | eigenvectors as local frame, eigenvalues as invariants |
| 9 (Inductive transfer) | locality preserved | the learned map is gauge-equivariant on the unmeasured domain | rotor-constrained transfer |
| 10 (Sheaf) | E_B ∘ τ = C ∘ E_A at each glue point | gauge consistency across chart overlaps | transition functions are rotors/motors |

The seed cards are a gauge-theoretic pattern library. The invariants
are gauge consistency conditions. The operations are gauge
transformations. The residuals are gauge inconsistency measures.

### 4.1 The composition as a gauge diagram

The seed cards compose by signature. The functional-maps composition
(Seed 7 → 2 → 3 → 4 → 5 → 8 → 10), read as a gauge diagram:

```
Extend-decompose-buffer (7)     G → G_i ∪ B_i
        |
        v  (cut into charts with buffer overlap)
Maps as operators (2)          C = Φ_Bᵀ S Φ_A
        |
        v  (C is the gauge transformation between chart bases)
Descriptor consistency (3)     ‖F_B - F_A Cᵀ‖²
        |
        v  (descriptors commute through C)
Structure commutation (4)      ‖CΛ_A - Λ_B C‖²
        |
        v  (C respects the local geometry; residual is gauge inconsistency)
Canonicalization (5)           gauge fixing (rotor estimation)
        |
        v  (choose one canonical frame)
Factorize-then-evaluate (8)    k×k shipped, pgvector at runtime
        |
        v  (factorized form is gauge-equivalent)
Local-glue-global-verify (10)  residuals per boundary
        |
        v  (gauge consistency across chart overlaps)
```

Each arrow is a gauge operation. Each box is an invariant. The
composition is a gauge-theoretic pipeline.

## 5. The gauge as contract between compute tiers

The backend computes in one gauge: the full country spectral basis,
sparse eigsh on 5M nodes. The frontend computes in another: the
subgraph spectral basis, dense decomposition on a few thousand nodes.
These are different local frames on the same mathematical object.

```
Backend (K80, country-scale)          Frontend (RTX 4070, subgraph-scale)
  gauge: full spectral basis Λ_A        gauge: subgraph spectral basis Λ_B
  operator: sparse eigsh                operator: dense SVD / ganja.js
  scale: 5M nodes                       scale: ≤ few thousand nodes
         \                              /
          \                            /
           ------> transport C <------
                   (gauge transformation)
                          |
                          v
              commutativity residual
              ‖CΛ_A - Λ_B C‖  <  tol ?
                          |
                yes       |       no
                 \        |        /
                  contract holds   contract broken
                  (frontend is    (frontend diverged;
                   a valid local   agent re-runs or
                   view of         escalates)
                   backend)
```

The transport operator C is the gauge transformation between the two
frames. The commutativity residual (Seed 4) is the contract check. If
the residual is below tolerance, the frontend's subgraph-scale answer is
a valid local view of the backend's country-scale answer. If it is
above, the agent knows the frontend compute diverged, not that the
visual looks wrong.

### 5.1 The compute split

| Compute tier | Hardware | Gauge | Library | Scale |
|--------------|----------|-------|---------|-------|
| Backend | 2× K80 (12GB each) | country spectral basis | PyTorch + scipy.sparse + hand-rolled GA tensor ops | 5M nodes |
| Frontend | RTX 4070 (16GB) | subgraph spectral basis | ganja.js (exact GA) + ml-matrix (spectral) | ≤ few thousand |
| Agent | MCP orchestration | invariant checks | MCP tools calling both tiers | n/a |

The backend GA is hand-rolled PyTorch tensor ops simulating Clifford
algebra (per `ARCHITECTURE_PART_2_GA4CS.md` §2: "you cannot use standard
Python objects. You must treat standard PyTorch Tensors as components of
a Clifford Algebra basis"). No `clifford` or `cliffordtorch` package;
batched tensor multiplications approximate the geometric product. The
frontend GA is exact: ganja.js is a Clifford algebra code generator that
produces true algebraic classes.

The two tiers compute the same invariants in different ways. The
invariant check (the commutativity residual) verifies they agree.

### 5.2 Why the contract is checkable

The contract is checkable because the invariant is a residual, not a
visual judgment. The commutativity residual `‖CΛ_A - Λ_B C‖` is a
number. The agent compares it to a tolerance. This is the gate.

Without the gauge frame, the frontend and backend compute "the same
thing" in different ways, and disagreement is ambiguous: is it a bug, a
scale difference, or a numerical artifact? With the gauge frame,
disagreement is a broken gauge contract. The residual tells you how
broken. The agent acts on the number, not on a guess.

## 6. Agent orchestration: gauge-theoretic compute

The agent does not compute. It orchestrates: calls backend for
country-scale, calls frontend for subgraph-scale, checks the
commutativity residual, renders the result. The invariant is the gate.

### 6.1 The MCP tool surface

The MCP tool surface mirrors the seed cards. Each tool is a gauge
operation. Each tool returns the invariant residual alongside the
result. The agent chains tools and gates on the residuals.

| MCP tool | Gauge operation | Returns | Gate |
|----------|----------------|---------|------|
| runTransport | fit gauge transformation C between two frames | operator C, commutativity residual | residual < tol |
| runSpectralDecompose | choose local frame (gauge) | eigenvalues (invariants), eigenvectors (frame) | reconstruction error < tol |
| runCanonicalize | fix gauge (rotor estimation) | canonical form | idempotence check |
| runInductiveTransfer | transport measured → unmeasured | imputed factors | locality preserved |
| checkInvariant | verify the contract | pass/fail + residual | the seed's invariant |
| renderResult | visualize the result | deck.gl/three.js overlay | (visualization) |

### 6.2 A chain

```
runSpectralDecompose (backend, country)
        |
        v  (eigenvalues Λ_A, eigenvectors Φ_A)
runSpectralDecompose (frontend, subgraph)
        |
        v  (eigenvalues Λ_B, eigenvectors Φ_B)
runTransport (fit C between Φ_A, Φ_B)
        |
        v  (operator C, commutativity residual ‖CΛ_A - Λ_B C‖)
checkInvariant (commutativity residual < tol?)
        |
        +-- yes --> renderResult (deck.gl overlay)
        |
        +-- no  --> escalate or re-run with a different subgraph
```

The agent does not judge whether the result "looks right." It checks the
invariant. If the residual is below tolerance, the chain proceeds. If
not, the agent escalates or re-runs with a different subgraph.

### 6.3 Downstream tasks the agent can orchestrate

| Task | Seeds | Gauge operation | Verification |
|------|-------|----------------|--------------|
| Cross-domain transfer | 1 + 9 | transport operator C from measured neighbor to unmeasured subgraph | loop commutes on subgraph sample |
| Spectral drift audit | 8a + 4 | eigenvalue drift between snapshots | eigenvalues are gauge-invariant; disagreement signals real change |
| 3D mesh → graph alignment | GDL Project 2 + 1 | transport from mesh Laplacian frame to OSM graph frame | commutativity residual |
| Visual grounding | GDL Project 1 + 5 | adapter as gauge transformation (DINOv2 frame → OSM frame) | canonicalization is gauge fixing |

## 7. Where GA unifies and where it does not

GA is the unifier for gauge transformations: rotor estimation (Seed 5),
transport-by-rotor (Seed 1's gauge absorption), sheaf transition
functions (Seed 10), and the geometric product as the gauge-equivariant
operation (Seed 4). These are operations where the gauge structure is
the content, and GA represents the gauge as a first-class object.

GA is not the unifier for:

- **Spectral decomposition (Seeds 8, 8a)**: eigendecomposition of a
  Laplacian is a matrix operation. GA does not solve it. The eigenvalues
  are gauge-invariant scalars (a GA concept), but computing them
  requires numerical linear algebra, not the geometric product.
- **Optimal transport (Seed 1's transport plan)**: the OT plan is a
  matrix, not a multivector. GA helps with the gauge absorption inside
  the transport fit, not with the transport itself.
- **Feature embeddings (FastText, DINOv2)**: these are vectors in a
  feature space, not multivectors in a Clifford algebra. Lifting them
  to GA is meaningful only when the downstream operation uses the
  geometric structure (e.g. the adapter in GDL Project 1 uses the
  camera's rigid body motion, which is a motor in PGA).

The boundary: GA unifies the gauge operations. The spectral and
transport operations remain matrix math. The invariant check bridges
them: the commutativity residual is computed in matrix space but
verifies a gauge-theoretic contract.

### 7.1 The honest claim

The strong claim is not "any signal, any data can be represented in
vectors." The strong claim is: any geometric structure can be lifted to
multivectors, and the lifting makes gauge transformations first-class.
FastText embeddings are vectors but not multivectors in a meaningful
sense. The lifting is meaningful when the data has geometric structure:
positions, orientations, rigid body motions. The document states this
boundary, not universality.

## 8. The GDL blueprint blocks map to GA

The GDL blueprint (B, σ, P, A) connects to the vision projects plan and
to GA. Each block has a GA realization.

| Blueprint block | GDL role | GA realization | Seed card |
|-----------------|----------|----------------|-----------|
| B (equivariant operator) | G-equivariant linear map | rotors/motors (the G-equivariant transforms in GA) | 4 (commutation with structure operator) |
| σ (nonlinearity) | elementwise map | GA-native (sign-alignment = grade involution) | 5 (canonicalization) |
| P (coarsening) | multiresolution | PGA projective maps (camera projection = coarsening) | 7 (extend-decompose-buffer) |
| A (readout) | G-invariant output | GA invariants (norm, grade-0 projection) | 8a (spectral invariants) |
| Optimization | loss + gradient | GA autodiff (ganja.js `example_dual_backpropagation.html`) | 6 (iterative refinement) |

The vision projects' camera math (Project 1: OAK-D → DINOv2, Project 2:
OAK-D → 3D mesh) is PGA (Projective Geometric Algebra). ganja.js has
`example_pga2d_pose_estimation.html` and `example_pga3d_*` examples. The
GDL blueprint plan currently says "three.js for 3D view"; the math
underneath the 3D view (camera pose, projection, rigid transforms) is
PGA, and ganja.js is the proven JS implementation of it.

## 9. Open questions (ground-truth checks needed)

1. **Backend GA accuracy**: `ARCHITECTURE_PART_2_GA4CS.md` §2 shows a
   conceptual `GeometricMessagePassing` layer with a "simplified
   simulation" comment. If the backend GA is approximate, the invariant
   checks have a floor of approximation error that is not gauge
   inconsistency. Check: is there a test that pins the backend GA
   against a known result, the way `test_uslp_geo_formula.py` pins USLP?
2. **ganja.js rotor estimation precision**: the examples exist
   (`example_ga3d_rotor_estimation.html`), but no benchmark against
   `scipy.spatial.transform.Rotation` (the proven Procrustes solver). If
   ganja.js diverges from scipy by more than the invariant tolerance,
   the frontend gauge fixing will not match the backend gauge fixing,
   and the commutativity residual will always fail. Check: benchmark
   ganja.js rotor estimation against scipy on a known rotation, measure
   the angular error.
3. **Seed card stability**: open question 1 from the seed cards ("which
   seeds are genuinely distinct") must resolve before the gauge mapping
   is final. Seed 4 is Seed 3 with the geometry operator as descriptor;
   the gauge mapping should record the parent-child edge, not flatten
   it.
4. **OT in JS**: no proven JS optimal transport solver. Seed 1 (the
   master seed) needs OT for the transport plan. ganja.js helps with the
   gauge absorption inside the fit, not with the transport itself. A
   JS Sinkhorn or a WASM port of POT (Python OT) is the gap.

## 10. References

- [BRONSTEIN] Bronstein, Bruna, Cohen, Veličković. *Geometric Deep
  Learning: Grids, Groups, Graphs, Geodesics, and Gauges*. arXiv:2104.13478, 2021. §3.5 Blueprint, §2 Optimization, gauge symmetry.
- [GA4CS] Dorst, Fontijne, Mann. *Geometric Algebra for Computer
  Science*. Morgan Kaufmann, 2007. Rotors (Ch 7), outermorphisms (Ch 10), projections (Ch 4).
- [GANJA] De Keninck. ganja.js. Geometric Algebra code generator for
  JavaScript. Zenodo doi:10.5281/zenodo.3635774, 2020. `/home/thanos/projects/ganja_js`.
- [SEED_CARDS] `docs/plans/later-stages/MATHEMATICAL_PATTERN_LIBRARY_SEED_CARDS.md`. The repo's invariant-first pattern library.
- [GDL_VISION] `docs/plans/later-stages/GDL_BLUEPRINT_VISION_PROJECTS_PLAN.md`. The three vision-graph projects, GDL blueprint instantiation.
- [GA_GUIDELINE] `~/.gemini/antigravity/scratch/data-science-math/GEOMETRIC_GNN_GA_GUIDELINE.md`. GGNN expressivity bridged to GA.
- [GA4CS_ARCH] `~/.gemini/antigravity/scratch/data-science-math/ARCHITECTURE_PART_2_GA4CS.md`. PyTorch GA upgrade, hand-rolled Clifford tensor ops.
- [GAO_BRIDGE] `~/.gemini/antigravity/scratch/data-science-math/GAO_BRIDGE_COHEN_DORST.md`. Cohen linear algebra ↔ Dorst GA bridge.
- [GATR] Brehmer et al. *Geometric Algebra Transformer*. arXiv:2305.18415, 2023. Equivariant transformer in PGA.
- [CLIFFORD_GROUP] Ruhe et al. *Clifford Group Equivariant Neural
  Networks*. arXiv:2305.11141, 2023. O(n)- and E(n)-equivariant models in the Clifford group.
- [GCAN] Geometric Clifford Algebra Networks. ICML 2023. Group action
  layers in Clifford algebra for dynamical systems.
