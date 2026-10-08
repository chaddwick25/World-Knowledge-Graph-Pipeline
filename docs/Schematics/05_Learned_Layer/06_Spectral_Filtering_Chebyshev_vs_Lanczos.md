# Spectral Filtering: Chebyshev vs. Lanczos

> **Added 2026-08-19.** This document clarifies the distinction between two
> polynomial filtering approaches for graph spectral analysis, both
> referenced in Hamilton, *Graph Representation Learning*, Ch 7.1.3:
>
> - **Chebyshev (Defferrard et al., ChebNet, NeurIPS 2016)**: fixed filter,
>   transductive eigenvector computation
> - **Lanczos (Liao et al., Lanczos Networks, 2019b)**: learned filter,
>   inductive signal filtering
>
> Both are relevant to the WorldKG pipeline but serve different layers.
> Chebyshev for batch eigenvector computation (Step 5c), Lanczos for the
> runtime GPU learned layer (Schematics V2 §05).

## 1. The Shared Foundation: Polynomial Filtering on the Laplacian

Both approaches exploit the same identity from spectral graph theory:

```
f(L) · x = U · f(Λ) · Uᵀ · x
```

where `L = U Λ Uᵀ` is the eigendecomposition of the Laplacian. This says:
applying any function `f` of the Laplacian to a signal `x` is equivalent to
filtering the signal's spectral components by `f(λ)`.

The key insight is that you can evaluate `f(L) · x` **without ever computing
the eigenvectors U**, using a polynomial approximation of `f`:

```
f(L) ≈ Σₖ cₖ Tₖ(L̃)
```

where `Tₖ` are Chebyshev polynomials and `L̃ = 2L/λ_max - I` is the scaled
Laplacian (eigenvalues in [-1, 1]). The three-term recursion:

```
T₀(L̃) x = x
T₁(L̃) x = L̃ x
Tₖ(L̃) x = 2 L̃ T_{k-1}(L̃) x - T_{k-2}(L̃) x
```

evaluates the polynomial using only sparse matrix-vector products. Each step
is one pass over the graph's edges. No eigendecomposition needed.

This is the "stream the calculations through the entire graph" property.
The polynomial propagates the filter across the graph topology using only
the sparse Laplacian structure.

## 2. Chebyshev: Fixed Filter, Transductive (Step 5c Batch Path)

### What "Fixed" Means

The Chebyshev coefficients `c₀, c₁, ..., c_K` are computed analytically from
a **chosen filter function**, not learned from graph data. For the
eigenvector computation prototype, the filter is the heat kernel:

```
f(λ) = exp(-tλ)
```

The coefficients are determined by two scalars:

```
α = t · λ_max / 2          (one scalar from the graph)
coeffs = Chebyshev expansion of exp(-α(x+1)) on [-1, 1]    (pure calculus)
```

The graph's structure enters only through `λ_max`, a single number estimated
via Lanczos. For IE, `λ_max ≈ 1.30` (not the theoretical bound of 2.0,
because the IE k-NN graph is not bipartite). The rest is mathematical
computation on the filter shape.

### What "Transductive" Means

The Chebyshev path finds the eigenvectors of **this specific graph**. It
doesn't generalize to other graphs. Each graph gets its own Laplacian, its
own `λ_max`, its own coefficients, its own eigenvectors. The pipeline is
*reusable* (same code works for any graph) but the output is *specific* to
the input graph.

### Why It's the Right Choice for Step 5c

Step 5c needs the **actual eigenvectors** of the Laplacian. They are
materialized as factor rows in `factor_spectral_node_metric` and resolved at
runtime via pgvector. The eigenvectors are the basis; the filter is just a
numerical tool to find them. The Rayleigh-Ritz step at the end recovers the
exact eigenvectors of `L` projected onto the filtered subspace, undoing the
filter's spectral distortion.

The filter `exp(-tλ)` is universally appropriate because graph Laplacians
*always* have their low-frequency eigenvalues clustered near zero. This is a
mathematical property of the Laplacian, not a dataset-specific pattern. No
learning is needed; the filter shape is known a priori.

### The Algorithm (prototype removed, Chebyshev approach rejected)

1. Build the sparse Laplacian L (`SparseGraph.normalized_laplacian`)
2. Estimate `λ_max` via Lanczos (one scalar from the graph)
3. Compute Chebyshev coefficients for `exp(-tλ)` on [-1, 1] (pure math)
4. Apply the filter to a random sketch: `Y = f(L) · Ω = Σₖ cₖ Tₖ(L̃) · Ω`
   (K sparse mat-vec passes)
5. Optional power iteration: `Y = f(L) · Y` (sharpen the subspace)
6. Orthonormalize: `Q = QR(Y)`
7. Rayleigh-Ritz: `B = Qᵀ L Q` (ℓ × ℓ dense eigenproblem)
8. Back-project: eigenvectors of L ≈ Q × eigenvectors of B

Memory: sparse L (~1.5 GB IE, ~7.5 GB CA) + sketch (N × ℓ × 8 bytes) +
workspace. **No GPU. No VRAM. CPU only.**

## 3. Lanczos: Learned Filter, Inductive (Runtime GPU Layer)

### What "Learned" Means

