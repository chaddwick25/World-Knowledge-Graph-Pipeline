"""
Celery tasks: Step 4 — USLP Spatial Link Prediction

Subgraph parallelisation via chord in canvas.py. The chord header runs
``_run_subgraph_uslp`` per subgraph; the callback ``step_4b_finalize_subgraph_uslp``
aggregates results.
"""

from __future__ import annotations
import logging
from pathlib import Path
from pipeline.tasks.helper import _log, _push_update
from pipeline.config import CountryConfig, SubgraphConfig
from pipeline.celery_app import (
    celery_app,
    PipelineTask,
    CELERY_AVAILABLE,
)


if CELERY_AVAILABLE:
    logger = logging.getLogger("pipeline")

    @celery_app.task(
        bind=True, base=PipelineTask,
        name="step_4_predict_spatial_links",
        max_retries=2, default_retry_delay=120,
    )
    def step_4_predict_spatial_links(self, config_dict: dict) -> dict:
        """Step 4: USLP spatial link prediction (gating layer)."""
        config_dict = self.setup_pipeline_context(config_dict)
        cfg = CountryConfig.from_dict(config_dict)

        igea_stats = config_dict.get('igea_stats', {})
        total_accepted = igea_stats.get('total_accepted', 0)

        if total_accepted == 0:
            _log(
                logger,
                "info",
                "Step 4: Skipping USLP (IGEA accepted 0 links)",
                country=cfg.iso,
                pipeline_run_id=cfg.pipeline_run_id,
            )
            _push_update(
                pipeline_run_id=cfg.pipeline_run_id,
                name="predict_spatial_links", status="completed",
                message="Skipped USLP (IGEA accepted 0 links)",
                pct=100, step=5,
            )
            return config_dict

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="predict_spatial_links", status="in_progress",
            message="Predicting spatial links (USLP)...",
            pct=10, step=5,
        )

        _log(
            logger,
            "info",
            "Step 4: Predict Spatial Links (USLP)",
            country=cfg.iso,
            threshold=cfg.uslp_threshold,
            top_k=cfg.uslp_top_k,
            max_heads=cfg.uslp_max_heads,
            use_gpu=cfg.uslp_use_gpu,
            pipeline_run_id=cfg.pipeline_run_id,
        )

        from django.core.management import call_command
        call_command(
            "predict_spatial_links",
            country=cfg.iso, max_heads=cfg.uslp_max_heads,
            limit=cfg.uslp_limit, threshold=cfg.uslp_threshold,
            top_k=cfg.uslp_top_k, gpu=cfg.uslp_use_gpu,
            gpu_device=cfg.uslp_gpu_device,
        )

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="predict_spatial_links", status="completed",
            message="Spatial link prediction complete.",
            pct=100, step=5,
        )

        _log(
            logger,
            "info",
            "Step 4 complete",
            country=cfg.iso,
            pipeline_run_id=cfg.pipeline_run_id,
        )
        return config_dict


    @celery_app.task(
        bind=True, base=PipelineTask,
        name="sub_run_subgraph_uslp",
        max_retries=1, default_retry_delay=60,
    )
    # TODO: Look into this(Legacy Code ?)
    def _run_subgraph_uslp(
        self, subgraph_dict: dict, parent_config: dict
    ) -> dict:
        """Run USLP for a single subgraph (parallel Group subtask for Step 4)."""
        sg = SubgraphConfig.from_dict(subgraph_dict)
        cfg = CountryConfig.from_dict(parent_config)

        _log(
            logger,
            "info",
            "Running USLP for subgraph",
            subgraph=sg.name,
            country=cfg.iso,
            pipeline_run_id=cfg.pipeline_run_id,
        )

        from django.core.management import call_command

        poly_file = sg.poly_path
        if not poly_file and cfg.snapshot_pbf_path:
            snap_poly = Path(cfg.snapshot_pbf_path).with_suffix('.poly')
            if snap_poly.exists():
                poly_file = str(snap_poly)

        if not poly_file:
            _log(
                logger,
                "warning",
                "No poly file for subgraph USLP — falling back to country-level",
                subgraph=sg.name,
                country=cfg.iso,
                pipeline_run_id=cfg.pipeline_run_id,
            )

        call_command(
            "predict_spatial_links",
            country=cfg.iso,
            poly_file=poly_file,
            max_heads=cfg.uslp_max_heads,
            limit=cfg.uslp_limit,
            threshold=cfg.uslp_threshold,
            top_k=cfg.uslp_top_k,
            gpu=cfg.uslp_use_gpu,
            gpu_device=cfg.uslp_gpu_device,
        )

        return {"subgraph": sg.name, "status": "completed", "poly_file": poly_file}


    @celery_app.task(
        bind=True, base=PipelineTask,
        name="step_4b_finalize_subgraph_uslp",
    )
    def step_4b_finalize_subgraph_uslp(
        self, aggregated_results: list, config_dict: dict = None
    ) -> dict:
        """Chord callback — finalize USLP after all subgraphs complete.

        ``config_dict`` is passed via ``chord(header, callback.s(config_dict))``
        in ``canvas.py`` so the callback can reconstruct CountryConfig for
        downstream logging.
        """
        if config_dict is None:
            for item in aggregated_results:
                if isinstance(item, dict) and "iso" in item and "slug" in item:
                    config_dict = item
                    break

        cfg = CountryConfig.from_dict(config_dict) if config_dict else None

        subgraph_results = [
            item for item in aggregated_results
            if isinstance(item, dict) and "subgraph" in item
        ]

        _log(
            logger,
            "info",
            "Step 4b: Finalizing subgraph USLP — all subgraphs complete",
            subgraph_count=len(subgraph_results),
            country=cfg.iso if cfg else "unknown",
            pipeline_run_id=cfg.pipeline_run_id if cfg else "unknown",
        )

        for sg_result in subgraph_results:
            _log(
                logger,
                "info",
                "Subgraph USLP result",
                subgraph=sg_result.get("subgraph"),
                status=sg_result.get("status"),
            )

        return config_dict or {"status": "completed", "subgraphs": subgraph_results}
