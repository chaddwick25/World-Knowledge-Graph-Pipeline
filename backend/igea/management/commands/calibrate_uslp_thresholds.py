"""
Calibrate USLP hyperparameter thresholds (limit + max_heads).

Scans the vector DB to count actual entity counts per country/subgraph,
then computes the maximum safe pool size (limit) that fits in GPU VRAM
and the expected head-entity count (max_heads) per region.

The two USLP thresholds from hyperparams.yaml are:

  uslp.limit      — max candidate pool entities loaded from DB into the
                    GPU spatial index.  Constrained by GPU VRAM:
                    persistent pool tensors (~844 bytes/entity) + per-batch
                    scoring matrices (~24 * H * N bytes peak).

  uslp.max_heads  — max head entities (with spatial literal tags like
                    is_in, addr:country) to process.  Constrained by
                    runtime, not memory (batches are bounded by
                    head_batch_size, adapted by _adaptive_head_batch_size).

Usage:
    python manage.py calibrate_uslp_thresholds
    python manage.py calibrate_uslp_thresholds --country CA
    python manage.py calibrate_uslp_thresholds --gpu cuda:0 --head-batch 256
    python manage.py calibrate_uslp_thresholds --dry-run
"""

import logging
import math
from collections import defaultdict

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

# ── GPU memory model ──────────────────────────────────────────────────────
# Derived from TorchUSLP's persistent tensors and score_batch's peak usage.
#
# Persistent per-pool-entity (N = pool size):
#   _gpu_pool_embeddings       (N, 100) float32 = N * 400 bytes
#   _gpu_pool_class_embeddings (N, 100) float32 = N * 400 bytes
#   _gpu_pool_coords           (N,   2) float32 = N *   8 bytes
#   _geohash_matrices[1,3,4]   (N,   2) float32 = N *  24 bytes  (3 × 8)
#   _d_max_tensors[1,3,4]      (N,)    float32 = N *  12 bytes  (3 × 4)
#   ─────────────────────────────────────────────────
#   Total persistent:  N * 844 bytes
#
# Per-batch peak (H = head batch size, N = pool size):
#   score_batch creates ~6 simultaneous (H, N) float32 tensors
#   (dlat, dlon, a, dist_km, geo_scores, radius_mask / name_scores / class_scores)
#   Peak: 6 * H * N * 4 = 24 * H * N bytes
#   (matches _adaptive_head_batch_size's estimate)
#
# FastText model overhead: ~200 MB (100-dim model on GPU)

FASTTEXT_OVERHEAD_BYTES = 200 * 1024 * 1024  # 200 MB
PERSISTENT_BYTES_PER_ENTITY = 844             # bytes per pool entity
PEAK_BYTES_PER_HEAD_PER_ENTITY = 24           # 6 tensors * 4 bytes float32
VRAM_SAFETY_FACTOR = 0.70                     # match _adaptive_head_batch_size
DEFAULT_HEAD_BATCH_SIZE = 256

# Spatial literal tag keys that identify head entities (from predict_spatial_links.py)
SPATIAL_LITERAL_KEYS = [
    'is_in', 'is_in:country', 'is_in:state', 'is_in:county',
    'addr:country', 'addr:state', 'addr:county', 'addr:city',
    'addr:suburb', 'addr:hamlet', 'addr:village', 'addr:town',
]


def compute_max_safe_pool_size(
    gpu_total_bytes: int,
    head_batch_size: int = DEFAULT_HEAD_BATCH_SIZE,
    fasttext_overhead: int = FASTTEXT_OVERHEAD_BYTES,
    safety: float = VRAM_SAFETY_FACTOR,
) -> dict:
    """Compute the maximum pool size (N) that keeps head_batch_size feasible.

    Solves for N in:
        free = total - fasttext - N * persistent_per_entity
        max_batch = free * safety / (N * peak_per_head_per_entity)
        max_batch >= head_batch_size

    => (total - fasttext - N * pp) * safety / (N * php) >= H
    => (total - fasttext - N * pp) * safety >= H * N * php
    => (total - fasttext) * safety >= N * (pp * safety + H * php)
    => N <= (total - fasttext) * safety / (pp * safety + H * php)

    Returns a dict with the max N, the resulting free VRAM, and the
    adaptive batch size at that N.
    """
    pp = PERSISTENT_BYTES_PER_ENTITY
    php = PEAK_BYTES_PER_HEAD_PER_ENTITY
    available = gpu_total_bytes - fasttext_overhead
    if available <= 0:
        return {
            "max_pool": 0,
            "free_vram_gb": 0.0,
            "adaptive_batch": 0,
            "persistent_vram_gb": 0.0,
        }

    max_n = int(available * safety / (pp * safety + head_batch_size * php))

    # Verify: compute adaptive batch at max_n
    persistent = max_n * pp
    free = available - persistent
    adaptive = max(1, int(free * safety / (max_n * php))) if max_n > 0 else 0

    return {
        "max_pool": max_n,
        "free_vram_gb": free / 1024**3,
        "adaptive_batch": adaptive,
        "persistent_vram_gb": persistent / 1024**3,
    }


