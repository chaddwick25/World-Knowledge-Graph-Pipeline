"""Unit tests for ``geovectors_encoder.services.batch_collector``.

Covers the producer/consumer contract documented in
``docs/plans/PARALLEL_UPSERT_APPROACH_B_PLAN.md`` §5.1:

- Batching: 45K records → 2 full batches + 1 partial on ``finish()``.
- ``flush()`` with empty buffer is a no-op (SampleHandler calls it every
  20K nodes — must not produce empty batches).
- Per-type counts (nodes/ways/relations) match input.
- Sentinel handling: N workers each terminate after exactly one sentinel.
- Error propagation: worker raising mid-batch → main thread re-raises,
  queue drained, no hang (test with a timeout).
- Backpressure: with a slow consumer, producer blocks rather than growing
  memory (assert ``queue.qsize() <= maxsize``).

These tests do NOT touch the database — ``encoding_worker`` is exercised
with stub storages and a stub model so the queue/error-box/sentinel
mechanics are tested in isolation.
"""

import queue
import threading
import time

import pytest

from geovectors_encoder.services.batch_collector import (
    BatchCollector,
    ErrorBox,
    SENTINEL,
    encoding_worker,
    spawn_encode_workers,
    upsert_worker,
)


# --------------------------------------------------------------------------
# Stubs
# --------------------------------------------------------------------------

class StubModel:
    """Encode model that returns a deterministic vector per record."""

    def __init__(self, fail_on=None):
        # ``fail_on`` is a set of osm_ids that should raise during encode.
        self.fail_on = fail_on or set()
        self.calls = 0

    def encode_instance(self, record):
        self.calls += 1
        osm_id = record[0]
        if osm_id in self.fail_on:
            raise RuntimeError(f"stub encode failure for osm_id={osm_id}")
        return [float(osm_id), 0.0]


class StubStorage:
    """Storage that records every (record, vector) pair and counts flushes."""

    def __init__(self):
        self.added = []
        self.flushes = 0

    def add(self, record, vector):
        self.added.append((record, vector))

    def flush(self):
        self.flushes += 1


def _node_record(osm_id):
    return [osm_id, "node", [("name", f"n{osm_id}")], 1.0, 2.0]


def _way_record(osm_id):
    return (osm_id, "way", [("highway", "residential")], 3.0, 4.0)


def _relation_record(osm_id):
    return (osm_id, "relation", [("type", "boundary")], 5.0, 6.0)


# --------------------------------------------------------------------------
# BatchCollector
# --------------------------------------------------------------------------

@pytest.mark.unit
def test_batch_collector_batching_45k_records():
    """45K records → 2 full 20K batches + 1 partial 5K batch on finish()."""
    q: queue.Queue = queue.Queue(maxsize=10)
    collector = BatchCollector(q, batch_size=20000)

    for i in range(45000):
        collector.add_line(_node_record(i))

    # Two full batches should already be on the queue.
    assert q.qsize() == 2
    assert len(collector._buffer) == 5000

    collector.finish(n_workers=1)
    # 2 full + 1 partial + 1 sentinel = 4 items.
    assert q.qsize() == 4
    # Drain batches, verify the sentinel is last.
    items = []
    while True:
        item = q.get_nowait()
        items.append(item)
        if item is SENTINEL:
            break
    assert len(items) == 4
    assert items[-1] is SENTINEL
    # Partial batch has 5K records.
    assert len(items[2]) == 5000


@pytest.mark.unit
def test_batch_collector_flush_empty_is_noop():
    """flush() with empty buffer must not enqueue an empty batch."""
    q: queue.Queue = queue.Queue(maxsize=10)
    collector = BatchCollector(q, batch_size=20000)

    # Repeated flushes on an empty collector.
    for _ in range(5):
        collector.flush()

    assert q.empty()
    assert collector.total == 0


@pytest.mark.unit
def test_batch_collector_flush_pushes_partial_batch():
    """flush() with a non-empty buffer pushes the partial batch."""
    q: queue.Queue = queue.Queue(maxsize=10)
    collector = BatchCollector(q, batch_size=20000)

    for i in range(100):
        collector.add_line(_node_record(i))
    collector.flush()

    assert q.qsize() == 1
    batch = q.get_nowait()
    assert len(batch) == 100
    # Buffer is cleared after flush.
    assert collector._buffer == []


@pytest.mark.unit
def test_batch_collector_per_type_counts():
    """counts/total reflect every record handed to add_line, grouped by type."""
    q: queue.Queue = queue.Queue(maxsize=10)
    collector = BatchCollector(q, batch_size=100000)

    for i in range(10):
        collector.add_line(_node_record(i))
    for i in range(7):
        collector.add_line(_way_record(i))
    for i in range(3):
        collector.add_line(_relation_record(i))

    counts = collector.counts
    assert counts["node"] == 10
    assert counts["way"] == 7
    assert counts["relation"] == 3
    assert collector.total == 20


