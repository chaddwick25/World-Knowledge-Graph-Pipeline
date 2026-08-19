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
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)


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
        import networkx as nx
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
            "build_graph: loaded %d entities for %s/%s (k=%d)",
            len(entities), country_code, snapshot_id, k,
        )

        adj = self.build_knn_graph(entities)

        G = nx.Graph()
        for osm_id, cls in class_map.items():
            G.add_node(osm_id, wkg_class=cls)
        for source, neighbors in adj.items():
            if source not in G:
                G.add_node(source)
            for target, weight in neighbors:
                if target not in G:
                    G.add_node(target)
                # Undirected — keep the max weight if both directions exist
                if G.has_edge(source, target):
                    G[source][target]['weight'] = max(
                        G[source][target]['weight'], weight
                    )
                else:
                    G.add_edge(source, target, weight=weight)

        logger.info(
            "build_graph: %s/%s → %d nodes, %d edges",
            country_code, snapshot_id, G.number_of_nodes(), G.number_of_edges(),
        )
        return G
