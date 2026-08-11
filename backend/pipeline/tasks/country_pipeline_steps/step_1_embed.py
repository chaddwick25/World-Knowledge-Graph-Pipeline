"""
Celery task: Step 1 — Embed OSM Entities

Preprocessing + GV-Tags + provisional GV-NLE + entropy gate + subgraph fan-out.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import logging

from django.conf import settings
from pipeline.celery_app import (
    pipeline_task,
    PipelineTask,
)
from pipeline.config import SubgraphConfig
from pipeline.envelopes import CountryEnvelope
from pipeline.task_decorator import pipeline_step
from pipeline.exceptions import EntropyGateBlocked
from pipeline.tasks.helper import (
    _log,
    preprocess_snapshot,
)

logger = logging.getLogger("pipeline")


def _should_drop_indexes_during_load(iso: str) -> bool:
    """Decide whether to drop/rebuild vector indexes during the bulk load.

    Controlled by the ``DROP_INDEXES_DURING_LOAD`` env var:
      - ``auto`` (default): True — HNSW maintenance per row is
        O(m * ef_construction), always drop + rebuild on partitioned tables.
      - ``true`` / ``1``:   Always drop/rebuild
      - ``false`` / ``0``:  Never drop/rebuild
    """
    mode = os.environ.get("DROP_INDEXES_DURING_LOAD", "auto").lower()
    if mode in ("true", "1", "yes"):
        return True
    if mode in ("false", "0", "no"):
        return False
    return True


@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_1_embed_osm_entities",
    max_retries=1, default_retry_delay=120,
)
@pipeline_step("embed_osm_entities", CountryEnvelope, 1.0)
def step_1_embed_osm_entities(self, env: CountryEnvelope) -> CountryEnvelope:
    """Step 1: Preprocessing + embedding + entropy gate + subgraphs.

    Small territories (Monaco, Belize, etc.) with has_subgraphs=False
    automatically skip the subgraph fan-out and run at country level only.
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
        "Step 1: Embed OSM Entities",
        country=env.iso,
        has_pretrained_nle=env.has_pretrained_nle,
        has_subgraphs=env.has_subgraphs,
        subgraph_count=len(env.subgraphs),
        pipeline_run_id=env.pipeline_run_id,
    )

    # Ensure the leaf partition exists before upserting.
    # On a fresh DB, the first call converts the empty monolith to a
    # partitioned table (one-time).  Subsequent calls are idempotent.
    from django.core.management import call_command
    try:
        call_command(
            "create_country_partitions",
            country=env.iso,
            snapshot=env.snapshot_date,
            skip_data=True,
            skip_mv=True,
        )
    except Exception as exc:
        _log(
            logger,
            "warning",
            "Partition creation failed — upserts will use root table",
            country=env.iso,
            error=str(exc),
            pipeline_run_id=env.pipeline_run_id,
        )

    # Snapshot preprocessing (v2 helper with explicit logger)
    preprocess_snapshot(env, logger=logger)

    from extraction.services.embedding_service import EmbeddingService
    drop_indexes = _should_drop_indexes_during_load(env.iso)
    if drop_indexes:
        _log(
            logger,
            "info",
            "Drop-indexes-during-load enabled for bulk upsert",
            country=env.iso,
            pipeline_run_id=env.pipeline_run_id,
        )
    result = EmbeddingService(Path(settings.EMBEDDINGS_ROOT)).run(
        env, drop_indexes_during_load=drop_indexes,
    )

    _log(
        logger,
        "info",
        "Entropy check",
        entropy=round(result["entropy"], 4),
        threshold=env.min_entropy,
        country=env.iso,
        pipeline_run_id=env.pipeline_run_id,
    )
    if result["entropy"] < env.min_entropy and result["entropy"] > 0.0:
        raise EntropyGateBlocked(
            entropy=result["entropy"], threshold=env.min_entropy,
        )

    # Subgraph fan-out: only if country HAS subgraphs
    if env.has_subgraphs and env.subgraphs:
        _log(
            logger,
            "info",
            "Launching subgraph embeddings in parallel",
            subgraph_count=len(env.subgraphs),
            country=env.iso,
            pipeline_run_id=env.pipeline_run_id,
        )
        subgraph_tasks = [
            _embed_subgraph.s(sg.to_dict(), env.to_dict())
            for sg in env.subgraphs
        ]
        from celery import group
        group(subgraph_tasks).apply_async()
    else:
        _log(
            logger,
            "info",
            "No subgraphs to process (small territory or no subgraphs configured)",
            country=env.iso,
            pipeline_run_id=env.pipeline_run_id,
        )

    _log(
        logger,
        "info",
        "Step 1 complete",
        country=env.iso,
        pipeline_run_id=env.pipeline_run_id,
    )
    return env


@pipeline_task(bind=True, base=PipelineTask, name="sub_embed_subgraph")
def _embed_subgraph(
    self, subgraph_dict: dict, parent_config: dict
) -> dict:
    """Embed a single subgraph (parallel Group subtask)."""
    sg = SubgraphConfig.from_dict(subgraph_dict)
    env = CountryEnvelope.from_dict(parent_config)
    _log(
        logger,
        "info",
        "Embedding subgraph",
        subgraph=sg.name,
        country=env.iso,
        pipeline_run_id=env.pipeline_run_id,
    )
    from geovectors_encoder.services.geovectors_service import (
        GeoVectorsEncoderService,
    )
    service = GeoVectorsEncoderService()
    result = service.generate_subgraph_pickle(
        country_name=env.name,
        subgraph_name=sg.name,
        continent=env.continent,
    )
    return {"subgraph": sg.name, "result": result}