def compute_adaptive_batch(
    pool_size: int,
    gpu_total_bytes: int,
    fasttext_overhead: int = FASTTEXT_OVERHEAD_BYTES,
    safety: float = VRAM_SAFETY_FACTOR,
) -> int:
    """Replicate TorchUSLP._adaptive_head_batch_size for a given pool size."""
    if pool_size == 0:
        return DEFAULT_HEAD_BATCH_SIZE
    available = gpu_total_bytes - fasttext_overhead
    persistent = pool_size * PERSISTENT_BYTES_PER_ENTITY
    free = available - persistent
    if free <= 0:
        return 1
    return max(1, int(free * safety / (pool_size * PEAK_BYTES_PER_HEAD_PER_ENTITY)))


class Command(BaseCommand):
    help = 'Calibrate USLP limit/max_heads thresholds by scanning entity counts and GPU VRAM'

    def add_arguments(self, parser):
        parser.add_argument(
            '--country',
            type=str,
            default=None,
            help='Only scan this ISO code (e.g., CA). Default: all countries in DB.',
        )
        parser.add_argument(
            '--gpu',
            type=str,
            default='cuda:0',
            help='GPU device to calibrate for (default: cuda:0). Use "cpu" for no GPU.',
        )
        parser.add_argument(
            '--head-batch',
            type=int,
            default=DEFAULT_HEAD_BATCH_SIZE,
            help=f'Required head batch size (default: {DEFAULT_HEAD_BATCH_SIZE}). '
                 f'Larger = faster but more VRAM per batch.',
        )
        parser.add_argument(
            '--snapshot-date',
            type=str,
            default=None,
            help='Snapshot partition key YYYY_MM_DD (default: latest per country).',
        )
        parser.add_argument(
            '--include-subgraphs',
            action='store_true',
            help='Also scan per-subgraph entity counts (slow: spatial queries). '
                 'Country-level counts are sufficient for threshold recommendations.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Only compute GPU memory model, skip DB scan.',
        )
        parser.add_argument(
            '--json',
            action='store_true',
            help='Output results as JSON (for programmatic use).',
        )

    def handle(self, *args, **options):
        country = options.get('country')
        gpu_device = options.get('gpu')
        head_batch = options.get('head_batch')
        snapshot_date = options.get('snapshot_date')
        dry_run = options.get('dry_run')
        include_subgraphs = options.get('include_subgraphs')
        as_json = options.get('json')

        # ── 1. GPU memory model ────────────────────────────────────────────
        gpu_info = self._get_gpu_info(gpu_device)
        gpu_total = gpu_info["total_bytes"]

        max_pool_info = compute_max_safe_pool_size(
            gpu_total_bytes=gpu_total,
            head_batch_size=head_batch,
        )

        # Also compute for a range of pool sizes to show the trade-off curve
        trade_off = []
        for n in [50_000, 100_000, 200_000, 500_000, 750_000, 1_000_000,
                   1_500_000, 2_000_000, 3_000_000, 5_000_000]:
            if n > max_pool_info["max_pool"] * 1.5:
                break
            batch = compute_adaptive_batch(n, gpu_total)
            persistent_gb = n * PERSISTENT_BYTES_PER_ENTITY / 1024**3
            free_gb = (gpu_total - FASTTEXT_OVERHEAD_BYTES - n * PERSISTENT_BYTES_PER_ENTITY) / 1024**3
            trade_off.append({
                "pool_size": n,
                "adaptive_batch": batch,
                "persistent_gb": round(persistent_gb, 2),
                "free_vram_gb": round(max(free_gb, 0), 2),
                "fits": batch >= head_batch,
            })

        if dry_run:
            if as_json:
                import json
                self.stdout.write(json.dumps({
                    "gpu": gpu_info,
                    "max_pool": max_pool_info,
                    "trade_off": trade_off,
                }, indent=2))
            else:
                self._print_gpu_report(gpu_info, max_pool_info, trade_off, head_batch)
            return

        # ── 2. DB scan: entity counts per country/subgraph ─────────────────
        regions = self._scan_entity_counts(
            country, snapshot_date, include_subgraphs=include_subgraphs,
        )

        # ── 3. Compute recommendations ─────────────────────────────────────
        recommendations = self._compute_recommendations(
            regions, max_pool_info, head_batch, gpu_total,
        )

        # ── 4. Output ──────────────────────────────────────────────────────
        if as_json:
            import json
            self.stdout.write(json.dumps({
                "gpu": gpu_info,
                "max_pool": max_pool_info,
                "trade_off": trade_off,
                "regions": regions,
                "recommendations": recommendations,
            }, indent=2, default=str))
        else:
            self._print_gpu_report(gpu_info, max_pool_info, trade_off, head_batch)
            self._print_region_report(regions, recommendations, max_pool_info)

    # ── GPU info ──────────────────────────────────────────────────────────

    def _get_gpu_info(self, device: str) -> dict:
        """Get GPU total memory. Falls back to 16 GB if torch unavailable."""
        if device == 'cpu' or not device.startswith('cuda'):
            return {
                "device": "cpu",
                "name": "CPU (no GPU)",
                "total_bytes": 0,
                "total_gb": 0.0,
            }
        try:
            import torch
            if not torch.cuda.is_available():
                return {
                    "device": device,
                    "name": "CUDA unavailable",
                    "total_bytes": 0,
                    "total_gb": 0.0,
                }
            idx = int(device.split(':')[-1]) if ':' in device else 0
            props = torch.cuda.get_device_properties(idx)
            return {
                "device": device,
                "name": props.name,
                "total_bytes": props.total_memory,
                "total_gb": round(props.total_memory / 1024**3, 1),
            }
        except Exception as exc:
            logger.warning("GPU detection failed: %s", exc)
            return {
                "device": device,
                "name": f"Unknown ({exc})",
                "total_bytes": 16 * 1024**3,  # assume 16 GB
                "total_gb": 16.0,
            }

    # ── DB scan ───────────────────────────────────────────────────────────

    def _scan_entity_counts(
        self, country_filter: str, snapshot_date: str,
        include_subgraphs: bool = False,
    ) -> list:
        """Scan the vector DB for entity counts and head counts per region.

        Uses raw SQL ``COUNT(*)`` with partition pruning for speed — the
        ORM path triggers expensive ``geom__isnull`` and ``tags__has_any_keys``
        subqueries on 50M+ row tables.  Raw SQL with explicit partition
        predicates (``snapshot_id``, ``country_code``) lets Postgres prune
        to the leaf partition and use the partial indexes there.

        Returns a list of dicts sorted by entities_with_geom descending:
            {
                "country": "CA",
                "subgraph": "ontario",       # None for country-level
                "snapshot_id": "2025_12_31",
                "total_entities": 1_048_003,
                "entities_with_geom": 1_040_000,
                "head_entities": 12_500,
                "current_limit_hit": True,   # entities_with_geom > 200000
                "current_heads_hit": False,  # head_entities > 50000
            }
        """
        from django.db import connections
        from worldkg_nca.models import OsmEntity

        table = OsmEntity._meta.db_table

        # Determine which countries to scan
        if country_filter:
            countries = [country_filter.upper()]
        else:
            with connections['vectors'].cursor() as cur:
                if snapshot_date:
                    cur.execute(
                        f"SELECT DISTINCT country_code FROM {table} "
                        f"WHERE snapshot_id = %s AND country_code IS NOT NULL "
                        f"ORDER BY country_code",
                        [snapshot_date],
                    )
                else:
                    cur.execute(
                        f"SELECT DISTINCT country_code FROM {table} "
                        f"WHERE country_code IS NOT NULL ORDER BY country_code"
                    )
                countries = [r[0] for r in cur.fetchall() if r[0]]

        if not countries:
            self.stdout.write(self.style.WARNING(
                "No countries found in DB. Run the pipeline first."
            ))
            return []

        # Build the tags ?| array literal once
        tag_keys = ','.join(f"'{k}'" for k in SPATIAL_LITERAL_KEYS)

        regions = []
        for cc in countries:
            self.stdout.write(f"  Scanning {cc}...")

            # Single query: total, with_geom, head_count in one pass.
            # Uses partition pruning on snapshot_id + country_code.
            with connections['vectors'].cursor() as cur:
                if snapshot_date:
                    cur.execute(f"""
                        SELECT
                            COUNT(*) AS total,
                            COUNT(*) FILTER (WHERE geom IS NOT NULL) AS with_geom,
                            COUNT(*) FILTER (
                                WHERE geom IS NOT NULL
                                  AND tags ?| ARRAY[{tag_keys}]
                            ) AS head_count
                        FROM {table}
                        WHERE country_code = %s AND snapshot_id = %s
                    """, [cc, snapshot_date])
                else:
                    cur.execute(f"""
                        SELECT
                            COUNT(*) AS total,
                            COUNT(*) FILTER (WHERE geom IS NOT NULL) AS with_geom,
                            COUNT(*) FILTER (
                                WHERE geom IS NOT NULL
                                  AND tags ?| ARRAY[{tag_keys}]
                            ) AS head_count
                        FROM {table}
                        WHERE country_code = %s
                    """, [cc])
                row = cur.fetchone()

            total, with_geom, head_count = row
            if total == 0:
                continue

            regions.append({
                "country": cc,
                "subgraph": None,
                "snapshot_id": snapshot_date or "latest",
                "total_entities": total,
                "entities_with_geom": with_geom,
                "head_entities": head_count,
                "current_limit_hit": with_geom > 200_000,
                "current_heads_hit": head_count > 50_000,
            })
            self.stdout.write(
                f"    total={total:,}  geom={with_geom:,}  heads={head_count:,}"
            )

            # Per-subgraph breakdown is opt-in (slow: 13 spatial queries)
            if include_subgraphs:
                subgraph_counts = self._scan_subgraphs(cc, snapshot_date, table, tag_keys)
                for sg in subgraph_counts:
                    sg["current_limit_hit"] = sg["entities_with_geom"] > 200_000
                    sg["current_heads_hit"] = sg["head_entities"] > 50_000
                    regions.append(sg)

        regions.sort(key=lambda r: r["entities_with_geom"], reverse=True)
        return regions

    def _scan_subgraphs(
        self, country_code: str, snapshot_date: str,
        table: str, tag_keys: str,
    ) -> list:
        """Count entities per subgraph using poly-file spatial filtering.

        Uses raw SQL ``ST_Within`` for speed.  This is still O(subgraphs)
        spatial queries, so it's opt-in via ``--include-subgraphs``.
        """
        from django.db import connections
        from pipeline.envelopes import CountryEnvelope

        try:
            env = CountryEnvelope.from_db(country_code, snapshot_date=snapshot_date)
        except Exception:
            return []

        if not env.has_subgraphs or not env.subgraphs:
            return []

        results = []
        for sg in env.subgraphs:
            if not sg.poly_path:
                continue

            try:
                from worldkg_nca.services.wikidata_service import parse_poly_to_wkt
                wkt = parse_poly_to_wkt(str(sg.poly_path))
                if not wkt:
                    continue

                with connections['vectors'].cursor() as cur:
                    if snapshot_date:
                        cur.execute(f"""
                            SELECT
                                COUNT(*) AS with_geom,
                                COUNT(*) FILTER (
                                    WHERE tags ?| ARRAY[{tag_keys}]
                                ) AS head_count
                            FROM {table}
                            WHERE country_code = %s
                              AND snapshot_id = %s
                              AND geom IS NOT NULL
                              AND ST_Within(geom, ST_GeomFromText(%s, 4326))
                        """, [country_code, snapshot_date, wkt])
                    else:
                        cur.execute(f"""
                            SELECT
                                COUNT(*) AS with_geom,
                                COUNT(*) FILTER (
                                    WHERE tags ?| ARRAY[{tag_keys}]
                                ) AS head_count
                            FROM {table}
                            WHERE country_code = %s
                              AND geom IS NOT NULL
                              AND ST_Within(geom, ST_GeomFromText(%s, 4326))
                        """, [country_code, wkt])
                    row = cur.fetchone()

                with_geom, head_count = row
                results.append({
                    "country": country_code,
                    "subgraph": sg.name,
                    "snapshot_id": snapshot_date or "latest",
                    "total_entities": with_geom,
                    "entities_with_geom": with_geom,
                    "head_entities": head_count,
                })
                self.stdout.write(
                    f"    {sg.name}: geom={with_geom:,}  heads={head_count:,}"
                )
            except Exception as exc:
                logger.warning(
                    "Subgraph scan failed for %s/%s: %s",
                    country_code, sg.name, exc,
                )

        return results

    # ── Recommendations ───────────────────────────────────────────────────

    def _compute_recommendations(
        self, regions: list, max_pool_info: dict,
        head_batch: int, gpu_total: int,
    ) -> dict:
        """Compute recommended limit and max_heads from the scan results."""
        if not regions:
            return {}

        # limit: the max pool size that fits in VRAM, but not less than
        # the largest subgraph (so no subgraph is truncated).
        max_safe = max_pool_info["max_pool"]
        largest_pool = max(r["entities_with_geom"] for r in regions)

        # Recommended limit = min(largest_pool, max_safe) rounded up to 10K
        recommended_limit = min(largest_pool, max_safe)
        recommended_limit = math.ceil(recommended_limit / 10_000) * 10_000

        # max_heads: the largest head count across all regions, rounded up
        # to 5K.  This ensures no region's head entities are truncated.
        largest_heads = max(r["head_entities"] for r in regions)
        recommended_max_heads = math.ceil(largest_heads / 5_000) * 5_000

        # Check if current thresholds are sufficient
        current_limit = 200_000
        current_max_heads = 50_000

        truncated_regions = [
            r for r in regions
            if r["entities_with_geom"] > current_limit
        ]
        head_truncated = [
            r for r in regions
            if r["head_entities"] > current_max_heads
        ]

        return {
            "current_limit": current_limit,
            "current_max_heads": current_max_heads,
            "recommended_limit": recommended_limit,
            "recommended_max_heads": recommended_max_heads,
            "max_safe_pool": max_safe,
            "largest_pool": largest_pool,
            "largest_heads": largest_heads,
            "pool_truncated_count": len(truncated_regions),
            "heads_truncated_count": len(head_truncated),
            "pool_truncated_regions": [
                f"{r['country']}/{r['subgraph'] or 'country-level'}"
                for r in truncated_regions
            ],
            "heads_truncated_regions": [
                f"{r['country']}/{r['subgraph'] or 'country-level'}"
                for r in head_truncated
            ],
            "head_batch_size": head_batch,
            "adaptive_batch_at_recommended_limit": compute_adaptive_batch(
                recommended_limit, gpu_total,
            ),
        }

    # ── Output formatting ─────────────────────────────────────────────────

    def _print_gpu_report(self, gpu_info, max_pool_info, trade_off, head_batch):
        self.stdout.write(self.style.SUCCESS(
            f"\n{'='*70}\n"
            f"USLP Threshold Calibration — GPU Memory Model\n"
            f"{'='*70}\n"
        ))
        self.stdout.write(f"  Device:     {gpu_info['name']}")
        self.stdout.write(f"  Total VRAM: {gpu_info['total_gb']:.1f} GB")
        self.stdout.write(f"  FastText overhead: {FASTTEXT_OVERHEAD_BYTES / 1024**2:.0f} MB")
        self.stdout.write(f"  Persistent/entity: {PERSISTENT_BYTES_PER_ENTITY} bytes")
        self.stdout.write(f"  Peak/batch/entity: {PEAK_BYTES_PER_HEAD_PER_ENTITY} bytes")
        self.stdout.write(f"  Safety factor:     {VRAM_SAFETY_FACTOR}")
        self.stdout.write(f"  Required head batch: {head_batch}")
        self.stdout.write("")

        self.stdout.write(self.style.SUCCESS(
            f"  Max safe pool size: {max_pool_info['max_pool']:,} entities "
            f"(head_batch={head_batch} feasible)\n"
            f"  Persistent VRAM:    {max_pool_info['persistent_vram_gb']:.2f} GB\n"
            f"  Free VRAM:          {max_pool_info['free_vram_gb']:.2f} GB\n"
            f"  Adaptive batch:     {max_pool_info['adaptive_batch']}\n"
        ))

        self.stdout.write(self.style.SUCCESS("  Pool size trade-off:"))
        self.stdout.write(f"  {'Pool':>10}  {'Adapt.Batch':>12}  {'Persistent':>12}  {'Free VRAM':>12}  {'Fits?':>8}")
        self.stdout.write(f"  {'─'*10}  {'─'*12}  {'─'*12}  {'─'*12}  {'─'*8}")
        for t in trade_off:
            fits = "✓" if t["fits"] else "✗"
            self.stdout.write(
                f"  {t['pool_size']:>10,}  {t['adaptive_batch']:>12}  "
                f"{t['persistent_gb']:>11.2f}G  {t['free_vram_gb']:>11.2f}G  {fits:>8}"
            )
        self.stdout.write("")

    def _print_region_report(self, regions, recommendations, max_pool_info):
        self.stdout.write(self.style.SUCCESS(
            f"\n{'='*70}\n"
            f"USLP Threshold Calibration — DB Scan Results\n"
            f"{'='*70}\n"
        ))

        if not regions:
            self.stdout.write(self.style.WARNING("  No regions found in DB."))
            return

        # Region table
        self.stdout.write(f"  {'Region':<40}  {'Pool':>10}  {'Heads':>8}  {'LimHit':>7}  {'HeadHit':>8}")
        self.stdout.write(f"  {'─'*40}  {'─'*10}  {'─'*8}  {'─'*7}  {'─'*8}")
        for r in regions:
            label = f"{r['country']}/{r['subgraph'] or 'country-level'}"
            if len(label) > 38:
                label = label[:35] + "..."
            lim = "YES" if r["current_limit_hit"] else ""
            hed = "YES" if r["current_heads_hit"] else ""
            self.stdout.write(
                f"  {label:<40}  {r['entities_with_geom']:>10,}  "
                f"{r['head_entities']:>8,}  {lim:>7}  {hed:>8}"
            )
        self.stdout.write("")

        # Recommendations
        self.stdout.write(self.style.SUCCESS(
            f"{'='*70}\n"
            f"Recommendations for hyperparams.yaml\n"
            f"{'='*70}\n"
        ))
        self.stdout.write(f"  Current thresholds:")
        self.stdout.write(f"    uslp.limit:      {recommendations['current_limit']:,}")
        self.stdout.write(f"    uslp.max_heads:  {recommendations['current_max_heads']:,}")
        self.stdout.write("")

        self.stdout.write(f"  Recommended thresholds:")
        self.stdout.write(self.style.SUCCESS(
            f"    uslp.limit:      {recommendations['recommended_limit']:,}  "
            f"(max safe: {recommendations['max_safe_pool']:,}, "
            f"largest pool: {recommendations['largest_pool']:,})"
        ))
        self.stdout.write(self.style.SUCCESS(
            f"    uslp.max_heads:  {recommendations['recommended_max_heads']:,}  "
            f"(largest head count: {recommendations['largest_heads']:,})"
        ))
        self.stdout.write("")

        if recommendations["pool_truncated_count"]:
            self.stdout.write(self.style.WARNING(
                f"  ⚠ {recommendations['pool_truncated_count']} region(s) "
                f"currently truncated by limit=200000:"
            ))
            for r in recommendations["pool_truncated_regions"]:
                self.stdout.write(f"    • {r}")
            self.stdout.write("")

        if recommendations["heads_truncated_count"]:
            self.stdout.write(self.style.WARNING(
                f"  ⚠ {recommendations['heads_truncated_count']} region(s) "
                f"currently truncated by max_heads=50000:"
            ))
            for r in recommendations["heads_truncated_regions"]:
                self.stdout.write(f"    • {r}")
            self.stdout.write("")

        self.stdout.write(self.style.SUCCESS(
            f"  Adaptive batch at recommended limit: "
            f"{recommendations['adaptive_batch_at_recommended_limit']}\n"
        ))
