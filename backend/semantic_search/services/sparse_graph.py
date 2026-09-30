"""PyG-style COO graph representation (SparseGraph).

Extracted from ``knn_graph_service.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN) — the memory-efficient
alternative to NetworkX for large k-NN graphs.
"""

import numpy as np
from typing import Dict
from dataclasses import dataclass, field


@dataclass
class SparseGraph:
    """PyG-style COO graph representation — memory-efficient alternative to
    NetworkX for large graphs.

    NetworkX stores graphs as dict-of-dicts (~15 GB for IE's 2.47M nodes /
    73.8M edges).  ``SparseGraph`` stores just the COO arrays (~1.5 GB) and
    builds scipy sparse matrices lazily.  This is the representation the
    spectral / signal / community / factor-writer services consume in
    Step 5c for large countries.

    Attributes:
        node_ids: (N,) int64 array of OSM IDs (row index → osm_id mapping)
        edge_index: (2, E) int64 array of node *indices* (undirected edges
            are stored once; ``to_csr(symmetrize=True)`` adds both
            directions)
        edge_weight: (E,) float64 array of k-NN edge weights
        class_map: {osm_id: wkg_class} node attributes (optional)
    """

    node_ids: np.ndarray
    edge_index: np.ndarray
    edge_weight: np.ndarray
    class_map: Dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Basic properties
    # ------------------------------------------------------------------

    @property
    def n_nodes(self) -> int:
        return len(self.node_ids)

    @property
    def n_edges(self) -> int:
        return self.edge_index.shape[1]

    @property
    def _node_index(self) -> Dict:
        """osm_id → row index (built lazily, cached on the instance)."""
        if getattr(self, "_node_index_cache", None) is None:
            self._node_index_cache = {
                int(osm_id): i for i, osm_id in enumerate(self.node_ids)
            }
        return self._node_index_cache

    @property
    def degrees(self) -> np.ndarray:
        """(N,) int64 degree of each node in the undirected graph.

        Counts **unique** neighbours (canonical deduplicated edges),
        matching ``nx.Graph.degree`` semantics.
        """
        if getattr(self, "_degrees_cache", None) is None:
            lo, hi, _ = self._canonical_edges
            deg = (
                np.bincount(lo, minlength=self.n_nodes)
                + np.bincount(hi, minlength=self.n_nodes)
            )
            loops = lo == hi
            if loops.any():
                deg -= np.bincount(lo[loops], minlength=self.n_nodes)
            self._degrees_cache = deg
        return self._degrees_cache

    # ------------------------------------------------------------------
    # scipy sparse conversions
    # ------------------------------------------------------------------

    @property
    def _canonical_edges(self):
        """Undirected edges deduplicated with **max** weight.

        k-NN is asymmetric — i's k nearest may include j without j's
        including i — so both (i,j) and (j,i) can appear in
        ``edge_index``.  The NetworkX path keeps ``max(weight)`` for such
        parallel edges; CSR ``sum_duplicates`` would sum them instead.
        This property canonicalises each pair to (min, max) and reduces
        by max, matching the NetworkX semantics exactly.
        """
        if getattr(self, "_canonical_cache", None) is None:
            n = self.n_nodes
            lo = np.minimum(self.edge_index[0], self.edge_index[1])
            hi = np.maximum(self.edge_index[0], self.edge_index[1])
            if lo.size == 0:
                self._canonical_cache = (
                    lo.astype(np.int64), hi.astype(np.int64),
                    self.edge_weight.astype(np.float64),
                )
                return self._canonical_cache
            key = lo.astype(np.int64) * n + hi.astype(np.int64)
            order = np.argsort(key, kind="stable")
            key_s = key[order]
            w_s = self.edge_weight[order]
            # Segment boundaries → max-reduce within each duplicate run
            starts = np.r_[0, np.flatnonzero(np.diff(key_s)) + 1]
            uniq_key = key_s[starts]
            uniq_w = np.maximum.reduceat(w_s, starts)
            self._canonical_cache = (
                (uniq_key // n).astype(np.int64),
                (uniq_key % n).astype(np.int64),
                uniq_w,
            )
        return self._canonical_cache

    def to_csr(self, symmetrize: bool = True):
        """Weighted adjacency matrix as CSR.

        Args:
            symmetrize: if True, store both (i,j) and (j,i) — the
                undirected semantics of the NetworkX path (parallel k-NN
                edges keep the max weight via ``_canonical_edges``).
        """
        import scipy.sparse as sp

        n = self.n_nodes
        lo, hi, w = self._canonical_edges
        if symmetrize:
            rows = np.concatenate([lo, hi])
            cols = np.concatenate([hi, lo])
            data = np.concatenate([w, w])
        else:
            rows, cols, data = lo, hi, w
        return sp.csr_matrix(
            (data, (rows, cols)), shape=(n, n),
        ).asfptype().tocsr()

    def normalized_laplacian(self):
        """Normalized Laplacian ``L = I - D^{-1/2} W D^{-1/2}`` (CSR).

        Matches ``nx.normalized_laplacian_matrix`` semantics for the
        weighted undirected case — including the NetworkX convention
        (since v1.11) that isolated nodes get a **zero** diagonal entry.
        """
        import scipy.sparse as sp

        A = self.to_csr(symmetrize=True)
        deg = np.asarray(A.sum(axis=1)).ravel()
        isolated = deg == 0
        deg[isolated] = 1.0  # avoid div-by-zero; zeroed out below
        d_inv_sqrt = 1.0 / np.sqrt(deg)
        D = sp.diags(d_inv_sqrt)
        n = self.n_nodes
        L = (sp.identity(n, format="csr") - D @ A @ D).tocsr()
        if isolated.any():
            # nx sets L[i,i] = 0 for isolated nodes
            L = (L - sp.diags(isolated.astype(float))).tocsr()
        return L

    def laplacian(self):
        """Combinatorial Laplacian ``L = D - W`` (CSR)."""
        import scipy.sparse as sp

        A = self.to_csr(symmetrize=True)
        deg = np.asarray(A.sum(axis=1)).ravel()
        return (sp.diags(deg) - A).tocsr()

    def components(self):
        """Connected components via scipy csgraph.

        Returns:
            (component_of, component_size_of) — both dicts keyed by
            osm_id, matching the ``FactorNodeWriter`` contract.
        """
        from scipy.sparse.csgraph import connected_components

        n_comp, labels = connected_components(
            self.to_csr(symmetrize=True), directed=False,
        )
        sizes = np.bincount(labels, minlength=n_comp)
        component_of = {
            int(self.node_ids[i]): int(labels[i]) for i in range(self.n_nodes)
        }
        component_size_of = {
            int(self.node_ids[i]): int(sizes[labels[i]])
            for i in range(self.n_nodes)
        }
        return component_of, component_size_of

    # ------------------------------------------------------------------
    # Interop
    # ------------------------------------------------------------------

    @classmethod
    def from_nx(cls, G) -> "SparseGraph":
        """Build a SparseGraph from a NetworkX graph (tests / small graphs).

        Node attributes: only ``wkg_class`` is preserved (the only
        attribute the Step 5c consumers read).
        """
        node_ids = np.array(sorted(G.nodes()), dtype=np.int64)
        index = {int(osm_id): i for i, osm_id in enumerate(node_ids)}
        edges, weights = [], []
        for u, v, data in G.edges(data=True):
            edges.append((index[int(u)], index[int(v)]))
            weights.append(float(data.get("weight", 1.0)))
        edge_index = (
            np.array(edges, dtype=np.int64).T
            if edges else np.zeros((2, 0), dtype=np.int64)
        )
        class_map = {
            int(node): data.get("wkg_class")
            for node, data in G.nodes(data=True)
            if data.get("wkg_class")
        }
        return cls(
            node_ids=node_ids,
            edge_index=edge_index,
            edge_weight=np.array(weights, dtype=np.float64),
            class_map=class_map,
        )

    def to_nx(self):
        """Convert to ``networkx.Graph`` (small graphs only — GraphML debug
        artifact serialization, heat-kernel unit tests)."""
        import networkx as nx

        G = nx.Graph()
        for osm_id in self.node_ids:
            G.add_node(int(osm_id))
        for i in range(self.n_edges):
            # edge_index holds *row indices* into node_ids, not osm_ids
            u = int(self.node_ids[self.edge_index[0, i]])
            v = int(self.node_ids[self.edge_index[1, i]])
            w = float(self.edge_weight[i])
            if G.has_edge(u, v):
                G[u][v]["weight"] = max(G[u][v]["weight"], w)
            else:
                G.add_edge(u, v, weight=w)
        for osm_id, cls in self.class_map.items():
            G.nodes[int(osm_id)]["wkg_class"] = cls
        return G
