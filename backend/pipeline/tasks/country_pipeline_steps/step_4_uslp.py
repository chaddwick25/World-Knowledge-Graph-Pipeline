"""
Celery tasks: Step 4 — USLP Spatial Link Prediction

Subgraph parallelisation via chord in canvas.py. The chord header runs
``_run_subgraph_uslp`` per subgraph; the callback ``step_4b_finalize_subgraph_uslp``
aggregates results.
"""

from __future__ import annotations
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
    """Step 4: USLP spatial link prediction (gating layer)."""

    _log(
        logger,
        "info",
        "Step 4: Predict Spatial Links (USLP)",
        country=env.iso,
        threshold=env.uslp_threshold,
        top_k=env.uslp_top_k,
        max_heads=env.uslp_max_heads,
        use_gpu=env.uslp_use_gpu,
        pipeline_run_id=env.pipeline_run_id,
    )

    from django.core.management import call_command
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
