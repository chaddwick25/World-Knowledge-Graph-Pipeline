"""GraphSignalService — encode WorldKG artifacts as graph signals.

A graph signal assigns a value to each node in the graph. We encode
WorldKG class labels (``wkg_class``) as node signals on the k-NN graph,
then compute:

- **Dirichlet energy** ``sᵀLs``: measures how much the signal varies across
  edges. Low → classes cluster spatially. High → classes are mixed.
  [GRAPH_REP:Ch3]
- **Signal diffusion** ``(L + μI)⁻¹s``: smooths the signal across the
  graph, revealing spatial structure of semantic classes after accounting
  for connectivity.

Phase 0 invariants (enforced by ``tests/unit/test_graph_signal_service.py``):
- ``sᵀLs ≥ 0`` (L is PSD)
- ``sᵀLs = 0`` for a constant signal on a connected graph
- ``(αs)ᵀL(αs) = α²(sᵀLs)``  (quadratic scaling)

References:
- [GRAPH_REP:Ch3] — Graph signals, Dirichlet energy
"""

import logging

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla
import networkx as nx

logger = logging.getLogger(__name__)


class GraphSignalService:
    """Encode WorldKG artifacts as graph signals and compute smoothness.

    A graph signal assigns a value to each node in the graph. We encode
    WorldKG class labels (wkg_class) as node signals on the k-NN graph,
    then compute:

    - Signal smoothness (Dirichlet energy): s^T L s  [GRAPH_REP:Ch3]
      Low → classes cluster spatially on the graph
      High → classes are mixed across neighborhoods

    - Signal diffusion: (L + μI)⁻¹ · s
      Smooths the signal across the graph, revealing spatial structure
      of semantic classes after accounting for connectivity.

    Invariants (Phase 0):
    - s^T L s ≥ 0 (L is PSD)
    - s^T L s = 0 for constant signal on connected graph
    - (αs)^T L (αs) = α² (s^T L s)  (quadratic scaling)
    """

    def build_class_signal(self, G, entity_class_map: dict):
        """Build node signal vector from wkg_class assignments.

        Args:
            G: k-NN graph — ``networkx.Graph`` or ``SparseGraph``
            entity_class_map: {osm_id → wkg_class} for entities on graph nodes

        Returns:
            signal: np.ndarray (N,) — class index per node
            class_index: dict mapping class name → int index
        """
        nodes = _node_list(G)
        classes = sorted(set(entity_class_map.values()))
        class_index = {cls: i for i, cls in enumerate(classes)}

        signal = np.zeros(len(nodes), dtype=float)
        for i, node in enumerate(nodes):
            if node in entity_class_map:
                signal[i] = class_index[entity_class_map[node]]
        return signal, class_index

    def build_onehot_signal(self, G, entity_class_map: dict):
        """Build one-hot encoded signal matrix (N × C)."""
        signal, class_index = self.build_class_signal(G, entity_class_map)
        n, c = len(signal), len(class_index)
        onehot = np.zeros((n, c))
        if c > 0:
            onehot[np.arange(n), signal.astype(int)] = 1.0
        return onehot, class_index

    def signal_smoothness(self, G, signal: np.ndarray) -> float:
        """Compute Dirichlet energy: ``sᵀLs``  [GRAPH_REP:Ch3].

        Low smoothness value → signal varies a lot across edges (classes
        are mixed). High smoothness value → signal is uniform in
        neighborhoods (classes cluster).

        Invariant: ``sᵀLs ≥ 0`` (L is PSD)
        Invariant: ``sᵀLs = 0`` for constant signal on connected graph
        """
        L = _combinatorial_laplacian(G)
        if signal.ndim == 1:
            return float(signal.T @ L @ signal)
        # Multi-channel signal (one-hot)
        return float(np.trace(signal.T @ L @ signal))

    def diffuse_signal(self, G, signal: np.ndarray, mu: float = 0.1):
        """Graph signal diffusion: ``(L + μI)⁻¹ · s``.

        Smooths the signal across the graph — reveals spatial structure
        of semantic classes after accounting for connectivity.

        The regularization parameter μ prevents singular systems (L is
        singular — has eigenvalue 0). μ > 0 ensures invertibility.
        """
        L = _combinatorial_laplacian(G)
        n = L.shape[0]
        A = (L + mu * sp.identity(n)).tocsc()
        return spla.spsolve(A, signal)


# ---------------------------------------------------------------------------
# Graph-type helpers — accept networkx.Graph (small/tests) or SparseGraph
# ---------------------------------------------------------------------------

def _node_list(G):
    """Ordered list of node osm_ids for either graph representation."""
    from semantic_search.services.knn_graph_service import SparseGraph

    if isinstance(G, SparseGraph):
        return [int(osm_id) for osm_id in G.node_ids]
    return list(G.nodes())


def _combinatorial_laplacian(G):
    """Combinatorial Laplacian ``L = D - W`` (scipy CSR) for either
    graph representation."""
    from semantic_search.services.knn_graph_service import SparseGraph

    if isinstance(G, SparseGraph):
        return G.laplacian()
    return nx.laplacian_matrix(G).astype(float)
