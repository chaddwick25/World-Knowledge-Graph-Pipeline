"""
Celery task: Step 2 — Harvest Wikidata Candidates

Runs SPARQL queries to harvest candidate Wikidata entities for a country.
"""

from __future__ import annotations
import logging
from pipeline.config import CountryConfig
from django.core.management import call_command
from pipeline.tasks.helper import _log, _push_update
from pipeline.celery_app import (
    celery_app,
    PipelineTask,
    CELERY_AVAILABLE,
)

if CELERY_AVAILABLE:
    logger = logging.getLogger("pipeline")
    @celery_app.task(
        bind=True, base=PipelineTask,
        name="step_2_harvest_wikidata",
        max_retries=2, default_retry_delay=60,
    )
    def step_2_harvest_wikidata(self, config_dict: dict) -> dict:
        """Step 2: Harvest Wikidata candidates via SPARQL."""
        config_dict = self.setup_pipeline_context(config_dict)
        cfg = CountryConfig.from_dict(config_dict)

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="harvest_wikidata", status="in_progress",
            message="Harvesting Wikidata SPARQL candidates...",
            pct=10, step=3,
        )

        _log(
            logger,
            "info",
            "Step 2: Harvest Wikidata candidates",
            country=cfg.iso,
            wikidata_qid=cfg.wikidata_qid,
            pipeline_run_id=cfg.pipeline_run_id,
        )

        call_command(
            "harvest_wikidata_candidates",
            country=cfg.iso, limit=cfg.uslp_max_heads,
            enrich_classes=True,
        )

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="harvest_wikidata", status="completed",
            message="Wikidata harvest complete.",
            pct=100, step=3,
        )

        _log(
            logger,
            "info",
            "Step 2 complete",
            country=cfg.iso,
            pipeline_run_id=cfg.pipeline_run_id,
        )
        return config_dict
