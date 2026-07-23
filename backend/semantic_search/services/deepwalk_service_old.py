"""
Weighted DeepWalk Service for GV-NLE Spatial Embeddings

Implements weighted random walks and Skip-Gram training to generate spatial embeddings
from k-NN graphs. Based on the GeoVectors paper approach.

Process:
1. Generate weighted random walks on k-NN graph
2. Train Skip-Gram model (similar to Word2Vec) on walks
3. Entity embeddings capture spatial context

Walk sampling uses edge weights to bias towards closer neighbors.
"""

import numpy as np
from typing import List, Dict, Tuple
from collections import defaultdict
import logging
from gensim.models import Word2Vec

logger = logging.getLogger(__name__)


class WeightedDeepWalkService:
    """
    Trains spatial embeddings using weighted DeepWalk on k-NN graphs.
    
    The weighted random walk algorithm:
    - Start at random node
    - At each step, sample next node proportional to edge weights
    - Generate walks of fixed length
    - Repeat for many walks per node
    
    The Skip-Gram model learns embeddings where:
    - Entities appearing in similar walk contexts get similar embeddings
    - Captures geographic proximity patterns
    """
    
    def __init__(self, 
                 embedding_dim: int = 300,
                 walk_length: int = 80,
                 num_walks: int = 10,
                 window_size: int = 10,
                 workers: int = 4,
                 min_count: int = 0,
                 sg: int = 1,  # Skip-Gram (1) vs CBOW (0)
                 epochs: int = 5):
        """
        Initialize weighted DeepWalk trainer.
        
        Args:
            embedding_dim: Dimension of output embeddings (default: 300)
            walk_length: Length of each random walk (default: 80)
            num_walks: Number of walks per node (default: 10)
            window_size: Context window for Skip-Gram (default: 10)
            workers: Number of parallel workers (default: 4)
            min_count: Minimum frequency for entities (default: 0 to include all)
            sg: 1 for Skip-Gram, 0 for CBOW (default: 1)
            epochs: Training epochs (default: 5)
        """
        self.embedding_dim = embedding_dim
        self.walk_length = walk_length
        self.num_walks = num_walks
        self.window_size = window_size
        self.workers = workers
        self.min_count = min_count
        self.sg = sg
        self.epochs = epochs
        self.model = None
    
    def generate_weighted_walk(self, 
                               graph: Dict[int, List[Tuple[int, float]]], 
                               start_node: int) -> List[str]:
        """
        Generate a single weighted random walk starting from a node.
        
        Weighted sampling: Probability of choosing neighbor j from node i is:
            P(j|i) = w(i,j) / Σ(w(i,k)) for all neighbors k
        
        Args:
            graph: k-NN graph structure {node: [(neighbor, weight), ...]}
            start_node: Starting node for walk
            
        Returns:
            List of node IDs (as strings) representing the walk path
        """
        walk = [str(start_node)]
        current = start_node
        
        for _ in range(self.walk_length - 1):
            if current not in graph or len(graph[current]) == 0:
                # Dead end - can't continue walk
                break
            
            neighbors = graph[current]
            
            # Extract neighbor IDs and weights
            neighbor_ids = [n for n, w in neighbors]
            weights = np.array([w for n, w in neighbors])
            
            # Normalize weights to probabilities
            probabilities = weights / weights.sum()
            
            # Sample next node
            next_node = np.random.choice(neighbor_ids, p=probabilities)
            walk.append(str(next_node))
            current = next_node
        
        return walk
    
    def generate_walks(self, 
                       graph: Dict[int, List[Tuple[int, float]]]) -> List[List[str]]:
        """
        Generate all random walks for the graph.
        
        Args:
            graph: k-NN graph structure
            
        Returns:
            List of walks, where each walk is a list of node ID strings
        """
        nodes = list(graph.keys())
        num_nodes = len(nodes)
        total_walks = num_nodes * self.num_walks
        
        logger.info(f"Generating {total_walks} walks ({self.num_walks} per node, "
                   f"length {self.walk_length})...")
        
        walks = []
        for walk_iter in range(self.num_walks):
            if walk_iter > 0 and walk_iter % 2 == 0:
                logger.info(f"Walk iteration {walk_iter}/{self.num_walks}...")
            
            # Shuffle nodes for each iteration
            np.random.shuffle(nodes)
            
            for i, node in enumerate(nodes):
                if i % 5000 == 0 and i > 0:
                    logger.info(f"  Generated {i}/{num_nodes} walks for iteration {walk_iter}...")
                
                walk = self.generate_weighted_walk(graph, node)
                walks.append(walk)
        
        logger.info(f"Generated {len(walks)} walks total")
        return walks
    
    def train(self, graph: Dict[int, List[Tuple[int, float]]]) -> 'Word2Vec':
        """
        Train Skip-Gram model on weighted random walks.
        
        Args:
            graph: k-NN graph structure
            
        Returns:
            Trained Word2Vec model
        """
        # Generate walks
        walks = self.generate_walks(graph)
        
        logger.info(f"Training Skip-Gram model "
                   f"(dim={self.embedding_dim}, window={self.window_size}, "
                   f"epochs={self.epochs})...")
        
        # Train Word2Vec (Skip-Gram)
        model = Word2Vec(
            sentences=walks,
            vector_size=self.embedding_dim,
            window=self.window_size,
            min_count=self.min_count,
            sg=self.sg,
            workers=self.workers,
            epochs=self.epochs,
            seed=42  # For reproducibility
        )
        
        self.model = model
        
        logger.info(f"Training complete. Vocabulary size: {len(model.wv)}")
        return model
    
    def get_embedding(self, node_id: int) -> np.ndarray:
        """
        Get embedding for a specific node.
        
        Args:
            node_id: OSM entity ID
            
        Returns:
            300D embedding vector (or configured dimension)
            
        Raises:
            ValueError: If node not in vocabulary
        """
        if self.model is None:
            raise ValueError("Model not trained yet. Call train() first.")
        
        node_str = str(node_id)
        if node_str not in self.model.wv:
            raise ValueError(f"Node {node_id} not in vocabulary")
        
        return self.model.wv[node_str]
    
    def get_all_embeddings(self) -> Dict[int, np.ndarray]:
        """
        Get embeddings for all nodes in vocabulary.
        
        Returns:
            Dict mapping node_id -> embedding vector
        """
        if self.model is None:
            raise ValueError("Model not trained yet. Call train() first.")
        
        embeddings = {}
        for node_str in self.model.wv.index_to_key:
            node_id = int(node_str)
            embeddings[node_id] = self.model.wv[node_str]
        
        return embeddings
    
    def encode_unseen_entity(self, 
                             entity_coords: Tuple[float, float],
                             training_entities: List[Dict],
                             k: int = 50) -> np.ndarray:
        """
        Encode unseen entity using weighted average of k nearest training entities.
        
        From GeoVectors paper:
            GV-NLE(o_new) = Σ(w(o_new, o_i) · NLE(o_i)) / Σ(w(o_new, o_i))
        where:
            w(o_new, o_i) = ln(1 + 1/dist(o_new, o_i))
        
        Args:
            entity_coords: (lat, lon) of unseen entity
            training_entities: List of dicts with 'osm_id', 'lat', 'lon'
            k: Number of nearest neighbors to use (default: 50)
            
        Returns:
            300D embedding vector for unseen entity
        """
        from semantic_search.services.knn_graph_service import KNNGraphService
        
        if self.model is None:
            raise ValueError("Model not trained yet. Call train() first.")
        
        # Calculate distances to all training entities
        knn_service = KNNGraphService(k=k)
        distances = []
        
        for entity in training_entities:
            entity_coord = (entity['lat'], entity['lon'])
            dist = knn_service.calculate_distance(entity_coords, entity_coord)
            
            # Check if entity embedding exists
            if str(entity['osm_id']) in self.model.wv:
                distances.append((entity['osm_id'], dist))
        
        # Sort by distance and take k nearest
        distances.sort(key=lambda x: x[1])
        k_nearest = distances[:k]
        
        # Calculate weighted average
        weighted_sum = np.zeros(self.embedding_dim)
        weight_sum = 0.0
        
        for osm_id, dist_km in k_nearest:
            weight = knn_service.calculate_edge_weight(dist_km)
            embedding = self.model.wv[str(osm_id)]
            weighted_sum += weight * embedding
            weight_sum += weight
        
        return weighted_sum / weight_sum
    
    def save_model(self, path: str):
        """Save trained model to disk."""
        if self.model is None:
            raise ValueError("Model not trained yet. Call train() first.")
        
        self.model.save(path)
        logger.info(f"Model saved to {path}")
    
    def load_model(self, path: str):
        """Load trained model from disk."""
        self.model = Word2Vec.load(path)
        logger.info(f"Model loaded from {path}")
