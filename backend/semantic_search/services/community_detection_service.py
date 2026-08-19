"""CommunityDetectionService — spatial community detection on the k-NN graph.

Uses the Louvain method (greedy modularity optimization) to partition the
k-NN graph into communities. Each community is a set of spatially proximate
entities that form a cluster.

The k-NN graph captures spatial proximity, so communities correspond to
spatial clusters of entities. This enables queries like:
- "Where are the clusters of cafes in this region?"
- "How many distinct neighborhoods exist in this city?"
- "Which community has the highest density of schools?"

Phase 0 invariants (enforced by ``tests/unit/test_community_detection_service.py``):
- Every node belongs to exactly one community (partition)
- Number of communities ≤ number of nodes
- Modularity Q ∈ [-0.5, 1] (typically [0, 0.5] for good partitions)

References:
- [GRAPH_REP:Ch4] — Community detection, modularity
"""

import logging

import networkx as nx

logger = logging.getLogger(__name__)


class CommunityDetectionService:
    """Detect spatial communities in the k-NN graph.

    Uses the Louvain method (greedy modularity optimization) to partition
    the k-NN graph into communities. Each community is a set of spatially
    proximate entities that form a cluster.

    [GRAPH_REP:Ch4] — Community detection, modularity

    The k-NN graph captures spatial proximity, so communities correspond
    to spatial clusters of entities. This enables queries like:
    - "Where are the clusters of cafes in this region?"
    - "How many distinct neighborhoods exist in this city?"
    - "Which community has the highest density of schools?"

    Invariants (Phase 0):
    - Every node belongs to exactly one community (partition)
    - Number of communities ≤ number of nodes
    - Modularity Q ∈ [-0.5, 1] (typically [0, 0.5] for good partitions)
    """

    def detect_communities(self, G: nx.Graph, resolution: float = 1.0) -> dict:
        """Detect communities using the Louvain method.

        Args:
            G: k-NN graph (undirected)
            resolution: higher values → smaller communities

        Returns:
            dict with ``communities`` (list of lists of node IDs),
            ``modularity``, ``node_to_community``, ``community_count``.
        """
        if G.is_directed():
            G = G.to_undirected()

        if G.number_of_nodes() == 0:
            return {
                "communities": [],
                "modularity": 0.0,
                "node_to_community": {},
                "community_count": 0,
            }

        communities = nx.algorithms.community.louvain_communities(
            G, resolution=resolution
        )
        try:
            modularity = nx.algorithms.community.modularity(G, communities)
        except Exception:
            modularity = 0.0

        node_to_community = {}
        for i, comm in enumerate(communities):
            for node in comm:
                node_to_community[node] = i

        return {
            "communities": [list(c) for c in communities],
            "modularity": float(modularity),
            "node_to_community": node_to_community,
            "community_count": len(communities),
        }

    def filter_communities_by_class(
        self,
        communities: list,
        entity_class_map: dict,
        target_class: str,
    ) -> list:
        """Filter communities by dominant WorldKG class.

        Args:
            communities: list of communities (each a list/iterable of node IDs)
                as returned by ``detect_communities``.
            entity_class_map: {osm_id → wkg_class}
            target_class: WorldKG class to filter on (e.g. "wkgs:Cafe")

        Returns:
            list of dicts with ``community_index``, ``node_count``,
            ``dominant_class``, ``class_distribution`` for communities
            whose dominant class matches ``target_class``.
        """
        result = []
        for i, comm in enumerate(communities):
            classes = [entity_class_map.get(n, "unknown") for n in comm]
            class_counts = {}
            for c in classes:
                class_counts[c] = class_counts.get(c, 0) + 1
            if not class_counts:
                continue
            dominant = max(class_counts, key=class_counts.get)
            if dominant == target_class:
                result.append({
                    "community_index": i,
                    "node_count": len(comm),
                    "dominant_class": dominant,
                    "class_distribution": class_counts,
                })
        return result
