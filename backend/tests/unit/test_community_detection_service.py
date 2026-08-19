"""Invariant tests for CommunityDetectionService.

Pins the Phase 0 invariants from GRAPH_SPECTRAL_TEMPORAL_PLAN.md §"Phase 0:
Community Detection":

- Every node belongs to exactly one community (partition)
- Number of communities ≤ number of nodes
- Modularity Q ∈ [-0.5, 1] (typically [0, 0.5] for good partitions)

These tests do NOT require a database.
"""

import os

import pytest
import networkx as nx

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

from semantic_search.services.community_detection_service import (
    CommunityDetectionService,
)


def _two_cluster_graph() -> nx.Graph:
    """Two clearly-separated cliques connected by a single edge."""
    G = nx.Graph()
    G.add_edges_from([(0, 1), (1, 2), (2, 0), (0, 2)])  # clique 1
    G.add_edges_from([(3, 4), (4, 5), (5, 3), (3, 5)])  # clique 2
    G.add_edge(2, 3)  # bridge
    return G


def test_every_node_in_exactly_one_community():
    """Partition property: each node appears in exactly one community."""
    G = _two_cluster_graph()
    result = CommunityDetectionService().detect_communities(G)
    node_to_comm = result["node_to_community"]
    assert len(node_to_comm) == G.number_of_nodes()
    # Each node appears exactly once across all communities
    flat = [n for comm in result["communities"] for n in comm]
    assert sorted(flat) == sorted(node_to_comm.keys())


def test_community_count_leq_node_count():
    """#communities ≤ #nodes (Phase 0 invariant)."""
    G = _two_cluster_graph()
    result = CommunityDetectionService().detect_communities(G)
    assert result["community_count"] <= G.number_of_nodes()


def test_modularity_in_valid_range():
    """Modularity Q ∈ [-0.5, 1] (Phase 0 invariant)."""
    G = _two_cluster_graph()
    result = CommunityDetectionService().detect_communities(G)
    assert -0.5 <= result["modularity"] <= 1.0


def test_two_cluster_graph_yields_two_communities():
    """Two clearly-separated cliques → ~2 communities with high modularity."""
    G = _two_cluster_graph()
    result = CommunityDetectionService().detect_communities(G)
    assert result["community_count"] == 2
    assert result["modularity"] > 0.3


def test_empty_graph_returns_empty_result():
    """Empty graph → empty communities (no crash)."""
    G = nx.Graph()
    result = CommunityDetectionService().detect_communities(G)
    assert result["community_count"] == 0
    assert result["communities"] == []


def test_directed_graph_converted_to_undirected():
    """DiGraph is converted to undirected for community detection."""
    G = nx.DiGraph()
    G.add_edges_from([(0, 1), (1, 2), (2, 0), (3, 4), (4, 5), (5, 3), (2, 3)])
    result = CommunityDetectionService().detect_communities(G)
    assert result["community_count"] >= 1
    assert result["node_to_community"]


def test_filter_communities_by_class():
    """filter_communities_by_class returns communities with the target dominant class."""
    G = _two_cluster_graph()
    svc = CommunityDetectionService()
    result = svc.detect_communities(G)
    communities = result["communities"]
    # Class map: clique 1 = Cafe, clique 2 = School
    entity_class_map = {0: "wkgs:Cafe", 1: "wkgs:Cafe", 2: "wkgs:Cafe",
                        3: "wkgs:School", 4: "wkgs:School", 5: "wkgs:School"}
    cafe_communities = svc.filter_communities_by_class(
        communities, entity_class_map, "wkgs:Cafe"
    )
    school_communities = svc.filter_communities_by_class(
        communities, entity_class_map, "wkgs:School"
    )
    # Each class should be dominant in exactly one community
    assert len(cafe_communities) >= 1
    assert len(school_communities) >= 1
    # All returned cafe communities should have wkgs:Cafe as dominant
    for c in cafe_communities:
        assert c["dominant_class"] == "wkgs:Cafe"
