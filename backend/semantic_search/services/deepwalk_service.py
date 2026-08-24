"""
Enhanced Weighted DeepWalk Service with Explicit Optimization Control

Improvements:
1. Explicit negative sampling control
2. Learning rate scheduling visibility
3. Loss monitoring via callbacks
4. Damped edge weights (GeoVectors paper)
5. Hyperparameter validation
"""

import numpy as np
from typing import List, Dict, Tuple, Optional, Callable
from collections import defaultdict
import logging
from gensim.models import Word2Vec
from gensim.models.callbacks import CallbackAny2Vec
from multiprocessing import Pool, cpu_count
import random

logger = logging.getLogger(__name__)

# Global variables for multiprocessing
_GLOBAL_GRAPH = None
_GLOBAL_WALK_LENGTH = None

def _init_walk_worker(graph, walk_length):
    """Initialize worker with graph data."""
    global _GLOBAL_GRAPH, _GLOBAL_WALK_LENGTH
    _GLOBAL_GRAPH = graph
    _GLOBAL_WALK_LENGTH = walk_length
    # Seed random for this worker
    random.seed()
    np.random.seed()

def _generate_single_walk(start_node):
    """Generate a single weighted random walk (worker function)."""
    global _GLOBAL_GRAPH, _GLOBAL_WALK_LENGTH
    
    walk = [str(start_node)]
    current = start_node
    
    for _ in range(_GLOBAL_WALK_LENGTH - 1):
        if current not in _GLOBAL_GRAPH or not _GLOBAL_GRAPH[current]:
            break
        
        neighbors = _GLOBAL_GRAPH[current]
        neighbor_ids = np.array([n for n, w in neighbors])
        weights = np.array([w for n, w in neighbors])
        
        if weights.sum() == 0:
            break
        
        probs = weights / weights.sum()
        next_node = np.random.choice(neighbor_ids, p=probs)
        walk.append(str(next_node))
        current = next_node
    
    return walk


class LossLogger(CallbackAny2Vec):
    """Callback to log training loss per epoch."""
    
    def __init__(self):
        self.epoch = 0
        self.losses = []
    
    def on_epoch_end(self, model):
        loss = model.get_latest_training_loss()
        self.losses.append(loss)
        logger.info(f"Epoch {self.epoch}: loss={loss:.4f}")
        self.epoch += 1


