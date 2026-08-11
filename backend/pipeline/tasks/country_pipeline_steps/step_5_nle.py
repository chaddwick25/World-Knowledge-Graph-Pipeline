"""
Celery tasks: Step 5 — Train GV-NLE

Authoritative DeepWalk embeddings for country and subgraph level.
"""

from __future__ import annotations
import dataclasses
import logging
from pipeline.config import SubgraphConfig
from pipeline.envelopes import CountryEnvelope
from pipeline.task_decorator import pipeline_step
from pipeline.tasks.helper import _log
from pipeline.celery_app import (
    pipeline_task,
    PipelineTask,
)

logger = logging.getLogger("pipeline")

@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_5_train_gv_nle",
    max_retries=1, default_retry_delay=300,
)
@pipeline_step("train_gv_nle", CountryEnvelope, 5.0)
def step_5_train_gv_nle(self, env: CountryEnvelope) -> CountryEnvelope:
    """Step 5: Train authoritative GV-NLE (DeepWalk).

    Small territories (has_subgraphs=False) run at country level only,
    skipping the subgraph fan-out.
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

    _log(
        logger,
        "info",
        "Step 5: Train GV-NLE",
        country=env.iso,
        k=env.deepwalk_k,
        embedding_dim=env.deepwalk_embedding_dim,
        walk_length=env.deepwalk_walk_length,
        num_walks=env.deepwalk_num_walks,
        use_gpu=env.deepwalk_use_gpu,
        has_subgraphs=env.has_subgraphs,
        pipeline_run_id=env.pipeline_run_id,
    )

    if env.has_subgraphs and env.subgraphs:
        subgraph_tasks = [
            _train_subgraph_gv_nle.s(sg.to_dict(), env.to_dict())
            for sg in env.subgraphs
        ]
        from celery import group
        group(subgraph_tasks).apply_async()
    else:
        _log(
            logger,
            "info",
            "No subgraphs — training at country level (small territory)",
            country=env.iso,
            pipeline_run_id=env.pipeline_run_id,
        )
        from geovectors_encoder.services.gv_nle_training_service import (
            GvNleTrainingService,
        )
        GvNleTrainingService().run(env)

    _log(
        logger,
        "info",
        "Step 5 complete",
        country=env.iso,
        pipeline_run_id=env.pipeline_run_id,
    )
    return env


@pipeline_task(
    bind=True, base=PipelineTask,
    name="sub_train_subgraph_gv_nle",
)
def _train_subgraph_gv_nle(
    self, subgraph_dict: dict, parent_config: dict
) -> dict:
    """Train GV-NLE for a single subgraph.

    Uses a file-based lock (fcntl) to serialize GPU access across prefork
    workers.  Only one subgraph trains on the GPU at a time; others wait.
    """
    sg = SubgraphConfig.from_dict(subgraph_dict)
    env = CountryEnvelope.from_dict(parent_config)

    _log(
        logger,
        "info",
        "Training GV-NLE for subgraph",
        subgraph=sg.name,
        country=env.iso,
        pipeline_run_id=env.pipeline_run_id,
    )

    import os
    import fcntl
    import tempfile

    lock_path = os.path.join(tempfile.gettempdir(), "gpu_training.lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)  # blocks until lock acquired
        from geovectors_encoder.services.gv_nle_training_service import (
            GvNleTrainingService,
        )
        return GvNleTrainingService().run_subgraph(sg, env)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