In Lanczos Networks (Liao et al., 2019b), the polynomial coefficients are
**learned from data during training**. A GNN learns the optimal filter shape
for the task. The filter adapts to what the model needs:

- "Find amenities similar to this" → learns a filter that emphasizes
  semantic-geographic spectral components
- "Find structurally similar neighborhoods" → learns a filter that
  emphasizes topological-spectral components
- "Predict where new amenities might appear" → learns a filter that
  emphasizes temporal-spectral components

The coefficients are task-dependent, not fixed. This is the inductive
learning layer.

### What "Inductive" Means

A learned filter generalizes across graphs and tasks. Once trained, the
filter can be applied to a new graph without recomputing eigenvectors. The
polynomial recursion works on any Laplacian. This is critical for the
runtime layer, where a full eigendecomposition per query is unaffordable.

### Why It's the Right Choice for the Runtime GPU Layer

The runtime layer (Schematics V2 §05 Learned Layer) operates on **graph
signals**, not eigenvectors. A query like "find cafes similar to this one"
is a signal filtering problem: diffuse the anchor's signal across the graph
and rank nodes by the filtered output. A learned Lanczos filter can do this
in K sparse mat-vec passes on the GPU. No eigenvector lookup, no factor
table join, just polynomial filtering on the Laplacian.

This is where the "software edges" from the pipeline (spectral, temporal,
USLP, semantic, geographic, ontological) become the filter's training
signal. The learned filter discovers which spectral components matter for
each task and composes them into a single polynomial.

### The Conceptual Algorithm (future runtime path)

1. Pre-train: GNN learns filter coefficients per task type (trained on
   factor tables + query patterns during the pipeline)
2. Runtime: given a query + anchor signal x:
   a. Select the learned filter `f_task(L)` based on the query concept
      footprint
   b. Apply: `y = f_task(L) · x` (K sparse mat-vec passes on GPU)
   c. Rank nodes by y
3. No eigenvector computation at runtime. No factor table join needed (the
   filter replaces the lookup).

## 4. The Two-Layer Architecture

| | Batch (pipeline Step 5c) | Runtime (Schematics V2 §05) |
|---|---|---|
| Method | Chebyshev-filtered SVD | Lanczos-learned filter |
| Output | Exact eigenvectors of L | `f_task(L) · x` (signal filtering) |
| Store | `factor_spectral_node_metric` | Ranked nodes (per query) |
| Serve | Materialized for SQL/pgvector joins at runtime | GPU inference on filtered SUPPORT set |
| Scope | Transductive: this graph's basis | Inductive: generalizes across graphs |
| Filter | Fixed: `exp(-tλ)` | Learned: task-adaptive |
| Hardware | CPU, no GPU required | GPU, learned inference |

The batch layer produces the ground truth eigenvectors. The runtime layer
can either:
- **Join** the factor tables (current path, SQL + pgvector), or
- **Filter** via a learned polynomial (future path, GPU Lanczos)

Both produce the same type of output (ranked nodes for a query), but the
filter path avoids the factor table lookup entirely. The factor tables
remain as the training data for the learned filters and as the fallback when
the GPU path is unavailable.

## 5. Why the Distinction Matters

The Chebyshev and Lanczos approaches are often conflated because both use
polynomials of the Laplacian. But they serve fundamentally different
purposes:

| Property | Chebyshev (ChebNet) | Lanczos (Lanczos Networks) |
|---|---|---|
| Filter shape | Fixed (chosen a priori) | Learned from data |
| Coefficients | Analytical (calculus) | Trained (gradient descent) |
| Purpose | Eigenvector computation | Signal filtering |
| Generalization | Transductive (this graph) | Inductive (any graph) |
| Pipeline layer | Batch (Step 5c) | Runtime (Schematics V2 §05) |
| Hardware | CPU, no GPU | GPU (learned inference) |
| Output | Eigenvectors of L | Filtered signal f(L)·x |
| Adaptation | λ_max only (one scalar) | Full task-dependent filter |

The Chebyshev path is a **numerical method**. It finds eigenvectors
efficiently by preconditioning the spectrum. The Lanczos path is a
**machine learning method**. It learns to filter signals without
eigenvectors. Both are valid; they solve different problems.

## 6. References

- Hamilton, W.L., *Graph Representation Learning*, Ch 7.1.3, spectral graph
  convolutions, the identity `f(L)·x = U·f(Λ)·Uᵀ·x`, and the
  Chebyshev/Lanczos distinction.
- Defferrard, M., Bresson, X., & Vandergheynst, P. (2016). *Convolutional
  neural networks on graphs with fast localized spectral filtering.*
  NeurIPS, ChebNet, the Chebyshev polynomial recursion.
- Liao, R., et al. (2019b). *Lanczos Networks: Learning polynomial
  approximations of the Laplacian.*, learned polynomial filters via the
  Lanczos algorithm.
- Dasoulas, L., Lutzeyer, J., & Vazirgiannis, M. (2021). *Learning
  Parametrised Graph Shift Operators.* ICLR, Theorem 1: all GSOs in the
  PGSO family share eigenvectors up to a diagonal similarity transform; the
  Chebyshev filter can be applied to any GSO.
- Mason, J.C. & Handscomb, D.C. (2002). *Chebyshev Polynomials.* Chapman
  and Hall, Chebyshev polynomial properties for approximation theory.