class WeightedDeepWalkService:
    """
    Weighted DeepWalk with explicit optimization control.
    
    Key Enhancements:
    - Negative sampling hyperparameter control
    - Learning rate scheduling
    - Loss monitoring
    - Damped edge weights (GeoVectors formula)

    IMPORTANT — double-damping guard:
    ``KNNGraphService.calculate_edge_weight()`` already applies the GeoVectors
    training formula ``max(1/ln(max(d,1.1)), e)`` and returns pre-damped weights
    in the range [e, ~10.5].  If you pass such weights to ``apply_damped_weights``
    a second time, the formula ``max(1/ln(max(w,1.1)), e)`` collapses *all*
    weights to e (≈ 2.718) because 1/ln(≥e) ≤ 1 < e — distance information is
    destroyed.  Always set ``apply_damping=False`` (the default) when the graph
    originates from ``KNNGraphService.build_knn_graph()``.
    """
    
    def __init__(self,
                 embedding_dim: int = 100,
                 walk_length: int = 80,
                 num_walks: int = 10,
                 window_size: int = 5,
                 workers: int = 4,
                 min_count: int = 0,
                 
                 # Optimization hyperparameters (GeoVectors paper §5.1)
                 negative: int = 5,              # Negative samples
                 alpha: float = 0.025,           # Initial learning rate
                 min_alpha: float = 0.0001,      # Final learning rate
                 sample: float = 1e-5,           # Subsampling threshold (lower for graph node IDs)
                 ns_exponent: float = 0.75,      # Negative sampling power
                 epochs: int = 1,                # Single pass (walks provide diversity)
                 
                 # Advanced options
                 apply_damping: bool = False,    # False: weights from KNNGraphService are pre-damped
                 compute_loss: bool = True,      # Monitor training loss
                 seed: int = 42):
        """
        Initialize enhanced DeepWalk trainer.

        Args:
            embedding_dim:  Output embedding dimension (100 per GeoVectors-master).
            walk_length:    Number of nodes per walk (80 per paper).
            num_walks:      Walks per starting node (10 per paper).
            window_size:    Skip-gram context window (5 per paper).
            workers:        Parallel workers.
            min_count:      Minimum node frequency.

            negative:       Number of negative samples per positive.
            alpha:          Initial learning rate.
            min_alpha:      Final learning rate (linear decay).
            sample:         High-frequency subsampling threshold.
            ns_exponent:    Negative sampling distribution power (0.75 = standard).
            epochs:         Training epochs.

            apply_damping:  Apply ``max(1/ln(max(w,1.1)), e)`` damping inside
                            ``apply_damped_weights()``.  Set to ``True`` only
                            when the graph holds *raw haversine distances* as
                            weights.  Leave ``False`` (default) when the graph
                            comes from ``KNNGraphService.build_knn_graph()``
                            whose weights are already damped — re-applying
                            collapses all weights to e and destroys distance
                            information.
            compute_loss:   Track training loss per epoch.
            seed:           Random seed for reproducibility.
        """
        self.embedding_dim = embedding_dim
        self.walk_length = walk_length
        self.num_walks = num_walks
        self.window_size = window_size
        self.workers = workers
        self.min_count = min_count
        
        # Optimization hyperparameters
        self.negative = negative
        self.alpha = alpha
        self.min_alpha = min_alpha
        self.sample = sample
        self.ns_exponent = ns_exponent
        self.epochs = epochs
        
        # Advanced options
        self.apply_damping = apply_damping
        self.compute_loss = compute_loss
        self.seed = seed
        
        self.model = None
        self.loss_logger = None
        
        # Validate hyperparameters
        self._validate_hyperparameters()
    
    def _validate_hyperparameters(self):
        """Validate optimization hyperparameters."""
        if not 1 <= self.negative <= 20:
            logger.warning(f"Unusual negative samples: {self.negative}. "
                          f"Typical range: 5-20")
        
        if not 0.001 <= self.alpha <= 0.1:
            logger.warning(f"Unusual learning rate: {self.alpha}. "
                          f"Typical range: 0.01-0.05")
        
        if self.ns_exponent != 0.75:
            logger.info(f"Using non-standard negative sampling exponent: "
                       f"{self.ns_exponent} (default=0.75)")
    
    def apply_damped_weights(self, graph: Dict[int, List[Tuple[int, float]]]) -> Dict[int, List[Tuple[int, float]]]:
        """
        Apply the GeoVectors damped-weight formula to a graph whose edge weights
        are **raw haversine distances in kilometres**.

        Formula (GeoVectors paper §3.2 / WeightedDeepWalkGraph._damp_and_row_norm):
            w' = max(1 / ln(max(w, 1.1)), e)   where e ≈ 2.718
        The 1.1 km clamp gives every sub-1.1 km distance the same maximum weight
        (1/ln(1.1) ≈ 10.49); the e floor keeps every edge's transition probability
        non-zero for the random walk.

        WARNING — pre-damped input:
        If the graph was built by ``KNNGraphService.build_knn_graph()``, its weights
        are already in the range [e, ~10.5] (output of ``calculate_edge_weight``).
        Applying this formula a second time maps every weight to
        ``max(1/ln(≥e), e) = max(≤1, e) = e``, collapsing the entire distribution
        to a constant and destroying all distance information.  Do not call this
        method on pre-damped graphs — keep ``apply_damping=False`` (the default).

        Args:
            graph: k-NN graph whose edge weights are raw haversine distances (km).

        Returns:
            Graph with damped weights (same structure).

        Raises:
            ValueError: If any weight is already ≥ e, indicating the graph is
                pre-damped and calling this method would be incorrect.
        """
        # Guard: detect pre-damped weights using the hard floor signature.
        # calculate_edge_weight() always floors at exactly np.e, so the minimum
        # weight in a pre-damped graph is np.e (within float precision).
        # Raw haversine distances hitting np.e exactly is astronomically unlikely
        # and their minimum will typically be << e for any graph with close neighbors.
        # Full scan (O(E), O(1) memory) — a partial sample could miss the floor.
        min_weight = min(
            (w for neighbors in graph.values() for _, w in neighbors),
            default=None,
        )
        if min_weight is not None and abs(min_weight - np.e) < 1e-9:
            raise ValueError(
                "apply_damped_weights received a graph whose minimum edge weight "
                f"equals e ({np.e:.6f}), the hard floor set by "
                "KNNGraphService.calculate_edge_weight — indicating the graph is "
                "pre-damped. Re-applying damping collapses all weights to e and "
                "destroys distance information. Set apply_damping=False when using "
                "KNNGraphService output."
            )

        damped_graph = {}
        for node, neighbors in graph.items():
            damped_neighbors = []
            for neighbor_id, weight in neighbors:
                # Damped weight: max(1/ln(max(w, 1.1)), e) — matches the reference
                # WeightedDeepWalkGraph._damp_and_row_norm clamp semantics exactly:
                # sub-1.1 km distances get the maximum weight 1/ln(1.1), not a
                # lower value, and zero/negative weights cannot divide by ln(0).
                damped_w = max(1.0 / np.log(max(weight, 1.1)), np.e)
                damped_neighbors.append((neighbor_id, damped_w))
            damped_graph[node] = damped_neighbors

        logger.info("Applied damped edge weights (GeoVectors §3.2 training formula)")
        return damped_graph
    
    def generate_weighted_walk(self, 
                               graph: Dict[int, List[Tuple[int, float]]], 
                               start_node: int) -> List[str]:
        """
        Generate single weighted random walk.
        
        Uses edge weights as sampling probabilities.
        """
        walk = [str(start_node)]
        current = start_node
        
        for _ in range(self.walk_length - 1):
            neighbors = graph.get(current, [])
            if not neighbors:
                break
            
            # Extract neighbor IDs and weights
            neighbor_ids = [n[0] for n in neighbors]
            weights = np.array([n[1] for n in neighbors])
            
            # Normalize to probabilities
            probs = weights / weights.sum()
            
            # Sample next node
            next_node = np.random.choice(neighbor_ids, p=probs)
            walk.append(str(next_node))
            current = next_node
        
        return walk
    
    def generate_walks(self, graph: Dict[int, List[Tuple[int, float]]]) -> List[List[str]]:
        """Generate all random walks using memory-safe batched parallel processing."""
        nodes = list(graph.keys())
        num_nodes = len(nodes)
        total_walks = num_nodes * self.num_walks
        
        # Use fewer workers to prevent OOM (4 workers instead of 26)
        # Each worker gets a copy of the 66M-edge graph, so we limit parallelism
        num_workers = min(4, cpu_count())
        
        logger.info(f"Generating {total_walks} walks using {num_workers} workers (memory-safe mode)...")
        logger.info(f"Graph size: {len(graph)} nodes, {sum(len(v) for v in graph.values())} edges")
        
        # Create list of starting nodes (each node repeated num_walks times)
        start_nodes = []
        for _ in range(self.num_walks):
            shuffled = nodes.copy()
            np.random.shuffle(shuffled)
            start_nodes.extend(shuffled)
        
        # Process in batches to limit memory usage
        batch_size = 500000  # Process 500k walks at a time
        all_walks = []
        
        for batch_start in range(0, len(start_nodes), batch_size):
            batch_end = min(batch_start + batch_size, len(start_nodes))
            batch_nodes = start_nodes[batch_start:batch_end]
            
            logger.info(f"Processing batch {batch_start//batch_size + 1}/{(len(start_nodes)-1)//batch_size + 1}: "
                       f"{len(batch_nodes)} walks ({batch_start}/{len(start_nodes)})...")
            
            chunksize = max(1, len(batch_nodes) // (num_workers * 4))
            
            with Pool(num_workers, initializer=_init_walk_worker, initargs=(graph, self.walk_length)) as pool:
                batch_walks = pool.map(_generate_single_walk, batch_nodes, chunksize=chunksize)
            
            all_walks.extend(batch_walks)
            logger.info(f"  Batch complete: {len(all_walks)}/{len(start_nodes)} total walks generated")
        
        logger.info(f"Generated {len(all_walks)} walks (avg length: {np.mean([len(w) for w in all_walks]):.1f})")
        return all_walks
    
    def train(self, graph: Dict[int, List[Tuple[int, float]]]) -> 'Word2Vec':
        """
        Train Skip-Gram model with explicit optimization control.
        
        Args:
            graph: k-NN graph structure
            
        Returns:
            Trained Word2Vec model
        """
        # Apply damped weights if enabled
        if self.apply_damping:
            graph = self.apply_damped_weights(graph)
        
        # Generate walks
        walks = self.generate_walks(graph)
        
        # Setup loss logging
        callbacks = []
        if self.compute_loss:
            self.loss_logger = LossLogger()
            callbacks.append(self.loss_logger)
        
        # Log optimization hyperparameters
        logger.info(
            f"Training Skip-Gram with explicit optimization:\n"
            f"  Embedding dim: {self.embedding_dim}\n"
            f"  Window size: {self.window_size}\n"
            f"  Negative samples: {self.negative}\n"
            f"  Learning rate: {self.alpha} → {self.min_alpha}\n"
            f"  Subsampling: {self.sample}\n"
            f"  NS exponent: {self.ns_exponent}\n"
            f"  Epochs: {self.epochs}\n"
            f"  Damped weights: {self.apply_damping}"
        )
        
        # Train Word2Vec with explicit hyperparameters
        model = Word2Vec(
            sentences=walks,
            vector_size=self.embedding_dim,
            window=self.window_size,
            min_count=self.min_count,
            sg=1,                          # Skip-Gram
            hs=0,                          # Use negative sampling (not hierarchical softmax)
            negative=self.negative,        # Number of negative samples
            alpha=self.alpha,              # Initial learning rate
            min_alpha=self.min_alpha,      # Final learning rate
            sample=self.sample,            # Subsampling threshold
            ns_exponent=self.ns_exponent,  # Negative sampling distribution power
            workers=self.workers,
            epochs=self.epochs,
            compute_loss=self.compute_loss,
            callbacks=callbacks,
            seed=self.seed
        )
        
        self.model = model
        
        # Report training results
        logger.info(f"Training complete. Vocabulary: {len(model.wv)} entities")
        
        if self.compute_loss and self.loss_logger:
            logger.info(f"Loss trajectory: {self.loss_logger.losses}")
        
        return model
    
    def get_optimization_report(self) -> Dict:
        """
        Get detailed optimization report.
        
        Returns:
            Dictionary with optimization metrics
        """
        if self.model is None:
            return {'error': 'Model not trained'}
        
        report = {
            'vocabulary_size': len(self.model.wv),
            'embedding_dim': self.embedding_dim,
            'hyperparameters': {
                'negative_samples': self.negative,
                'learning_rate_initial': self.alpha,
                'learning_rate_final': self.min_alpha,
                'subsampling_threshold': self.sample,
                'ns_exponent': self.ns_exponent,
                'window_size': self.window_size,
                'epochs': self.epochs,
            },
            'damped_weights': self.apply_damping,
        }
        
        if self.loss_logger:
            report['losses'] = self.loss_logger.losses
            report['final_loss'] = self.loss_logger.losses[-1] if self.loss_logger.losses else None
        
        return report
    
    def get_embedding(self, node_id: int) -> np.ndarray:
        """Get embedding for specific node."""
        if self.model is None:
            raise ValueError("Model not trained")
        
        node_str = str(node_id)
        if node_str not in self.model.wv:
            raise ValueError(f"Node {node_id} not in vocabulary")
        
        return self.model.wv[node_str]
    
    def get_all_embeddings(self) -> Dict[int, np.ndarray]:
        """Get all node embeddings."""
        if self.model is None:
            raise ValueError("Model not trained")
        
        return {
            int(node_id): self.model.wv[node_id]
            for node_id in self.model.wv.index_to_key
        }
    
    def save_model(self, path: str):
        """Save trained model."""
        if self.model is None:
            raise ValueError("Model not trained")
        
        self.model.save(path)
        logger.info(f"Model saved to {path}")
    
    def load_model(self, path: str):
        """Load trained model."""
        self.model = Word2Vec.load(path)
        logger.info(f"Model loaded from {path}")
