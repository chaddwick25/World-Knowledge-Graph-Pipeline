"""Benchmark: chunk_size × workers sweep for Step 1 embed + upsert throughput.

Investigates whether **2 workers + 60k batch** outperforms the current
production default of **1 worker + 20k batch** (3,283 rows/s baseline from
``docs/plans/completed/PARALLEL_UPSERT_APPROACH_B_PLAN.md``).

The 8-worker regression (34% slower) was caused by CPU saturation — 10
threads on 4 cores (2.5x oversubscription).  2 workers + 1 upsert + 1
pyosmium = 4 threads on 4 cores = **zero oversubscription**, which is a
fundamentally different regime.  Tripling the batch size reduces per-batch
SQL overhead (fewer WAL flushes / transaction commits) and gives each
encoding thread more work between queue handoffs.

## Configurations swept

| Label | Workers | Chunk size | Rationale |
|-------|---------|------------|-----------|
| baseline | 1 | 20 000 | Current production default |
| 2w-20k | 2 | 20 000 | Isolate worker effect (same batch) |
| 2w-40k | 2 | 40 000 | Double batch, modest parallelism |
| 2w-60k | 3 | 60 000 | Proposed: triple batch + 2 workers |
| 1w-60k | 1 | 60 000 | Isolate batch-size effect (no parallelism) |
| 4w-20k | 4 | 20 000 | Mid-parallelism reference |
| 4w-40k | 4 | 40 000 | Mid-parallelism + larger batch |

## Requirements

- Docker services up (postgres-default, postgres-vectors, redis, backend)
- A country snapshot PBF on disk (default: Belize ``BZ``, ~21 MB / ~3K
  entities — small enough for fast iteration; override with
  ``BENCH_COUNTRY=IE`` for a larger dataset)
- Leaf partitions created before each run via
  ``create_country_partitions --country XX --skip-data --skip-mv``

## Running

    # Quick: Belize (~3K entities, ~10s per config)
    docker compose exec backend python -m pytest \
        tests/integration/test_chunk_size_benchmark.py \
        -m integration -s -p no:asyncio

    # Larger: Ireland (~9.7M entities, ~50-70 min per config)
    BENCH_COUNTRY=IE BENCH_ENABLED=1 docker compose exec backend python -m pytest \
        tests/integration/test_chunk_size_benchmark.py \
        -m integration -s -p no:asyncio

    # Or run directly (no pytest):
    BENCH_ENABLED=1 docker compose exec backend python \
        tests/integration/test_chunk_size_benchmark.py

    # Custom sweep (comma-separated):
    BENCH_SWEEP="1:20000,2:60000,1:60000" BENCH_ENABLED=1 \
        docker compose exec backend python \
        tests/integration/test_chunk_size_benchmark.py

## Output

Prints a per-configuration table:

    ┌──────────────┬─────────┬──────────┬───────────┬──────────────┬───────────┐
    │ config       │ workers │ chunk_sz │ rows      │ wall_clock_s │ rows/sec  │
    ├──────────────┼─────────┼──────────┼───────────┼──────────────┼───────────┤
    │ baseline     │       1 │    20000 │     3023  │       12.4   │    243.8  │
    │ 2w-60k       │       2 │    60000 │     3023  │        9.1   │    332.2  │
    └──────────────┴─────────┴──────────┴───────────┴──────────────┴───────────┘

The test **does not assert** a winner — it collects and prints the data.
The caller decides whether to adopt a configuration based on the numbers.
"""

from __future__ import annotations

import os
import time
import unittest
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pytest

# Ensure DJANGO_SETTINGS_MODULE is set for direct-script execution
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")


# ────────────────────────────────────────────────────────────────────────────
# Configuration
# ────────────────────────────────────────────────────────────────────────────

def _enabled() -> bool:
    return os.environ.get("BENCH_ENABLED", "0") == "1"


def _country() -> str:
    return os.environ.get("BENCH_COUNTRY", "BZ")


def _sweep() -> List[Tuple[int, int]]:
    """Parse BENCH_SWEEP env var or return the default sweep.

    Format: ``workers:chunk_size,workers:chunk_size,...``
    Default: ``1:20000,2:20000,2:40000,2:60000,1:60000,4:20000,4:40000``
    """
    raw = os.environ.get("BENCH_SWEEP")
    if raw:
        result = []
        for part in raw.split(","):
            w, c = part.strip().split(":")
            result.append((int(w), int(c)))
        return result
    return [
        (1, 20_000),
        (2, 20_000),
        (2, 40_000),
        (2, 60_000),
        (1, 60_000),
        (4, 20_000),
        (4, 40_000),
    ]


def _repeats() -> int:
    """Number of repeats per config (to detect variance)."""
    return int(os.environ.get("BENCH_REPEATS", "1"))