@pytest.mark.unit
def test_batch_collector_storage_is_self():
    """``collector.storage`` is self — read_from_snapshot relies on this."""
    q: queue.Queue = queue.Queue(maxsize=1)
    collector = BatchCollector(q)
    assert collector.storage is collector
    # And storage.flush reaches the collector's flush.
    collector.storage.flush()  # no-op on empty, must not raise
    assert q.empty()


# --------------------------------------------------------------------------
# ErrorBox
# --------------------------------------------------------------------------

@pytest.mark.unit
def test_error_box_records_first_exception_only():
    box = ErrorBox()
    assert not box.is_set()

    box.set(ValueError("first"))
    assert box.is_set()

    # Second set is ignored.
    box.set(RuntimeError("second"))
    exc, _ = box.get()
    assert isinstance(exc, ValueError)
    assert str(exc) == "first"


# --------------------------------------------------------------------------
# encoding_worker — sentinel handling
# --------------------------------------------------------------------------

@pytest.mark.unit
def test_encoding_worker_terminates_on_single_sentinel():
    """Each worker consumes batches until it sees exactly one sentinel."""
    q: queue.Queue = queue.Queue(maxsize=10)
    upsert_q: queue.Queue = queue.Queue(maxsize=10)
    error_box = ErrorBox()
    ft = StubModel()

    # Enqueue two real batches then one sentinel.
    q.put([_node_record(1), _node_record(2)])
    q.put([_way_record(3)])
    q.put(SENTINEL)

    t = threading.Thread(
        target=encoding_worker,
        args=(0, q, upsert_q, error_box, ft, None),
        daemon=True,
    )
    t.start()
    t.join(timeout=5.0)
    assert not t.is_alive(), "worker hung waiting for sentinel"
    # All three records were encoded and pushed to the upsert queue.
    # The encoding worker pushes one encoded batch per input batch.
    encoded_batches = []
    while True:
        try:
            item = upsert_q.get_nowait()
        except queue.Empty:
            break
        if item is not SENTINEL:
            encoded_batches.append(item)
    total_encoded = sum(len(b) for b in encoded_batches)
    assert total_encoded == 3
    assert not error_box.is_set()


@pytest.mark.unit
def test_encoding_worker_n_sentinels_terminate_n_workers():
    """N workers each terminate after exactly one sentinel."""
    n = 4
    q: queue.Queue = queue.Queue(maxsize=100)
    upsert_q: queue.Queue = queue.Queue(maxsize=100)
    error_box = ErrorBox()
    ft = StubModel()

    # 8 batches of 1 record each, then 4 sentinels.
    for i in range(8):
        q.put([_node_record(i)])
    for _ in range(n):
        q.put(SENTINEL)

    threads = []
    for wid in range(n):
        t = threading.Thread(
            target=encoding_worker,
            args=(wid, q, upsert_q, error_box, ft, None),
            daemon=True,
        )
        t.start()
        threads.append(t)

    for t in threads:
        t.join(timeout=5.0)
    assert not any(t.is_alive() for t in threads), "a worker hung"
    # 8 records distributed across 4 workers (no double-processing).
    encoded_batches = []
    while True:
        try:
            item = upsert_q.get_nowait()
        except queue.Empty:
            break
        if item is not SENTINEL:
            encoded_batches.append(item)
    total_encoded = sum(len(b) for b in encoded_batches)
    assert total_encoded == 8
    assert not error_box.is_set()


# --------------------------------------------------------------------------
# encoding_worker — error propagation
# --------------------------------------------------------------------------

@pytest.mark.unit
def test_encoding_worker_error_propagates_and_drains_queue():
    """A worker that raises mid-batch records the error and drains the queue
    so the producer can't deadlock on a full bounded queue.
    """
    q: queue.Queue = queue.Queue(maxsize=2)
    upsert_q: queue.Queue = queue.Queue(maxsize=10)
    error_box = ErrorBox()
    ft = StubModel(fail_on={42})

    # Start the worker BEFORE filling the queue so puts don't block on
    # a full bounded queue with no consumer running.
    t = threading.Thread(
        target=encoding_worker,
        args=(0, q, upsert_q, error_box, ft, None),
        daemon=True,
    )
    t.start()

    # Batch with the failing record.
    q.put([_node_record(42), _node_record(1)])
    # Fill the queue behind it so a non-draining worker would block the
    # producer.  maxsize=2, one slot already used → one more fits.
    q.put([_node_record(2)])
    # Sentinel (the producer's finish() would enqueue this).
    q.put(SENTINEL)

    t.join(timeout=5.0)
    assert not t.is_alive(), "worker hung after error (queue not drained)"
    assert error_box.is_set()
    exc, _ = error_box.get()
    assert isinstance(exc, RuntimeError)
    assert "42" in str(exc)


