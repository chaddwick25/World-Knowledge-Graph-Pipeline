"""
GPU-Accelerated USLP Service

Extends SpatialLinkPredictionService with GPU acceleration for:
- Batch FastText embedding generation
- Vectorized haversine distance calculations
- GPU-based scoring and filtering

Provides 10-20x speedup over CPU-optimized version.
"""

import logging
import numpy as np
import torch
from typing import List, Dict, Optional
from igea.services.spatial_link_prediction import (
    SpatialLinkPredictionService,
    SPATIAL_LITERAL_TAGS,
    TAG_TO_RELATION,
    RELATION_GEOHASH_PRECISION,
    RELATION_TO_NATURAL_TEXT,
    ACCEPTANCE_THRESHOLD,
)
from igea.services.gpu_fasttext_service import GPUFastTextService
from igea.services.gpu_haversine_service import GPUHaversineService

logger = logging.getLogger(__name__)


class GPUAcceleratedUSLP(SpatialLinkPredictionService):
    """
    GPU-accelerated Unsupervised Spatial Link Prediction service.
    
    Extends the base USLP service with GPU acceleration for:
    - FastText embeddings (batch processing)
    - Haversine distance (vectorized)
    - Cosine similarity (GPU tensors)
    """
    
    def __init__(self, device: str = 'cuda:0', use_fp64: bool = False, **kwargs):
        """
        Initialize GPU-accelerated USLP service.
        
        Args:
            device: CUDA device ('cuda:0', 'cuda:1', etc.)
            use_fp64: Use FP64 precision for haversine (recommended for K80)
            **kwargs: Additional arguments for base class
        """
        super().__init__(**kwargs)
        
        self.device = torch.device(device if torch.cuda.is_available() else 'cpu')
        self.use_fp64 = use_fp64
        
        # GPU services
        self.gpu_fasttext = None
        self.gpu_haversine = None
        
        # GPU cache for candidate pool
        self._gpu_pool_coords = None
        self._gpu_pool_embeddings = None
        self._gpu_pool_class_embeddings = None
        
        if torch.cuda.is_available():
            logger.info(f"GPU USLP: Using device {self.device} ({torch.cuda.get_device_name(self.device)})")
            logger.info(f"GPU USLP: FP64 precision = {use_fp64}")
        else:
            logger.warning("GPU USLP: CUDA not available, falling back to CPU")
    
    def _init_gpu_services(self):
        """Lazy initialize GPU services."""
        if self.gpu_fasttext is None:
            from django.conf import settings
            config = getattr(settings, 'SPATIAL_SEMANTICS_CONFIG', {})
            model_path = config.get('fasttext_model_path', settings.FASTTEXT_MODEL_PATH)
            self.gpu_fasttext = GPUFastTextService(
                model_path=model_path,
                device=str(self.device)
            )
        
        if self.gpu_haversine is None:
            self.gpu_haversine = GPUHaversineService(
                device=str(self.device),
                use_fp64=self.use_fp64
            )
    
    def load_candidate_pool(self, entities: List[Dict]) -> int:
        """
        Load candidate pool and precompute GPU embeddings.
        
        Args:
            entities: List of candidate entity dicts
            
        Returns:
            Number of candidates loaded
        """
        # Call parent to load pool
        count = super().load_candidate_pool(entities)
        
        # Precompute GPU data
        self._precompute_gpu_data()
        
        return count
    
    def _precompute_gpu_data(self):
        """Precompute GPU tensors for candidate pool."""
        if not self._pool:
            return
        
        self._init_gpu_services()
        
        logger.info(f"GPU USLP: Precomputing embeddings for {len(self._pool):,} candidates...")
        
        # Extract coordinates
        coords = np.array([[e['lat'], e['lon']] for e in self._pool], dtype=np.float32)
        self._gpu_pool_coords = torch.from_numpy(coords).to(self.device)
        
        # Log candidate pool bounds for debugging
        lats = coords[:, 0]
        lons = coords[:, 1]
        logger.info(f"GPU USLP: Candidate pool bounds - lat: [{lats.min():.4f}, {lats.max():.4f}], lon: [{lons.min():.4f}, {lons.max():.4f}]")
        
        # Precompute name embeddings (names are in tags dict, matching CPU _name_score)
        names = []
        for e in self._pool:
            tags = e.get('tags', {})
            name = tags.get('name', tags.get('name:en', ''))
            names.append(name or '')
        self._gpu_pool_embeddings = self.gpu_fasttext.precompute_embeddings(names)
        
        # Precompute class embeddings (fall back to OSM tags when wkg_class is None)
        classes = []
        for e in self._pool:
            wkg_class = e.get('wkg_class')
            if wkg_class:
                classes.append(wkg_class.replace('wkgs:', '').replace(':', ' '))
            else:
                classes.append(SpatialLinkPredictionService._derive_class_text_from_tags(e.get('tags', {})) or '')
        self._gpu_pool_class_embeddings = self.gpu_fasttext.precompute_embeddings(classes)
        
        logger.info(f"GPU USLP: Precomputed {len(self._pool):,} embeddings on {self.device}")
        logger.info(f"GPU USLP: Memory allocated: {torch.cuda.memory_allocated(self.device) / 1024**3:.2f} GB")
    
    def predict_links_for_entity(
        self,
        head_osm_id: str,
        head_lat: float,
        head_lon: float,
        head_tags: Dict,
        threshold: float = ACCEPTANCE_THRESHOLD,
        top_k: int = 5,
    ) -> List[Dict]:
        """
        GPU-accelerated link prediction for a single entity.
        
        Uses vectorized GPU operations for scoring all candidates.
        """
        if not self._pool:
            logger.warning("GPU USLP: pool is empty")
            return []
        
        self._init_gpu_services()
        
        all_scored = []
        
        for tag_key, tag_value in head_tags.items():
            if tag_key not in SPATIAL_LITERAL_TAGS and tag_key not in TAG_TO_RELATION:
                continue
            if not isinstance(tag_value, str) or not tag_value.strip():
                continue
            
            relation = TAG_TO_RELATION.get(tag_key, tag_key)
            literal = tag_value.strip()
            
            # Get search radius
            precision = RELATION_GEOHASH_PRECISION.get(relation, 4)
            radius_km = {1: 5000, 3: 156, 4: 39}.get(precision, 156)
            
            # GPU-accelerated spatial filtering
            candidate_lats = self._gpu_pool_coords[:, 0]
            candidate_lons = self._gpu_pool_coords[:, 1]
            
            indices, distances = self.gpu_haversine.filter_by_radius(
                head_lat, head_lon,
                candidate_lats, candidate_lons,
                radius_km
            )
            
            # Debug: log first few entities with spatial filtering results
            # if head_osm_id in [self._pool[i]['osm_id'] for i in range(min(5, len(self._pool)))] or len(all_scored) == 0:
            #     logger.info(f"GPU USLP: Entity {head_osm_id} at ({head_lat:.4f}, {head_lon:.4f}), relation={relation}, radius={radius_km}km, candidates_in_radius={len(indices)}/{len(self._pool)}")
            
            if len(indices) == 0:
                continue
            
            # Filter out self
            mask = torch.tensor([self._pool[i]['osm_id'] != head_osm_id for i in indices.cpu().numpy()],
                              device=self.device)
            indices = indices[mask]
            distances = distances[mask]
            
            if len(indices) == 0:
                continue
            
            # GPU-accelerated scoring
            total_scores, geo_scores, name_scores, class_scores = self._score_candidates_gpu(
                literal, relation, distances, indices
            )
            
            # total_scores are unnormalized sums [0, 3.0]
            normalized_scores = total_scores / 3.0
            
            # Debug: log top scores for first few entities (log regardless of threshold)
            if len(all_scored) < 5:  # Log first 5 entities processed
                top_k_scores = torch.topk(normalized_scores, min(3, len(normalized_scores)))
                top_indices = torch.topk(normalized_scores, min(3, len(normalized_scores))).indices
                # logger.info(f"GPU USLP: Entity {head_osm_id} relation={relation} literal={literal} top scores: {top_k_scores.values.cpu().numpy()} (threshold={threshold})")
                # if len(top_indices) > 0:
                #     logger.info(f"Calculating scores for top candidates...")
                # for i, idx in enumerate(top_indices):
                #     logger.info(f"GPU USLP:   - [{i}] geo={geo_scores[idx]:.3f}, name={name_scores[idx]:.3f}, class={class_scores[idx]:.3f}, total={total_scores[idx]:.3f}, norm={normalized_scores[idx]:.3f}")
            
            # Collect all scored candidates (threshold filtering done in persist_links)
            for idx, total, norm, geo, name, cls in zip(
                indices.cpu().numpy(),
                total_scores.cpu().numpy(),
                normalized_scores.cpu().numpy(),
                geo_scores.cpu().numpy(),
                name_scores.cpu().numpy(),
                class_scores.cpu().numpy()
            ):
                all_scored.append({
                    'head_osm_id': head_osm_id,
                    'relation': relation,
                    'tail_osm_id': self._pool[idx]['osm_id'],
                    'unnormalized_score': round(float(total), 4),
                    'normalized_score': round(float(norm), 4),
                    'score': round(float(norm), 4),  # backward-compat alias
                    'geo_score': round(float(geo), 4),
                    'name_score': round(float(name), 4),
                    'topo_score': round(float(cls), 4),
                    'literal': literal,
                })
        
        # Sort and return top_k
        all_scored.sort(key=lambda x: x['normalized_score'], reverse=True)
        return all_scored[:top_k]
    
    def _score_candidates_gpu(self,
                             literal: str,
                             relation: str,
                             distances: torch.Tensor,
                             indices: torch.Tensor) -> tuple:
        """
        GPU-accelerated scoring for candidate entities.
        
        Args:
            literal: Literal string from head entity
            relation: Relation type
            distances: Haversine distances (N,)
            indices: Candidate indices (N,)
            
        Returns:
            Tuple of (total_scores, geo_scores, name_scores, class_scores)
        """
        # Geo score: 1 / (1 + dist_km)
        geo_scores = 1.0 / (1.0 + distances)
        
        # Name score: cosine similarity
        literal_emb = self.gpu_fasttext.calculate_embedding_batch([literal])
        candidate_name_embs = self._gpu_pool_embeddings[indices]
        name_scores = self.gpu_fasttext.cosine_similarity_batch(literal_emb, candidate_name_embs)
        
        # Class score: cosine similarity (use natural language mapping for relation)
        rel_text = RELATION_TO_NATURAL_TEXT.get(relation, relation)
        relation_emb = self.gpu_fasttext.calculate_embedding_batch([rel_text])
        candidate_class_embs = self._gpu_pool_class_embeddings[indices]
        class_scores = self.gpu_fasttext.cosine_similarity_batch(relation_emb, candidate_class_embs)
        
        # Total score (sum of three components)
        total_scores = geo_scores + name_scores + class_scores
        
        return total_scores, geo_scores, name_scores, class_scores
    
    def get_gpu_info(self) -> Dict:
        """Get GPU device and memory information."""
        if not torch.cuda.is_available():
            return {'cuda_available': False, 'device': 'cpu'}
        
        info = {
            'cuda_available': True,
            'device': str(self.device),
            'device_name': torch.cuda.get_device_name(self.device),
            'memory_allocated_gb': torch.cuda.memory_allocated(self.device) / 1024**3,
            'memory_reserved_gb': torch.cuda.memory_reserved(self.device) / 1024**3,
            'pool_size': len(self._pool) if self._pool else 0,
            'gpu_embeddings_loaded': self._gpu_pool_embeddings is not None,
        }
        
        return info
