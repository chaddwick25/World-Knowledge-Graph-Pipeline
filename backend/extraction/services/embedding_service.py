"""Stateless service: embed OSM entities from a snapshot PBF.

Handles FastText + NLE model loading, writer setup, snapshot reading,
WorldKG class enrichment, and entropy computation.

Pure data plane — no Celery, no logging dispatcher, no WS push.
Receives a CountryEnvelope, reads files, writes to DB,
returns a result dict.
"""

from __future__ import annotations

import itertools
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Dict

if TYPE_CHECKING:
    from pipeline.envelopes import CountryEnvelope

logger = logging.getLogger(__name__)

# Phase gate for the dual-encoding (FastText + NLE) parallel path.
# Phase 1 ships the FastText-only parallel path; Phase 2 enables dual after
# the pickle-country parity test (PARALLEL_UPSERT_APPROACH_B_PLAN.md §5.2)
# passes.
#
# Enabled 2026-08-14: all countries currently have pickle_path=None, so
# flipping this gate routes has_pretrained_nle=True countries to
# _run_parallel (FastText-only) — identical encoding to the single-threaded
# DBOnlyWriter path, just parallelized.  The _run_parallel_dual path is
# only reached when pickle_path is set, which is not the case today.
# If a country with a pickle is added later, _run_parallel_dual will be
# exercised and should be parity-tested first (§5.2).
_PARALLEL_DUAL_ENABLED = True


