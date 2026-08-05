"""
Celery task: Step 6 — Mark Search Ready

Post-pipeline hook that marks a country as search-ready in
CountrySearchProcessing and finalizes the PipelineRun record.
"""

from __future__ import annotations
import dataclasses
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
        name="step_6_mark_search_ready",
        max_retries=2, default_retry_delay=30,
    )
    @pipeline_step("mark_search_ready", CountryEnvelope, 6.0)
    def step_6_mark_search_ready(self, env: CountryEnvelope) -> CountryEnvelope:
        """Step 6: Mark country as search-ready in CountrySearchProcessing.

        After all pipeline steps succeed, this task:
          1. Upserts CountrySearchProcessing with is_processed=True
          2. Finalizes PipelineRun as COMPLETED
          3. Sends pipeline_complete WebSocket message
        """
        # Rehydrate subgraphs from DB to ensure fresh data
        if not env.has_subgraphs or not env.subgraphs:
            try:
                fresh = CountryEnvelope.from_db(env.iso, snapshot_date=env.snapshot_date)
                if fresh.has_subgraphs and fresh.subgraphs:
                    env = dataclasses.replace(
                        env,
                        subgraphs=fresh.subgraphs,
                        state=dataclasses.replace(env.state, has_subgraphs=True),
                    )
                    _log(
                        logger,
                        "info",
                        "Rehydrated subgraphs from DB",
                        country=env.iso,
                        subgraph_count=len(env.subgraphs),
                        pipeline_run_id=env.pipeline_run_id,
                    )
            except Exception as exc:
                _log(
                    logger,
                    "info",
                    "Subgraph rehydration failed, using config as-is",
                    country=env.iso,
                    error=str(exc),
                    pipeline_run_id=env.pipeline_run_id,
                )

        from orchestration.services.search_ready_service import SearchReadyService
        SearchReadyService().run(env)

        _log(
            logger,
            "info",
            "Step 6 complete — country marked search-ready",
            country=env.iso,
            pipeline_run_id=env.pipeline_run_id,
        )
        return env
