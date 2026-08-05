"""
Celery task: Step 3 — Run IGEA

Iterative Geographic Entity Alignment (NCA + cross-attention).
"""

from __future__ import annotations
import logging
from pipeline.envelopes import CountryEnvelope
from pipeline.task_decorator import pipeline_step
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
        name="step_3_run_igea",
        max_retries=2, default_retry_delay=60,
    )
    @pipeline_step("run_igea", CountryEnvelope, 3.0)
    def step_3_run_igea(self, env: CountryEnvelope) -> CountryEnvelope:
        """Step 3: Iterative Geographic Entity Alignment (NCA + cross-attention)."""
        _log(
            logger,
            "info",
            "Step 3: Run IGEA",
            country=env.iso,
            pipeline_run_id=env.pipeline_run_id,
        )

        from igea.services.igea_pipeline_service import IgeaPipelineService
        stats = IgeaPipelineService().run(env)

        if stats is None:
            _log(
                logger,
                "info",
                "Step 3: no Wikidata candidates harvested, skipping IGEA",
                country=env.iso,
                pipeline_run_id=env.pipeline_run_id,
            )
            return env

        _log(
            logger,
            "info",
            f"Step 3 complete: {stats['total_accepted']} accepted, "
            f"{stats['iterations_run']} iterations",
            country=env.iso,
            pipeline_run_id=env.pipeline_run_id,
        )

        # Pass IGEA stats to step 4 via envelope state so it can skip USLP
        # if no links were accepted
        return env.with_state(
            igea_stats=stats,
            igea_accepted=stats.get('total_accepted', 0),
        )