# ────────────────────────────────────────────────────────────────────────────
# Result tracking
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class BenchResult:
    label: str
    workers: int
    chunk_size: int
    entity_count: int = 0
    wall_clock_s: float = 0.0
    rows_per_s: float = 0.0
    error: Optional[str] = None
    repeats: List[float] = field(default_factory=list)

    def compute_throughput(self):
        if self.wall_clock_s > 0 and self.entity_count > 0:
            self.rows_per_s = self.entity_count / self.wall_clock_s


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _truncate_leaf(snapshot_id: str, country_code: str) -> None:
    from worldkg_nca.models import OsmEntity
    OsmEntity.objects.filter(
        snapshot_id=snapshot_id, country_code=country_code,
    ).delete()


def _row_count(snapshot_id: str, country_code: str) -> int:
    from worldkg_nca.models import OsmEntity
    return OsmEntity.objects.filter(
        snapshot_id=snapshot_id, country_code=country_code,
    ).count()


def _run_embed(workers: int, chunk_size: int, iso: str, run_label: str) -> Dict:
    """Run EmbeddingService for ``iso`` with the given workers + chunk_size.

    Returns the service result dict (``entity_count``, ``entropy``, ``has_nle``).
    """
    from dataclasses import replace

    from core.services.snapshot.embedding_service import EmbeddingService
    from django.conf import settings
    from pipeline import hyperparams as _hyperparams_mod
    from pipeline.envelopes import CountryEnvelope, ModelHyperparams

    # Build the base envelope, then patch snapshot_date on the mutable state.
    # snapshot_date lives on CountryRunState (mutable), accessed via env.state.
    # IMPORTANT: use underscores, not hyphens — the snapshot_id becomes part
    # of the leaf partition table name (embeddings_<snapshot>_<country>),
    # and PostgreSQL identifiers can't contain hyphens.
    env = CountryEnvelope.from_db(iso)
    base_snapshot = env.snapshot_date
    env.state.snapshot_date = f"{base_snapshot}_{run_label}"

    # Patch the hyperparams cache (parallel_upsert section) so this run uses
    # the sweep's workers + chunk_size.  The YAML-loaded defaults are frozen
    # per process; replacing the cache is the per-run override mechanism.
    hp = ModelHyperparams.load_from_yaml()
    _hyperparams_mod._HYPERPARAMS_CACHE = replace(
        hp,
        parallel_upsert_workers=workers,
        parallel_upsert_chunk_size=chunk_size,
    )

    service = EmbeddingService(
        embeddings_root=getattr(settings, "EMBEDDINGS_ROOT", "/app/data/embeddings")
    )
    return service.run(env, skip_post_process=True)


def _config_label(workers: int, chunk_size: int) -> str:
    if workers == 1 and chunk_size == 20_000:
        return "baseline"
    # Use underscores — label becomes part of the SQL partition table name
    return f"{workers}w_{chunk_size // 1000}k"


# ────────────────────────────────────────────────────────────────────────────
# Benchmark runner
# ────────────────────────────────────────────────────────────────────────────

