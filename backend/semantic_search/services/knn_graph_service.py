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
from dataclasses import dataclass, field
from haversine import haversine, Unit
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)


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



class KNNGraphService:
    """
    Builds k-NN graphs for spatial embedding generation.
    
    The graph structure captures geographic relationships:
    - Nodes: OSM entities (identified by osm_id)
    - Edges: k nearest neighbors
    - Weights: Logarithmically damped inverse distance
    
    Formula from GeoVectors paper:
        w(o1, o2) = ln(1 + 1/dist_km(o1, o2))
    
    This weighting scheme:
    - Gives higher weights to closer entities
    - Prevents extreme weights for very close entities (log damping)
    - Ensures all weights > 0
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
        Calculate edge weight from distance using GeoVectors TRAINING formula.
        
        Formula (from WeightedDeepWalkGraph.py lines 32-33):
            w'(d) = max(1/ln(max(d, 1.1)), e)
        
        Where:
        - d: haversine distance in kilometers
        - Minimum distance clamped to 1.1 km (avoids ln(1) = 0)
        - Result clamped to e ≈ 2.718 as minimum weight
        - Effect: Closer entities get exponentially higher weights
        
        For very close entities (distance ≈ 0), we use max(epsilon, 1.1) to prevent
        both division by zero and ln(1) = 0. This handles OSM entities with 
        identical coordinates.
        
        Args:
            distance_km: Distance in kilometers
            epsilon: Safety minimum distance (default: 1e-6 km = 1mm)
            
        Returns:
            Edge weight (higher = closer), minimum value of e ≈ 2.718
        """
        # Clamp distance to minimum 1.1 km (GeoVectors paper requirement)
        # Also handle duplicate coordinates with epsilon
        distance_km = max(distance_km, max(epsilon, 1.1))
        
        # GeoVectors training formula: w' = max(1/ln(d), e)
        weight = 1.0 / np.log(distance_km)
        
        # Clamp to minimum e ≈ 2.718
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
        nn = NearestNeighbors(
            n_neighbors=k + 1, 
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
