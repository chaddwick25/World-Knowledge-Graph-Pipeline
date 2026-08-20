"""SpectralAnalysisService — Laplacian eigendecomposition on the k-NN graph.

Computes the top-k smallest eigenvalues/eigenvectors of the normalized
Laplacian ``L = I - D^{-1/2} W D^{-1/2}``  [GRAPH_REP:Eq 3.2] on the k-NN
graph built by ``KNNGraphService`` in Step 5.

The k-NN graph captures spatial proximity of entities via haversine
distances — it changes across snapshots as entities are added/removed,
making it ideal for temporal drift analysis.

Phase 0 invariants (enforced by ``tests/unit/test_spectral_analysis_service.py``):
- Eigenvalues are non-negative and sorted ascending
- λ₀ ≈ 0 (trivial eigenvalue)
- Eigenvectors are orthonormal: ΦᵀΦ ≈ I
- All eigenvalues ∈ [0, 2] for normalized Laplacian

Solver selection:
- GPU ``torch.lobpcg`` — when CUDA is available and graph fits in VRAM
  (small/medium graphs). Fastest path (~500s for IE on RTX 4070 Ti SUPER).
- CPU shift-invert randomized SVD (CG + AMG) — for large graphs that
  exceed GPU VRAM. Uses a rational filter ``1/(λ + σ)`` applied via
  Conjugate Gradient with pyamg Algebraic Multigrid preconditioning.
  See ``docs/issues/STEP_5C_CHEBYSHEV_FILTER_FAILURE.md`` for the full
  analysis of why polynomial filters (Chebyshev) failed and rational
  filters (shift-invert) are needed for clustered spectra.
- CPU ``eigsh(which='SM')`` — fallback for small graphs / unit tests
  where the shift-invert overhead is not worthwhile.

References:
- [COHEN:Ch13] — Eigendecomposition
- [GRAPH_REP:Ch3] — Graph Laplacian, spectral features
- [COHEN:Ch15] — Matrix exponential (heat kernel)
- Halko, Martinsson, Tropp (2011) — Randomized SVD with power iteration
- Defferrard et al. (2016) — Chebyshev polynomial filtering (ChebNet)
- Liao et al. (2019b) — Lanczos Networks (learned polynomial filters)
"""

import logging

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla
import networkx as nx

logger = logging.getLogger(__name__)

# Node count above which the shift-invert solver is preferred over GPU
# LOBPCG (even when CUDA is available) to avoid VRAM exhaustion.
# GPU LOBPCG VRAM at k=65, float32: ~2240 bytes/node (sparse tensor +
# eigenvectors + LOBPCG workspace).  16 GB VRAM minus ~2 GB torch
# overhead ≈ 14 GB available → ~6.25M nodes theoretical max, ~5M with
# safety margin for fragmentation and convergence spikes.
# IE (~2.47M nodes) fits comfortably; Canada (~12M) does not.
SHIFT_INVERT_NODE_THRESHOLD = 5_000_000


