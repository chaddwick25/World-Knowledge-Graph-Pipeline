"""
Celery task: Finalize Planet Init Chain.

Marks PipelineRun as COMPLETED and sends pipeline_complete WebSocket message
so the frontend UI updates.
"""

from __future__ import annotations
from datetime import datetime, timezone
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
import logging
from orchestration.models import PipelineRun
from pipeline.celery_app import (
    pipeline_task,
    PipelineTask,
)

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

    # Send pipeline_complete WebSocket message
    try:
        channel_layer = get_channel_layer()
        if channel_layer is not None:
            async_to_sync(channel_layer.group_send)(
                f"pipeline_{pipeline_run_id}",
                {
                    "type": "pipeline_complete",
                    "session_id": pipeline_run_id,
                    "status": "completed",
                    "error": "",
                },
            )
            logger.info(
                "Sent pipeline_complete WS for %s",
                pipeline_run_id,
            )
    except Exception as exc:
        logger.warning(
            "Failed to send pipeline_complete WS for %s: %s",
            pipeline_run_id,
            exc,
        )

    return pipeline_run_id
