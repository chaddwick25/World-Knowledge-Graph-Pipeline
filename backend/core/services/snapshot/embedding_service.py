"""Stateless service: embed OSM entities from a snapshot PBF.

Handles FastText model loading, writer setup, snapshot reading,
WorldKG class enrichment, and entropy computation.  Step 1 is
FastText-only (GV-Tags); GV-NLE comes from Step 5 training and the
inductive query-time path (see
docs/Schematics/01_Encoder/01_Two_Axis_vs_Single_Axis_Encoding.md).

Pure data plane — no Celery, no logging dispatcher, no WS push.
Receives a CountryEnvelope, reads files, writes to DB,
returns a result dict.
"""

from __future__ import annotations

import itertools
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Dict

from django.conf import settings

if TYPE_CHECKING:
    from pipeline.envelopes import CountryEnvelope

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Embed OSM entities from a snapshot PBF.

    Stateless — takes its root path in ``__init__`` (dependency injection).
    """

    def __init__(self, embeddings_root: Path) -> None:
        self.embeddings_root = Path(embeddings_root)

    @staticmethod
    def _resolve_snapshot_uuid(country_code: str, snapshot_date: str):
        """Look up the Snapshot UUID for (country_code, snapshot_date).

        Returns a string UUID or None if the Snapshot row doesn't exist yet.
        """
        try:
            from osmsnapshot.models import Snapshot
            snap = Snapshot.objects.filter(
                country_code=country_code, snapshot_date=snapshot_date,
            ).only("id").first()
            return str(snap.id) if snap else None
        except Exception:
            return None

    def run(self, cfg: "CountryEnvelope",
            pbf_path_override: str = None,
            skip_post_process: bool = False) -> Dict:
        """Run the embedding pipeline for a country.

        Args:
            cfg: CountryEnvelope with snapshot_pbf_path, pickle_path,
                 has_pretrained_nle, iso, snapshot_date, etc.
            pbf_path_override: If set, read from this PBF instead of
                ``cfg.snapshot_pbf_path``.  Used by parallel subgraph upsert
                tasks to read per-subgraph PBFs.
            skip_post_process: If True, skip WorldKG enrichment + entropy
                computation (done by the caller after all subgraphs complete).

        Returns:
            ``{"entropy": float, "has_nle": bool, "entity_count": int}``.
        """
        from geovectors_encoder.services.geovectors_service import DBOnlyWriter
        from geovectors_encoder.services.vector_storage_service import (
            VectorStorageService,
        )
        from geovectors_encoder.core.models.fasttext import FastTextModel
        from geovectors_encoder.core.util import read_from_snapshot

        pbf_path = pbf_path_override or cfg.snapshot_pbf_path

        # Resolve the Snapshot UUID for this (country_code, snapshot_date).
        # This is stored on OsmEntity.source_snapshot_id for cross-DB provenance.
        source_snapshot_id = self._resolve_snapshot_uuid(cfg.iso, cfg.snapshot_date)

        # Parallel upsert configuration (Approach B).  Default workers=8
        # enables the parallel encode + upsert path; set to 1 for the
        # legacy single-threaded path.
        workers = _parallel_upsert_workers()
        queue_depth = _parallel_upsert_queue_depth()
        chunk_size = _parallel_upsert_chunk_size()
        logger.info(
            "EmbeddingService config: workers=%d queue_depth=%d chunk_size=%d [country=%s]",
            workers, queue_depth, chunk_size, cfg.iso,
        )
        if workers > 1 and pbf_path:
            pbf_mb = _pbf_size_mb(pbf_path)
            min_mb = _parallel_upsert_min_pbf_mb()
            if pbf_mb is not None and pbf_mb < min_mb:
                logger.info(
                    "PBF %.1f MB < PARALLEL_UPSERT_MIN_PBF_MB %d; using single-threaded path [country=%s]",
                    pbf_mb, min_mb, cfg.iso,
                )
                workers = 1

        # Step 1 is FastText-only (GV-Tags).  The two-axis Step-1 path
        # (DualEncodingWriter / _run_parallel_dual) was removed 2026-09-10 —
        # see docs/issues/TICKET_REMOVE_DUAL_ENCODER.md.  GV-NLE comes from
        # Step 5 training + the inductive query-time path, so the parallel
        # path needs no NLE gating.
        if workers > 1:
            entity_count = self._run_parallel(
                cfg, pbf_path, workers, queue_depth, source_snapshot_id,
            )
        else:
            # === legacy single-threaded path (unchanged behavior) ===
            ft_model = FastTextModel()
            tags_storage = VectorStorageService(
                model_type="tags", version=cfg.snapshot_date,
                snapshot_id=cfg.snapshot_date, country_code=cfg.iso,
                source_snapshot_id=source_snapshot_id,
            )

            writer = DBOnlyWriter(ft_model, tags_storage)
            n_data, w_data, r_data = read_from_snapshot(
                pbf_path, writer=writer, max_runs=2,
                chunk_size=_parallel_upsert_chunk_size(),
            )
            for record in itertools.chain(w_data, r_data):
                writer.add_line(record)
            tags_storage.flush()
            entity_count = len(n_data) + len(w_data) + len(r_data)
            # === end legacy single-threaded path ===

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

    # ------------------------------------------------------------------
    # Parallel encode + upsert (Approach B — in-memory batch fan-out)
    # ------------------------------------------------------------------

    def _run_parallel(self, cfg: "CountryEnvelope", pbf_path: str,
                      workers: int, queue_depth: int,
                      source_snapshot_id: str = None) -> int:
        """Parallel FastText-only encode + upsert.

        Spawns ``workers`` consumer threads, streams the PBF once through
        ``read_from_snapshot`` on the main thread via a ``BatchCollector``
        (drop-in writer replacement), then joins workers and re-raises the
        first worker error if any.

        Args:
            source_snapshot_id: Snapshot UUID for provenance, passed through
                to every upserted ``OsmEntity`` row.

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
            has_nle=False, source_snapshot_id=source_snapshot_id,
        )
        collector = BatchCollector(work_queue)
        try:
            n_data, w_data, r_data = read_from_snapshot(
                pbf_path, writer=collector, max_runs=2,
                chunk_size=_parallel_upsert_chunk_size(),
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


# ----------------------------------------------------------------------
# Parallel upsert knob parsing (Approach B configuration).
# The knobs are declared env-backed in settings.py; parsed here lazily so
# tests can setattr() per call.  Logged at Step 1 start.
# ----------------------------------------------------------------------

def _cfg_int(name: str, default: int) -> int:
    raw = getattr(settings, name, None)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid int for %s=%r; using default %d", name, raw, default)
        return default


def _parallel_upsert_workers() -> int:
    """Encoding threads.  8 = default (parallel encode, serial upsert).

    Set to 1 to force the legacy single-threaded path.
    """
    return max(1, _cfg_int("PARALLEL_UPSERT_WORKERS", 1))


def _parallel_upsert_queue_depth() -> int:
    """Batches in flight per worker (queue maxsize = workers * depth)."""
    return max(1, _cfg_int("PARALLEL_UPSERT_QUEUE_DEPTH", 2))


def _parallel_upsert_min_pbf_mb() -> int:
    """Skip parallel path for PBFs smaller than this (overhead dominates)."""
    return max(0, _cfg_int("PARALLEL_UPSERT_MIN_PBF_MB", 0))


def _parallel_upsert_chunk_size() -> int:
    """Batch size (records per flush) passed to ``read_from_snapshot``.

    Default 20000 (the upstream GeoVectors value).  Increasing this reduces
    per-batch SQL overhead (fewer WAL flushes / transaction commits) at the
    cost of higher peak memory.  Tripling to 60000 with 2 workers is a
    proposed throughput optimization — see
    ``docs/Schematics/OSM_Streaming_Core_Pattern.md`` §9.3 and the
    benchmark test ``tests/integration/test_chunk_size_benchmark.py``.

    Memory note: each encoded record is ~400D × 4 bytes = ~1.6 KB for the
    GV-Tags vector, plus raw record overhead.  60k records ≈ 96 MB per
    encoded batch; with 2 workers + 1 upsert in flight ≈ 288 MB for encoded
    vectors alone (plus the ~2 GB FastText model).
    """
    return max(1000, _cfg_int("PARALLEL_UPSERT_CHUNK_SIZE", 20000))


def _pbf_size_mb(pbf_path: str):
    """Return PBF size in MB, or None if the file can't be stat'd."""
    try:
        return Path(pbf_path).stat().st_size / (1024 * 1024)
    except OSError:
        return None