class SpectralAnalysisService:
    """Compute and cache Laplacian spectral features per country snapshot.

    Operates on the k-NN graph built by ``KNNGraphService`` in Step 5.
    The k-NN graph captures spatial proximity of entities via haversine
    distances — it changes across snapshots as entities are added/removed,
    making it ideal for temporal drift analysis.

    Computes the top-k smallest eigenvalues/eigenvectors of the normalized
    Laplacian ``L = I - D^{-1/2} W D^{-1/2}``  [GRAPH_REP:Eq 3.2].

    Invariants (Phase 0 — enforced by tests):
    - Eigenvalues are non-negative and sorted ascending
    - λ₀ ≈ 0 (trivial eigenvalue)
    - Eigenvectors are orthonormal: ΦᵀΦ ≈ I
    - All eigenvalues ∈ [0, 2] for normalized Laplacian

    For large graphs (IE ~2.5M nodes, Norway ~1-3M nodes), the GPU
    ``torch.lobpcg`` solver (used when CUDA is available) computes the
    eigendecomposition in minutes without LU factorization.  CPU
    fallback (``eigsh which='SM'``) is slow for large graphs.
    Use k=64 for large countries if needed.
    """

    def compute_spectral_features(self, G, k: int = 128) -> dict:
        """Extract top-k smallest eigenvectors/eigenvalues of normalized Laplacian.

        Args:
            G: Undirected k-NN graph — either a ``networkx.Graph`` (small
               graphs, tests) or a ``SparseGraph`` (large graphs — the
               PyG-style COO representation from
               ``KNNGraphService.build_sparse_graph``).  A DiGraph is
               converted to undirected for spectral analysis — direction
               doesn't affect connectivity.
            k: Number of eigenvalues/eigenvectors to compute

        Returns:
            dict with eigenvalues, eigenvectors, fiedler_vector,
            algebraic_connectivity, spectral_gap, node_count, edge_count
        """
        from semantic_search.services.knn_graph_service import SparseGraph

        if isinstance(G, SparseGraph):
            n = G.n_nodes
            edge_count = len(G._canonical_edges[2])
            node_order = [int(osm_id) for osm_id in G.node_ids]
            L_builder = G.normalized_laplacian
        else:
            # NetworkX path (small graphs / tests)
            if G.is_directed():
                G = G.to_undirected()
            n = G.number_of_nodes()
            edge_count = G.number_of_edges()
            node_order = list(G.nodes())
            L_builder = lambda: nx.normalized_laplacian_matrix(G).astype(float)

        if n == 0:
            return {
                "eigenvalues": [],
                "eigenvectors": np.zeros((0, 0)),
                "fiedler_vector": [],
                "algebraic_connectivity": 0.0,
                "spectral_gap": 0.0,
                "node_count": 0,
                "edge_count": 0,
                "node_order": [],
                "solver": "trivial",
                "solve_time": 0.0,
            }

        # eigsh requires k < n. Clamp k to n-1 (we drop the trivial λ₀=0
        # afterwards, so we request k+1 and keep k).
        k_request = min(k + 1, n)

        # Compute normalized Laplacian: L = I - D^{-1/2} W D^{-1/2}
        L = L_builder()

        if k_request <= 1:
            # Trivial graph — only the constant eigenvector exists
            eigenvalues = np.array([0.0])
            eigenvectors = np.ones((n, 1)) / np.sqrt(n)
            solver_name = "trivial"
            solve_time = 0.0
        else:
            import time as _time
            _t0 = _time.time()
            solver_name = self._select_solver(n, k_request)
            eigenvalues, eigenvectors = self._eigsh_solve(L, k_request, n)
            solve_time = _time.time() - _t0

        # Sort ascending (eigsh doesn't guarantee order)
        idx = eigenvalues.argsort()
        eigenvalues = eigenvalues[idx]
        eigenvectors = eigenvectors[:, idx]

        # Drop the trivial λ₀=0 eigenvalue/eigenvector
        eigenvalues = eigenvalues[1:]
        eigenvectors = eigenvectors[:, 1:]

        if len(eigenvalues) > 0:
            fiedler_vector = eigenvectors[:, 0]
            algebraic_connectivity = float(eigenvalues[0])
            spectral_gap = float(eigenvalues[-1] - eigenvalues[0])
        else:
            fiedler_vector = np.zeros(n)
            algebraic_connectivity = 0.0
            spectral_gap = 0.0

        return {
            "eigenvalues": eigenvalues.tolist(),
            "eigenvectors": eigenvectors,
            "fiedler_vector": fiedler_vector.tolist(),
            "algebraic_connectivity": algebraic_connectivity,
            "spectral_gap": spectral_gap,
            "node_count": n,
            "edge_count": edge_count,
            # Node ordering of the eigenvector rows.  FactorNodeWriter
            # uses this to map eigenvector rows back to osm_ids.
            "node_order": node_order,
            # Solver metadata for run reports
            "solver": solver_name,
            "solve_time": solve_time,
        }

    # ── Eigendecomposition solvers ──────────────────────────────────────

    @staticmethod
    def _select_solver(n, k_request, nnz=None):
        """Select the eigensolver and return its name for run reports.

        Solver selection (explicit and observable via logs):

        1. **GPU LOBPCG** — when CUDA is available AND the graph is
           below the shift-invert threshold (fits in VRAM) AND a VRAM
           pre-check passes.  Fastest path for small/medium graphs.

        2. **CPU shift-invert randomized SVD** (CG + AMG) — for large
           graphs above the threshold.  Uses a rational filter
           ``1/(λ + σ)`` that provides sharp spectral separation near
           zero, unlike polynomial filters (Chebyshev) which fail on
           clustered spectra.  No GPU required.

        3. **CPU ``eigsh(which='SM')``** — fallback for small graphs
           and unit tests where the shift-invert overhead (AMG build,
           CG solves) is not worthwhile.  Also used when GPU VRAM is
           insufficient (fragmentation after many sequential solves).

        See ``docs/issues/STEP_5C_CHEBYSHEV_FILTER_FAILURE.md`` for the
        full solver comparison and why shift-invert replaced Chebyshev.
        """
        if _cuda_available() and n >= 10_000 and n < SHIFT_INVERT_NODE_THRESHOLD:
            # VRAM pre-check: prevent OOM from VRAM fragmentation
            if nnz is not None:
                has_vram, free_bytes, needed_bytes = _check_gpu_vram(n, k_request, nnz)
                if not has_vram:
                    logger.warning(
                        "Solver: CPU eigsh which=SM (n=%d, k=%d) — GPU VRAM "
                        "insufficient (%.1f GB free, ~%.1f GB needed) — "
                        "fragmentation after sequential solves",
                        n, k_request,
                        free_bytes / 1024**3, needed_bytes / 1024**3,
                    )
                    return "cpu_eigsh_sm"
            logger.info(
                "Solver: GPU LOBPCG (n=%d, k=%d) — small graph, CUDA available",
                n, k_request,
            )
            return "gpu_lobpcg"

        if n >= SHIFT_INVERT_NODE_THRESHOLD:
            logger.info(
                "Solver: CPU shift-invert randomized SVD (n=%d, k=%d) — "
                "large graph, CG + AMG preconditioner, no GPU required",
                n, k_request,
            )
            return "cpu_shift_invert"

        logger.info(
            "Solver: CPU eigsh which=SM (n=%d, k=%d) — small graph fallback",
            n, k_request,
        )
        return "cpu_eigsh_sm"

    @staticmethod
    def _eigsh_solve(L, k_request, n):
        """Dispatch to the selected eigensolver."""
        nnz = L.nnz if hasattr(L, 'nnz') else None

        if _cuda_available() and n >= 10_000 and n < SHIFT_INVERT_NODE_THRESHOLD:
            # VRAM pre-check before attempting GPU
            if nnz is not None:
                has_vram, free_bytes, needed_bytes = _check_gpu_vram(n, k_request, nnz)
                if not has_vram:
                    logger.warning(
                        " eigsh_solve: GPU VRAM insufficient — using CPU eigsh "
                        "(n=%d, free=%.1f GB, needed=~%.1f GB)",
                        n, free_bytes / 1024**3, needed_bytes / 1024**3,
                    )
                    return _eigsh_cpu(L, k_request, n)
            return _eigsh_gpu(L, k_request, n)

        if n >= SHIFT_INVERT_NODE_THRESHOLD:
            return _shift_invert_solve(L, k_request, n)

        return _eigsh_cpu(L, k_request, n)

    def compute_heat_kernel(self, G: nx.Graph, source_node, t_values) -> dict:
        """Compute heat kernel ``u(t) = e^{-tL} · δ_source`` for event diffusion.

        Used for event/temporal questions: "What's affected by event X
        within 1 hour?" The heat kernel simulates how a signal (event)
        diffuses across the k-NN graph over time.

        Uses ``expm_multiply`` for sparse matrix exponential — O(N) per
        time step, not O(N²) as a full matrix exp would be.
        [COHEN:Ch15] — Matrix exponential

        Invariants (Phase 0):
        - Conservation: Σᵢ u(t)ᵢ = 1 for all t (mass preserved)
        - Non-negativity: u(t) ≥ 0 for all t
        - Identity: u(0) = δ_source
        - Steady state: u(t→∞) → uniform on connected component

        Args:
            G: k-NN graph
            source_node: OSM node ID where the event originates
            t_values: list of diffusion times (higher = more spread)

        Returns:
            {t: diffusion_map} where diffusion_map is list of floats (N,)
        """
        from scipy.sparse.linalg import expm_multiply

        if G.is_directed():
            G = G.to_undirected()

        n = G.number_of_nodes()
        if n == 0:
            return {t: [] for t in t_values}

        # Use the combinatorial Laplacian for diffusion (mass-preserving)
        L = nx.laplacian_matrix(G).astype(float)
        nodes = list(G.nodes())
        try:
            source_idx = nodes.index(source_node)
        except ValueError:
            raise KeyError(
                f"source_node {source_node!r} not in graph "
                f"({n} nodes)"
            )

        delta = np.zeros(n)
        delta[source_idx] = 1.0

        results = {}
        for t in t_values:
            u = expm_multiply(-t * L, delta)
            results[t] = u.tolist()
        return results


