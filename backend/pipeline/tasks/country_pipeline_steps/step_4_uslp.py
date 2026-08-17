"""
Celery tasks: Step 4 — USLP Spatial Link Prediction

Subgraph parallelisation via chord in canvas.py. The chord header runs
``_run_subgraph_uslp`` per subgraph; the callback ``step_4b_finalize_subgraph_uslp``
aggregates results.

When subgraphs are not available at canvas-build time (fresh DB, subgraphs
generated during Step 1), this task rehydrates subgraphs from the DB and
self-dispatches per-subgraph USLP instead of running at country level.
This prevents under-prediction on the first pipeline run after a DB reset.
"""

from __future__ import annotations
import dataclasses
import logging
from pathlib import Path
from pipeline.tasks.helper import _log
from pipeline.config import SubgraphConfig
from pipeline.envelopes import CountryEnvelope
from pipeline.task_decorator import pipeline_step
from pipeline.celery_app import (
    pipeline_task,
    PipelineTask,
)

logger = logging.getLogger("pipeline")

@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_4_predict_spatial_links",
    max_retries=2, default_retry_delay=120,
)
@pipeline_step("predict_spatial_links", CountryEnvelope, 4.0)
def step_4_predict_spatial_links(self, env: CountryEnvelope) -> CountryEnvelope:
    """Step 4: USLP spatial link prediction (gating layer).

    Rehydrates subgraphs from DB at the start (like Step 5) so that
    subgraphs generated during Step 1 are picked up even if the canvas
    chord was built with ``has_subgraphs=False``.
    """

    # Rehydrate subgraphs from DB to ensure fresh data.
    # On a fresh DB, subgraphs are generated during Step 1's
    # preprocess_snapshot, but the canvas chord was already built with
    # has_subgraphs=False.  This rehydration picks up the newly generated
    # subgraphs so we can fan out per-subgraph instead of country-level.
    rehydrated = False
    if not env.has_subgraphs or not env.subgraphs:
        try:
            fresh = CountryEnvelope.from_db(env.iso, snapshot_date=env.snapshot_date)
            if fresh.has_subgraphs and fresh.subgraphs:
                env = dataclasses.replace(
                    env,
                    subgraphs=fresh.subgraphs,
                    state=dataclasses.replace(env.state, has_subgraphs=True),
                )
                rehydrated = True
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

    _log(
        logger,
        "info",
        "Step 4: Predict Spatial Links (USLP)",
        country=env.iso,
        threshold=env.uslp_threshold,
        top_k=env.uslp_top_k,
        max_heads=env.uslp_max_heads,
        use_gpu=env.uslp_use_gpu,
        has_subgraphs=env.has_subgraphs,
        subgraph_count=len(env.subgraphs),
        rehydrated=rehydrated,
        pipeline_run_id=env.pipeline_run_id,
    )

    from django.core.management import call_command

    if env.has_subgraphs and env.subgraphs:
        # Per-subgraph USLP — fan out to each subgraph's polygon.
        # This runs inline (not via Celery chord) because the canvas chord
        # was already built with the stale has_subgraphs=False envelope.
        # The chord callback (step_4b) will be a no-op since we handle
        # everything here.
        _log(
            logger,
            "info",
            "Step 4: Running per-subgraph USLP (self-dispatched)",
            country=env.iso,
            subgraph_count=len(env.subgraphs),
            pipeline_run_id=env.pipeline_run_id,
        )
        for sg in env.subgraphs:
            poly_file = sg.poly_path
            if not poly_file and env.snapshot_pbf_path:
                snap_poly = Path(env.snapshot_pbf_path).with_suffix('.poly')
                if snap_poly.exists():
                    poly_file = str(snap_poly)
            if not poly_file:
                _log(
                    logger,
                    "warning",
                    "No poly file for subgraph USLP — skipping",
                    subgraph=sg.name,
                    country=env.iso,
                    pipeline_run_id=env.pipeline_run_id,
                )
                continue
            _log(
                logger,
                "info",
                "Running USLP for subgraph",
                subgraph=sg.name,
                country=env.iso,
                pipeline_run_id=env.pipeline_run_id,
            )
            call_command(
                "predict_spatial_links",
                country=env.iso,
                poly_file=poly_file,
                max_heads=env.uslp_max_heads,
                limit=env.uslp_limit,
                threshold=env.uslp_threshold,
                top_k=env.uslp_top_k,
                gpu=env.uslp_use_gpu,
                gpu_device=env.uslp_gpu_device,
                snapshot_date=env.snapshot_date,
            )
    else:
        # Country-level USLP (no subgraphs available).
        call_command(
            "predict_spatial_links",
            country=env.iso, max_heads=env.uslp_max_heads,
            limit=env.uslp_limit, threshold=env.uslp_threshold,
            top_k=env.uslp_top_k, gpu=env.uslp_use_gpu,
            gpu_device=env.uslp_gpu_device,
            snapshot_date=env.snapshot_date,
        )

    _log(
        logger,
        "info",
        "Step 4 complete",
        country=env.iso,
        pipeline_run_id=env.pipeline_run_id,
    )
    return env


