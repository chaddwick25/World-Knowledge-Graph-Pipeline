"""
Stress-test for chunked persist_links.

Generates a large synthetic link list (matching real AU run scale: ~28M links)
and verifies that persist_links can handle it without OOM by chunking
bulk_create in batches of 5000.

Usage (from backend/):
    DJANGO_SETTINGS_MODULE=backend.settings python -m igea.test_persist_links_chunked

Options:
    --links   Number of synthetic links to generate  (default: 100_000)
    --full    Use full-scale 28M links (WARNING: slow, needs DB space)
"""
import argparse
import os
import time
import tracemalloc
import uuid

import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

from igea.services.spatial_link_prediction import SpatialLinkPredictionService


def build_synthetic_links(count: int):
    """Generate synthetic link dicts matching predict_links_batched output."""
    print(f"Generating {count:,} synthetic link dicts...")
    t0 = time.time()
    links = []
    for i in range(count):
        score = 0.3 + (i % 7000) / 10000.0  # spread across accept/reject
        links.append({
            "head_osm_id": 10_000_000 + (i // 50),
            "relation": "addrCity",
            "tail_osm_id": i,
            "normalized_score": round(score, 4),
            "unnormalized_score": round(score * 3.0, 4),
            "score": round(score, 4),
            "literal": f"head_{i // 50}",
            "geo_score": round(score * 0.4, 4),
            "name_score": round(score * 0.3, 4),
            "topo_score": round(score * 0.3, 4),
        })
    elapsed = time.time() - t0
    print(f"  Built {count:,} link dicts in {elapsed:.1f}s")
    return links


def main():
    parser = argparse.ArgumentParser(description="Test chunked persist_links")
    parser.add_argument("--links", type=int, default=100_000,
                        help="Number of synthetic links (default 100k)")
    parser.add_argument("--full", action="store_true",
                        help="Full-scale 28M links (slow)")
    args = parser.parse_args()

    count = 28_000_000 if args.full else args.links
    snapshot_id = uuid.uuid4()
    country_name = "TEST_PERSIST_CHUNKED"
    threshold = 0.7

    links = build_synthetic_links(count)

    # Count expected accepted/rejected
    n_accepted = sum(1 for l in links if l["normalized_score"] >= threshold)
    n_rejected = len(links) - n_accepted
    print(f"  Expected: {n_accepted:,} accepted, {n_rejected:,} rejected")

    # Track memory
    tracemalloc.start()

    service = SpatialLinkPredictionService()

    print(f"\nRunning persist_links on {count:,} links (chunked bulk_create)...")
    t0 = time.time()
    total_saved = service.persist_links(
        links,
        snapshot_id=snapshot_id,
        country_name=country_name,
        threshold=threshold,
    )
    elapsed = time.time() - t0

    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(f"\n{'='*60}")
    print(f"persist_links complete")
    print(f"  Links processed:  {count:,}")
    print(f"  Records saved:    {total_saved:,}")
    print(f"  Time:             {elapsed:.1f}s")
    print(f"  Rate:             {count / max(elapsed, 0.01):,.0f} links/s")
    print(f"  Peak RAM (traced): {peak / 1024**2:.1f} MB")
    print(f"{'='*60}")

    # Verify counts in DB
    from igea.models import SpatialTripletScore, SpatialTripletScoreRejected

    db_accepted = SpatialTripletScore.objects.filter(
        snapshot_id=snapshot_id, country_name=country_name
    ).count()
    db_rejected = SpatialTripletScoreRejected.objects.filter(
        snapshot_id=snapshot_id, country_name=country_name
    ).count()

    print(f"\nDB verification:")
    print(f"  Accepted in DB: {db_accepted:,} (expected {n_accepted:,})")
    print(f"  Rejected in DB: {db_rejected:,} (expected {n_rejected:,})")

    ok = db_accepted == n_accepted and db_rejected == n_rejected
    if ok:
        print("  ✓ Counts match")
    else:
        print("  ✗ COUNT MISMATCH")

    # Cleanup test data
    print(f"\nCleaning up test records...")
    del_a, _ = SpatialTripletScore.objects.filter(
        snapshot_id=snapshot_id, country_name=country_name
    ).delete()
    del_r, _ = SpatialTripletScoreRejected.objects.filter(
        snapshot_id=snapshot_id, country_name=country_name
    ).delete()
    print(f"  Deleted {del_a:,} accepted + {del_r:,} rejected")

    if ok:
        print("\n✓ TEST PASSED")
    else:
        print("\n✗ TEST FAILED")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
