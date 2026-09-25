"""Integration parity test: single-threaded vs parallel embed + upsert.

Per ``docs/plans/PARALLEL_UPSERT_APPROACH_B_PLAN.md`` §5.2:

- Fixture: smallest available country PBF (Belize, ~21 MB / ~3K entities).
- Run A: ``PARALLEL_UPSERT_WORKERS=1``.  Snapshot row counts + a sample of
  ``gv_tags_embedding`` values.
- Run B: ``PARALLEL_UPSERT_WORKERS=4``, same PBF, fresh leaf.
- Assert: identical row count, identical embeddings for sampled
  ``(osm_type, osm_id)``, identical ``entity_count`` return, entropy within
  float tolerance.

Step 1 is FastText-only (GV-Tags): the former dual-encoding (FastText + NLE
from pickle) variant was removed 2026-09-10 with `DualEncodingWriter` /
`_run_parallel_dual` — see ``docs/issues/TICKET_REMOVE_DUAL_ENCODER.md``.
GV-NLE comes from Step 5 training + the inductive query-time path.

This is a true integration test — it requires:
- Docker services up (postgres-default, postgres-vectors, redis, backend)
- A Belize snapshot PBF on disk (or the country specified by
  ``PARITY_TEST_COUNTRY`` env var, default ``BZ``)
- The leaf partition created via ``create_country_partitions --country BZ
  --skip-data --skip-mv`` before each run

The test is gated behind the ``integration`` marker and a
``PARITY_TEST_ENABLED=1`` env var so it does not run in the default unit
suite.  Run it manually:

    docker compose exec backend python -m pytest \
        tests/integration/test_parallel_embed_parity.py \
        -m integration -s -p no:asyncio

or, to skip pytest entirely:

    docker compose exec backend python \
        tests/integration/test_parallel_embed_parity.py

The script path (``__main__``) runs ``run_parity_check()`` directly.
"""

from __future__ import annotations

import os
import unittest
from typing import Dict, List, Tuple

# Django models are imported lazily inside tests so the module can be
# imported without Django setup (for collection / syntax checks).


def _enabled() -> bool:
    return os.environ.get("PARITY_TEST_ENABLED", "0") == "1"


def _country() -> str:
    return os.environ.get("PARITY_TEST_COUNTRY", "BZ")


def _workers_parallel() -> int:
    return int(os.environ.get("PARITY_TEST_WORKERS", "4"))


def _embedding_sample(osm_type: str, osm_ids: List[int]) -> List[Tuple]:
    """Return ``[(osm_id, gv_tags_embedding), ...]`` for the given IDs."""
    from worldkg_nca.models import OsmEntity

    rows = OsmEntity.objects.filter(
        osm_type=osm_type, osm_id__in=osm_ids,
    ).values_list("osm_id", "gv_tags_embedding")
    return list(rows)


def _row_count(snapshot_id: str, country_code: str) -> int:
    from worldkg_nca.models import OsmEntity

    return OsmEntity.objects.filter(
        snapshot_id=snapshot_id, country_code=country_code,
    ).count()


def _pg_stat_activity_count() -> int:
    """Count active connections on the vectors DB."""
    from django.db import connections

    with connections["vectors"].cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE state <> 'idle';")
        return cur.fetchone()[0]


def _truncate_leaf(snapshot_id: str, country_code: str) -> None:
    """Delete all rows for this (snapshot, country) leaf before a run."""
    from worldkg_nca.models import OsmEntity

    OsmEntity.objects.filter(
        snapshot_id=snapshot_id, country_code=country_code,
    ).delete()


def _run_embed(workers: int, iso: str) -> Dict:
    """Run EmbeddingService for ``iso`` with the given worker count.

    The worker count is applied via the hyperparams cache (parallel_upsert
    section) — the YAML-loaded defaults are frozen per process.

    Returns the service result dict (``entity_count``, ``entropy``, ``has_nle``).
    """
    from dataclasses import replace

    from core.services.snapshot.embedding_service import EmbeddingService
    from django.conf import settings
    from pipeline import envelopes as _envelopes_mod
    from pipeline.envelopes import CountryEnvelope, ModelHyperparams

    env = CountryEnvelope.from_db(iso, hyperparam_overrides={
        "skip_enrich": True,
        "min_entropy": 0.0,
    })
    # Use a per-run snapshot_id suffix so parallel and single runs land in
    # different leaves and we can compare counts without cross-contamination.
    suffix = "p" if workers > 1 else "s"
    env.snapshot_date = f"{env.snapshot_date}_{suffix}"

    hp = ModelHyperparams.load_from_yaml()
    _envelopes_mod._HYPERPARAMS_CACHE = replace(
        hp,
        parallel_upsert_workers=workers,
    )

    service = EmbeddingService(embeddings_root=getattr(settings, "EMBEDDINGS_ROOT", "/app/data/embeddings"))
    return service.run(
        env,
        skip_post_process=True,
    )


