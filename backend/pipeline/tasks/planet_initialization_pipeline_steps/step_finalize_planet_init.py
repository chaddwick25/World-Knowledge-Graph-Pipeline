"""
Celery task: Finalize Planet Init Chain.

Marks PipelineRun as COMPLETED and sends pipeline_complete WebSocket message
so the frontend UI updates.
"""

from __future__ import annotations
from datetime import datetime, timezone
import logging
from orchestration.models import PipelineRun
from pipeline.celery_app import (
    pipeline_task,
    PipelineTask,
)
from pipeline.tasks.helper import _send_pipeline_complete

logger = logging.getLogger("pipeline")

@pipeline_task(
    bind=True,
    base=PipelineTask,
    name="step_finalize_planet_init",
    max_retries=1,
    default_retry_delay=300,
)
def _finalize_planet_init_chain(self, previous_result, pipeline_run_id: str) -> str:
    """Final callback in the planet init Celery chain.

    Marks the PipelineRun as COMPLETED and sends a pipeline_complete
    WebSocket message so the frontend knows initialization is done.
    """
    logger.info(
        "Finalizing planet init chain (pipeline_run_id=%s)",
        pipeline_run_id,
    )
    try:
        run = PipelineRun.objects.get(id=pipeline_run_id)
        run.status = PipelineRun.PipelineStatus.COMPLETED
        run.completed_at = datetime.now(timezone.utc)
        run.save(update_fields=["status", "completed_at"])
        logger.info(
            "PipelineRun %s marked as COMPLETED",
            pipeline_run_id,
        )
    except PipelineRun.DoesNotExist:
        logger.warning(
            "PipelineRun %s not found — cannot mark as COMPLETED",
            pipeline_run_id,
        )

    _send_pipeline_complete(pipeline_run_id, status="completed")

    return pipeline_run_id
