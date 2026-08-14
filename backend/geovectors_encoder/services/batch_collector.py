"""Parallel encode + upsert fan-out for Step 1 (Approach B).

This module implements the in-process producer/consumer thread pool that
parallelizes the encode + upsert stage of ``step_1_embed_osm_entities``
within a single Celery task.  See
``docs/plans/PARALLEL_UPSERT_APPROACH_B_PLAN.md`` for the full design.

Contract (do not break — ``read_from_snapshot`` and the
``itertools.chain(w_data, r_data)`` loop in ``EmbeddingService.run`` depend
on this):

- ``BatchCollector`` exposes ``add_line(record)`` and ``storage`` (self),
  so it is a drop-in replacement for ``DBOnlyWriter`` / ``DualEncodingWriter``
  from the perspective of ``core/util.py:read_from_snapshot`` and the
  post-dependency-pass loop.
- ``storage.flush()`` is a no-op when the buffer is empty (``SampleHandler``
  calls it every ``chunk_size`` nodes — must not enqueue empty batches).
- ``finish(n_workers)`` flushes any partial batch and enqueues one sentinel
  per worker so every consumer thread terminates exactly once.
- ``counts`` / ``total`` reflect every record handed to ``add_line``,
  grouped by OSM type (``node`` / ``way`` / ``relation``).

Thread-safety (verified against the code, see plan §2.3):

- ``FastTextModel`` and ``NLEModel`` are shared across worker threads.
  FastText inference releases the GIL; ``NLEModel.encode_coords`` issues a
  PostGIS KNN query via ``DjangoPostgresDB.get_pool_connection()`` which
  returns the *calling thread's* Django connection — each worker gets its
  own psycopg2 connection automatically.
- One ``VectorStorageService`` per type per worker thread = fully isolated.
- Worker threads close their own Django connections in ``finally`` via
  ``connections.close_all()`` (no request lifecycle in Celery threads).
"""

from __future__ import annotations

import logging
import queue
import threading
import traceback
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Sentinel enqueued once per worker to signal "no more batches".
# Distinct object identity is the termination signal — never put a batch
# that compares equal to this on the queue.
SENTINEL = object()