# ---------------------------------------------------------------------------
# Eigendecomposition solver helpers
# ---------------------------------------------------------------------------

def _cuda_available():
    """True when torch is importable and CUDA is available."""
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


def _estimate_gpu_vram_needed(n, k_request, nnz):
    """Estimate VRAM needed for GPU LOBPCG in bytes.

    Components:
    - Sparse Laplacian: nnz * (8 bytes index + 4 bytes value) ≈ nnz * 12
    - Eigenvectors: n * k * 4 bytes (float32)
    - LOBPCG workspace: ~3x eigenvectors (block vectors + residuals + directions)
    - torch overhead: ~2 GB
    """
    sparse_bytes = nnz * 12
    eigvec_bytes = n * k_request * 4
    workspace_bytes = eigvec_bytes * 3
    overhead_bytes = 2 * 1024**3  # 2 GB torch overhead
    return sparse_bytes + eigvec_bytes + workspace_bytes + overhead_bytes


def _check_gpu_vram(n, k_request, nnz):
    """Check if GPU has enough free VRAM for LOBPCG.

    Returns (has_vram, free_bytes, needed_bytes).
    """
    try:
        import torch
        free_bytes, total_bytes = torch.cuda.mem_get_info('cuda:0')
        needed = _estimate_gpu_vram_needed(n, k_request, nnz)
        # Require 20% safety margin
        has_vram = free_bytes > needed * 1.2
        return has_vram, free_bytes, needed
    except Exception:
        return True, 0, 0  # If check fails, let LOBPCG try


