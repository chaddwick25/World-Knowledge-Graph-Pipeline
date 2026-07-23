"""
Celery task: Step 3 — Run IGEA

Iterative Geographic Entity Alignment (NCA + cross-attention).
"""

from __future__ import annotations
import logging
from pathlib import Path
from pipeline.config import CountryConfig
from pipeline.tasks.helper import _log, _push_update
from igea.services.iterative_alignment_service import IterativeEntityAlignmentService
from semantic_search.services.fasttext_service import FastTextEmbeddingService
from worldkg_nca.services.wikidata_service import parse_poly_to_wkt
from worldkg_nca.services.wikidata_service import WikidataCandidateService
from pipeline.celery_app import (
    celery_app,
    PipelineTask,
    CELERY_AVAILABLE,
)

if CELERY_AVAILABLE:
    logger = logging.getLogger("pipeline")
    @celery_app.task(
        bind=True, base=PipelineTask,
        name="step_3_run_igea",
        max_retries=2, default_retry_delay=60,
    )
    def step_3_run_igea(self, config_dict: dict) -> dict:
        """Step 3: Iterative Geographic Entity Alignment (NCA + cross-attention)."""

        config_dict = self.setup_pipeline_context(config_dict)
        cfg = CountryConfig.from_dict(config_dict)
        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="run_igea", status="in_progress",
            message="Running iterative geographic entity alignment (IGEA)...",
            pct=10, step=4,
        )
        _log(
            logger,
            "info",
            "Step 3: Run IGEA",
            country=cfg.iso,
            pipeline_run_id=cfg.pipeline_run_id,
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
            _log(
                logger,
                "info",
                "Step 3: no Wikidata candidates harvested, skipping IGEA",
                country=cfg.iso,
                pipeline_run_id=cfg.pipeline_run_id,
            )
            return config_dict

        ft_service = FastTextEmbeddingService()
        for c in candidates:
            if not c.get('embedding') and c.get('label'):
                tag_counts = {c['label'].lower(): 1}
                emb = ft_service.calculate_embedding(tag_counts)
                if emb is not None:
                    c['embedding'] = emb.tolist()

        igea.load_wikidata_candidates(candidates)
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
            polygon_wkt=polygon_wkt,
        )

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="run_igea", status="completed",
            message=f"IGEA complete: {stats['total_accepted']} accepted",
            pct=100, step=4,
        )
        _log(
            logger,
            "info",
            f"Step 3 complete: {stats['total_accepted']} accepted, "
            f"{stats['iterations_run']} iterations",
            country=cfg.iso,
            pipeline_run_id=cfg.pipeline_run_id,
        )

        # Pass IGEA stats to step 4 so it can skip USLP if no links were accepted
        config_dict['igea_stats'] = stats
        return config_dict