class ErrorBox:
    """Thread-safe single-slot container for the first worker exception.

    Only the first exception is recorded; subsequent workers that fail
    simply drain the queue so the producer can't deadlock on a full
    bounded queue.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._exc: Optional[BaseException] = None
        self._tb: Optional[str] = None

    def set(self, exc: BaseException) -> None:
        with self._lock:
            if self._exc is None:
                self._exc = exc
                self._tb = traceback.format_exc()

    def get(self) -> Tuple[Optional[BaseException], Optional[str]]:
        with self._lock:
            return self._exc, self._tb

    def is_set(self) -> bool:
        with self._lock:
            return self._exc is not None


class BatchCollector:
    """Drop-in writer replacement that fans records out to a worker pool.

    ``add_line`` buffers raw OSM records (the same shapes
    ``DBOnlyWriter.add_line`` accepts: node ``[id, 'node', tags, lat, lon]``
    and way/relation ``(id, type, tags, x, y)`` tuples from ``concat_data``).
    When the buffer reaches ``batch_size`` records, the batch is pushed onto
    the bounded queue for consumer threads to encode + upsert.

    No encoding happens here — that is the workers' job.  This keeps the
    pyosmium callback (GIL-held) cheap and lets the GIL-released FastText
    inference run in parallel across workers.

    Attributes:
        counts: Per-type record counts ``{"node": N, "way": N, "relation": N}``.
        total:  Sum of all counts (convenience for ``entity_count`` return).
    """

    def __init__(self, work_queue: "queue.Queue[List[Any]]", batch_size: int = 20000) -> None:
        self._queue = work_queue
        self._batch_size = batch_size
        self._buffer: List[Any] = []
        self._counts: Dict[str, int] = {"node": 0, "way": 0, "relation": 0}
        # ``storage`` is self so ``read_from_snapshot`` / ``SampleHandler``
        # can call ``writer.storage.flush()`` and reach ``BatchCollector.flush``.
        self.storage = self

    @property
    def counts(self) -> Dict[str, int]:
        return dict(self._counts)

    @property
    def total(self) -> int:
        return sum(self._counts.values())

    def add_line(self, record: Any) -> None:
        """Buffer a record; push a full batch to the queue when ready.

        Records are indexed by ``record[1]`` (OSM type) for counting.
        Unknown types fall into a ``"other"`` bucket so counts never lose
        rows, but the supported types (node/way/relation) are the only ones
        produced by ``read_from_snapshot``.
        """
        self._buffer.append(record)
        osm_type = record[1] if len(record) > 1 else None
        if osm_type in self._counts:
            self._counts[osm_type] += 1
        else:
            self._counts[osm_type] = self._counts.get(osm_type, 0) + 1
        if len(self._buffer) >= self._batch_size:
            self._push_batch()

    def flush(self) -> None:
        """Push any partial batch to the queue.  No-op when the buffer is empty.

        ``SampleHandler`` calls ``writer.storage.flush()`` every ``chunk_size``
        nodes — this must not enqueue empty batches (workers would spin on
        zero-record batches).
        """
        if self._buffer:
            self._push_batch()

    def finish(self, n_workers: int) -> None:
        """Flush the final partial batch and enqueue one sentinel per worker."""
        self.flush()
        for _ in range(n_workers):
            self._queue.put(SENTINEL)

    def _push_batch(self) -> None:
        batch = self._buffer
        self._buffer = []
        # Bounded queue → blocks (backpressure) if workers are behind.
        # This caps in-flight memory at ~workers*queue_depth*batch_size records.
        self._queue.put(batch)


def encoding_worker(
    worker_id: int,
    work_queue: "queue.Queue[Any]",
    error_box: ErrorBox,
    ft_model: Any,
    nle_model: Optional[Any],
    tags_storage: Any,
    nle_storage: Optional[Any],
) -> None:
    """Consume batches until a sentinel is seen; encode + upsert each record.

    One ``VectorStorageService`` per type is created by the caller and bound
    to this worker thread — they are never shared.  ``ft_model`` and
    ``nle_model`` are shared across workers (read-only inference, see plan
    §2.3).

    On exception: record it in ``error_box``, then drain the queue to
    sentinel so the producer thread can't block on a full bounded queue.
    Django connections for *this* thread are always closed in ``finally``.
    """
    from django.db import connections

    try:
        while True:
            batch = work_queue.get()
            if batch is SENTINEL:
                break
            for record in batch:
                try:
                    tag_vec = ft_model.encode_instance(record)
                except Exception:
                    # A single bad record shouldn't kill the worker — but if
                    # encoding is fundamentally broken we want to know fast.
                    # Re-raise to trigger the error-box path below.
                    raise
                if tag_vec is not None:
                    tags_storage.add(record, tag_vec)
                if nle_model is not None:
                    nle_vec = nle_model.encode_instance(record)
                    if nle_vec is not None and nle_storage is not None:
                        nle_storage.add(record, nle_vec)
            tags_storage.flush()
            if nle_storage is not None:
                nle_storage.flush()
    except Exception as exc:
        error_box.set(exc)
        logger.error(
            "[parallel-embed] worker %d failed: %s", worker_id, exc, exc_info=True,
        )
        # Drain the queue so the producer never blocks on queue.put().
        # Stop after consuming n_workers sentinels worth — in practice we
        # just drain until we've seen enough sentinels that the queue is
        # empty, which happens naturally because finish() enqueues exactly
        # n_workers of them.
        try:
            while True:
                item = work_queue.get_nowait()
                if item is SENTINEL:
                    continue
        except queue.Empty:
            pass
    finally:
        # Belt-and-braces: flush any partial batch in this worker's storages.
        try:
            tags_storage.flush()
        except Exception:
            logger.warning("[parallel-embed] worker %d tags flush failed in finally", worker_id)
        if nle_storage is not None:
            try:
                nle_storage.flush()
            except Exception:
                logger.warning("[parallel-embed] worker %d nle flush failed in finally", worker_id)
        # Close THIS thread's Django connections (no request lifecycle in
        # Celery worker threads to do it for us).  Pattern exists in
        # geovectors_service.py:1250-1252.
        try:
            connections.close_all()
        except Exception:
            logger.warning("[parallel-embed] worker %d connections.close_all failed", worker_id)


def spawn_encode_workers(
    work_queue: "queue.Queue[Any]",
    n_workers: int,
    ft_model: Any,
    nle_model: Optional[Any],
    snapshot_date: str,
    country_code: str,
    has_nle: bool,
) -> Tuple[List[threading.Thread], ErrorBox]:
    """Create and start ``n_workers`` consumer threads.

    Each thread gets its own ``VectorStorageService`` per type (never shared).
    Returns ``(threads, error_box)``.  Caller is responsible for
    ``collector.finish(n_workers)``, ``join``-ing the threads, and checking
    ``error_box`` after join.
    """
    from geovectors_encoder.services.vector_storage_service import VectorStorageService

    error_box = ErrorBox()
    threads: List[threading.Thread] = []
    for worker_id in range(n_workers):
        tags_storage = VectorStorageService(
            model_type="tags", version=snapshot_date,
            snapshot_id=snapshot_date, country_code=country_code,
        )
        nle_storage = None
        if has_nle:
            nle_storage = VectorStorageService(
                model_type="nle", version=snapshot_date,
                snapshot_id=snapshot_date, country_code=country_code,
            )
        thread = threading.Thread(
            target=encoding_worker,
            args=(worker_id, work_queue, error_box, ft_model, nle_model,
                  tags_storage, nle_storage),
            name=f"parallel-embed-{worker_id}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    return threads, error_box
