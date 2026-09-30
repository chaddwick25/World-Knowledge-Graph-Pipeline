"""
k-NN Graph Builder Service for GV-NLE Spatial Embeddings

Constructs weighted k-nearest neighbor graphs of OSM entities based on geographic distance.
Implements the GeoVectors paper approach:
- k=50 nearest neighbors
- Edge weights: w = ln(1 + 1/distance_km)
- Distance metric: Haversine formula for lat/lon coordinates

This graph is used as input to weighted DeepWalk to generate GV-NLE spatial embeddings.
"""

import numpy as np
from typing import List, Dict, Tuple
from haversine import haversine, Unit
import logging

from semantic_search.services.sparse_graph import SparseGraph

logger = logging.getLogger(__name__)


class KNNGraphService:
    """
    Builds k-NN graphs for spatial embedding generation.

    The graph structure captures geographic relationships:
    - Nodes: OSM entities (identified by osm_id)
    - Edges: k nearest neighbors
    - Weights: Logarithmically damped inverse distance (IDW damped weight)

    Training formula (GeoVectors paper §3.2, WeightedDeepWalkGraph._damp_and_row_norm):
        w'(o1, o2) = max(1 / ln(max(dist_km(o1, o2), 1.1)), e)

    Note: This is the graph-construction (training) formula, distinct from the NLE
    inference formula used in NLEModel.encode_coords() (paper §3.4):
        w_enc(o, oj) = ln(1 + 1/dist_km(o, oj))
    The two are not interchangeable — the training formula's `max(..., e)` floor
    ensures graph connectivity for the stochastic transition matrix; the inference
    formula computes a normalized weighted mean that does not require a floor.

    IMPORTANT: build_knn_graph() already applies the training formula. Do NOT
    apply damping a second time in WeightedDeepWalkService — pass apply_damping=False
    to avoid double-damping, which collapses all edge weights to e (≈ 2.718) and
    destroys distance information. The train_gv_nle management command enforces
    this with apply_damping=False at the call site (line 561).
    """
    
    def __init__(self, k: int = 50):
        """
        Initialize k-NN graph builder.
        
        Args:
            k: Number of nearest neighbors (default: 50 from paper)
        """
        self.k = k
    
    @staticmethod
    def calculate_distance(coord1: Tuple[float, float], 
                          coord2: Tuple[float, float]) -> float:
        """
        Calculate haversine distance between two lat/lon coordinates.
        
        Args:
            coord1: (latitude, longitude) tuple
            coord2: (latitude, longitude) tuple
            
        Returns:
            Distance in kilometers
        """
        return haversine(coord1, coord2, unit=Unit.KILOMETERS)
    
    @staticmethod
    def calculate_edge_weight(distance_km: float, epsilon: float = 1e-6) -> float:
        """
        Calculate edge weight from distance using the GeoVectors TRAINING formula.

        Formula (GeoVectors paper §3.2, reference: WeightedDeepWalkGraph._damp_and_row_norm
        lines 32-33 — Numba JIT path):
            w'(d) = max(1 / ln(max(d, 1.1)), e)

        Where:
        - d:   haversine distance in kilometers
        - 1.1: minimum-distance clamp — avoids ln(1) = 0 (division by zero);
               entities closer than 1.1 km all receive the same maximum weight.
        - e:   Euler's number (~2.718) — weight floor that keeps the graph fully
               connected (no zero-weight edges break the random walk probability).
        - Effect: weight decreases monotonically with distance; very close entities
               get high weights, distant ones are floored at e.

        This is the TRAINING formula only — used to build the CSR transition matrix
        for DeepWalk. It is NOT the same as the NLE inference formula (paper §3.4):
            w_enc = ln(1 + 1/dist_km)   ← used only in NLEModel.encode_coords()

        IMPORTANT: the returned weight is already fully damped. Do not pass it
        through WeightedDeepWalkService.apply_damped_weights() again (double-damping
        collapses all weights to e, destroying distance information).

        Args:
            distance_km: Haversine distance in kilometers.
            epsilon:     Safety minimum distance (default: 1e-6 km = 1 mm) used only
                         for entities with identical coordinates where haversine = 0.

        Returns:
            Edge weight ≥ e ≈ 2.718 (higher = closer).
        """
        # Clamp distance to minimum 1.1 km (GeoVectors training formula requirement).
        # epsilon guard handles identical-coordinate entities (haversine = 0).
        distance_km = max(distance_km, max(epsilon, 1.1))

        # GeoVectors training formula: w' = max(1/ln(d), e)
        weight = 1.0 / np.log(distance_km)

        # Floor at e ≈ 2.718 to ensure all edges have non-zero transition probability.
        return max(weight, np.e)
    
    def build_knn_graph(self, entities: List[Dict]) -> Dict[int, List[Tuple[int, float]]]:
        """
        Build k-NN graph from entity list.
        
        Args:
            entities: List of dicts with keys:
                - osm_id: OSM entity ID
                - lat: Latitude
                - lon: Longitude
        
        Returns:
            Dict mapping osm_id -> [(neighbor_osm_id, edge_weight), ...]
            Each entity has k neighbors sorted by distance (closest first)
        
        Example:
            {
                123: [(456, 5.2), (789, 4.8), ...],  # 50 neighbors
                456: [(123, 5.2), (789, 3.1), ...],
                ...
            }
        """
        n = len(entities)
        if n == 0:
            logger.warning("No entities provided to build_knn_graph. Returning empty graph.")
            return {}
        
        if n < self.k:
            logger.warning(f"Only {n} entities available, but k={self.k}. Using k={n-1}.")
            k = max(1, n - 1)
        else:
            k = self.k

        # Edge case: single entity — no neighbors possible.
        # Return a graph with the node but no edges.
        if n == 1:
            logger.warning(f"Only 1 entity provided — returning single-node graph with no edges.")
            osm_id = entities[0]['osm_id']
            return {osm_id: []}

        logger.info(f"Building k-NN graph for {n} entities (k={k}) using multi-threaded BallTree...")

        # Convert coordinates to radians exactly for scikit-learn Haversine formula
        osm_ids = [entity['osm_id'] for entity in entities]
        coords_rad = np.radians([[e['lat'], e['lon']] for e in entities])

        from sklearn.neighbors import NearestNeighbors
        import multiprocessing

        # Avoid Loky-backed parallel loop warnings when running inside a child process
        # (e.g., inside a multiprocessing worker or Django management command)
        current_proc = multiprocessing.current_process()
        is_child = current_proc.name != 'MainProcess' and not current_proc.name.startswith('Process-')
        n_jobs = 1 if is_child else -1

        # Build ultra-fast tree indexing mapping
        # k + 1 because the query will return the coordinate mapped strictly to itself at distance 0
        # Cap at n to satisfy sklearn's n_neighbors <= n_samples constraint
        n_neighbors = min(k + 1, n)
        nn = NearestNeighbors(
            n_neighbors=n_neighbors,
            metric='haversine',
            n_jobs=n_jobs,
            algorithm='ball_tree'
        )
        nn.fit(coords_rad)
        
        # Parallel nearest neighbor extraction! 
        distances_rad, indices = nn.kneighbors(coords_rad)
        
        # Convert radian matrix back uniformly to Kilometers
        distances_km = distances_rad * 6371.0  
        
        graph = {}
        for i in range(n):
            if i % 150000 == 0 and i > 0:
                logger.info(f"Structured {i}/{n} undirected node mappings...")
                
            osm_id = osm_ids[i]
            neighbors = []
            
            # Loop strictly through the k+1 neighbors derived for this specific node
            for j in range(len(indices[i])):
                neighbor_idx = indices[i][j]
                if neighbor_idx == i:
                    continue  # Logically skip itself!
                    
                dist_km = distances_km[i][j]
                weight = self.calculate_edge_weight(dist_km)
                neighbors.append((osm_ids[neighbor_idx], weight))
                
                # Truncate at k if necessary 
                if len(neighbors) == k:
                    break
                    
            graph[osm_id] = neighbors
        
        logger.info(f"k-NN graph built: {len(graph)} nodes, {len(graph) * k} edges")
        return graph
    
    def build_knn_graph_from_db(self, region_filter: Dict = None) -> Dict[int, List[Tuple[int, float]]]:
        """
        Build k-NN graph directly from database entities.
        
        Args:
            region_filter: Optional dict to filter entities
                Example: {'osm_type': 'node', 'tags__amenity__isnull': False}
        
        Returns:
            k-NN graph structure
        """
        from worldkg_nca.models import OsmEntity
        from django.contrib.gis.geos import Point
        
        # Query entities
        queryset = OsmEntity.objects.using('vectors').all()
        
        if region_filter:
            queryset = queryset.filter(**region_filter)
        
        # Extract coordinates
        entities = []
        for entity in queryset.iterator():
            if entity.geom:
                entities.append({
                    'osm_id': entity.osm_id,
                    'lat': entity.geom.y,  # PostGIS Point: x=lon, y=lat
                    'lon': entity.geom.x,
                })
        
        logger.info(f"Loaded {len(entities)} entities from database")
        
        return self.build_knn_graph(entities)
    
    def export_graph_to_edgelist(self, graph: Dict[int, List[Tuple[int, float]]], 
                                  output_path: str):
        """
        Export graph to weighted edge list format for DeepWalk.
        
        Format: source target weight (one edge per line)
        
        Args:
            graph: k-NN graph structure
            output_path: Path to save edge list file
        """
        with open(output_path, 'w') as f:
            for source, neighbors in graph.items():
                for target, weight in neighbors:
                    f.write(f"{source} {target} {weight}\n")
        
        logger.info(f"Exported graph to {output_path}")
    
    def get_graph_statistics(self, graph: Dict[int, List[Tuple[int, float]]]) -> Dict:
        """
        Calculate statistics about the k-NN graph.

        Args:
            graph: k-NN graph structure

        Returns:
            Dict with statistics
        """
        num_nodes = len(graph)
        num_edges = sum(len(neighbors) for neighbors in graph.values())

        # Weight statistics
        all_weights = [weight for neighbors in graph.values()
                      for _, weight in neighbors]

        return {
            'num_nodes': num_nodes,
            'num_edges': num_edges,
            'avg_degree': num_edges / num_nodes if num_nodes > 0 else 0,
            'weight_mean': np.mean(all_weights),
            'weight_std': np.std(all_weights),
            'weight_min': np.min(all_weights),
            'weight_max': np.max(all_weights),
        }

    # ──────────────────────────────────────────────────────────────────────
    # NetworkX graph construction (spectral / community analysis substrate)
    # ──────────────────────────────────────────────────────────────────────

    def build_graph(
        self,
        country_code: str,
        snapshot_id: str,
        k: int = None,
        entity_limit: int = None,
    ):
        """Build a weighted ``networkx.Graph`` for a country snapshot.

        Loads OSM entities from the vectors DB filtered by ``country_code``
        and ``snapshot_id``, builds the k-NN adjacency (same haversine +
        log-damped inverse-distance weighting as DeepWalk), and returns an
        undirected ``networkx.Graph`` ready for spectral / community analysis.

        Node identifiers are ``osm_id`` (int), matching the convention used
        by ``build_knn_graph``. Edge attribute ``weight`` holds the k-NN
        edge weight. Node attribute ``wkg_class`` is populated when
        available so callers (e.g. ``GraphSignalService``) can build the
        class signal without a second DB round-trip.

        .. note::
            For large countries this NetworkX representation costs ~15 GB
            (IE: 2.47M nodes / 73.8M edges).  Use ``build_sparse_graph``
            instead — it returns the PyG-style COO ``SparseGraph``
            (~1.5 GB) and never materialises the dict-of-dicts.  This
            method remains for backward compatibility and small graphs.

        Args:
            country_code: ISO 3166-1 alpha-2 code (e.g. "BZ")
            snapshot_id: ``YYYY_MM_DD`` snapshot partition key
            k: Override the default neighbor count (default: ``self.k``)
            entity_limit: Optional cap on the number of entities loaded
                (useful for quick tests / large-country sampling)

        Returns:
            ``networkx.Graph`` — undirected, weighted, with ``wkg_class``
            node attributes where available.
        """
        return self.build_sparse_graph(
            country_code, snapshot_id, k=k, entity_limit=entity_limit,
        ).to_nx()

    def build_sparse_graph(
        self,
        country_code: str,
        snapshot_id: str,
        k: int = None,
        entity_limit: int = None,
    ):
        """Build a PyG-style COO ``SparseGraph`` for a country snapshot.

        Same DB load + BallTree k-NN adjacency as ``build_graph``, but
        returns the memory-efficient COO representation (~1.5 GB for IE's
        2.47M nodes / 73.8M edges, vs ~15 GB for the NetworkX graph) —
        no NetworkX dict-of-dicts is ever materialised.

        Node identifiers: ``node_ids`` (N,) holds the OSM IDs;
        ``edge_index`` (2, E) holds node *indices* into ``node_ids``;
        ``edge_weight`` (E,) holds the k-NN edge weights.  ``class_map``
        maps osm_id → wkg_class (the only node attribute Step 5c
        consumers read).

        Duplicate directed k-NN pairs (i→j and j→i both present) are
        *not* merged here — consumers go through
        ``SparseGraph._canonical_edges`` which deduplicates with max
        weight, matching the undirected NetworkX semantics.

        Args:
            country_code: ISO 3166-1 alpha-2 code (e.g. "BZ")
            snapshot_id: ``YYYY_MM_DD`` snapshot partition key
            k: Override the default neighbor count (default: ``self.k``)
            entity_limit: Optional cap on the number of entities loaded

        Returns:
            ``SparseGraph``
        """
        from worldkg_nca.models import OsmEntity

        if k is None:
            k = self.k

        qs = OsmEntity.objects.using('vectors').filter(
            geom__isnull=False,
            country_code__iexact=country_code,
            snapshot_id=snapshot_id,
        )
        if entity_limit is not None:
            qs = qs[:entity_limit]

        entities = []
        class_map = {}
        for entity in qs.iterator():
            if not entity.geom:
                continue
            entities.append({
                'osm_id': entity.osm_id,
                'lat': entity.geom.y,
                'lon': entity.geom.x,
            })
            if entity.wkg_class:
                class_map[entity.osm_id] = entity.wkg_class

        logger.info(
            "build_sparse_graph: loaded %d entities for %s/%s (k=%d)",
            len(entities), country_code, snapshot_id, k,
        )

        adj = self.build_knn_graph(entities)

        # Nodes: every osm_id that appears as a source or target
        all_ids = set(adj.keys())
        for neighbors in adj.values():
            all_ids.update(t for t, _ in neighbors)
        node_ids = np.array(sorted(all_ids), dtype=np.int64)
        index = {int(osm_id): i for i, osm_id in enumerate(node_ids)}

        src_list, dst_list, w_list = [], [], []
        for source, neighbors in adj.items():
            i = index[int(source)]
            for target, weight in neighbors:
                src_list.append(i)
                dst_list.append(index[int(target)])
                w_list.append(weight)

        edge_index = (
            np.array([src_list, dst_list], dtype=np.int64)
            if src_list else np.zeros((2, 0), dtype=np.int64)
        )
        sg = SparseGraph(
            node_ids=node_ids,
            edge_index=edge_index,
            edge_weight=np.array(w_list, dtype=np.float64),
            class_map=class_map,
        )
        logger.info(
            "build_sparse_graph: %s/%s → %d nodes, %d edges (COO)",
            country_code, snapshot_id, sg.n_nodes, sg.n_edges,
        )
        return sg

    def build_sparse_graph_for_subgraph(
        self,
        country_code: str,
        snapshot_id: str,
        subgraph_slug: str,
        poly_path: str = None,
        bbox: tuple = None,
        buffer_deg: float = 0.45,
        k: int = None,
        return_core_ids: bool = False,
    ):
        """Build a SparseGraph for a single subgraph (admin region).

        Filters entities by the subgraph's polygon (preferred) or bbox,
        with a buffer zone for correct k-NN neighbor assignment at
        boundaries.  Mirrors the geo-fence logic from
        ``train_gv_nle``'s subgraph path.

        The buffer ensures border nodes have realistic spatial neighbors
        (entities just outside the subgraph boundary), but only entities
        strictly inside the polygon are included in the output graph.
        This matches Step 5's write-back semantics.

        With ``return_core_ids=True`` (Option A from the functional maps
        plan), the returned ``SparseGraph`` includes **all** buffered
        entities (core + buffer), and the ``core_ids`` set is returned
        alongside so the caller can prune at factor-write time.  This
        gives shared buffer entities exact eigen-loadings from the
        spectral solve — needed for transport matrix computation.

        Args:
            country_code: ISO 3166-1 alpha-2 (e.g. "IE")
            snapshot_id: ``YYYY_MM_DD`` partition key
            subgraph_slug: Subgraph identifier (for logging)
            poly_path: Path to .poly file for the subgraph boundary.
                If None, bbox is used instead.
            bbox: Tuple of (min_lon, min_lat, max_lon, max_lat).  Used
                only when poly_path is None or fails to parse.
            buffer_deg: Buffer in degrees around the polygon for k-NN
                neighbor loading (default 0.45° ≈ 50 km).
            k: Override the default neighbor count
            return_core_ids: If True, return ``(SparseGraph, core_ids)``
                where the graph includes all buffered entities (Option A).
                If False (default, backward-compatible), return just the
                core-pruned ``SparseGraph``.

        Returns:
            ``SparseGraph`` scoped to the subgraph's entities, or
            ``(SparseGraph, core_ids)`` if ``return_core_ids=True``.
        """
        from worldkg_nca.models import OsmEntity
        from worldkg_nca.services.wikidata_service import (
            parse_poly_to_wkt, parse_poly_bbox, bbox_to_wkt,
        )
        from django.contrib.gis.geos import GEOSGeometry

        if k is None:
            k = self.k

        # ── Resolve the subgraph boundary ──
        strict_wkt = None
        if poly_path:
            strict_wkt = parse_poly_to_wkt(poly_path)
            if strict_wkt is None:
                logger.warning(
                    "build_sparse_graph_for_subgraph: poly parse failed "
                    "for %s, falling back to bbox", poly_path,
                )
                bbox = parse_poly_bbox(poly_path)
                if bbox:
                    strict_wkt = bbox_to_wkt(*bbox)

        if strict_wkt is None and bbox is not None:
            strict_wkt = bbox_to_wkt(*bbox)

        if strict_wkt is None:
            logger.warning(
                "build_sparse_graph_for_subgraph: no boundary for %s — "
                "falling back to country-level filter",
                subgraph_slug,
            )
            return self.build_sparse_graph(
                country_code, snapshot_id, k=k,
            )

        strict_geo = GEOSGeometry(strict_wkt, srid=4326)
        buffer_geo = strict_geo.buffer(buffer_deg)
        buffer_wkt = buffer_geo.wkt

        # ── Load entities in the buffer zone (k-NN graph scope) ──
        buffer_geom = GEOSGeometry(buffer_wkt, srid=4326)
        qs = OsmEntity.objects.using('vectors').filter(
            geom__isnull=False,
            geom__within=buffer_geom,
            country_code__iexact=country_code,
            snapshot_id=snapshot_id,
        ).order_by('osm_id').distinct('osm_id')

        entities = []
        class_map = {}
        core_ids = set()  # entities strictly inside the polygon

        for entity in qs.iterator():
            if not entity.geom:
                continue
            entities.append({
                'osm_id': entity.osm_id,
                'lat': entity.geom.y,
                'lon': entity.geom.x,
            })
            if entity.wkg_class:
                class_map[entity.osm_id] = entity.wkg_class
            if strict_geo.contains(entity.geom):
                core_ids.add(entity.osm_id)

        logger.info(
            "build_sparse_graph_for_subgraph: %s/%s/%s — loaded %d entities "
            "(%d core, %d buffer) (k=%d)",
            country_code, snapshot_id, subgraph_slug,
            len(entities), len(core_ids), len(entities) - len(core_ids), k,
        )

        if len(entities) < 2:
            logger.warning(
                "build_sparse_graph_for_subgraph: %s has < 2 entities — "
                "returning empty graph",
                subgraph_slug,
            )
            return SparseGraph(
                node_ids=np.array([], dtype=np.int64),
                edge_index=np.zeros((2, 0), dtype=np.int64),
                edge_weight=np.array([], dtype=np.float64),
                class_map={},
            )

        # ── Build k-NN adjacency on all buffer entities ──
        adj = self.build_knn_graph(entities)

        if return_core_ids:
            # Option A (functional maps plan): return the FULL buffered
            # graph (core + buffer entities).  The spectral solve runs on
            # all buffered entities, giving shared buffer entities exact
            # eigen-loadings for transport matrix computation.  The caller
            # prunes to core at factor-write time using the returned
            # ``core_ids`` set.
            all_ids = set(adj.keys())
            for neighbors in adj.values():
                all_ids.update(t for t, _ in neighbors)
            node_ids = np.array(sorted(all_ids), dtype=np.int64)
            index = {int(osm_id): i for i, osm_id in enumerate(node_ids)}

            src_list, dst_list, w_list = [], [], []
            for source, neighbors in adj.items():
                i = index[int(source)]
                for target, weight in neighbors:
                    src_list.append(i)
                    dst_list.append(index[int(target)])
                    w_list.append(weight)

            edge_index = (
                np.array([src_list, dst_list], dtype=np.int64)
                if src_list else np.zeros((2, 0), dtype=np.int64)
            )
            sg = SparseGraph(
                node_ids=node_ids,
                edge_index=edge_index,
                edge_weight=np.array(w_list, dtype=np.float64),
                class_map=class_map,
            )
            logger.info(
                "build_sparse_graph_for_subgraph: %s/%s → %d nodes, %d edges "
                "(COO, Option A — full buffered graph, %d core)",
                subgraph_slug, snapshot_id, sg.n_nodes, sg.n_edges,
                len(core_ids),
            )
            return sg, core_ids

        # ── Prune to core entities only (drop buffer-only nodes) ──
        # Keep edges where BOTH endpoints are core entities.
        # This ensures the spectral graph is the induced subgraph on
        # core entities, with k-NN neighbors computed from the full
        # buffer zone (correct boundary behavior).
        core_adj = {}
        for source, neighbors in adj.items():
            if source not in core_ids:
                continue
            core_neighbors = [(t, w) for t, w in neighbors if t in core_ids]
            if core_neighbors:
                core_adj[source] = core_neighbors

        # Build node_ids from core entities that appear in the adjacency
        all_ids = set(core_adj.keys())
        for neighbors in core_adj.values():
            all_ids.update(t for t, _ in neighbors)
        node_ids = np.array(sorted(all_ids), dtype=np.int64)
        index = {int(osm_id): i for i, osm_id in enumerate(node_ids)}

        src_list, dst_list, w_list = [], [], []
        for source, neighbors in core_adj.items():
            i = index[int(source)]
            for target, weight in neighbors:
                src_list.append(i)
                dst_list.append(index[int(target)])
                w_list.append(weight)

        edge_index = (
            np.array([src_list, dst_list], dtype=np.int64)
            if src_list else np.zeros((2, 0), dtype=np.int64)
        )
        sg = SparseGraph(
            node_ids=node_ids,
            edge_index=edge_index,
            edge_weight=np.array(w_list, dtype=np.float64),
            class_map=class_map,
        )
        logger.info(
            "build_sparse_graph_for_subgraph: %s/%s → %d nodes, %d edges (COO)",
            subgraph_slug, snapshot_id, sg.n_nodes, sg.n_edges,
        )
        return sg
