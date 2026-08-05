"""
Celery task: Step 2 — Harvest Wikidata Candidates

Runs SPARQL queries to harvest candidate Wikidata entities for a country.
"""

from __future__ import annotations
import logging
from pipeline.envelopes import CountryEnvelope
from pipeline.task_decorator import pipeline_step
from django.core.management import call_command
from pipeline.tasks.helper import _log
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
    @pipeline_step("harvest_wikidata", CountryEnvelope, 2.0)
    def step_2_harvest_wikidata(self, env: CountryEnvelope) -> CountryEnvelope:
        """Step 2: Harvest Wikidata candidates via SPARQL."""
        _log(
            logger,
            "info",
            "Step 2: Harvest Wikidata candidates",
            country=env.iso,
            wikidata_qid=env.wikidata_qid,
            pipeline_run_id=env.pipeline_run_id,
        )

        call_command(
            "harvest_wikidata_candidates",
            country=env.iso, limit=env.uslp_max_heads,
            enrich_classes=True,
        )

        _log(
            logger,
            "info",
            "Step 2 complete",
            country=env.iso,
            pipeline_run_id=env.pipeline_run_id,
        )
        return env