def run_benchmark() -> List[BenchResult]:
    """Run the full sweep and return results.

    Requires Django setup (called by the test or __main__).
    """
    from django.core.management import call_command
    from pipeline.envelopes import CountryEnvelope

    iso = _country()
    sweep = _sweep()
    repeats = _repeats()

    # Probe the base snapshot date
    probe = CountryEnvelope.from_db(iso)
    base_snapshot = probe.snapshot_date

    results: List[BenchResult] = []

    for workers, chunk_size in sweep:
        label = _config_label(workers, chunk_size)
        result = BenchResult(label=label, workers=workers, chunk_size=chunk_size)
        snapshot_id = f"{base_snapshot}_{label}"

        for rep in range(repeats):
            run_label = f"{label}_r{rep}" if repeats > 1 else label
            snap = f"{base_snapshot}_{run_label}"

            # Create + truncate the leaf partition
            try:
                call_command(
                    "create_country_partitions",
                    country=iso, snapshot=snap,
                    skip_data=True, skip_mv=True,
                )
            except Exception as exc:
                # Partition may already exist — that's fine
                pass
            _truncate_leaf(snap, iso)

            # Run + time it
            t0 = time.monotonic()
            try:
                svc_result = _run_embed(workers, chunk_size, iso, run_label)
                elapsed = time.monotonic() - t0
                count = _row_count(snap, iso)
                result.entity_count = count
                result.repeats.append(elapsed)
                if repeats == 1:
                    result.wall_clock_s = elapsed
                else:
                    # Use the median repeat
                    result.repeats.sort()
                    result.wall_clock_s = result.repeats[len(result.repeats) // 2]
                result.compute_throughput()
                print(
                    f"  [{label}] rep {rep}: {count} rows in {elapsed:.1f}s "
                    f"= {count / elapsed:.0f} rows/s"
                )
            except Exception as exc:
                result.error = str(exc)
                print(f"  [{label}] rep {rep}: FAILED — {exc}")
                break

        results.append(result)

    return results


def print_results_table(results: List[BenchResult]):
    """Print a formatted table of benchmark results."""
    if not results:
        print("No results to display.")
        return

    # Find the baseline for comparison
    baseline = next((r for r in results if r.label == "baseline"), None)
    baseline_rps = baseline.rows_per_s if baseline and baseline.rows_per_s > 0 else None

    print()
    print("┌──────────────┬─────────┬──────────┬───────────┬──────────────┬───────────┬──────────┐")
    print("│ config       │ workers │ chunk_sz │ rows      │ wall_clock_s │ rows/sec  │ vs base  │")
    print("├──────────────┼─────────┼──────────┼───────────┼──────────────┼───────────┼──────────┤")
    for r in results:
        if r.error:
            print(f"│ {r.label:<12} │ {r.workers:>7} │ {r.chunk_size:>8} │ {'ERROR':>9} │ {'—':>12} │ {'—':>9} │ {'—':>8} │")
            continue
        vs_base = ""
        if baseline_rps and r.rows_per_s > 0:
            delta = (r.rows_per_s / baseline_rps - 1) * 100
            vs_base = f"{delta:+.0f}%"
        print(
            f"│ {r.label:<12} │ {r.workers:>7} │ {r.chunk_size:>8} │ "
            f"{r.entity_count:>9,} │ {r.wall_clock_s:>12.1f} │ "
            f"{r.rows_per_s:>9.0f} │ {vs_base:>8} │"
        )
    print("└──────────────┴─────────┴──────────┴───────────┴──────────────┴───────────┴──────────┘")

    if baseline_rps:
        best = max((r for r in results if r.rows_per_s > 0), key=lambda r: r.rows_per_s)
        print(f"\nBaseline: {baseline_rps:.0f} rows/s")
        print(f"Best:     {best.rows_per_s:.0f} rows/s ({best.label})")
        if best.rows_per_s > baseline_rps:
            improvement = (best.rows_per_s / baseline_rps - 1) * 100
            print(f"  → {improvement:+.0f}% vs baseline")
        else:
            print(f"  → No improvement over baseline (single-threaded wins)")

    # Print repeat variance if multiple repeats
    has_repeats = any(len(r.repeats) > 1 for r in results)
    if has_repeats:
        print("\nRepeat variance:")
        for r in results:
            if len(r.repeats) > 1:
                import statistics
                mean = statistics.mean(r.repeats)
                stdev = statistics.stdev(r.repeats) if len(r.repeats) > 1 else 0
                print(f"  {r.label}: mean={mean:.1f}s stdev={stdev:.1f}s cv={stdev/mean*100:.1f}%")


# ────────────────────────────────────────────────────────────────────────────
# Test class (pytest-compatible)
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.django_db(transaction=False)
class ChunkSizeBenchmark(unittest.TestCase):
    """Sweep chunk_size × workers and print a throughput comparison table.

    Gated behind ``BENCH_ENABLED=1`` so it doesn't run in the default suite.
    Does NOT assert a winner — it collects data and prints it.  The caller
    decides whether to adopt a configuration.
    """

    @unittest.skipUnless(_enabled(), "Set BENCH_ENABLED=1 to run")
    def test_chunk_size_workers_sweep(self):
        results = run_benchmark()
        print_results_table(results)

        # Sanity: every non-error result should have upserted some rows
        for r in results:
            if not r.error:
                self.assertGreater(
                    r.entity_count, 0,
                    f"{r.label}: expected non-zero entity count",
                )

        # Print a recommendation hint
        baseline = next((r for r in results if r.label == "baseline"), None)
        if baseline and baseline.rows_per_s > 0:
            winners = [r for r in results if r.rows_per_s > baseline.rows_per_s * 1.10]
            if winners:
                best = max(winners, key=lambda r: r.rows_per_s)
                print(f"\n>>> RECOMMENDATION: {best.label} is {best.rows_per_s / baseline.rows_per_s * 100 - 100:.0f}% faster than baseline")
                print(f"    Set parallel_upsert.workers={best.workers} parallel_upsert.chunk_size={best.chunk_size} in backend/pipeline/hyperparams.yaml")
            else:
                print("\n>>> RECOMMENDATION: No config beat baseline by >10%. Keep single-threaded 20k.")


# ────────────────────────────────────────────────────────────────────────────
# __main__ entry point (run without pytest)
# ────────────────────────────────────────────────────────────────────────────

def main() -> int:
    import sys
    # Ensure the backend package is importable when running as a script
    # (pytest handles this via python_paths, but direct execution doesn't)
    _backend_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _backend_root not in sys.path:
        sys.path.insert(0, _backend_root)
    import django
    django.setup()
    suite = unittest.TestLoader().loadTestsFromTestCase(ChunkSizeBenchmark)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
