"""Stateless service: mark a country as search-ready.

Post-pipeline hook that:
  1. Upserts CountrySearchProcessing with is_processed=True
  2. Finalizes PipelineRun as COMPLETED
  3. Sends pipeline_complete WebSocket message

Pure data plane — no Celery. The task calls this service and handles
the Celery-specific logging/WS dispatch.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict

from django.utils import timezone as tz

if TYPE_CHECKING:
    from pipeline.envelopes import CountryEnvelope

logger = logging.getLogger(__name__)


class SearchReadyService:
    """Mark a country as search-ready and finalize the pipeline run.

    Stateless — no constructor args.
    """

    def __init__(self) -> None:
        pass

    def run(self, cfg: "CountryEnvelope") -> Dict:
        """Mark country as search-ready, finalize PipelineRun, send WS.

        Also transitions the linked ``SnapshotJob`` (if any) to COMPLETED
        with the run's entity/alignment/spatial-link counts. This is the
        DB ground-truth update described in
        ``docs/plans/TEMPORAL_SNAPSHOT_REFACTOR.md`` Phase D1.

        Args:
            cfg: CountryEnvelope with iso, slug, name, pipeline_run_id.

        Returns:
            ``{"country": str, "created": bool, "run_finalized": bool,
            "snapshot_job_updated": bool}``.
        """
        from core.models import (
            CountrySearchProcessing, PipelineRun,
        )
        from osmsnapshot.models import SnapshotJob

        # 1. Upsert CountrySearchProcessing
        country_processing, created = CountrySearchProcessing.objects.update_or_create(
            country_name=(cfg.slug or cfg.name).replace('-', '_'),
            defaults={
                "is_processed": True,
                "processing_completed_at": tz.now(),
            },
        )
        logger.info(
            "CountrySearchProcessing set to is_processed=True [country=%s created=%s]",
            cfg.iso, created,
        )

        # 2. Finalize PipelineRun as COMPLETED
        run_finalized = False
        run = None
        try:
            run = PipelineRun.objects.get(id=cfg.pipeline_run_id)
            run.status = PipelineRun.PipelineStatus.COMPLETED
            run.completed_at = datetime.now(timezone.utc)
            run.save(update_fields=["status", "completed_at"])
            run_finalized = True
        except PipelineRun.DoesNotExist:
            logger.warning(
                "PipelineRun %s not found — cannot mark as COMPLETED",
                cfg.pipeline_run_id,
            )

        # 3. Transition the linked SnapshotJob to COMPLETED (DB ground truth)
        snapshot_job_updated = False
        if run is not None:
            for job in SnapshotJob.objects.filter(pipeline_run=run):
                try:
                    job.mark_completed(
                        total_entities=(
                            run.total_entities_processed if run else 0
                        ),
                        total_aligned=(
                            run.total_entities_aligned if run else 0
                        ),
                        total_spatial_links=(
                            run.total_spatial_links if run else 0
                        ),
                    )
                    snapshot_job_updated = True
                except Exception as exc:
                    logger.warning(
                        "Failed to mark SnapshotJob %s as COMPLETED: %s",
                        job.id, exc,
                    )

        # 4. Send pipeline_complete WebSocket message
        try:
            from channels.layers import get_channel_layer
            from asgiref.sync import async_to_sync
            channel_layer = get_channel_layer()
            if channel_layer is not None:
                async_to_sync(channel_layer.group_send)(
                    f"pipeline_{cfg.pipeline_run_id}",
                    {
                        "type": "pipeline_complete",
                        "session_id": str(cfg.pipeline_run_id),
                        "status": "completed",
                        "error": "",
                    },
                )
        except Exception as exc:
            logger.warning("Failed to send pipeline_complete WS: %s", exc)

        return {
            "country": cfg.iso,
            "created": created,
            "run_finalized": run_finalized,
            "snapshot_job_updated": snapshot_job_updated,
        }
