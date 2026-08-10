"""
Celery task: Step 1 — Embed OSM Entities

Preprocessing + GV-Tags + provisional GV-NLE + entropy gate + subgraph fan-out.

For countries WITH subgraphs:
  1. preprocess_snapshot() generates the country PBF + subgraph PBFs
  2. create_country_partitions creates the leaf partition
  3. Drop indexes once (coordinated)
  4. Dispatch a chord of _embed_subgraph_upsert tasks (one per subgraph PBF)
     → each task reads its subgraph PBF and upserts into the country leaf
  5. _finalize_subgraph_upserts callback: rebuild indexes, enrich, entropy

For countries WITHOUT subgraphs:
  1. preprocess_snapshot() generates the country PBF
  2. create_country_partitions creates the leaf partition
  3. EmbeddingService.run() reads the country PBF and upserts (single-threaded)
  4. NLE pickle generation subgraph fan-out (if subgraphs exist for NLE only)
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

# Countries with >10M OSM entities where drop-indexes-during-load gives 3-5x
# faster upserts.  See docs/plans/OSMENTITY_MONOLITH_OPTIMIZATION.md Phase 5.
_LARGE_COUNTRIES = frozenset({
    "AU", "CA", "US", "BR", "IN", "CN", "RU", "ID", "MX", "JP",
    "DE", "FR", "ES", "IT", "PL", "TR", "AR", "CO", "ZA", "GB",
})


def _should_drop_indexes_during_load(iso: str) -> bool:
    """Decide whether to drop/rebuild vector indexes during the bulk load.

    Controlled by the ``DROP_INDEXES_DURING_LOAD`` env var:
      - ``auto`` (default): True when using the partitioned table (HNSW
        maintenance per row is expensive even for small countries like CV).
        Falls back to the large-country list for the monolith.
      - ``true`` / ``1``:   Always drop/rebuild
      - ``false`` / ``0``:  Never drop/rebuild
    """
    mode = os.environ.get("DROP_INDEXES_DURING_LOAD", "auto").lower()
    if mode in ("true", "1", "yes"):
        return True
    if mode in ("false", "0", "no"):
        return False
    # auto: on the partitioned table, HNSW indexes on leaf partitions make
    # per-row upserts O(m * ef_construction) — always drop + rebuild.
    # Use the runtime pg_partitioned_table check (sole source of truth).
    from django.db import connections
    try:
        with connections["vectors"].cursor() as cursor:
            cursor.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_partitioned_table pt
                    JOIN pg_class c ON c.oid = pt.partrelid
                    WHERE c.relname = 'semantic_search_osmentity'
                );
            """)
            if cursor.fetchone()[0]:
                return True
    except Exception:
        pass
    return iso.upper() in _LARGE_COUNTRIES