class ParallelEmbedParityTest(unittest.TestCase):
    """Assert single-threaded and parallel runs produce identical DB state."""

    @unittest.skipUnless(_enabled(), "Set PARITY_TEST_ENABLED=1 to run")
    def test_fasttext_only_parity(self):
        """Run A (workers=1) vs Run B (workers=4): same row count + embeddings."""
        from django.core.management import call_command

        iso = _country()
        # Build the envelope once to read snapshot_date + has_pretrained_nle.
        from pipeline.envelopes import CountryEnvelope
        probe = CountryEnvelope.from_db(iso)
        base_snapshot = probe.snapshot_date

        # --- Run A: single-threaded ---
        call_command("create_country_partitions", country=iso, snapshot=f"{base_snapshot}_s", skip_data=True, skip_mv=True)
        _truncate_leaf(f"{base_snapshot}_s", iso)
        result_single = _run_embed(workers=1, iso=iso)
        count_single = _row_count(f"{base_snapshot}_s", iso)

        # Sample 50 entity IDs from the single-threaded run for embedding parity.
        from worldkg_nca.models import OsmEntity
        sample_ids = list(
            OsmEntity.objects.filter(
                snapshot_id=f"{base_snapshot}_s", country_code=iso,
            ).values_list("osm_id", flat=True)[:50]
        )
        sample_single = _embedding_sample("node", sample_ids) if sample_ids else []

        # --- Run B: parallel ---
        call_command("create_country_partitions", country=iso, snapshot=f"{base_snapshot}_p", skip_data=True, skip_mv=True)
        _truncate_leaf(f"{base_snapshot}_p", iso)
        conn_before = _pg_stat_activity_count()
        result_parallel = _run_embed(workers=_workers_parallel(), iso=iso)
        count_parallel = _row_count(f"{base_snapshot}_p", iso)
        conn_after = _pg_stat_activity_count()

        # --- Assertions ---
        # Row counts must match exactly.
        self.assertEqual(
            count_single, count_parallel,
            f"row count mismatch: single={count_single} parallel={count_parallel}",
        )
        # entity_count return must match (collector counts all three types;
        # see plan §8.3 — this is an intentional accuracy fix).
        self.assertEqual(
            result_single["entity_count"], result_parallel["entity_count"],
            f"entity_count mismatch: single={result_single['entity_count']} "
            f"parallel={result_parallel['entity_count']}",
        )
        # Embedding parity for the sampled IDs.  Vectors are L2-normalized
        # floats; allow tiny tolerance for floating-point determinism.
        sample_parallel = _embedding_sample("node", sample_ids) if sample_ids else []
        self.assertEqual(len(sample_single), len(sample_parallel))
        single_map = dict(sample_single)
        parallel_map = dict(sample_parallel)
        for osm_id in sample_ids:
            v1 = single_map.get(osm_id)
            v2 = parallel_map.get(osm_id)
            self.assertIsNotNone(v1, f"osm_id={osm_id} missing in single run")
            self.assertIsNotNone(v2, f"osm_id={osm_id} missing in parallel run")
            # pgvector returns lists; compare element-wise with tolerance.
            for a, b in zip(v1, v2):
                self.assertAlmostEqual(float(a), float(b), places=5,
                                        msg=f"embedding mismatch for osm_id={osm_id}")

        # No connection leak from worker threads.
        # Allow a small delta for transient activity, but the parallel run
        # must not leave extra open connections after workers join.
        self.assertLessEqual(
            conn_after, conn_before + 1,
            f"connection leak: before={conn_before} after={conn_after}",
        )


def run_parity_check() -> int:
    """Entry point for running the parity check without pytest."""
    import django
    django.setup()
    suite = unittest.TestLoader().loadTestsFromTestCase(ParallelEmbedParityTest)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    import sys
    sys.exit(run_parity_check())