class EmbeddingService:
    """Embed OSM entities from a snapshot PBF.

    Stateless — takes its root path in ``__init__`` (dependency injection).
    """

    def __init__(self, embeddings_root: Path) -> None:
        self.embeddings_root = Path(embeddings_root)

    def run(self, cfg: "CountryEnvelope", drop_indexes_during_load: bool = False,
            pbf_path_override: str = None,
            skip_index_drop: bool = False,
            skip_index_rebuild: bool = False,
            skip_post_process: bool = False) -> Dict:
        """Run the embedding pipeline for a country.

        Args:
            cfg: CountryEnvelope with snapshot_pbf_path, pickle_path,
                 has_pretrained_nle, iso, snapshot_date, etc.
            drop_indexes_during_load: If True, drop HNSW vector indexes
                HNSW) before the bulk upsert and rebuild them in parallel
                after.  Gives 3-5x faster upserts for large countries
                (Phase 5 of OSMENTITY_MONOLITH_OPTIMIZATION.md).
            pbf_path_override: If set, read from this PBF instead of
                ``cfg.snapshot_pbf_path``.  Used by parallel subgraph upsert
                tasks to read per-subgraph PBFs.
            skip_index_drop: If True, skip dropping indexes (coordinated by
                the caller for parallel subgraph upserts).
            skip_index_rebuild: If True, skip rebuilding indexes (coordinated
                by the caller for parallel subgraph upserts).
            skip_post_process: If True, skip WorldKG enrichment + entropy
                computation (done by the caller after all subgraphs complete).

        Returns:
            ``{"entropy": float, "has_nle": bool, "entity_count": int}``.
        """
        from geovectors_encoder.services.geovectors_service import (
            DBOnlyWriter,
            DualEncodingWriter,
        )
        from geovectors_encoder.services.vector_storage_service import (
            VectorStorageService,
        )
        from geovectors_encoder.core.models.fasttext import FastTextModel
        from geovectors_encoder.core.models.nle import NLEModel
        from geovectors_encoder.core.db import DjangoPostgresDB
        from geovectors_encoder.core.util import read_from_snapshot

        pbf_path = pbf_path_override or cfg.snapshot_pbf_path

        # Parallel upsert configuration (Approach B).  Default workers=8
        # enables the parallel encode + upsert path; set to 1 for the
        # legacy single-threaded path.
        workers = _parallel_upsert_workers()
        queue_depth = _parallel_upsert_queue_depth()
        if workers > 1 and pbf_path:
            pbf_mb = _pbf_size_mb(pbf_path)
            min_mb = _parallel_upsert_min_pbf_mb()
            if pbf_mb is not None and pbf_mb < min_mb:
                logger.info(
                    "PBF %.1f MB < PARALLEL_UPSERT_MIN_PBF_MB %d; using single-threaded path [country=%s]",
                    pbf_mb, min_mb, cfg.iso,
                )
                workers = 1

        use_parallel = workers > 1 and (
            not cfg.has_pretrained_nle or _PARALLEL_DUAL_ENABLED
        )
        if workers > 1 and cfg.has_pretrained_nle and not _PARALLEL_DUAL_ENABLED:
            logger.info(
                "Parallel dual-encoding path not yet enabled (phase-1 gate); "
                "using single-threaded path [country=%s]",
                cfg.iso,
            )

        should_drop = drop_indexes_during_load and not skip_index_drop
        if should_drop:
            self._drop_vector_indexes(cfg.iso, cfg.snapshot_date)

        try:
            if use_parallel:
                if cfg.has_pretrained_nle and cfg.pickle_path:
                    entity_count = self._run_parallel_dual(
                        cfg, pbf_path, workers, queue_depth,
                    )
                else:
                    entity_count = self._run_parallel(
                        cfg, pbf_path, workers, queue_depth,
                    )
            else:
                # === legacy single-threaded path (unchanged behavior) ===
                ft_model = FastTextModel()
                tags_storage = VectorStorageService(
                    model_type="tags", version=cfg.snapshot_date,
                    snapshot_id=cfg.snapshot_date, country_code=cfg.iso,
                )

                if cfg.has_pretrained_nle and cfg.pickle_path:
                    logger.info(
                        "Dual encoding mode (FastText + NLE from pickle) [pickle_path=%s country=%s]",
                        cfg.pickle_path, cfg.iso,
                    )
                    writer, nle_storage = self._build_dual_writer(cfg, ft_model, tags_storage)
                else:
                    if not cfg.has_pretrained_nle:
                        logger.info(
                            "FastText-only mode (no pre-trained NLE model) [country=%s]",
                            cfg.iso,
                        )
                    writer = DBOnlyWriter(ft_model, tags_storage)
                    nle_storage = None

                n_data, w_data, r_data = read_from_snapshot(
                    pbf_path, writer=writer, max_runs=2,
                )
                for record in itertools.chain(w_data, r_data):
                    writer.add_line(record)
                tags_storage.flush()
                if nle_storage:
                    nle_storage.flush()
                    # NLEModel doesn't expose destroy in all versions; guard it
                    try:
                        if hasattr(writer, "nle_encoder") and hasattr(writer.nle_encoder, "destroy"):
                            writer.nle_encoder.destroy()
                    except Exception:
                        pass
                entity_count = len(n_data) + len(w_data) + len(r_data)
                # === end legacy single-threaded path ===
        finally:
            if should_drop and not skip_index_rebuild:
                self._rebuild_vector_indexes(cfg.iso, cfg.snapshot_date)

        if skip_post_process:
            return {
                "entropy": 0.0,
                "has_nle": cfg.has_pretrained_nle,
                "entity_count": entity_count,
            }

        # Enrich WorldKG classes + compute entropy (v2 helpers)
        from pipeline.tasks.helper import enrich_worldkg_classes, compute_entropy
        enrich_worldkg_classes(cfg, logger=logger)
        entropy = compute_entropy(cfg, logger=logger)

        return {
            "entropy": entropy,
            "has_nle": cfg.has_pretrained_nle,
            "entity_count": entity_count,
        }

    def _drop_vector_indexes(self, country_code: str, snapshot_id: str) -> None:
        """Drop HNSW indexes for fast bulk load (no per-row maintenance).

        Per-leaf scoped (PER_LEAF_INDEX_LIFECYCLE_PLAN.md §3.1): only the
        current country's leaf partition is touched, so other countries'
        search stays online during this upsert.
        """
        from django.core.management import call_command

        logger.info(
            "Dropping HNSW indexes on leaf %s_%s for bulk load...",
            snapshot_id, country_code,
        )
        call_command(
            "drop_osmentity_vector_indexes",
            country=country_code, snapshot=snapshot_id,
        )
        logger.info("Vector indexes dropped.")

    def _rebuild_vector_indexes(self, country_code: str, snapshot_id: str) -> None:
        """Rebuild HNSW indexes after bulk load (per-leaf scoped).

        Per-leaf scoped (PER_LEAF_INDEX_LIFECYCLE_PLAN.md §3.1-3.2): only the
        current country's leaf is rebuilt, so the cost scales with the country
        being upserted, not the total rows across all countries.

        NOTE: the `static_embedding` HNSW index is NOT rebuilt here.  It is
        only meaningful after Step 5 (GV-NLE training) populates
        `static_embedding`.  Build it via `compute_static_embeddings` +
        `create_static_embedding_hnsw_index` after Step 5, or wire it into
        Step 6 in a future change.  Rebuilding it in Step 1 was wasted work
        (mostly-NULL rows).
        """
        from django.core.management import call_command

        logger.info(
            "Rebuilding HNSW indexes on leaf %s_%s...",
            snapshot_id, country_code,
        )
        call_command(
            "create_osmentity_vector_indexes",
            country=country_code, snapshot=snapshot_id,
        )
        logger.info("Vector indexes rebuilt.")

    def _build_dual_writer(self, cfg, ft_model, tags_storage):
        """Build a DualEncodingWriter for the FastText + NLE path.

        Returns (writer, nle_storage).
        """
        from geovectors_encoder.core.models.nle import NLEModel
        from geovectors_encoder.core.db import DjangoPostgresDB
        from geovectors_encoder.services.geovectors_service import DualEncodingWriter
        from geovectors_encoder.services.vector_storage_service import VectorStorageService

        db_bridge = DjangoPostgresDB()
        nle_model = NLEModel(
            str(Path(cfg.pickle_path).parent),
            njobs=1, db=db_bridge,
        )
        nle_model.load_indexes()
        nle_storage = VectorStorageService(
            model_type="nle", version=cfg.snapshot_date,
            snapshot_id=cfg.snapshot_date, country_code=cfg.iso,
        )
        writer = DualEncodingWriter(
            tag_encoder=ft_model,
            nle_encoder=nle_model,
            tag_storage=tags_storage,
            nle_storage=nle_storage,
        )
        return writer, nle_storage

    # ------------------------------------------------------------------
    # Parallel encode + upsert (Approach B — in-memory batch fan-out)
    # ------------------------------------------------------------------

    def _run_parallel(self, cfg: "CountryEnvelope", pbf_path: str,
                      workers: int, queue_depth: int) -> int:
        """Parallel FastText-only encode + upsert.

        Phase 1 path: ``cfg.has_pretrained_nle`` is False (no pickle).
        Spawns ``workers`` consumer threads, streams the PBF once through
        ``read_from_snapshot`` on the main thread via a ``BatchCollector``
        (drop-in writer replacement), then joins workers and re-raises the
        first worker error if any.

        Returns the total entity count (nodes + ways + relations).
        """
        import queue as queue_mod
        from geovectors_encoder.core.models.fasttext import FastTextModel
        from geovectors_encoder.core.util import read_from_snapshot
        from geovectors_encoder.services.batch_collector import (
            BatchCollector,
            SENTINEL,
            spawn_encode_workers,
        )

        logger.info(
            "Parallel embed (FastText-only): workers=%d queue_depth=%d [country=%s]",
            workers, queue_depth, cfg.iso,
        )
        ft_model = FastTextModel()
        work_queue: "queue_mod.Queue" = queue_mod.Queue(maxsize=workers * queue_depth)
        threads, upsert_thread, upsert_queue, error_box = spawn_encode_workers(
            work_queue, workers, ft_model, nle_model=None,
            snapshot_date=cfg.snapshot_date, country_code=cfg.iso,
            has_nle=False,
        )
        collector = BatchCollector(work_queue)
        try:
            n_data, w_data, r_data = read_from_snapshot(
                pbf_path, writer=collector, max_runs=2,
            )
            for record in itertools.chain(w_data, r_data):
                collector.add_line(record)
            # Send sentinels to encoding workers
            collector.finish(workers)
            # Wait for all encoding workers to finish pushing to upsert_queue
            for t in threads:
                t.join()
            # Send sentinel to upsert worker
            upsert_queue.put(SENTINEL)
            # Wait for upsert worker to finish
            upsert_thread.join()
            exc, tb = error_box.get()
            if exc is not None:
                raise RuntimeError(
                    f"parallel embed worker failed: {exc}"
                ) from exc
        finally:
            # Belt-and-braces: ensure workers can't hang if the producer
            # raised before finish().  Sentinels are idempotent — extra ones
            # are dropped by workers (they break on the first sentinel).
            try:
                collector.finish(workers)
            except Exception:
                pass
            try:
                upsert_queue.put(SENTINEL)
            except Exception:
                pass
        return collector.total

    def _run_parallel_dual(self, cfg: "CountryEnvelope", pbf_path: str,
                           workers: int, queue_depth: int) -> int:
        """Parallel FastText + NLE encode + upsert (dual encoding).

        Phase 2 path: ``cfg.has_pretrained_nle`` is True and a pickle exists.
        Shares one ``NLEModel`` across workers (read-only inference; each
        worker's KNN query uses its own thread-local Django connection —
        see plan §2.3).  ``NLEModel.destroy()`` is called from the main
        thread after workers join (unchanged position relative to encoding).
        """
        import queue as queue_mod
        from geovectors_encoder.core.models.fasttext import FastTextModel
        from geovectors_encoder.core.util import read_from_snapshot
        from geovectors_encoder.services.batch_collector import (
            BatchCollector,
            SENTINEL,
            spawn_encode_workers,
        )

        logger.info(
            "Parallel embed (FastText + NLE dual): workers=%d queue_depth=%d [country=%s]",
            workers, queue_depth, cfg.iso,
        )
        ft_model = FastTextModel()
        nle_model = self._build_shared_nle_model(cfg)
        work_queue: "queue_mod.Queue" = queue_mod.Queue(maxsize=workers * queue_depth)
        threads, upsert_thread, upsert_queue, error_box = spawn_encode_workers(
            work_queue, workers, ft_model, nle_model=nle_model,
            snapshot_date=cfg.snapshot_date, country_code=cfg.iso,
            has_nle=True,
        )
        collector = BatchCollector(work_queue)
        try:
            n_data, w_data, r_data = read_from_snapshot(
                pbf_path, writer=collector, max_runs=2,
            )
            for record in itertools.chain(w_data, r_data):
                collector.add_line(record)
            # Send sentinels to encoding workers
            collector.finish(workers)
            # Wait for all encoding workers to finish pushing to upsert_queue
            for t in threads:
                t.join()
            # Send sentinel to upsert worker
            upsert_queue.put(SENTINEL)
            # Wait for upsert worker to finish
            upsert_thread.join()
            exc, tb = error_box.get()
            if exc is not None:
                raise RuntimeError(
                    f"parallel embed worker failed: {exc}"
                ) from exc
        finally:
            try:
                collector.finish(workers)
            except Exception:
                pass
            try:
                upsert_queue.put(SENTINEL)
            except Exception:
                pass
            # NLEModel.destroy() is a no-op on DjangoPostgresDB (Django owns
            # connections) but call it for parity with the single-threaded path.
            try:
                nle_model.destroy()
            except Exception:
                pass
        return collector.total

    def _build_shared_nle_model(self, cfg: "CountryEnvelope"):
        """Build a single NLEModel shared across worker threads."""
        from geovectors_encoder.core.models.nle import NLEModel
        from geovectors_encoder.core.db import DjangoPostgresDB

        db_bridge = DjangoPostgresDB()
        nle_model = NLEModel(
            str(Path(cfg.pickle_path).parent),
            njobs=1, db=db_bridge,
        )
        nle_model.load_indexes()
        return nle_model


# ----------------------------------------------------------------------
# Parallel upsert env-var parsing (Approach B configuration).
# Parsed here (not in settings.py) per the plan — no envelope changes,
# no settings.py changes.  Logged at Step 1 start.
# ----------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid int for %s=%r; using default %d", name, raw, default)
        return default


def _parallel_upsert_workers() -> int:
    """Encoding threads.  8 = default (parallel encode, serial upsert).

    Set to 1 to force the legacy single-threaded path.
    """
    return max(1, _env_int("PARALLEL_UPSERT_WORKERS", 8))


def _parallel_upsert_queue_depth() -> int:
    """Batches in flight per worker (queue maxsize = workers * depth)."""
    return max(1, _env_int("PARALLEL_UPSERT_QUEUE_DEPTH", 2))


def _parallel_upsert_min_pbf_mb() -> int:
    """Skip parallel path for PBFs smaller than this (overhead dominates)."""
    return max(0, _env_int("PARALLEL_UPSERT_MIN_PBF_MB", 0))


def _pbf_size_mb(pbf_path: str):
    """Return PBF size in MB, or None if the file can't be stat'd."""
    try:
        return Path(pbf_path).stat().st_size / (1024 * 1024)
    except OSError:
        return None
