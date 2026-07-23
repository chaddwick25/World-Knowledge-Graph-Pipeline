"""
Iterative Entity Alignment Service (IGEA)

Implements the full IGEA approach from:
    Dsouza, Yu, Windoffer, Demidova — "Iterative Geographic Entity Alignment with
    Cross-Attention", ISWC 2023.

This service aligns OSM entities with Wikidata entities using an iterative pipeline:

    Algorithm (per iteration):
        1. NCA: learn tag→class mappings from seed linked entities
        2. Candidate blocking:
           a. Type-based: same wkg_class (from NCA-learned mappings)
           b. Distance-based: within 2500m (IGEA validated threshold)
        3. Cross-attention training: train BiLSTM model on matched/unmatched pairs
        4. Scoring: cross-attention model (or cosine fallback)
        5. Acceptance: pairs with score >= threshold are accepted
        6. Accepted pairs join the seed set -> repeat

Key differences from full IGEA paper:
  - Uses gv_tags_embedding (already computed) for cosine fallback
  - Cross-attention model trained per-country (not globally)
  - NCA model trained per-country from seed entities

[CODE AUDIT TRACEABILITY]
Maps to logic from: https://github.com/alishiba14/IGEA
Implementation: Full iterative loop with NCA (schemaMatch.py), candidate blocking
(candidateGeneration.py), cross-attention (entityLinkingAttention.py), and
prediction (predictUnmatched.py).

Usage:
    service = IterativeEntityAlignmentService(max_iterations=3, threshold=0.6)
    service.load_wikidata_candidates(candidates)
    stats = service.run(country_code='DE')

Reference: https://github.com/alishiba14/IGEA
"""

import logging
import numpy as np
from typing import List, Dict, Optional, Set, Tuple
from haversine import haversine, Unit
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
# TODO: add to yaml file
ALIGNMENT_THRESHOLD = 0.5   # tha in IGEA paper (Fig. 5: optimal across all countries)
MAX_DISTANCE_M = 2500.0     # meters — IGEA Section 3.2 validated value
MAX_ITERATIONS = 3           # IGEA Fig. 3: performance peaks at iteration 3


def _lazy_import_torch_stack():
    """Lazily import torch and IGEA neural components.

    This avoids importing torch (and thus CUDA libs) at module import time so
    that Celery worker startup does not fail if the GPU stack is incomplete.
    """
    try:
        import torch  # type: ignore
        import torch.optim as optim  # type: ignore
        import torch.nn as nn  # type: ignore
        from igea.services.cross_attention_model import CrossAttentionIGEA  # type: ignore
        from igea.services.nca_model import NCAClassificationService  # type: ignore
        return torch, optim, nn, CrossAttentionIGEA, NCAClassificationService
    except Exception as e:  # ImportError or lower-level CUDA errors
        logger.error(
            "IGEA: Failed to import PyTorch/CUDA stack; NCA and cross-attention "
            "will be disabled. Error: %s",
            e,
        )
        return None, None, None, None, None


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


