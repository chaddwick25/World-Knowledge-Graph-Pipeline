"""Stateless service: run IGEA (Iterative Geographic Entity Alignment).

Wraps IterativeEntityAlignmentService + WikidataCandidateService +
FastTextEmbeddingService into a single pipeline-callable service.

Pure data plane — no Celery, no logging dispatcher, no WS push.
Receives a CountryEnvelope, returns IGEA stats dict.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from pipeline.envelopes import CountryEnvelope

logger = logging.getLogger(__name__)


class IgeaPipelineService:
    """Run IGEA alignment for a country.

    Stateless — no constructor args (uses internal services).
    """

    def __init__(self) -> None:
        pass

    def run(self, cfg: "CountryEnvelope") -> Optional[Dict]:
        """Run IGEA for a country.

        Args:
            cfg: CountryEnvelope with iso, poly_path, snapshot_pbf_path.

        Returns:
            IGEA stats dict (``{"total_accepted": int, "iterations_run": int, ...}``)
            or ``None`` if no candidates were harvested.
        """
        from igea.services.iterative_alignment_service import (
            IterativeEntityAlignmentService,
        )
        from semantic_search.services.fasttext_service import FastTextEmbeddingService
        from worldkg_nca.services.wikidata_service import (
            parse_poly_to_wkt,
            WikidataCandidateService,
        )

        # TODO: Double-check the threshold
        igea = IterativeEntityAlignmentService(
            max_iterations=3,
            threshold=0.6,
            max_distance_m=2500.0,
            enable_nca=True,
            enable_cross_attention_training=True,
        )
        wd_service = WikidataCandidateService()
        candidates = wd_service.harvest_by_country(cfg.iso, limit=50_000)
        if not candidates:
            logger.info(
                "Step 3: no Wikidata candidates harvested, skipping IGEA [country=%s]",
                cfg.iso,
            )
            return None

        # Backfill missing embeddings via FastText
        ft_service = FastTextEmbeddingService()
        for c in candidates:
            if not c.get('embedding') and c.get('label'):
                tag_counts = {c['label'].lower(): 1}
                emb = ft_service.calculate_embedding(tag_counts)
                if emb is not None:
                    c['embedding'] = emb.tolist()

        igea.load_wikidata_candidates(candidates)

        # Resolve poly path
        poly_path = cfg.poly_path
        if not poly_path and cfg.snapshot_pbf_path:
            snap_poly = Path(cfg.snapshot_pbf_path).with_suffix('.poly')
            if snap_poly.exists():
                poly_path = str(snap_poly)

        # TODO: Double-check if missing polygon_wkt throws an error
        polygon_wkt = None
        if poly_path:
            polygon_wkt = parse_poly_to_wkt(poly_path)

        stats = igea.run(
            country_code=cfg.iso,
            snapshot_id=cfg.snapshot_date,
            polygon_wkt=polygon_wkt,
        )

        logger.info(
            "IGEA complete: %d accepted, %d iterations [country=%s]",
            stats['total_accepted'], stats['iterations_run'], cfg.iso,
        )

        return stats
