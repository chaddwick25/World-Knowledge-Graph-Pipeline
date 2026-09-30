"""
TransE Graph Embedding Service

Native PyTorch implementation of TransE for topological constraint validation.
Per Mann et al., ISWC 2023 - USLP Section 3.3

TransE formula: d = ||h + r - t||_1 (L1 norm)
Score conversion: c = 1.0 / (1.0 + d)

This replaces the OpenKE dependency with a lightweight native implementation.
"""

import logging
import torch
import numpy as np
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# TODO: refactoe and make notebook
class TransEGraphEmbeddingService:
    """
    Native PyTorch TransE implementation for spatial link prediction.
    
    Computes topological constraint scores for (head, relation, tail) triplets.
    """
    
    def __init__(self):
        self.entity_embeddings = None
        self.relation_embeddings = None
        self.entity2id: Dict[str, int] = {}
        self.rel2id: Dict[str, int] = {}
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    
    def load_model(self, model_path: str, entity2id: Dict, rel2id: Dict):
        """
        Load pre-trained TransE embeddings.
        
        Args:
            model_path: Path to .pt file with entity and relation embeddings
            entity2id: Dict mapping entity IDs to embedding indices
            rel2id: Dict mapping relation names to embedding indices
        """
        try:
            checkpoint = torch.load(model_path, map_location=self.device)
            self.entity_embeddings = checkpoint['entity_embeddings'].to(self.device)
            self.relation_embeddings = checkpoint['relation_embeddings'].to(self.device)
            self.entity2id = entity2id
            self.rel2id = rel2id
            logger.info(f"TransE: Loaded {len(entity2id)} entities, {len(rel2id)} relations")
        except Exception as e:
            logger.error(f"TransE: Failed to load model from {model_path}: {e}")
            raise
    
    def score_triple(self, head_id: int, relation: str, tail_id: int) -> float:
        """
        Score a (head, relation, tail) triplet using TransE.
        
        Formula: d = ||h + r - t||_1
        Score: c = 1.0 / (1.0 + d)
        
        Args:
            head_id: OSM ID of head entity
            relation: Relation name (e.g., 'isInCounty')
            tail_id: OSM ID of tail entity
        
        Returns:
            Topological constraint score in (0, 1]
        """
        if self.entity_embeddings is None or self.relation_embeddings is None:
            return 0.0
        
        # Map IDs to embedding indices
        head_idx = self.entity2id.get(str(head_id))
        tail_idx = self.entity2id.get(str(tail_id))
        rel_idx = self.rel2id.get(relation)
        
        if head_idx is None or tail_idx is None or rel_idx is None:
            return 0.0
        
        # Get embeddings
        h = self.entity_embeddings[head_idx]
        r = self.relation_embeddings[rel_idx]
        t = self.entity_embeddings[tail_idx]
        
        # TransE distance: ||h + r - t||_1
        distance = torch.norm(h + r - t, p=1).item()
        
        # Convert to score: c = 1 / (1 + d)
        score = 1.0 / (1.0 + distance)
        
        return score
    
    def score_batch(self, triplets: list) -> np.ndarray:
        """
        Score a batch of triplets.
        
        Args:
            triplets: List of (head_id, relation, tail_id) tuples
        
        Returns:
            Array of scores
        """
        scores = []
        for head_id, relation, tail_id in triplets:
            scores.append(self.score_triple(head_id, relation, tail_id))
        return np.array(scores)