def _eigsh_gpu(L, k_request, n):
    """GPU eigendecomposition via ``torch.lobpcg``.

    Converts the scipy sparse Laplacian to a torch sparse tensor on
    ``cuda:0`` and runs LOBPCG (Locally Optimal Block Preconditioned
    Conjugate Gradient) — a block eigensolver that computes k smallest
    eigenvalues simultaneously without LU factorization.

    Memory: sparse CSR (~1 GB for 74M nonzeros) + eigenvectors
    (k × N × 4 bytes ≈ 640 MB) + LOBPCG workspace (~2-3 GB)
    fits in 16 GB VRAM (RTX 4070 Ti SUPER).

    Includes a VRAM pre-check: if free VRAM is insufficient, falls back
    to CPU eigsh instead of OOMing.  This prevents subgraph failures
    when VRAM fragmentation accumulates across many subgraph solves.

    Args:
        L: scipy sparse normalized Laplacian (N x N)
        k_request: number of eigenpairs (k+1 to include trivial λ₀)
        n: matrix dimension

    Returns:
        (eigenvalues, eigenvectors) as numpy arrays — eigenvalues
        unsorted (caller sorts).
    """
    import torch

    # ── VRAM pre-check ──
    nnz = L.nnz
    has_vram, free_bytes, needed_bytes = _check_gpu_vram(n, k_request, nnz)
    if not has_vram:
        logger.warning(
            "GPU VRAM insufficient for LOBPCG: need ~%.1f GB, free %.1f GB "
            "— falling back to CPU eigsh (n=%d, k=%d, nnz=%d)",
            needed_bytes / 1024**3, free_bytes / 1024**3,
            n, k_request, nnz,
        )
        return _eigsh_cpu(L, k_request, n)

    # ── Aggressive cleanup before allocating ──
    torch.cuda.synchronize()
    torch.cuda.empty_cache()
    import gc
    gc.collect()
    torch.cuda.empty_cache()

    # Convert scipy sparse → torch sparse COO
    L_coo = L.tocoo()
    indices = torch.tensor(
        np.vstack([L_coo.row, L_coo.col]), dtype=torch.long,
    )
    values = torch.tensor(L_coo.data, dtype=torch.float32)
    L_torch = torch.sparse_coo_tensor(
        indices, values, L_coo.shape,
    ).coalesce().to('cuda:0')

    # Free CPU-side conversion tensors
    del indices, values, L_coo

    # LOBPCG: block eigensolver, no LU factorization needed
    # niter=500 gives eigenvalues accurate to ~1e-8 (float64) or
    # ~1e-4 (float32) — sufficient for pgvector diffusion ranking
    logger.info(
        "GPU lobpcg: n=%d k=%d device=cuda:0 (VRAM: %.1f GB free, ~%.1f GB needed)",
        n, k_request,
        free_bytes / 1024**3, needed_bytes / 1024**3,
    )
    torch.cuda.synchronize()
    eigenvalues_t, eigenvectors_t = torch.lobpcg(
        L_torch,
        k=k_request,
        largest=False,
        niter=500,
        tol=1e-8,
    )
    torch.cuda.synchronize()

    # Copy results to CPU immediately, then free GPU tensors
    eigenvalues = eigenvalues_t.cpu().numpy().astype(np.float64)
    eigenvectors = eigenvectors_t.cpu().numpy().astype(np.float64)

    # Free GPU memory aggressively
    del L_torch, eigenvalues_t, eigenvectors_t
    torch.cuda.synchronize()
    torch.cuda.empty_cache()

    return eigenvalues, eigenvectors


