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

        Args:
            cfg: CountryEnvelope with iso, slug, name, pipeline_run_id.

        Returns:
            ``{"country": str, "created": bool, "run_finalized": bool}``.
        """
        from orchestration.models import CountrySearchProcessing, PipelineRun

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

        # 3. Send pipeline_complete WebSocket message
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
        }