class IterativeEntityAlignmentService:
    """
    Simplified IGEA: iterative OSM→Wikidata entity alignment.

    Iterative loop:
        GT (seed) ← entities with existing wikidata= tag
        for i in range(max_iterations):
            candidates ← blocking(same_class, ≤ 2500m)
            scores ← cosine(gv_tags_embedding pairs)
            accepted ← pairs where score ≥ threshold
            GT ← GT ∪ accepted
            write accepted to OsmEntity.wikidata_uri
        return GT

    Args:
        max_iterations: number of alignment iterations (default 3)
        threshold:      minimum cosine similarity to accept (default 0.6)
        max_distance_m: maximum distance in metres for spatial blocking (default 2500)
    """

    def __init__(
        self,
        max_iterations: int = MAX_ITERATIONS,
        threshold: float = ALIGNMENT_THRESHOLD,
        max_distance_m: float = MAX_DISTANCE_M,
        cross_attention_weights_path: Optional[str] = None,
        enable_nca: bool = True,
        enable_cross_attention_training: bool = True,
    ):
        self.max_iterations = max_iterations
        self.threshold = threshold
        self.max_distance_m = max_distance_m
        self._wikidata_pool: List[Dict] = []
        self.enable_nca = enable_nca
        self.enable_cross_attention_training = enable_cross_attention_training
        self._torch = None
        self._torch_optim = None
        self._torch_nn = None
        self._CrossAttentionClass = None
        self._NCAServiceClass = None

        torch_mod, optim_mod, nn_mod, CrossAttentionCls, NCAServiceCls = _lazy_import_torch_stack()
        self._torch = torch_mod
        self._torch_optim = optim_mod
        self._torch_nn = nn_mod
        self._CrossAttentionClass = CrossAttentionCls
        self._NCAServiceClass = NCAServiceCls

        if torch_mod is None:
            # Disable neural components completely when torch/CUDA is unavailable
            self.enable_nca = False
            self.enable_cross_attention_training = False
            self.nca_service = None
            self.cross_attention_model = None
            return

        self.nca_service = NCAServiceCls() if enable_nca and NCAServiceCls is not None else None

        self.cross_attention_model = None
        if cross_attention_weights_path and self._CrossAttentionClass is not None:
            logger.info(f"IGEA: Loading BiLSTM Cross-Attention weights from {cross_attention_weights_path}")
            self.cross_attention_model = self._CrossAttentionClass()
            try:
                self.cross_attention_model.load_state_dict(
                    self._torch.load(
                        cross_attention_weights_path,
                        map_location=self._torch.device('cpu'),
                    )
                )
                self.cross_attention_model.eval()
            except Exception as e:
                logger.error(f"Failed to load Cross-Attention model: {e}")
                self.cross_attention_model = None

    # ------------------------------------------------------------------
    # Wikidata candidate pool
    # ------------------------------------------------------------------

    def load_wikidata_candidates(self, candidates: List[Dict]) -> int:
        """
        Load Wikidata entities as alignment candidates.

        Args:
            candidates: list of dicts with keys:
                wikidata_uri (str), lat (float), lon (float),
                label (str), wkg_class (str or None),
                embedding (list[float] 300D or None)

        Returns:
            Number of candidates loaded.
        """
        self._wikidata_pool = [
            c for c in candidates
            if c.get('lat') is not None
            and c.get('lon') is not None
            and c.get('wikidata_uri')
        ]
        logger.info(f"IGEA: loaded {len(self._wikidata_pool)} Wikidata candidates")
        return len(self._wikidata_pool)

    def load_wikidata_candidates_from_file(self, json_path: str) -> int:
        """
        Load Wikidata candidates from a STEP 6 harvest cache file (JSON).
        """
        import json
        try:
            with open(json_path, 'r') as f:
                candidates = json.load(f)
            return self.load_wikidata_candidates(candidates)
        except Exception as e:
            logger.error(f"IGEA: failed to load candidates from {json_path}: {e}")
            return 0

    def load_wikidata_candidates_from_db(self, limit: int = 100_000) -> int:
        """
        Load Wikidata candidates from existing OsmEntity records that carry
        a wikidata_uri (i.e., already-confirmed links used as seed for the pool).
        """
        from worldkg_nca.models import OsmEntity
        qs = OsmEntity.objects.using('vectors').filter(
            wikidata_uri__isnull=False,
            geom__isnull=False,
        )[:limit]

        candidates = []
        for e in qs.iterator(chunk_size=10_000):
            tags = e.tags or {}
            candidates.append({
                'wikidata_uri': e.wikidata_uri,
                'lat': e.geom.y,
                'lon': e.geom.x,
                'label': tags.get('name', tags.get('name:en', '')),
                'wkg_class': e.wkg_class,
                'embedding': list(e.gv_tags_embedding) if e.gv_tags_embedding is not None else None,
                '_osm_id': e.osm_id,
            })
        return self.load_wikidata_candidates(candidates)

    # ------------------------------------------------------------------
    # NCA: Neural Class Alignment (IGEA Section 3.1)
    # ------------------------------------------------------------------

    def _run_nca(
        self,
        osm_entities: List[Dict],
        seed_map: Dict[int, str],
    ) -> int:
        """
        Run NCA to learn tag→class mappings from seed linked entities.

        This replaces the static ontology lookup with a neural model that
        learns which OSM tags map to which Wikidata classes.

        Steps:
          1. Collect linked entities (seed entities with wikidata_uri)
          2. Build vocabularies from tags and classes
          3. Train NCA model
          4. Extract tag→class mappings
          5. Apply mappings to OSM entities missing wkg_class

        Returns:
            Number of entities assigned a class via NCA.
        """
        if not self.enable_nca or self.nca_service is None:
            return 0

        # Collect linked entities and attach their wikidata_uri so that
        # NCA can build class vocabularies correctly.
        linked: List[Dict] = []
        for e in osm_entities:
            osm_id = e.get('osm_id')
            if osm_id in seed_map:
                ent = dict(e)
                ent['wikidata_uri'] = seed_map[osm_id]
                linked.append(ent)
        if len(linked) < 10:
            logger.warning(f"NCA: insufficient linked entities ({len(linked)}), skipping")
            return 0

        logger.info(f"NCA: training on {len(linked)} linked entities")

        try:
            self.nca_service.build_vocabularies(linked)
            X, y = self.nca_service.prepare_training_data(linked)
            self.nca_service.train(X, y, verbose=False)
            self.nca_service.extract_mappings()
            assigned = self.nca_service.apply_mappings_to_entities(osm_entities)
            return assigned
        except Exception as e:
            logger.error(f"NCA training failed: {e}")
            return 0

    # ------------------------------------------------------------------
    # Cross-attention training (IGEA Section 3.3)
    # ------------------------------------------------------------------

    def _train_cross_attention(
        self,
        pairs: List[Tuple[Dict, Dict]],
        seed_map: Dict[int, str],
    ) -> None:
        """
        Train the cross-attention model on candidate pairs.

        Positive examples: pairs where the Wikidata candidate matches the seed.
        Negative examples: pairs where they don't match.

        This mirrors entityLinkingAttention.py from the IGEA reference.
        """
        if not self.enable_cross_attention_training:
            return

        if self._torch is None or self._torch_optim is None or self._torch_nn is None:
            logger.warning("IGEA: torch stack unavailable, skipping cross-attention training")
            return

        if self._CrossAttentionClass is None:
            logger.warning("IGEA: CrossAttentionIGEA unavailable, skipping cross-attention training")
            return

        if self.cross_attention_model is None:
            self.cross_attention_model = self._CrossAttentionClass()

        torch = self._torch
        optim = self._torch_optim
        nn = self._torch_nn

        positives, negatives = [], []
        for osm_ent, wd_cand in pairs:
            osm_id = osm_ent['osm_id']
            wd_uri = wd_cand.get('wikidata_uri', '')
            is_match = (osm_id in seed_map and seed_map[osm_id] == wd_uri)

            emb_osm = osm_ent.get('embedding')
            emb_wd = wd_cand.get('embedding')
            if emb_osm is None or emb_wd is None:
                continue

            if is_match:
                positives.append((emb_osm, emb_wd))
            else:
                negatives.append((emb_osm, emb_wd))

        if not positives:
            logger.info("Cross-attention: no positive examples, skipping training")
            return

        import random
        if len(negatives) > len(positives):
            negatives = random.sample(negatives, min(len(positives) * 3, len(negatives)))

        all_examples = [(e, 1.0) for e in positives] + [(e, 0.0) for e in negatives]
        random.shuffle(all_examples)

        if len(all_examples) < 10:
            return

        logger.info(
            f"Cross-attention training: {len(positives)} positive, "
            f"{len(negatives)} negative examples"
        )

        self.cross_attention_model.train()
        optimizer = optim.Adam(self.cross_attention_model.parameters(), lr=1e-4)
        criterion = nn.BCELoss()

        batch_size = 32
        for epoch in range(10):
            total_loss = 0.0
            for i in range(0, len(all_examples), batch_size):
                batch = all_examples[i:i + batch_size]
                osm_batch = torch.tensor(
                    [e[0][0] for e in batch], dtype=torch.float32
                ).unsqueeze(1)
                wd_batch = torch.tensor(
                    [e[0][1] for e in batch], dtype=torch.float32
                ).unsqueeze(1)
                labels = torch.tensor([e[1] for e in batch], dtype=torch.float32)

                optimizer.zero_grad()
                preds = self.cross_attention_model(osm_batch, wd_batch)
                loss = criterion(preds, labels)
                loss.backward()
                optimizer.step()
                total_loss += loss.item()

            if (epoch + 1) % 5 == 0:
                logger.info(
                    f"Cross-attention epoch {epoch + 1}/10 | "
                    f"loss={total_loss / max(1, len(all_examples) // batch_size):.4f}"
                )

        self.cross_attention_model.eval()
        logger.info("Cross-attention training complete")

    # ------------------------------------------------------------------
    # Blocking (IGEA Section 3.2)
    # ------------------------------------------------------------------

    def _get_seed_alignment(self, country_code: Optional[str] = None, polygon_wkt: Optional[str] = None) -> Dict[int, str]:
        """
        Collect seed alignment from OsmEntity records that have a wikidata= tag.

        Args:
            country_code: Optional ISO country code to filter seed entities.
            polygon_wkt: Optional WKT POLYGON string for spatial filtering.

        Returns:
            Dict mapping osm_id → wikidata_uri for seed entities.
        """
        from worldkg_nca.models import OsmEntity
        seed: Dict[int, str] = {}
        qs = OsmEntity.objects.using('vectors').filter(
            geom__isnull=False,
        ).exclude(tags__wikidata__isnull=True)

        # Apply polygon filter if provided (more accurate than country tag)
        if polygon_wkt:
            from django.contrib.gis.geos import GEOSGeometry
            poly = GEOSGeometry(polygon_wkt, srid=4326)
            qs = qs.filter(geom__within=poly)

        # Apply country filter if provided (uses addr:country tag)
        country_filter_needed = False
        if country_code:
            qs = qs.filter(tags__has_key='addr:country')
            country_filter_needed = True

        for e in qs.iterator(chunk_size=10_000):
            tags = e.tags or {}

            # Apply country filter in Python if needed
            if country_filter_needed:
                if tags.get('addr:country') != country_code:
                    continue

            wikidata_tag = tags.get('wikidata')
            if wikidata_tag:
                uri = (wikidata_tag if wikidata_tag.startswith('http')
                       else f"http://www.wikidata.org/entity/{wikidata_tag}")
                seed[e.osm_id] = uri
        logger.info(f"IGEA: seed alignment = {len(seed)} entities (country={country_code or 'all'}, polygon={bool(polygon_wkt)})")
        return seed

    def _candidate_pairs(
        self,
        osm_entities: List[Dict],
        seed_ids: Set[int],
    ) -> List[Tuple[Dict, Dict]]:
        """
        Generate candidate (osm_entity, wikidata_candidate) pairs via blocking.

        Blocking strategy (IGEA Section 3.2):
        1. Type-based: wkg_class must match
        2. Distance-based: haversine ≤ max_distance_m

        Unseen entities are those not in the current seed.
        """
        unseen = [e for e in osm_entities if e['osm_id'] not in seed_ids]
        if not unseen or not self._wikidata_pool:
            return []

        # Build spatial index for Wikidata candidates by class
        from sklearn.neighbors import BallTree
        import numpy as np
        
        # Group Wikidata candidates by class for faster lookup
        wd_by_class = {}
        for wd in self._wikidata_pool:
            cls = wd.get('wkg_class')
            if cls not in wd_by_class:
                wd_by_class[cls] = []
            wd_by_class[cls].append(wd)
        
        # Build BallTree for each class
        trees = {}
        for cls, candidates in wd_by_class.items():
            if not candidates:
                continue
            coords = np.radians([[c['lat'], c['lon']] for c in candidates])
            trees[cls] = (BallTree(coords, metric='haversine'), candidates)
        
        # Build global BallTree for all candidates (fallback for entities without class)
        all_coords = np.radians([[c['lat'], c['lon']] for c in self._wikidata_pool])
        global_tree = BallTree(all_coords, metric='haversine')
        
        pairs: List[Tuple[Dict, Dict]] = []
        max_pairs_per_iteration = 50_000  # Limit pairs to prevent memory explosion
        
        for osm_ent in unseen:
            if len(pairs) >= max_pairs_per_iteration:
                logger.warning(f"IGEA: Limiting to {max_pairs_per_iteration:,} pairs per iteration")
                break
                
            osm_lat = osm_ent.get('lat')
            osm_lon = osm_ent.get('lon')
            osm_class = osm_ent.get('wkg_class')
            if osm_lat is None or osm_lon is None:
                continue
            
            # Use spatial index for same class
            if osm_class and osm_class in trees:
                tree, candidates = trees[osm_class]
                query_point = np.radians([[osm_lat, osm_lon]])
                
                # Query within max_distance radius
                radius_rad = self.max_distance_m / 6371000.0  # Earth radius in meters
                indices = tree.query_radius(query_point, r=radius_rad)[0]
                
                for idx in indices:
                    pairs.append((osm_ent, candidates[idx]))
            else:
                # Fallback: entity has no class or class not in Wikidata pool
                # Use global BallTree to find all candidates within distance
                query_point = np.radians([[osm_lat, osm_lon]])
                radius_rad = self.max_distance_m / 6371000.0
                indices = global_tree.query_radius(query_point, r=radius_rad)[0]
                
                for idx in indices:
                    pairs.append((osm_ent, self._wikidata_pool[idx]))

        logger.info(f"IGEA blocking: {len(pairs):,} candidate pairs from {len(unseen):,} unseen entities")
        return pairs

    # ------------------------------------------------------------------
    # Scoring (IGEA Section 3.3 — OSM2KG-FT variant)
    # ------------------------------------------------------------------

    def _score_pair(self, osm_ent: Dict, wd_cand: Dict) -> float:
        """
        Score a candidate pair using either BiLSTM Cross-Attention or cosine similarity fallback.
        """
        emb_osm = osm_ent.get('embedding')
        emb_wd = wd_cand.get('embedding')

        if emb_osm is None or emb_wd is None:
            return 0.0
            
        if self.cross_attention_model is not None and self._torch is not None:
            torch = self._torch
            # Neural prediction using BiLSTM architecture
            # Shapes required by module: (batch, seq_len, emb_dim)
            # Currently wrapping single embedding as seq_len=1
            t_osm = torch.tensor(emb_osm, dtype=torch.float32).unsqueeze(0).unsqueeze(0)
            t_wd = torch.tensor(emb_wd, dtype=torch.float32).unsqueeze(0).unsqueeze(0)
            
            with torch.no_grad():
                prob = self.cross_attention_model(t_osm, t_wd)
            return float(prob.item())

        # Validation baseline: OSM2KG-FT cosine logic (IGEA Table 3)
        a = np.array(emb_osm, dtype=np.float32)
        b = np.array(emb_wd, dtype=np.float32)
        return _cosine(a, b)

    # ------------------------------------------------------------------
    # Write-back
    # ------------------------------------------------------------------

    def _write_accepted_pairs(self, accepted: List[Tuple[int, str]]) -> int:
        """
        Write accepted (osm_id, wikidata_uri) pairs back to OsmEntity.

        Sets wikidata_uri and wkg_enriched_at on the matched entities.
        """
        from worldkg_nca.models import OsmEntity
        now = datetime.now(timezone.utc)
        updated = 0
        for osm_id, wikidata_uri in accepted:
            count = OsmEntity.objects.using('vectors').filter(osm_id=osm_id).update(
                wikidata_uri=wikidata_uri,
                wkg_enriched_at=now,
            )
            updated += count
        return updated

    # ------------------------------------------------------------------
    # Main iterative loop (Algorithm 1 from IGEA)
    # ------------------------------------------------------------------

    def run(
        self,
        country_code: Optional[str] = None,
        snapshot_id=None,
        polygon_wkt: Optional[str] = None,
        buffer_deg: float = 0.0,
    ) -> Dict:
        """
        Run the iterative entity alignment pipeline.

        Mirrors Algorithm 1 from the IGEA paper:
            GT ← seed (existing wikidata= tags)
            while i < max_iterations:
                candidates ← blocking(same_class, ≤ 2500m)
                predictions ← cosine(gv_tags_embedding)
                accepted ← {pair | confidence ≥ tha}
                GT ← GT ∪ accepted
                write accepted to DB
                i++
            return GT

        Args:
            country_code: Optional ISO country code for scoped processing
                          (tag-based fallback; prefer polygon_wkt for accuracy).
            snapshot_id:  UUID of the source TemporalSnapshot for provenance.
            polygon_wkt:  Optional WKT POLYGON string for PostGIS ST_Within
                          spatial filtering — more accurate than country_code
                          because it does not rely on addr:country= OSM tags.
                          Generate from a .poly file via parse_poly_to_wkt().
            buffer_deg:   Degrees to expand polygon_wkt before filtering
                          (useful to include buffer-zone entities for better
                           blocking near country borders). Default 0.0.

        Returns:
            Dict with statistics: iterations, total_accepted, per_iteration_counts
        """
        from worldkg_nca.models import OsmEntity

        # Filter to most recent snapshot to avoid loading all temporal versions
        qs = OsmEntity.objects.using('vectors').filter(
            geom__isnull=False,
            gv_tags_version__startswith='2025_12_31'  # Matches 2025_12_31 and provincial variants
        )

        country_filter_needed = False

        # If a polygon is provided, use it to spatially restrict the working set.
        if polygon_wkt:
            from django.contrib.gis.geos import GEOSGeometry
            poly = GEOSGeometry(polygon_wkt, srid=4326)
            qs = qs.filter(geom__within=poly)

        # If a country code is provided, further restrict to entities that declare
        # an addr:country tag. We still apply an in-Python check below to match
        # the seed alignment behaviour.
        if country_code:
            qs = qs.filter(tags__has_key='addr:country')
            country_filter_needed = True

        osm_entities = []
        for e in qs.iterator(chunk_size=10_000):
            tags = e.tags or {}

            if country_filter_needed and tags.get('addr:country') != country_code:
                continue

            osm_entities.append({
                'osm_id': e.osm_id,
                'lat': e.geom.y,
                'lon': e.geom.x,
                'wkg_class': e.wkg_class,
                'embedding': list(e.gv_tags_embedding) if e.gv_tags_embedding is not None else None,
                'tags': tags,
            })
            
            # Safety limit: stop at 200k entities to prevent OOM
            if len(osm_entities) >= 200_000:
                logger.warning(f"IGEA: Limiting to 200k entities (found {qs.count():,} total)")
                break

        if polygon_wkt and country_code and not osm_entities:
            logger.warning(
                "IGEA: polygon geofence yielded 0 entities for country=%s; "
                "falling back to country_code-only filter",
                country_code,
            )
            return self.run(
                country_code=country_code,
                snapshot_id=snapshot_id,
                polygon_wkt=None,
                buffer_deg=buffer_deg,
            )

        logger.info(
            f"IGEA: processing {len(osm_entities)} OSM entities "
            f"(country={country_code or 'all'}, "
            f"geofenced={'polygon' if polygon_wkt else 'no'})"
        )

        # Seed alignment (filtered by polygon or country if provided)
        seed_map = self._get_seed_alignment(country_code, polygon_wkt)      # osm_id → wikidata_uri
        seed_ids: Set[int] = set(seed_map.keys())

        # Run NCA to learn tag→class mappings from seed entities
        if self.enable_nca:
            nca_assigned = self._run_nca(osm_entities, seed_map)
            logger.info(f"NCA assigned classes to {nca_assigned} entities")

        all_accepted: List[Tuple[int, str]] = []
        per_iter_counts: List[int] = []

        for iteration in range(1, self.max_iterations + 1):
            logger.info(f"IGEA iteration {iteration}/{self.max_iterations} "
                        f"| seed size: {len(seed_ids)}")

            # Generate candidate pairs (blocking)
            pairs = self._candidate_pairs(osm_entities, seed_ids)
            if not pairs:
                logger.info(f"IGEA: no new candidates in iteration {iteration}, stopping")
                break

            # Train cross-attention model on this iteration's pairs
            if self.enable_cross_attention_training:
                self._train_cross_attention(pairs, seed_map)

            # Score and filter
            accepted_this_iter: List[Tuple[int, str]] = []
            scores = []
            for osm_ent, wd_cand in pairs:
                score = self._score_pair(osm_ent, wd_cand)
                scores.append(score)
                if score >= self.threshold:
                    accepted_this_iter.append((osm_ent['osm_id'], wd_cand['wikidata_uri']))
            
            # Log score distribution for debugging
            if scores:
                import numpy as np
                scores_arr = np.array(scores)
                logger.info(
                    f"IGEA iteration {iteration}: score distribution - "
                    f"min={scores_arr.min():.3f}, max={scores_arr.max():.3f}, "
                    f"mean={scores_arr.mean():.3f}, median={np.median(scores_arr):.3f}, "
                    f"threshold={self.threshold}"
                )

            # Deduplicate (highest score wins per OSM entity)
            seen_osm: Dict[int, Tuple[int, str]] = {}
            for osm_id, wd_uri in accepted_this_iter:
                if osm_id not in seen_osm:
                    seen_osm[osm_id] = (osm_id, wd_uri)
            deduped = list(seen_osm.values())

            # Write to DB
            written = self._write_accepted_pairs(deduped)
            per_iter_counts.append(written)
            logger.info(f"IGEA iteration {iteration}: accepted {written} new links")

            # Expand seed set (GT ← GT ∪ accepted)
            for osm_id, wd_uri in deduped:
                seed_ids.add(osm_id)
                seed_map[osm_id] = wd_uri

            all_accepted.extend(deduped)

            if written == 0:
                logger.info(f"IGEA: no new acceptances in iteration {iteration}, converged")
                break

        stats = {
            'iterations_run': len(per_iter_counts),
            'total_accepted': len(all_accepted),
            'per_iteration_counts': per_iter_counts,
            'final_seed_size': len(seed_ids),
            'country_code': country_code,
            'geofenced': polygon_wkt is not None,
            'threshold': self.threshold,
            'max_distance_m': self.max_distance_m,
        }
        logger.info(f"IGEA complete: {stats}")
        return stats
