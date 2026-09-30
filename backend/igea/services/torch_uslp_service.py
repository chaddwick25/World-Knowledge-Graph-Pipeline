import logging
import torch
import numpy as np
import geohash2
from typing import List, Dict, Tuple, Optional
from collections import defaultdict

from igea.services.gpu_uslp_service import GPUAcceleratedUSLP
from igea.services.spatial_link_prediction import (
    RELATION_GEOHASH_PRECISION,
    RELATION_TO_NATURAL_TEXT,
    _D_MAX_PER_CLUSTER,
    _FALLBACK_D_MAX,
)

logger = logging.getLogger(__name__)


class TorchUSLP(GPUAcceleratedUSLP):
    """
    Full-GPU batched USLP scoring with precomputed geohash cluster similarity matrices.
    Overrides the pool load and batched prediction paths to eliminate per-entity Python loops
    and use PyTorch for the entire 3-space scoring pipeline.
    """

    def __init__(self, device: str = 'cuda:0', use_fp64: bool = False, head_batch_size: int = 256, **kwargs):
        super().__init__(device=device, use_fp64=use_fp64, **kwargs)
        self.head_batch_size = head_batch_size
        self._geohash_matrices: Dict[int, Tuple[torch.Tensor, torch.Tensor]] = {}
        self._geohash_hash_to_idx: Dict[int, Dict[str, int]] = {}
        self._relation_emb_cache: Dict[str, torch.Tensor] = {}
        # Per-precision (N,) d_max tensors aligned with pool rows, for the
        # paper's geo formula 1 - d/d_max (Fix 1, §3.1.4).
        self._d_max_tensors: Dict[int, torch.Tensor] = {}

    def load_candidate_pool(self, entities: List[Dict]) -> int:
        """
        Load candidate pool, precompute GPU embeddings, build geohash similarity matrices,
        and cache relation embeddings.
        """
        count = super().load_candidate_pool(entities)
        if count == 0:
            return count

        # Precompute geohash cluster similarity matrices for each precision level used
        self._build_geohash_matrices()
        
        # Cache relation embeddings to avoid CPU calls during scoring
        self._build_relation_embedding_cache()
        
        return count

    def _build_geohash_matrices(self):
        """
        Precompute geohash cluster centers in radians for the candidate pool
        for each precision level. This avoids OOV issues with head entities
        that aren't in the pool's geohash set.

        Also builds per-row d_max tensors for the paper's geo formula
        (1 - d/d_max, d_max per tail cluster — Fix 1, §3.1.4).
        """
        logger.info(f"TorchUSLP: Precomputing candidate geohash centers on {self.device}...")
        lats_np = self._gpu_pool_coords[:, 0].cpu().numpy()
        lons_np = self._gpu_pool_coords[:, 1].cpu().numpy()

        for precision in [1, 3, 4]:
            geohashes = [geohash2.encode(lat, lon, precision=precision)
                         for lat, lon in zip(lats_np, lons_np)]

            centers = torch.zeros((len(geohashes), 2), dtype=torch.float32, device=self.device)
            for i, h in enumerate(geohashes):
                lat_s, lon_s = geohash2.decode(h)
                centers[i, 0] = float(lat_s)
                centers[i, 1] = float(lon_s)

            # Store as radians to speed up scoring Haversine
            self._geohash_matrices[precision] = torch.deg2rad(centers)

            # Per-row d_max from the module global (populated by the parent
            # class's load_candidate_pool via _compute_d_max_per_precision).
            d_max_map = _D_MAX_PER_CLUSTER.get(precision, {})
            fallback = _FALLBACK_D_MAX.get(precision, 39.0)
            d_max_np = np.array(
                [d_max_map.get(g, fallback) for g in geohashes], dtype=np.float32
            )
            self._d_max_tensors[precision] = torch.from_numpy(d_max_np).to(self.device)

            logger.info(f"TorchUSLP: Precomputed precision {precision} centers (size: {centers.shape[0]}x2)")

    def _build_relation_embedding_cache(self):
        """Precompute and cache GPU embeddings for all USLP relations."""
        self._init_gpu_services()
        logger.info(f"TorchUSLP: Caching FastText embeddings for {len(RELATION_TO_NATURAL_TEXT)} relations...")
        for relation, nat_text in RELATION_TO_NATURAL_TEXT.items():
            cpu_emb = self.gpu_fasttext.calculate_embedding(nat_text)  # (300,) np
            self._relation_emb_cache[relation] = (
                torch.from_numpy(cpu_emb).unsqueeze(0).to(self.device)  # (1, 300) GPU
            )

    def _get_relation_embedding(self, relation: str) -> torch.Tensor:
        """Get cached relation embedding, computing it on-the-fly if missing."""
        if relation not in self._relation_emb_cache:
            nat_text = RELATION_TO_NATURAL_TEXT.get(relation, relation)
            cpu_emb = self.gpu_fasttext.calculate_embedding(nat_text)
            self._relation_emb_cache[relation] = torch.from_numpy(cpu_emb).unsqueeze(0).to(self.device)
        return self._relation_emb_cache[relation]

    def _group_heads_by_relation(self, head_entities: List[Dict]) -> Dict[str, List[Dict]]:
        """
        Group head entities by relation based on their spatial literal tags.
        A single head entity may appear in multiple groups if it has multiple tags.
        """
        from igea.services.spatial_link_prediction import SPATIAL_LITERAL_TAGS, TAG_TO_RELATION
        
        relation_groups = defaultdict(list)
        for head in head_entities:
            tags = head.get('tags', {})
            for tag_key, tag_value in tags.items():
                if tag_key not in SPATIAL_LITERAL_TAGS and tag_key not in TAG_TO_RELATION:
                    continue
                if not isinstance(tag_value, str) or not tag_value.strip():
                    continue

                relation = TAG_TO_RELATION.get(tag_key, tag_key)
                literal = tag_value.strip()
                
                # Create a shallow copy for this specific relation/literal pair
                head_instance = head.copy()
                head_instance['literal'] = literal
                relation_groups[relation].append(head_instance)
                
        return relation_groups

    def score_batch(
        self,
        head_literal_embs: torch.Tensor,
        head_lats: torch.Tensor,
        head_lons: torch.Tensor,
        relation: str,
        return_components: bool = False,
    ):
        """
        Score H head entities against all N pool candidates in three spaces.

        Args:
            return_components: When True, return a tuple
                (normalized_scores, geo_scores, name_scores, class_scores)
                with the three component (H, N) matrices kept separate (for
                per-space persistence — Fix 2).  When False (default, hot
                path), return only the (H, N) normalized score matrix with
                in-place accumulation to bound GPU memory.

        Returns:
            (H, N) normalized score matrix, or a 4-tuple of
            (normalized, geo, name, class) (H, N) matrices.
        """
        precision = RELATION_GEOHASH_PRECISION.get(relation, 4)
        pool_centers_rad = self._geohash_matrices[precision]

        # -- Space 1: Geo (geohash cluster similarity) --
        # Decode head geohash centers
        head_geohashes = [geohash2.encode(lat, lon, precision=precision)
                          for lat, lon in zip(head_lats.cpu().numpy(), head_lons.cpu().numpy())]

        head_centers = torch.zeros((len(head_geohashes), 2), dtype=torch.float32, device=self.device)
        for i, h in enumerate(head_geohashes):
            lat_s, lon_s = geohash2.decode(h)
            head_centers[i, 0] = float(lat_s)
            head_centers[i, 1] = float(lon_s)

        head_centers_rad = torch.deg2rad(head_centers)
        del head_centers

        # (H, N) Haversine distance between geohash centers
        dlat = pool_centers_rad[:, 0].unsqueeze(0) - head_centers_rad[:, 0].unsqueeze(1)
        dlon = pool_centers_rad[:, 1].unsqueeze(0) - head_centers_rad[:, 1].unsqueeze(1)

        a = (torch.sin(dlat / 2)**2
             + torch.cos(head_centers_rad[:, 0]).unsqueeze(1)
             * torch.cos(pool_centers_rad[:, 0]).unsqueeze(0)
             * torch.sin(dlon / 2)**2)
        del dlat, dlon, head_centers_rad

        dist_km_centers = 6371.0 * 2 * torch.arcsin(torch.sqrt(a.clamp(0, 1)))
        del a
        # Paper formula: 1 - d/d_max, d_max per tail cluster (column max of
        # the reference repo's distance matrix; dataprep_utils.py:83).
        d_max_rows = self._d_max_tensors[precision]  # (N,) km, per pool row
        geo_scores = torch.clamp(
            1.0 - dist_km_centers / d_max_rows.unsqueeze(0), min=0.0, max=1.0
        )
        del dist_km_centers

        # Spatial radius mask
        # 5000km, 156km and 39km
        radius_km = {1: 5000.0, 3: 156.0, 4: 39.0}.get(precision, 156.0)
        pool_lats = self._gpu_pool_coords[:, 0]
        pool_lons = self._gpu_pool_coords[:, 1]

        dlat = torch.deg2rad(pool_lats.unsqueeze(0) - head_lats.unsqueeze(1))
        dlon = torch.deg2rad(pool_lons.unsqueeze(0) - head_lons.unsqueeze(1))
        a = (torch.sin(dlat / 2)**2
             + torch.cos(torch.deg2rad(head_lats)).unsqueeze(1)
             * torch.cos(torch.deg2rad(pool_lats)).unsqueeze(0)
             * torch.sin(dlon / 2)**2)
        del dlat, dlon
        dist_km = 6371.0 * 2 * torch.arcsin(torch.sqrt(a.clamp(0, 1)))
        del a
        radius_mask_f = (dist_km <= radius_km).float()
        del dist_km

        geo_scores *= radius_mask_f

        # -- Space 2: Name (cosine similarity literal -> candidate name) --
        name_scores = torch.mm(head_literal_embs, self._gpu_pool_embeddings.t())

        # -- Space 3: Class (cosine similarity relation -> candidate class) --
        rel_emb = self._get_relation_embedding(relation)
        class_scores = torch.mm(rel_emb, self._gpu_pool_class_embeddings.t())
        class_scores = class_scores.expand(head_literal_embs.shape[0], -1)

        if return_components:
            # Keep components separate for per-space persistence (Fix 2).
            # Apply radius mask to geo only (name/class are not spatial).
            total = geo_scores + name_scores + class_scores
            total *= radius_mask_f
            total /= 3.0
            return total, geo_scores, name_scores, class_scores

        # -- Total score (in-place accumulation to minimize allocations) --
        geo_scores += name_scores
        del name_scores
        geo_scores += class_scores
        geo_scores *= radius_mask_f
        del radius_mask_f

        geo_scores /= 3.0
        return geo_scores

    def predict_links_batched(
        self,
        head_entities: List[Dict],
        threshold: float = 0.7,
        top_k: int = 50,
        head_batch_size: Optional[int] = None,
    ) -> List[Dict]:
        """
        Full-GPU batched scoring for all head entities.
        """
        if not self._pool:
            logger.warning("TorchUSLP: pool is empty")
            return []

        self._init_gpu_services()
        requested = head_batch_size or self.head_batch_size
        batch_size = self._adaptive_head_batch_size(requested)
        all_links = []
        
        relation_groups = self._group_heads_by_relation(head_entities)
        logger.info(f"TorchUSLP: Grouped {len(head_entities)} entities into {len(relation_groups)} relations")

        for relation, heads_with_literals in relation_groups.items():
            for chunk_start in range(0, len(heads_with_literals), batch_size):
                chunk = heads_with_literals[chunk_start : chunk_start + batch_size]

                lits = [h['literal'] for h in chunk]
                lit_embs = self.gpu_fasttext.calculate_embedding_batch(lits)
                lats = torch.tensor([h['lat'] for h in chunk], dtype=torch.float32, device=self.device)
                lons = torch.tensor([h['lon'] for h in chunk], dtype=torch.float32, device=self.device)
                scores, geo_mat, name_mat, class_mat = self.score_batch(
                    lit_embs, lats, lons, relation, return_components=True
                )
                # TODO: Update this to collect the rejected links
                for i, head in enumerate(chunk):
                    row = scores[i]
                    mask = row >= threshold
                    if not mask.any():
                        continue

                    k = min(top_k, mask.sum().item())
                    top_vals, top_idxs = torch.topk(row[mask], k)
                    masked_pool_idxs = torch.where(mask)[0][top_idxs]

                    # Gather per-space scores at the selected top-k indices.
                    geo_row = geo_mat[i][masked_pool_idxs].cpu().tolist()
                    name_row = name_mat[i][masked_pool_idxs].cpu().tolist()
                    class_row = class_mat[i][masked_pool_idxs].cpu().tolist()

                    for j, (score_val, pool_idx) in enumerate(
                        zip(top_vals.cpu().tolist(), masked_pool_idxs.cpu().tolist())
                    ):
                        all_links.append({
                            'head_osm_id': head['osm_id'],
                            'relation': relation,
                            'tail_osm_id': self._pool[pool_idx]['osm_id'],
                            'normalized_score': round(score_val, 4),
                            'unnormalized_score': round(score_val * 3.0, 4),
                            'score': round(score_val, 4),
                            'geo_score': round(geo_row[j], 4),
                            'name_score': round(name_row[j], 4),
                            'topo_score': round(class_row[j], 4),
                            'literal': head['literal'],
                        })

        return all_links

    def _adaptive_head_batch_size(self, requested: int) -> int:
        """Reduce head batch size for large pools to avoid GPU OOM.

        Uses torch.cuda.mem_get_info (free bytes on device) when available,
        which accounts for allocations by OTHER processes on the same GPU
        — important when multiple subgraph USLP tasks share the GPU.
        Falls back to total - allocated for older PyTorch versions.
        """
        N = len(self._pool) if self._pool else 0
        if not torch.cuda.is_available() or N == 0:
            return requested

        # torch.cuda.mem_get_info returns (free, total) — free accounts
        # for other processes' allocations. Available in PyTorch >= 1.11.
        try:
            free_bytes, _total = torch.cuda.mem_get_info(self.device)
        except AttributeError:
            free_bytes = (
                torch.cuda.get_device_properties(self.device).total_memory
                - torch.cuda.memory_allocated(self.device)
            )

        # Peak: ~6 simultaneous (H, N) float32 tensors during haversine
        # plus the radius mask computation (2 more H×N tensors).
        # Use 0.50 multiplier for safety — concurrent tasks may allocate
        # between our check and our tensor creation.
        bytes_per_head = N * 4 * 8
        safe = max(1, int(free_bytes * 0.50 / bytes_per_head))

        if safe < requested:
            logger.warning(
                "TorchUSLP: Adaptive batch size %d -> %d "
                "(pool=%s, free=%.1f GB)",
                requested, safe, f"{N:,}",
                free_bytes / 1024**3,
            )
        return min(requested, safe)

    def predict_links_batch(
        self,
        entities: List[Dict],
        threshold: float = 0.7,
        top_k: int = 50,
    ) -> List[Dict]:
        """Override to use the batched GPU path instead of entity loop."""
        logger.info(f"TorchUSLP: Starting batched prediction for {len(entities)} entities (batch_size={self.head_batch_size})")
        return self.predict_links_batched(entities, threshold, top_k)