@pipeline_task(
    bind=True, base=PipelineTask,
    name="sub_run_subgraph_uslp",
    max_retries=1, default_retry_delay=60,
)
def _run_subgraph_uslp(
    self, subgraph_dict: dict, parent_config: dict
) -> dict:
    """Run USLP for a single subgraph (parallel Group subtask for Step 4)."""
    sg = SubgraphConfig.from_dict(subgraph_dict)
    env = CountryEnvelope.from_dict(parent_config)

    _log(
        logger,
        "info",
        "Running USLP for subgraph",
        subgraph=sg.name,
        country=env.iso,
        pipeline_run_id=env.pipeline_run_id,
    )

    from django.core.management import call_command

    poly_file = sg.poly_path
    if not poly_file and env.snapshot_pbf_path:
        snap_poly = Path(env.snapshot_pbf_path).with_suffix('.poly')
        if snap_poly.exists():
            poly_file = str(snap_poly)

    if not poly_file:
        _log(
            logger,
            "warning",
            "No poly file for subgraph USLP — falling back to country-level",
            subgraph=sg.name,
            country=env.iso,
            pipeline_run_id=env.pipeline_run_id,
        )

    call_command(
        "predict_spatial_links",
        country=env.iso,
        poly_file=poly_file,
        max_heads=env.uslp_max_heads,
        limit=env.uslp_limit,
        threshold=env.uslp_threshold,
        top_k=env.uslp_top_k,
        gpu=env.uslp_use_gpu,
        gpu_device=env.uslp_gpu_device,
        snapshot_date=env.snapshot_date,
    )

    return {"subgraph": sg.name, "status": "completed", "poly_file": poly_file}


@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_4b_finalize_subgraph_uslp",
)
def step_4b_finalize_subgraph_uslp(
    self, aggregated_results: list, config_dict: dict = None
) -> dict:
    """Chord callback — finalize USLP after all subgraphs complete.

    ``config_dict`` is passed via ``chord(header, callback.s(config_dict))``
    in ``canvas.py`` so the callback can reconstruct the envelope for
    downstream logging.
    """
    if config_dict is None:
        for item in aggregated_results:
            if isinstance(item, dict) and "iso" in item and "slug" in item:
                config_dict = item
                break

    env = CountryEnvelope.from_dict(config_dict) if config_dict else None

    subgraph_results = [
        item for item in aggregated_results
        if isinstance(item, dict) and "subgraph" in item
    ]

    _log(
        logger,
        "info",
        "Step 4b: Finalizing subgraph USLP — all subgraphs complete",
        subgraph_count=len(subgraph_results),
        country=env.iso if env else "unknown",
        pipeline_run_id=env.pipeline_run_id if env else "unknown",
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
