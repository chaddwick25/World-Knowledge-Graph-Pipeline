"""Unit tests for SparseGraph (PyG-style COO representation).

Validates that the COO path produces identical results to the NetworkX
path for the consumers in Step 5c:
- normalized Laplacian matches nx.normalized_laplacian_matrix
- combinatorial Laplacian matches nx.laplacian_matrix
- degrees match nx.Graph.degree (max-weight dedup of parallel k-NN edges)
- connected components match nx.connected_components
- from_nx / to_nx round-trip preserves structure
- community detection on SparseGraph matches NetworkX Louvain

These tests do NOT require a database.
"""

import os

import numpy as np
import pytest
import networkx as nx

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

from semantic_search.services.knn_graph_service import SparseGraph
from semantic_search.services.spectral_analysis_service import (
    SpectralAnalysisService,
)
from semantic_search.services.graph_signal_service import GraphSignalService
from semantic_search.services.community_detection_service import (
    CommunityDetectionService,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _weighted_graph() -> nx.Graph:
    """Small weighted graph with an isolated node and class attributes."""
    G = nx.Graph()
    G.add_edge(10, 20, weight=5.0)  # max of the parallel pair (3.0, 5.0)
    G.add_edge(20, 30, weight=2.0)
    G.add_edge(30, 40, weight=1.0)
    G.add_edge(40, 20, weight=4.0)
    G.add_node(50)  # isolated node
    G.nodes[10]["wkg_class"] = "wkgs:Amenity"
    return G


# --------------------------------------------------------------------------- #
# SparseGraph structural correctness
# --------------------------------------------------------------------------- #

def test_from_nx_to_nx_round_trip():
    """from_nx → to_nx preserves nodes, edges, and max weights."""
    G = _weighted_graph()
    sg = SparseGraph.from_nx(G)
    G2 = sg.to_nx()

    assert set(G2.nodes()) == set(G.nodes())
    for u, v, d in G.edges(data=True):
        assert G2.has_edge(u, v)
        assert G2[u][v]["weight"] == pytest.approx(d["weight"])
    assert G2.number_of_edges() == G.number_of_edges()


def test_degrees_match_networkx():
    """SparseGraph.degrees matches nx.Graph.degree (unique neighbours)."""
    G = _weighted_graph()
    sg = SparseGraph.from_nx(G)
    nx_deg = dict(G.degree())
    for i, osm_id in enumerate(sg.node_ids):
        assert sg.degrees[i] == nx_deg[int(osm_id)]


def test_normalized_laplacian_matches_networkx():
    """SparseGraph.normalized_laplacian matches nx for weighted undirected."""
    G = _weighted_graph()
    sg = SparseGraph.from_nx(G)

    L_sparse = sg.normalized_laplacian().toarray()
    nodelist = [int(o) for o in sg.node_ids]
    L_nx = nx.normalized_laplacian_matrix(G, nodelist=nodelist).astype(float).toarray()

    np.testing.assert_allclose(L_sparse, L_nx, atol=1e-10)


def test_combinatorial_laplacian_matches_networkx():
    """SparseGraph.laplacian matches nx.laplacian_matrix."""
    G = _weighted_graph()
    sg = SparseGraph.from_nx(G)

    L_sparse = sg.laplacian().toarray()
    nodelist = [int(o) for o in sg.node_ids]
    L_nx = nx.laplacian_matrix(G, nodelist=nodelist).astype(float).toarray()

    np.testing.assert_allclose(L_sparse, L_nx, atol=1e-10)


def test_components_match_networkx():
    """SparseGraph.components matches nx.connected_components."""
    G = _weighted_graph()
    sg = SparseGraph.from_nx(G)

    component_of, component_size_of = sg.components()

    nx_comps = list(nx.connected_components(G))
    assert len(set(component_of.values())) == len(nx_comps)
    for osm_id, size in component_size_of.items():
        nx_size = next(len(c) for c in nx_comps if osm_id in c)
        assert size == nx_size


def test_max_weight_dedup():
    """Parallel k-NN edges keep max weight (not sum) in the CSR."""
    # (0→1, w=3) and (1→0, w=5) — nx keeps max=5; CSR sum would give 8
    sg = SparseGraph(
        node_ids=np.array([0, 1], dtype=np.int64),
        edge_index=np.array([[0, 1], [1, 0]], dtype=np.int64),
        edge_weight=np.array([3.0, 5.0]),
    )
    A = sg.to_csr().toarray()
    assert A[0, 1] == pytest.approx(5.0)
    assert A[1, 0] == pytest.approx(5.0)


# --------------------------------------------------------------------------- #
# Consumer equivalence: SparseGraph vs NetworkX
# --------------------------------------------------------------------------- #

def test_spectral_features_match_networkx_path():
    """compute_spectral_features on SparseGraph matches the NetworkX path."""
    G = nx.path_graph(30)
    for u, v in G.edges():
        G[u][v]["weight"] = 1.0
    sg = SparseGraph.from_nx(G)

    svc = SpectralAnalysisService()
    f_nx = svc.compute_spectral_features(G, k=5)
    f_sg = svc.compute_spectral_features(sg, k=5)

    np.testing.assert_allclose(
        f_sg["eigenvalues"], f_nx["eigenvalues"], atol=1e-8,
    )
    assert f_sg["node_count"] == f_nx["node_count"]
    assert f_sg["edge_count"] == f_nx["edge_count"]
    assert f_sg["node_order"] == f_nx["node_order"]


def test_signal_smoothness_matches_networkx_path():
    """signal_smoothness on SparseGraph matches the NetworkX path."""
    G = nx.path_graph(20)
    for u, v in G.edges():
        G[u][v]["weight"] = 2.0
    sg = SparseGraph.from_nx(G)

    signal = np.arange(20, dtype=float)
    gss = GraphSignalService()
    s_nx = gss.signal_smoothness(G, signal)
    s_sg = gss.signal_smoothness(sg, signal)
    assert s_sg == pytest.approx(s_nx)


def test_class_signal_matches_networkx_path():
    """build_class_signal on SparseGraph matches the NetworkX path."""
    G = nx.path_graph(10)
    for u, v in G.edges():
        G[u][v]["weight"] = 1.0
    for node in G.nodes():
        G.nodes[node]["wkg_class"] = "wkgs:Amenity" if node % 2 == 0 else "wkgs:Shop"
    sg = SparseGraph.from_nx(G)

    gss = GraphSignalService()
    sig_nx, idx_nx = gss.build_class_signal(G, {
        n: d["wkg_class"] for n, d in G.nodes(data=True)
    })
    sig_sg, idx_sg = gss.build_class_signal(sg, sg.class_map)

    assert idx_sg == idx_nx
    np.testing.assert_array_equal(sig_sg, sig_nx)


def test_community_detection_sparse_matches_nx():
    """SparseGraph community detection finds the same 2-cluster partition."""
    G = nx.Graph()
    G.add_edges_from([(0, 1), (1, 2), (2, 0)])
    G.add_edges_from([(3, 4), (4, 5), (5, 3)])
    G.add_edge(2, 3)
    for u, v in G.edges():
        G[u][v]["weight"] = 1.0

    result_nx = CommunityDetectionService().detect_communities(G)
    sg = SparseGraph.from_nx(G)
    result_sg = CommunityDetectionService().detect_communities(sg)

    # Partition property holds in both paths
    assert sorted(sum(result_sg["communities"], [])) == sorted(G.nodes())
    assert result_sg["community_count"] == 2
    assert result_sg["community_count"] == result_nx["community_count"]
    assert result_sg["modularity"] == pytest.approx(
        result_nx["modularity"], abs=0.05,
    )


def test_empty_sparse_graph():
    """Empty SparseGraph → empty results, no crash."""
    sg = SparseGraph(
        node_ids=np.array([], dtype=np.int64),
        edge_index=np.zeros((2, 0), dtype=np.int64),
        edge_weight=np.array([], dtype=np.float64),
    )
    assert sg.n_nodes == 0
    assert sg.n_edges == 0

    f = SpectralAnalysisService().compute_spectral_features(sg, k=4)
    assert f["node_count"] == 0
    assert f["eigenvalues"] == []

    c = CommunityDetectionService().detect_communities(sg)
    assert c["community_count"] == 0