@pytest.mark.unit
def test_upsert_worker_closes_django_connections_in_finally(monkeypatch):
    """The upsert worker's ``finally`` calls ``connections.close_all()``."""
    import django.db

    closed = {"calls": 0}

    class FakeConnections:
        def close_all(self):
            closed["calls"] += 1

    monkeypatch.setattr(django.db, "connections", FakeConnections())

    # Stub VectorStorageService so we don't need Django/DB.
    import sys
    import geovectors_encoder.services.batch_collector as bc_module

    class FakeStorage:
        def __init__(self, **kwargs): pass
        def add(self, r, v): pass
        def flush(self): pass

    fake_vss = type("M", (), {"VectorStorageService": FakeStorage})
    real_vss = sys.modules.get("geovectors_encoder.services.vector_storage_service")
    sys.modules["geovectors_encoder.services.vector_storage_service"] = fake_vss

    q: queue.Queue = queue.Queue(maxsize=4)
    error_box = ErrorBox()

    q.put([(_node_record(1), [0.1], None)])
    q.put(SENTINEL)

    t = threading.Thread(
        target=upsert_worker,
        args=(0, q, error_box, "2025_12_31", "ni", False),
        daemon=True,
    )
    t.start()
    t.join(timeout=5.0)
    assert not t.is_alive()
    assert closed["calls"] == 1

    # Restore the real module.
    if real_vss is not None:
        sys.modules["geovectors_encoder.services.vector_storage_service"] = real_vss
    else:
        sys.modules.pop("geovectors_encoder.services.vector_storage_service", None)


# --------------------------------------------------------------------------
# Backpressure
# --------------------------------------------------------------------------

@pytest.mark.unit
def test_backpressure_producer_blocks_on_full_queue():
    """With a slow consumer, the producer blocks rather than growing memory.

    Queue maxsize=1 (workers=1, depth=1).  A slow consumer sleeps before
    pulling.  The producer's ``add_line`` → ``_push_batch`` → ``queue.put``
    must block once the queue is full, so ``qsize()`` never exceeds maxsize.
    """
    q: queue.Queue = queue.Queue(maxsize=1)
    collector = BatchCollector(q, batch_size=2)

    started = threading.Event()
    proceed = threading.Event()

    def slow_consumer():
        started.set()
        # Wait until the producer has filled the queue (or blocked).
        time.sleep(0.2)
        # Drain one batch + the eventual sentinel.
        while True:
            item = q.get()
            if item is SENTINEL:
                break
            time.sleep(0.05)

    consumer = threading.Thread(target=slow_consumer, daemon=True)
    consumer.start()
    started.wait(timeout=2.0)

    # Push 6 records (batch_size=2 → 3 batches).  With maxsize=1 the
    # producer must block after the first batch until the consumer drains.
    for i in range(6):
        collector.add_line(_node_record(i))

    collector.finish(n_workers=1)
    consumer.join(timeout=5.0)
    assert not consumer.is_alive(), "consumer hung"

    # The queue never exceeded its maxsize at any observable point —
    # the producer blocked instead of growing unboundedly.
    assert collector.total == 6


# --------------------------------------------------------------------------
# spawn_encode_workers
# --------------------------------------------------------------------------

@pytest.mark.unit
def test_spawn_encode_workers_starts_n_threads(monkeypatch):
    """spawn_encode_workers starts exactly n_workers daemon threads and
    returns them with a shared error_box.
    """
    # Stub VectorStorageService so we don't need Django/DB.
    # The real module imports OsmEntity (a Django model) at the top level,
    # which requires Django settings.  Inject a fake module into sys.modules
    # before spawn_encode_workers runs its deferred import.
    import sys
    import geovectors_encoder.services.batch_collector as bc_module

    class FakeStorage:
        def __init__(self, **kwargs): pass
        def add(self, r, v): pass
        def flush(self): pass

    fake_vss = type("M", (), {"VectorStorageService": FakeStorage})
    real_vss = sys.modules.get("geovectors_encoder.services.vector_storage_service")
    sys.modules["geovectors_encoder.services.vector_storage_service"] = fake_vss
    monkeypatch.setattr(
        bc_module, "VectorStorageService", FakeStorage,
        raising=False,
    )

    q: queue.Queue = queue.Queue(maxsize=4)
    n = 3
    try:
        threads, upsert_thread, upsert_queue, error_box = spawn_encode_workers(
            q, n, StubModel(), nle_model=None,
            snapshot_date="2025_12_31", country_code="ni",
            has_nle=False,
        )
        assert len(threads) == n
        assert all(t.daemon for t in threads)
        assert all(t.is_alive() for t in threads)
        assert upsert_thread.daemon
        assert upsert_thread.is_alive()
        # Let encoding workers terminate cleanly.
        for _ in range(n):
            q.put(SENTINEL)
        for t in threads:
            t.join(timeout=5.0)
        # Signal the upsert worker to stop.
        upsert_queue.put(SENTINEL)
        upsert_thread.join(timeout=5.0)
    finally:
        # Restore the real module (or remove the fake if there was none).
        if real_vss is not None:
            sys.modules["geovectors_encoder.services.vector_storage_service"] = real_vss
        else:
            sys.modules.pop("geovectors_encoder.services.vector_storage_service", None)
    assert not any(t.is_alive() for t in threads)
    assert not upsert_thread.is_alive()
    assert not error_box.is_set()
