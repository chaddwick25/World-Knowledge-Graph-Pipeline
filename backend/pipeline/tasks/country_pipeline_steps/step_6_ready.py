"""
Celery task: Step 6 — Mark Search Ready

Post-pipeline hook that marks a country as search-ready in
CountrySearchProcessing and finalizes the PipelineRun record.
"""

from __future__ import annotations
import logging
from celery import group
from django.utils import timezone as tz
from django.conf import settings
from pipeline.config import CountryConfig
from pipeline.tasks.helper import _log, _push_update
from orchestration.models import CountrySearchProcessing
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
    def step_6_mark_search_ready(self, config_dict: dict) -> dict:
        """Step 6: Mark country as search-ready in CountrySearchProcessing.

        After all pipeline steps succeed, this task:
          1. Train SBERT projection heads (country/subgraph level)
          2. Upserts CountrySearchProcessing with is_processed=True
          3. Finalizes PipelineRun as COMPLETED
          4. Sends pipeline_complete WebSocket message
        """
        config_dict = self.setup_pipeline_context(config_dict)
        cfg = CountryConfig.from_dict(config_dict)

        # Rehydrate subgraphs from DB to ensure fresh data
        if not cfg.has_subgraphs or not cfg.subgraphs:
            try:
                fresh = CountryConfig.from_db(cfg.iso, snapshot_date=cfg.snapshot_date)
                if fresh.has_subgraphs and fresh.subgraphs:
                    cfg.subgraphs = fresh.subgraphs
                    cfg.has_subgraphs = True
                    config_dict = cfg.to_dict()
                    _log(
                        logger,
                        "info",
                        "Rehydrated subgraphs from DB",
                        country=cfg.iso,
                        subgraph_count=len(cfg.subgraphs),
                        pipeline_run_id=cfg.pipeline_run_id,
                    )
            except Exception as exc:
                _log(
                    logger,
                    "info",
                    "Subgraph rehydration failed, using config_dict as-is",
                    country=cfg.iso,
                    error=str(exc),
                    pipeline_run_id=cfg.pipeline_run_id,
                )

        # Train SBERT projection heads following GV-NLE pattern
        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="train_sbert_projection",
            status="in_progress",
            message="Training SBERT projection heads...",
            pct=10,
            step=7,
        )

        if cfg.has_subgraphs and cfg.subgraphs:
            subgraph_tasks = [
                _train_subgraph_sbert_projection.s(sg.to_dict(), cfg.to_dict())
                for sg in cfg.subgraphs
            ]
            group(subgraph_tasks).apply_async()
        else:
            _log(
                logger,
                "info",
                "No subgraphs — training SBERT projection at country level",
                country=cfg.iso,
                pipeline_run_id=cfg.pipeline_run_id,
            )
            _train_country_sbert_projection(cfg)

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="train_sbert_projection",
            status="completed",
            message="SBERT projection training complete.",
            pct=100,
            step=7,
        )

        _mark_country_search_ready(cfg)

        # Finalize PipelineRun as COMPLETED
        from orchestration.models import PipelineRun
        try:
            run = PipelineRun.objects.get(id=cfg.pipeline_run_id)
            run.status = PipelineRun.PipelineStatus.COMPLETED
            run.completed_at = __import__('django.utils.timezone', fromlist=['now']).now()
            run.save(update_fields=["status", "completed_at"])
        except PipelineRun.DoesNotExist:
            pass

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="mark_search_ready",
            status="completed",
            message="Pipeline completed successfully",
            pct=100,
        )

        # Notify WebSocket consumers that the pipeline is done
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
        except Exception:
            pass

        _log(
            logger,
            "info",
            "Step 6 complete — country marked search-ready",
            country=cfg.iso,
            pipeline_run_id=cfg.pipeline_run_id,
        )
        return config_dict

    def _train_country_sbert_projection(cfg: CountryConfig) -> None:
        """Train SBERT projection head at country level (small territory)."""
        from django.core.management import call_command
        from pathlib import Path

        # Resolve base path from country's poly_path or pbf_path
        base_path = None
        if cfg.poly_path:
            base_path = str(Path(cfg.poly_path).parent.parent)
        elif cfg.snapshot_pbf_path:
            base_path = str(Path(cfg.snapshot_pbf_path).parent)
        else:
            # Fallback to settings
            base_path = str(Path(settings.BASE_DATA_DIR or "/app/data") / "countries" / cfg.slug)

        _log(
            logger,
            "info",
            "Training SBERT projection head at country level",
            country=cfg.iso,
            base_path=base_path,
            pipeline_run_id=cfg.pipeline_run_id,
        )

        try:
            call_command(
                "train_sbert_projection",
                region=cfg.slug or cfg.name,
                base_path=base_path,
                sample_size=10000,
                epochs=50,
                batch_size=256,
            )
            _log(
                logger,
                "info",
                "Country-level SBERT projection head training complete",
                country=cfg.iso,
                pipeline_run_id=cfg.pipeline_run_id,
            )
        except Exception as e:
            _log(
                logger,
                "error",
                f"Failed to train country-level SBERT projection head: {e}",
                exc_info=True,
                country=cfg.iso,
                pipeline_run_id=cfg.pipeline_run_id,
            )

    @celery_app.task(
        bind=True, base=PipelineTask,
        name="sub_train_subgraph_sbert_projection",
    )
    def _train_subgraph_sbert_projection(
        self, subgraph_dict: dict, parent_config: dict
    ) -> dict:
        """Train SBERT projection head for a single subgraph."""
        from pipeline.config import SubgraphConfig
        from django.core.management import call_command
        from pathlib import Path

        sg = SubgraphConfig.from_dict(subgraph_dict)
        cfg = CountryConfig.from_dict(parent_config)

        # Resolve base path from subgraph's poly_path or pbf_path
        base_path = None
        if sg.poly_path:
            base_path = str(Path(sg.poly_path).parent.parent)
        elif sg.pbf_path:
            base_path = str(Path(sg.pbf_path).parent)
        elif cfg.poly_path:
            base_path = str(Path(cfg.poly_path).parent.parent)
        else:
            # Fallback to settings
            base_path = str(Path(settings.BASE_DATA_DIR or "/app/data") / "countries" / cfg.slug)

        _log(
            logger,
            "info",
            "Training SBERT projection head for subgraph",
            subgraph=sg.name,
            country=cfg.iso,
            base_path=base_path,
            pipeline_run_id=cfg.pipeline_run_id,
        )

        try:
            call_command(
                "train_sbert_projection",
                region=sg.slug,
                base_path=base_path,
                sample_size=10000,
                epochs=50,
                batch_size=256,
            )
            _log(
                logger,
                "info",
                "Subgraph SBERT projection head training complete",
                subgraph=sg.name,
                country=cfg.iso,
                pipeline_run_id=cfg.pipeline_run_id,
            )
            return {"subgraph": sg.name, "success": True}
        except Exception as e:
            _log(
                logger,
                "error",
                f"Failed to train subgraph SBERT projection head: {e}",
                exc_info=True,
                subgraph=sg.name,
                country=cfg.iso,
                pipeline_run_id=cfg.pipeline_run_id,
            )
            return {"subgraph": sg.name, "success": False, "error": str(e)}

    def _mark_country_search_ready(cfg: CountryConfig) -> None:
        """Upsert CountrySearchProcessing to mark a country as search-ready."""

        country_processing, created = CountrySearchProcessing.objects.update_or_create(
            country_name=(cfg.slug or cfg.name).replace('-', '_'),
            defaults={
                "is_processed": True,
                "processing_completed_at": tz.now(),
            },
        )

        _log(
            logger,
            "info",
            "CountrySearchProcessing set to is_processed=True",
            country=cfg.iso,
            country_name=cfg.name.replace('-', '_'),
            created=created,
            pipeline_run_id=cfg.pipeline_run_id,
        )
