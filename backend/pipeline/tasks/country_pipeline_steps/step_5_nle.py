"""
Celery tasks: Step 5 — Train GV-NLE

Authoritative DeepWalk embeddings for country and subgraph level.
"""

from __future__ import annotations
import logging
from celery import group
from pipeline.config import CountryConfig, SubgraphConfig
from pipeline.tasks.helper import _log, _push_update, run_gv_nle_training
from pipeline.celery_app import (
    celery_app,
    PipelineTask,
    CELERY_AVAILABLE,
)

if CELERY_AVAILABLE:
    logger = logging.getLogger("pipeline")
    
    @celery_app.task(
        bind=True, base=PipelineTask,
        name="step_5_train_gv_nle",
        max_retries=1, default_retry_delay=300,
    )
    def step_5_train_gv_nle(self, config_dict: dict) -> dict:
        """Step 5: Train authoritative GV-NLE (DeepWalk).

        Small territories (has_subgraphs=False) run at country level only,
        skipping the subgraph fan-out.
        """
        config_dict = self.setup_pipeline_context(config_dict)
        cfg = CountryConfig.from_dict(config_dict)

        # Rehydrate subgraphs from DB to ensure fresh data
        # (config_dict may be stale if dispatched before DB was updated)
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

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="train_gv_nle", status="in_progress",
            message="Training GV-NLE embeddings (DeepWalk)...",
            pct=10, step=6,
        )
        _log(
            logger,
            "info",
            "Step 5: Train GV-NLE",
            country=cfg.iso,
            k=cfg.deepwalk_k,
            embedding_dim=cfg.deepwalk_embedding_dim,
            walk_length=cfg.deepwalk_walk_length,
            num_walks=cfg.deepwalk_num_walks,
            use_gpu=cfg.deepwalk_use_gpu,
            has_subgraphs=cfg.has_subgraphs,
            pipeline_run_id=cfg.pipeline_run_id,
        )

        if cfg.has_subgraphs and cfg.subgraphs:
            subgraph_tasks = [
                _train_subgraph_gv_nle.s(sg.to_dict(), cfg.to_dict())
                for sg in cfg.subgraphs
            ]
            group(subgraph_tasks).apply_async()
        else:
            _log(
                logger,
                "info",
                "No subgraphs — training at country level (small territory)",
                country=cfg.iso,
                pipeline_run_id=cfg.pipeline_run_id,
            )
            # v2 helper accepts explicit logger; behavior identical to v1
            run_gv_nle_training(cfg, logger=logger)

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="train_gv_nle", status="completed",
            message="GV-NLE training complete.",
            pct=100, step=6,
        )
        _log(
            logger,
            "info",
            "Step 5 complete",
            country=cfg.iso,
            pipeline_run_id=cfg.pipeline_run_id,
        )
        return config_dict


    @celery_app.task(
        bind=True, base=PipelineTask,
        name="sub_train_subgraph_gv_nle",
    )
    def _train_subgraph_gv_nle(
        self, subgraph_dict: dict, parent_config: dict
    ) -> dict:
        """Train GV-NLE for a single subgraph."""
        sg = SubgraphConfig.from_dict(subgraph_dict)
        cfg = CountryConfig.from_dict(parent_config)

        _log(
            logger,
            "info",
            "Training GV-NLE for subgraph",
            subgraph=sg.name,
            country=cfg.iso,
            pipeline_run_id=cfg.pipeline_run_id,
        )

        from django.core.management import call_command
        call_command(
            "train_gv_nle",
            region=sg.slug, poly_file=sg.poly_path,
            k=cfg.deepwalk_k,
            embedding_dim=cfg.deepwalk_embedding_dim,
            walk_length=cfg.deepwalk_walk_length,
            num_walks=cfg.deepwalk_num_walks,
            workers=cfg.deepwalk_workers,
            gpu=cfg.deepwalk_use_gpu,
            gpu_device=cfg.deepwalk_gpu_device,
            buffer_deg=cfg.deepwalk_buffer_deg,
            batch_size=10000,
        )

        return {"subgraph": sg.name, "success": True}

