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

References:
- [COHEN:Ch13] — Eigendecomposition
- [GRAPH_REP:Ch3] — Graph Laplacian, spectral features
- [COHEN:Ch15] — Matrix exponential (heat kernel)
"""

import logging

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla
import networkx as nx

logger = logging.getLogger(__name__)


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

    For large graphs (Norway ~1-3M nodes), ``eigsh`` with k=128 takes
    ~30-60 minutes. This is acceptable for a batch pipeline step.
    Use k=64 for large countries if needed.
    """

    def compute_spectral_features(self, G: nx.Graph, k: int = 128) -> dict:
        """Extract top-k smallest eigenvectors/eigenvalues of normalized Laplacian.

        Args:
            G: Undirected k-NN graph (a DiGraph is converted to undirected
               for spectral analysis — direction doesn't affect connectivity)
            k: Number of eigenvalues/eigenvectors to compute

        Returns:
            dict with eigenvalues, eigenvectors, fiedler_vector,
            algebraic_connectivity, spectral_gap, node_count, edge_count
        """
        # Convert to undirected for spectral analysis
        if G.is_directed():
            G = G.to_undirected()

        n = G.number_of_nodes()
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
            }

        # eigsh requires k < n. Clamp k to n-1 (we drop the trivial λ₀=0
        # afterwards, so we request k+1 and keep k).
        k_request = min(k + 1, n)

        # Compute normalized Laplacian: L = I - D^{-1/2} W D^{-1/2}
        L = nx.normalized_laplacian_matrix(G).astype(float)

        if k_request <= 1:
            # Trivial graph — only the constant eigenvector exists
            eigenvalues = np.array([0.0])
            eigenvectors = np.ones((n, 1)) / np.sqrt(n)
        else:
            # k+1 because first eigenvalue is always 0 (trivial)
            eigenvalues, eigenvectors = spla.eigsh(L, k=k_request, which='SM')

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
            "edge_count": G.number_of_edges(),
            # Node ordering of the eigenvector rows (list(G.nodes()) after
            # the undirected conversion above).  FactorNodeWriter uses this
            # to map eigenvector rows back to osm_ids.
            "node_order": list(G.nodes()),
        }

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