def _has_subgraph_pbfs(subgraphs) -> bool:
    """Check if any subgraph has a PBF file that exists on disk."""
    for sg in subgraphs:
        if sg.pbf_path and Path(sg.pbf_path).exists():
            return True
    return False


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

    # Snapshot preprocessing (generates country PBF + subgraph PBFs)
    preprocess_snapshot(env, logger=logger)

    # Rehydrate subgraphs from DB AFTER preprocess_snapshot, which syncs
    # SubgraphProfile records with PBF/poly paths.  The envelope's subgraphs
    # may have been constructed before the PBFs existed (pbf_path=None).
    if env.has_subgraphs:
        try:
            fresh = CountryEnvelope.from_db(env.iso, snapshot_date=env.snapshot_date)
            if fresh.has_subgraphs and fresh.subgraphs:
                env = dataclasses.replace(
                    env,
                    subgraphs=fresh.subgraphs,
                )
                pbf_count = sum(
                    1 for sg in env.subgraphs
                    if sg.pbf_path and Path(sg.pbf_path).exists()
                )
                _log(
                    logger,
                    "info",
                    "Rehydrated subgraphs after preprocess_snapshot",
                    country=env.iso,
                    subgraph_count=len(env.subgraphs),
                    pbf_count=pbf_count,
                    pipeline_run_id=env.pipeline_run_id,
                )
        except Exception as exc:
            _log(
                logger,
                "warning",
                "Post-preprocess subgraph rehydration failed",
                country=env.iso,
                error=str(exc),
                pipeline_run_id=env.pipeline_run_id,
            )

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

    # ── Parallel subgraph upsert path ──────────────────────────────
    # For countries with subgraphs that have PBF files, dispatch a chord
    # of parallel upsert tasks (one per subgraph PBF).  Each task reads
    # its subgraph PBF and upserts into the same country leaf partition.
    # The chord callback rebuilds indexes, enriches, and checks entropy.
    if env.has_subgraphs and env.subgraphs and _has_subgraph_pbfs(env.subgraphs):
        subgraph_tasks = []
        for sg in env.subgraphs:
            if sg.pbf_path and Path(sg.pbf_path).exists():
                subgraph_tasks.append(
                    _embed_subgraph_upsert.s(sg.to_dict(), env.to_dict())
                )

        if subgraph_tasks:
            _log(
                logger,
                "info",
                "Launching parallel subgraph upserts",
                subgraph_count=len(subgraph_tasks),
                country=env.iso,
                drop_indexes=drop_indexes,
                pipeline_run_id=env.pipeline_run_id,
            )

            # Drop indexes once before all subgraph tasks
            if drop_indexes:
                EmbeddingService(Path(settings.EMBEDDINGS_ROOT))._drop_vector_indexes()

            from celery import chord
            chord(subgraph_tasks)(
                _finalize_subgraph_upserts.s(
                    env.to_dict(),
                    drop_indexes,
                )
            )
            # The chord callback returns the envelope — but since we're
            # in a @pipeline_step task, we need to return the env here.
            # The chord runs asynchronously; step_1 returns immediately.
            # The chord callback handles entropy gate + NLE fan-out.
            return env

    # ── Single-threaded country-level upsert path ───────────────────
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

    # Subgraph NLE pickle fan-out (for countries with subgraphs but no
    # subgraph PBFs — NLE pickles are generated from the country PBF)
    if env.has_subgraphs and env.subgraphs:
        _launch_nle_pickle_fanout(env)

    _log(
        logger,
        "info",
        "Step 1 complete",
        country=env.iso,
        pipeline_run_id=env.pipeline_run_id,
    )
    return env