def _eigsh_cpu(L, k_request, n):
    """CPU eigendecomposition via ``scipy.sparse.linalg.eigsh``.

    Original solver: Lanczos with ``which='SM'`` (smallest magnitude).
    Correct but slow for large graphs — convergence is poor when the
    target eigenvalues are clustered near 0 (as they always are for
    graph Laplacians).  Kept as a fallback for environments without
    GPU and for unit tests.

    Args:
        L: scipy sparse normalized Laplacian (N x N)
        k_request: number of eigenpairs (k+1 to include trivial λ₀)
        n: matrix dimension

    Returns:
        (eigenvalues, eigenvectors) as numpy arrays — eigenvalues
        unsorted (caller sorts).
    """
    eigenvalues, eigenvectors = spla.eigsh(L, k=k_request, which='SM')
    return eigenvalues, eigenvectors


def _shift_invert_solve(
    L, k_request, n,
    sigma=1e-5,
    n_oversamples=20,
    n_power_iter=2,
    cg_maxiter=300,
    cg_tol=1e-8,
    random_state=42,
    near_nullspace=None,
    coarse_solver="pinv2",
    max_levels=10,
    strength="algebraic_distance",
    periodic_qr=True,
):
    """CPU shift-invert randomized SVD via CG + AMG preconditioning.

    Uses a rational filter ``f(λ) = 1/(λ + σ)`` that provides sharp
    spectral separation near zero — the key advantage over polynomial
    filters (Chebyshev) which cannot create a sharp enough cutoff for
    tightly clustered spectra.

    The filter is applied via Conjugate Gradient solves of
    ``(L + σI) y = x`` with pyamg Algebraic Multigrid preconditioning.

    Algorithm:
        1. Build AMG preconditioner for (L + σI)
        2. Apply (L + σI)^{-1} to random sketch Ω  →  Y
        3. Power iterations: Y = (L + σI)^{-1} Y  (× n_power_iter)
        4. Orthonormalize: Q = QR(Y)
        5. Rayleigh-Ritz on original L: B = Qᵀ L Q  (dense ℓ×ℓ)
        6. Back-project: eigenvectors ≈ Q × eigenvectors of B

    Memory: sparse L + sketch (N × ℓ × 8) + AMG hierarchy.
    No GPU. No VRAM. CPU only.

    Args:
        L: scipy sparse normalized Laplacian (N x N)
        k_request: number of eigenpairs (k+1 to include trivial λ₀)
        n: matrix dimension
        sigma: shift parameter (smaller = more selective near zero)
        n_oversamples: extra sketch dimensions beyond k_request
        n_power_iter: power iterations on the shift-invert operator
        cg_maxiter: max CG iterations per solve
        cg_tol: CG convergence tolerance
        random_state: seed for reproducibility
        near_nullspace: optional ``(n, n_b)`` matrix of near-nullspace
            candidates passed to pyamg's ``smoothed_aggregation_solver``
            as the ``B`` argument. Guides AMG coarsening for the
            community-structure modes that the default (constant vector)
            doesn't capture. ``None`` preserves current behavior.
        coarse_solver: pyamg coarsest-level solver (``"pinv2"``,
            ``"pinv3"``, ``"splu"``).
        max_levels: AMG coarsening level cap.
        strength: pyamg strength-of-connection method (``"symmetric"``,
            ``"evolution"``, ``"algebraic_distance"``, ``"ode"``).
            ``"algebraic_distance"`` typically reduces CG iterations on
            heterogeneous k-NN graphs.
        periodic_qr: if True, re-orthogonalize the sketch between power
            iterations to prevent numerical rank collapse.

    Returns:
        (eigenvalues, eigenvectors) as numpy arrays — eigenvalues
        unsorted (caller sorts).
    """
    import pyamg

    L_shifted = (L + sigma * sp.identity(n, format="csr")).tocsr()

    logger.info(
        "Shift-invert: building AMG preconditioner for L + %.1e*I "
        "(nnz=%d, B=%s, coarse_solver=%s, max_levels=%d)...",
        sigma, L_shifted.nnz,
        f"{near_nullspace.shape}" if near_nullspace is not None else "None",
        coarse_solver, max_levels,
    )
    ml = pyamg.smoothed_aggregation_solver(
        L_shifted,
        B=near_nullspace,
        max_levels=max_levels,
        coarse_solver=coarse_solver,
        strength=strength,
    )
    M = ml.aspreconditioner()
    logger.info(
        "  AMG built: %d levels",
        len(ml.levels),
    )

    ell = min(k_request + n_oversamples, n)
    rng = np.random.default_rng(random_state)
    Omega = rng.standard_normal((n, ell))

    total_solves = ell * (1 + n_power_iter)
    logger.info(
        "Shift-invert: applying filter to sketch (k=%d, ℓ=%d, "
        "sigma=%.1e, power_iter=%d, %d CG solves total)...",
        k_request, ell, sigma, n_power_iter, total_solves,
    )

    # ── Per-pass CG iteration instrumentation ──
    # Records iterations and convergence status per column per pass.
    # This reveals whether the ~150-260 average is first-pass-heavy
    # (near-nullspace B helps) or uniform (AMG build knobs matter more).
    cg_iters_per_pass = []   # list of (pass_name, [iters per column])
    cg_failures_per_pass = []  # list of (pass_name, failure count)

    def _cg_solve(b, pass_name, col_idx):
        """Solve (L + σI) y = b via CG with AMG, record iteration count."""
        # scipy CG doesn't return iteration count directly; we use a
        # callback to count iterations.
        iter_count = [0]

        def _callback(xk):
            iter_count[0] += 1

        y, info = spla.cg(
            L_shifted, b, M=M,
            maxiter=cg_maxiter, tol=cg_tol,
            callback=_callback,
        )
        if info != 0:
            logger.warning(
                "CG did not converge (%s col %d): info=%d, iters=%d/%d",
                pass_name, col_idx, info, iter_count[0], cg_maxiter,
            )
        return y, iter_count[0], info

    # ── Pass 0: initial sketch Y = (L + σI)^{-1} Ω ──
    Y = np.empty((n, ell), dtype=np.float64)
    pass_iters = []
    pass_fails = 0
    for i in range(ell):
        Y[:, i], iters, info = _cg_solve(Omega[:, i], "pass0", i)
        pass_iters.append(iters)
        if info != 0:
            pass_fails += 1
    cg_iters_per_pass.append(("pass0", pass_iters))
    cg_failures_per_pass.append(("pass0", pass_fails))
    logger.info(
        "  Pass 0 done: iters min=%d max=%d mean=%.1f, failures=%d/%d",
        min(pass_iters), max(pass_iters), np.mean(pass_iters),
        pass_fails, ell,
    )

    # ── Power iterations ──
    # Optional periodic QR between passes to prevent numerical rank
    # collapse when n_power_iter > 1 (HMT power iteration stability).
    for p in range(n_power_iter):
        pass_iters = []
        pass_fails = 0
        for i in range(ell):
            Y[:, i], iters, info = _cg_solve(Y[:, i], f"pass{p+1}", i)
            pass_iters.append(iters)
            if info != 0:
                pass_fails += 1
        cg_iters_per_pass.append((f"pass{p+1}", pass_iters))
        cg_failures_per_pass.append((f"pass{p+1}", pass_fails))
        logger.info(
            "  Pass %d/%d done: iters min=%d max=%d mean=%.1f, failures=%d/%d",
            p + 1, n_power_iter,
            min(pass_iters), max(pass_iters), np.mean(pass_iters),
            pass_fails, ell,
        )
        # Periodic re-orthogonalization between power iterations
        if periodic_qr and p < n_power_iter - 1:
            Q_tmp, _ = np.linalg.qr(Y)
            Y = Q_tmp

    # ── Summary of CG instrumentation ──
    all_iters = []
    for pass_name, iters in cg_iters_per_pass:
        all_iters.extend(iters)
    total_iters = sum(all_iters)
    total_fails = sum(f for _, f in cg_failures_per_pass)
    logger.info(
        "Shift-invert CG summary: total_iters=%d, mean=%.1f, "
        "max=%d, failures=%d/%d solves",
        total_iters, np.mean(all_iters), max(all_iters),
        total_fails, total_solves,
    )

    # Orthonormalize
    Q, _ = np.linalg.qr(Y)
    del Y, Omega

    # Rayleigh-Ritz on the ORIGINAL Laplacian
    LQ = L @ Q
    B = Q.T @ LQ
    del LQ
    B = (B + B.T) / 2.0  # symmetrize

    ritz_values, ritz_vectors = np.linalg.eigh(B)

    # Back-project
    eigenvectors = Q @ ritz_vectors

    logger.info(
        "Shift-invert done: %d eigenpairs, Ritz range [%.6e, %.6e]",
        len(ritz_values), ritz_values[0], ritz_values[-1],
    )

    return ritz_values, eigenvectors

