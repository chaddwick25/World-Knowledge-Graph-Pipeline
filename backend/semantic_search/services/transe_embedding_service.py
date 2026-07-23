"""
Native PyTorch TransE implementation replacing OpenKE dependency.

This service natively restores the mathematical intent of `osm2kg` by implementing 
the TransE Knowledge Graph Embedding algorithm (h + r = t). DeepWalk natively clusters 
undirected proximities, whereas TransE uniquely isolates explicit topological relationships 
(e.g., 'isInCounty', 'addrCity').
"""

import logging
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Dict, List, Tuple
import numpy as np

logger = logging.getLogger(__name__)

class TransEModule(nn.Module):
    """
    TransE Scoring Module: d(h, r, t) = || h + r - t ||_p
    """
    def __init__(self, num_entities: int, num_relations: int, emb_dim: int = 300, p_norm: int = 1):
        super(TransEModule, self).__init__()
        self.emb_dim = emb_dim
        self.p_norm = p_norm
        
        self.entity_embeddings = nn.Embedding(num_entities, emb_dim)
        self.relation_embeddings = nn.Embedding(num_relations, emb_dim)
        
        # Initialize uniformly according to original paper
        limit = 6.0 / np.sqrt(emb_dim)
        nn.init.uniform_(self.entity_embeddings.weight, -limit, limit)
        nn.init.uniform_(self.relation_embeddings.weight, -limit, limit)
        
        # Normalize relation embeddings
        with torch.no_grad():
            self.relation_embeddings.weight.copy_(
                F.normalize(self.relation_embeddings.weight, p=2, dim=1)
            )

    def forward(self, head, relation, tail):
        # Normalize entities
        h = F.normalize(self.entity_embeddings(head), p=2, dim=1)
        t = F.normalize(self.entity_embeddings(tail), p=2, dim=1)
        r = self.relation_embeddings(relation)
        
        score = torch.norm(h + r - t, p=self.p_norm, dim=1)
        return score

    
class TransEGraphEmbeddingService:
    """
    Lightweight interface for generating, restoring, and evaluating hierarchical 
    TransE relational vectors locally.
    """
    def __init__(self, emb_dim: int = 300, p_norm: int = 1):
        self.emb_dim = emb_dim
        self.p_norm = p_norm
        self.model = None
        self.entity2id = {}
        self.relation2id = {}

    def load_model(self, model_path: str, entity2id: Dict[str, int], relation2id: Dict[str, int]):
        """Load pretrained TransE weights."""
        self.entity2id = entity2id
        self.relation2id = relation2id
        self.model = TransEModule(len(self.entity2id), len(self.relation2id), self.emb_dim, self.p_norm)
        self.model.load_state_dict(torch.load(model_path, map_location=torch.device('cpu')))
        self.model.eval()
        logger.info(f"Loaded TransE structure with {len(entity2id)} entities and {len(relation2id)} relations.")

    def score_triple(self, head_str: str, relation_str: str, tail_str: str) -> float:
        """
        Calculates exact OpenKE/TransE margin score without the OpenKE framework. 
        Lower score = higher probability they are linked!
        """
        if not self.model:
            return float('inf')
        
        try:
            h_id = self.entity2id[head_str]
            r_id = self.relation2id[relation_str]
            t_id = self.entity2id[tail_str]
        except KeyError:
            return float('inf') # Penalize severely if out of vocabulary
            
        head = torch.tensor([h_id], dtype=torch.long)
        relation = torch.tensor([r_id], dtype=torch.long)
        tail = torch.tensor([t_id], dtype=torch.long)
        
        with torch.no_grad():
            score = self.model(head, relation, tail).item()
        
        return score