def _launch_nle_pickle_fanout(env: CountryEnvelope) -> None:
    """Dispatch NLE pickle generation tasks for each subgraph."""
    _log(
        logger,
        "info",
        "Launching subgraph NLE pickle generation in parallel",
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


# ══════════════════════════════════════════════════════════════════════════
# Parallel subgraph upsert tasks
# ══════════════════════════════════════════════════════════════════════════

@pipeline_task(bind=True, base=PipelineTask, name="sub_embed_subgraph_upsert")
def _embed_subgraph_upsert(
    self, subgraph_dict: dict, parent_config: dict
) -> dict:
    """Upsert entities from a single subgraph PBF (parallel chord subtask).

    Reads the subgraph PBF, encodes with FastText (+ NLE if available),
    and upserts into the country leaf partition.  Indexes are NOT managed
    here — the caller (_finalize_subgraph_upserts) handles drop/rebuild.
    """
    sg = SubgraphConfig.from_dict(subgraph_dict)
    env = CountryEnvelope.from_dict(parent_config)

    if not sg.pbf_path or not Path(sg.pbf_path).exists():
        _log(
            logger,
            "warning",
            "Subgraph PBF not found — skipping upsert",
            subgraph=sg.name,
            pbf_path=sg.pbf_path,
            country=env.iso,
            pipeline_run_id=env.pipeline_run_id,
        )
        return {"subgraph": sg.name, "entity_count": 0, "status": "skipped"}

    _log(
        logger,
        "info",
        "Upserting subgraph entities",
        subgraph=sg.name,
        pbf_path=sg.pbf_path,
        country=env.iso,
        pipeline_run_id=env.pipeline_run_id,
    )

    from extraction.services.embedding_service import EmbeddingService
    result = EmbeddingService(Path(settings.EMBEDDINGS_ROOT)).run(
        env,
        drop_indexes_during_load=False,
        pbf_path_override=sg.pbf_path,
        skip_index_drop=True,
        skip_index_rebuild=True,
        skip_post_process=True,
    )

    _log(
        logger,
        "info",
        "Subgraph upsert complete",
        subgraph=sg.name,
        entity_count=result["entity_count"],
        country=env.iso,
        pipeline_run_id=env.pipeline_run_id,
    )

    return {
        "subgraph": sg.name,
        "entity_count": result["entity_count"],
        "status": "completed",
    }


@pipeline_task(bind=True, base=PipelineTask, name="step_1b_finalize_subgraph_upserts")
def _finalize_subgraph_upserts(
    self, subgraph_results: list, config_dict: dict = None,
    drop_indexes: bool = False,
) -> dict:
    """Chord callback — finalize after all subgraph upserts complete.

    1. Rebuild vector indexes (dropped before parallel upserts)
    2. Enrich WorldKG classes
    3. Compute entropy + check entropy gate
    4. Launch NLE pickle generation fan-out
    """
    env = CountryEnvelope.from_dict(config_dict) if config_dict else None

    total_entities = sum(
        r.get("entity_count", 0) for r in subgraph_results
        if isinstance(r, dict)
    )
    completed = sum(
        1 for r in subgraph_results
        if isinstance(r, dict) and r.get("status") == "completed"
    )
    skipped = sum(
        1 for r in subgraph_results
        if isinstance(r, dict) and r.get("status") == "skipped"
    )

    _log(
        logger,
        "info",
        "Finalizing subgraph upserts",
        country=env.iso if env else "unknown",
        total_entities=total_entities,
        subgraphs_completed=completed,
        subgraphs_skipped=skipped,
        pipeline_run_id=env.pipeline_run_id if env else "unknown",
    )

    # 1. Rebuild indexes
    if drop_indexes:
        from extraction.services.embedding_service import EmbeddingService
        _log(
            logger,
            "info",
            "Rebuilding vector indexes after parallel upsert",
            country=env.iso if env else "unknown",
            pipeline_run_id=env.pipeline_run_id if env else "unknown",
        )
        EmbeddingService(Path(settings.EMBEDDINGS_ROOT))._rebuild_vector_indexes()

    if not env:
        return {"status": "completed", "error": "no config"}

    # 2. Enrich WorldKG classes + 3. Compute entropy
    from extraction.services.embedding_service import EmbeddingService
    from pipeline.tasks.helper import enrich_worldkg_classes, compute_entropy
    enrich_worldkg_classes(env, logger=logger)
    entropy = compute_entropy(env, logger=logger)

    _log(
        logger,
        "info",
        "Entropy check (parallel upsert)",
        entropy=round(entropy, 4),
        threshold=env.min_entropy,
        country=env.iso,
        pipeline_run_id=env.pipeline_run_id,
    )
    if entropy < env.min_entropy and entropy > 0.0:
        raise EntropyGateBlocked(
            entropy=entropy, threshold=env.min_entropy,
        )

    # 4. NLE pickle fan-out
    if env.has_subgraphs and env.subgraphs:
        _launch_nle_pickle_fanout(env)

    _log(
        logger,
        "info",
        "Step 1 complete (parallel subgraph upsert)",
        country=env.iso,
        pipeline_run_id=env.pipeline_run_id,
    )

    return env.to_dict()


# ══════════════════════════════════════════════════════════════════════════
# NLE pickle generation (unchanged from original)
# ══════════════════════════════════════════════════════════════════════════

@pipeline_task(bind=True, base=PipelineTask, name="sub_embed_subgraph")
def _embed_subgraph(
    self, subgraph_dict: dict, parent_config: dict
) -> dict:
    """Generate NLE pickle for a single subgraph (parallel Group subtask)."""
    sg = SubgraphConfig.from_dict(subgraph_dict)
    env = CountryEnvelope.from_dict(parent_config)
    _log(
        logger,
        "info",
        "Generating NLE pickle for subgraph",
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
