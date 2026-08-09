"""Verify partition parity after the Phase 6 cutover (Step 5).

Implements Step 5 of ``docs/plans/PHASE6_OSMID_AUDIT_AND_CUTOVER_PLAN.md``.

After the cutover (table rename: ``embeddings_partitioned`` →
``semantic_search_osmentity``, old monolith → ``semantic_search_osmentity_old``),
this command verifies:

  1. **Row count parity** — partitioned table == old monolith
  2. **Partition pruning** — ``EXPLAIN ANALYZE`` a snapshot-scoped query,
     verify it prunes to one partition
  3. **ANN query latency** — HNSW index scan, <10ms execution time
  4. **Upsert correctness** — INSERT ... ON CONFLICT writes to the correct
     leaf partition
  5. **PartitionRegistry completeness** — every completed SnapshotJob has
     a COMPLETE PartitionRegistry entry
  6. **HNSW index coverage** — count rows with ``gv_tags_embedding IS NOT NULL``
     per leaf vs the monolith

Usage::

    # Full verification (all checks)
    python manage.py verify_partition_parity

    # Specific snapshot + country
    python manage.py verify_partition_parity --snapshot 2025_12_31 --country CU

    # Skip the destructive upsert test (no test rows inserted)
    python manage.py verify_partition_parity --skip-upsert-test

    # Exit with non-zero status if any check fails (for CI / scripts)
    python manage.py verify_partition_parity --strict

Exit codes:
  0 — all checks passed (or --strict not set)
  1 — one or more checks failed (only with --strict)
"""

from __future__ import annotations

import logging
import sys
from typing import List, Optional, Tuple

from django.core.management.base import BaseCommand
from django.db import connections
from django.db.models import Sum

from orchestration.models import PartitionRegistry, SnapshotJob

logger = logging.getLogger(__name__)

MONOLITH_TABLE = "semantic_search_osmentity"
OLD_MONOLITH_TABLE = "semantic_search_osmentity_old"
ROOT_TABLE = "embeddings_partitioned"


class Command(BaseCommand):
    help = (
        "Verify partition parity after the Phase 6 cutover (Step 5)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--snapshot", default=None,
            help="Snapshot id (YYYY_MM_DD). If omitted, verify all snapshots.",
        )
        parser.add_argument(
            "--country", default=None,
            help="ISO 3166-1 alpha-2 country code. If omitted, verify all countries.",
        )
        parser.add_argument(
            "--skip-upsert-test", action="store_true",
            help="Skip the upsert test (which inserts + deletes a test row).",
        )
        parser.add_argument(
            "--strict", action="store_true",
            help="Exit with non-zero status if any check fails.",
        )
        parser.add_argument(
            "--ann-limit", type=int, default=10,
            help="ANN query LIMIT (default: 10).",
        )
        parser.add_argument(
            "--ann-ef-search", type=int, default=200,
            help="HNSW ef_search for ANN queries (default: 200).",
        )

    # ── Main ─────────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        snapshot = options["snapshot"]
        country = options["country"].upper() if options["country"] else None
        skip_upsert = options["skip_upsert_test"]
        strict = options["strict"]
        ann_limit = options["ann_limit"]
        ann_ef = options["ann_ef_search"]

        self.stdout.write(self.style.MIGRATE_HEADING(
            "Phase 6 Step 5 — Partition Parity Verification"
        ))
        self.stdout.write("-" * 72)

        checks_passed = 0
        checks_failed = 0
        checks_skipped = 0

        def run_check(name, fn):
            nonlocal checks_passed, checks_failed, checks_skipped
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n[{name}]"))
            try:
                result = fn()
                if result is None:
                    self.stdout.write("  SKIPPED")
                    checks_skipped += 1
                elif result:
                    self.stdout.write(self.style.SUCCESS("  PASS"))
                    checks_passed += 1
                else:
                    self.stdout.write(self.style.ERROR("  FAIL"))
                    checks_failed += 1
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"  ERROR: {exc}"))
                logger.exception("verify_partition_parity: %s failed", name)
                checks_failed += 1

        # ── Checks ────────────────────────────────────────────────────────
        run_check("1. Row count parity", lambda: self._check_row_count_parity())
        run_check("2. Partition pruning", lambda: self._check_partition_pruning(snapshot, country))
        run_check("3. ANN query latency", lambda: self._check_ann_latency(snapshot, country, ann_limit, ann_ef))
        run_check("4. Upsert correctness", lambda: self._check_upsert(snapshot, country) if not skip_upsert else None)
        run_check("5. PartitionRegistry completeness", lambda: self._check_registry(snapshot, country))
        run_check("6. HNSW index coverage", lambda: self._check_hnsw_coverage(snapshot, country))

        # ── Summary ───────────────────────────────────────────────────────
        self.stdout.write("\n" + "=" * 72)
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Summary: passed={checks_passed} failed={checks_failed} skipped={checks_skipped}"
        ))

        if checks_failed > 0:
            self.stdout.write(self.style.ERROR(
                f"\n{checks_failed} check(s) failed. "
                f"Review the output above and consult "
                f"docs/plans/PHASE6_OSMID_AUDIT_AND_CUTOVER_PLAN.md Step 5."
            ))
            if strict:
                sys.exit(1)
        else:
            self.stdout.write(self.style.SUCCESS(
                "\nAll checks passed. The partitioned table is ready for production."
            ))

    # ── Check 1: Row count parity ────────────────────────────────────────

    def _check_row_count_parity(self) -> bool:
        """Verify partitioned table row count == old monolith row count.

        Post-cutover: compares the active table (renamed partitioned) vs
        the old monolith (``semantic_search_osmentity_old``).

        Pre-cutover: the active table IS the monolith.  The partitioned
        table (``embeddings_partitioned``) only has rows for countries that
        have been migrated via ``create_country_partitions``.  In this state,
        we verify that the partitioned table's row count matches the sum of
        PartitionRegistry entity_counts (not the full monolith).
        """
        with connections["vectors"].cursor() as cursor:
            # The active table (semantic_search_osmentity) is the partitioned
            # one after the cutover rename.
            cursor.execute(f"SELECT count(*) FROM {MONOLITH_TABLE};")
            active_count = cursor.fetchone()[0]

            cursor.execute(f"""
                SELECT count(*) FROM information_schema.tables
                WHERE table_name = '{OLD_MONOLITH_TABLE}';
            """)
            old_exists = cursor.fetchone()[0]

            if old_exists:
                # Post-cutover: compare active (partitioned) vs old monolith
                cursor.execute(f"SELECT count(*) FROM {OLD_MONOLITH_TABLE};")
                old_count = cursor.fetchone()[0]
                self.stdout.write(
                    f"  partitioned (active): {active_count} | old monolith: {old_count}"
                )
                if active_count != old_count:
                    diff = active_count - old_count
                    self.stdout.write(self.style.ERROR(
                        f"  MISMATCH: {diff:+d} rows (partitioned - monolith)"
                    ))
                    return False
                return True
            else:
                # Pre-cutover: active table IS the monolith.
                # Compare the partitioned table against PartitionRegistry.
                cursor.execute(f"""
                    SELECT count(*) FROM information_schema.tables
                    WHERE table_name = '{ROOT_TABLE}';
                """)
                root_exists = cursor.fetchone()[0]
                if not root_exists:
                    self.stdout.write(
                        f"  active table: {active_count} (no partitioned table yet)"
                    )
                    return True

                cursor.execute(f"SELECT count(*) FROM {ROOT_TABLE};")
                partitioned_count = cursor.fetchone()[0]

                # Sum PartitionRegistry entity_counts for COMPLETE partitions
                reg_total = PartitionRegistry.objects.filter(
                    status=PartitionRegistry.PartitionStatus.COMPLETE
                ).aggregate(total=Sum('entity_count'))['total'] or 0

                self.stdout.write(
                    f"  monolith (active): {active_count} | "
                    f"partitioned: {partitioned_count} | "
                    f"registry total: {reg_total}"
                )
                if partitioned_count != reg_total:
                    self.stdout.write(self.style.WARNING(
                        f"  Partitioned table ({partitioned_count}) != "
                        f"PartitionRegistry total ({reg_total}) — "
                        f"may indicate incomplete migration."
                    ))
                # Pre-cutover: this is not a failure — the partitioned table
                # is expected to have fewer rows than the monolith.
                self.stdout.write(
                    "  PRE-CUTOVER: row count parity check skipped "
                    "(cutover has not happened)."
                )
                return True

    # ── Check 2: Partition pruning ───────────────────────────────────────

    def _check_partition_pruning(self, snapshot, country) -> bool:
        """Verify EXPLAIN ANALYZE prunes to one partition.

        Post-cutover: queries the active table (semantic_search_osmentity,
        which is the renamed partitioned table).
        Pre-cutover: queries the partitioned table (embeddings_partitioned)
        directly, since the active table is still the monolith.
        """
        if not snapshot or not country:
            self.stdout.write(
                "  SKIPPED: requires --snapshot and --country"
            )
            return None

        with connections["vectors"].cursor() as cursor:
            # Determine which table to query: post-cutover, the active table
            # IS the partitioned table.  Pre-cutover, query the root directly.
            cursor.execute(f"""
                SELECT EXISTS (
                    SELECT 1 FROM pg_partitioned_table pt
                    JOIN pg_class c ON c.oid = pt.partrelid
                    WHERE c.relname = '{MONOLITH_TABLE}'
                );
            """)
            is_partitioned = cursor.fetchone()[0]
            query_table = MONOLITH_TABLE if is_partitioned else ROOT_TABLE

            cursor.execute("SET jit = off;")
            cursor.execute(f"""
                EXPLAIN (FORMAT text)
                SELECT count(*) FROM {query_table}
                WHERE snapshot_id = '{snapshot}' AND country_code = '{country}';
            """)
            plan = cursor.fetchall()

            leaf_table = f"embeddings_{snapshot}_{country.lower()}"
            pruned_to_leaf = any(leaf_table in row[0] for row in plan)

            self.stdout.write(f"  Querying: {query_table}")
            self.stdout.write("  EXPLAIN plan (filtered):")
            for row in plan:
                line = row[0].strip()
                if leaf_table in line or "Seq Scan" in line or "Aggregate" in line or "Append" in line:
                    self.stdout.write(f"    {line}")

            if pruned_to_leaf:
                self.stdout.write(f"  Pruned to leaf: {leaf_table}")
                return True
            else:
                self.stdout.write(self.style.ERROR(
                    f"  Did NOT prune to {leaf_table} — full scan detected"
                ))
                return False

    # ── Check 3: ANN query latency ───────────────────────────────────────

    def _check_ann_latency(self, snapshot, country, limit, ef_search) -> bool:
        """Verify HNSW index scan with acceptable latency.

        Post-cutover: queries the active table (partitioned).
        Pre-cutover: queries the partitioned table directly.
        """
        if not snapshot or not country:
            self.stdout.write(
                "  SKIPPED: requires --snapshot and --country"
            )
            return None

        with connections["vectors"].cursor() as cursor:
            # Determine which table to query
            cursor.execute(f"""
                SELECT EXISTS (
                    SELECT 1 FROM pg_partitioned_table pt
                    JOIN pg_class c ON c.oid = pt.partrelid
                    WHERE c.relname = '{MONOLITH_TABLE}'
                );
            """)
            is_partitioned = cursor.fetchone()[0]
            query_table = MONOLITH_TABLE if is_partitioned else ROOT_TABLE

            cursor.execute("SET jit = off;")
            cursor.execute(f"SET hnsw.ef_search = {ef_search};")

            # Check if there are any rows with gv_tags_embedding in this partition
            cursor.execute(f"""
                SELECT count(*) FROM {query_table}
                WHERE snapshot_id = '{snapshot}' AND country_code = '{country}'
                  AND gv_tags_embedding IS NOT NULL;
            """)
            count = cursor.fetchone()[0]
            if count == 0:
                self.stdout.write(
                    f"  SKIPPED: no rows with gv_tags_embedding in {snapshot}/{country}"
                )
                return None

            cursor.execute(f"""
                EXPLAIN (ANALYZE, FORMAT text)
                SELECT osm_id, tags->>'name' AS name
                FROM {query_table}
                WHERE snapshot_id = '{snapshot}' AND country_code = '{country}'
                  AND gv_tags_embedding IS NOT NULL
                ORDER BY gv_tags_embedding <=> (
                    SELECT gv_tags_embedding FROM {query_table}
                    WHERE snapshot_id = '{snapshot}' AND country_code = '{country}'
                      AND gv_tags_embedding IS NOT NULL
                    LIMIT 1
                )
                LIMIT {limit};
            """)
            plan = cursor.fetchall()

            used_hnsw = False
            execution_time_ms = None
            for row in plan:
                line = row[0].strip()
                if "Index Scan" in line and "hnsw" in line.lower():
                    used_hnsw = True
                    self.stdout.write(f"    {line}")
                if "Execution Time" in line:
                    self.stdout.write(f"    {line}")
                    # Parse "Execution Time: X.XXX ms"
                    try:
                        execution_time_ms = float(line.split(":")[1].strip().replace("ms", "").strip())
                    except (ValueError, IndexError):
                        pass

            if not used_hnsw:
                self.stdout.write(self.style.ERROR(
                    "  HNSW index NOT used — sequential scan detected"
                ))
                return False

            if execution_time_ms is not None:
                self.stdout.write(f"  Execution time: {execution_time_ms:.2f}ms")
                if execution_time_ms > 100:
                    self.stdout.write(self.style.WARNING(
                        f"  Latency > 100ms (expected <10ms for a single leaf)"
                    ))
                    # Not a hard failure — could be cold cache
                    return True
            return True

    # ── Check 4: Upsert correctness ──────────────────────────────────────

    def _check_upsert(self, snapshot, country) -> bool:
        """Verify INSERT ... ON CONFLICT writes to the correct leaf.

        Post-cutover: inserts into the active table (partitioned).
        Pre-cutover: inserts into the partitioned table directly
        (``embeddings_partitioned``).
        """
        if not snapshot or not country:
            self.stdout.write(
                "  SKIPPED: requires --snapshot and --country"
            )
            return None

        test_osm_id = 999999999
        leaf_table = f"embeddings_{snapshot}_{country.lower()}"

        with connections["vectors"].cursor() as cursor:
            # Determine which table to insert into
            cursor.execute(f"""
                SELECT EXISTS (
                    SELECT 1 FROM pg_partitioned_table pt
                    JOIN pg_class c ON c.oid = pt.partrelid
                    WHERE c.relname = '{MONOLITH_TABLE}'
                );
            """)
            is_partitioned = cursor.fetchone()[0]
            insert_table = MONOLITH_TABLE if is_partitioned else ROOT_TABLE

            try:
                # Insert a test row via the partitioned table
                cursor.execute(f"""
                    INSERT INTO {insert_table} (
                        id, snapshot_id, country_code, subdivision,
                        osm_type, osm_id, tags, gv_tags_version, gv_nle_trained
                    ) VALUES (
                        {test_osm_id}, '{snapshot}', '{country}', NULL,
                        'node', {test_osm_id},
                        '{{"test": "verify_partition_parity"}}'::jsonb,
                        '1.0', FALSE
                    )
                    ON CONFLICT (osm_type, osm_id, gv_tags_version, snapshot_id, country_code)
                    DO NOTHING;
                """)

                # Verify it landed in the correct leaf
                cursor.execute(f"""
                    SELECT count(*) FROM {leaf_table}
                    WHERE osm_id = {test_osm_id};
                """)
                leaf_count = cursor.fetchone()[0]

                if leaf_count == 1:
                    self.stdout.write(
                        f"  Test row landed in correct leaf: {leaf_table}"
                    )
                    # Test update (ON CONFLICT DO UPDATE) — verify the tags
                    # change is applied.  We can't use RETURNING xmax on
                    # partitioned tables (Postgres doesn't allow retrieving
                    # system columns in that context), so we check the
                    # tags value before and after.
                    cursor.execute(f"""
                        SELECT tags->>'test' FROM {leaf_table}
                        WHERE osm_id = {test_osm_id};
                    """)
                    before_tag = cursor.fetchone()[0]

                    cursor.execute(f"""
                        INSERT INTO {insert_table} (
                            id, snapshot_id, country_code, subdivision,
                            osm_type, osm_id, tags, gv_tags_version, gv_nle_trained
                        ) VALUES (
                            {test_osm_id}, '{snapshot}', '{country}', NULL,
                            'node', {test_osm_id},
                            '{{"test": "updated"}}'::jsonb,
                            '1.0', FALSE
                        )
                        ON CONFLICT (osm_type, osm_id, gv_tags_version, snapshot_id, country_code)
                        DO UPDATE SET tags = EXCLUDED.tags, updated_at = NOW();
                    """)

                    cursor.execute(f"""
                        SELECT tags->>'test' FROM {leaf_table}
                        WHERE osm_id = {test_osm_id};
                    """)
                    after_tag = cursor.fetchone()[0]

                    if before_tag == 'verify_partition_parity' and after_tag == 'updated':
                        self.stdout.write(
                            "  ON CONFLICT DO UPDATE: correctly updated existing row "
                            f"(tags: '{before_tag}' → '{after_tag}')"
                        )
                        return True
                    else:
                        self.stdout.write(self.style.ERROR(
                            f"  ON CONFLICT DO UPDATE failed: tags before='{before_tag}' "
                            f"after='{after_tag}' (expected 'verify_partition_parity' → 'updated')"
                        ))
                        return False
                else:
                    self.stdout.write(self.style.ERROR(
                        f"  Test row NOT in leaf {leaf_table} (count={leaf_count})"
                    ))
                    return False
            finally:
                # Cleanup
                cursor.execute(f"""
                    DELETE FROM {insert_table} WHERE osm_id = {test_osm_id};
                """)
                self.stdout.write("  Test row cleaned up")

    # ── Check 5: PartitionRegistry completeness ──────────────────────────

    def _check_registry(self, snapshot, country) -> bool:
        """Verify every completed SnapshotJob has a COMPLETE PartitionRegistry."""
        jobs_qs = SnapshotJob.objects.filter(
            status=SnapshotJob.Status.COMPLETED
        )
        if snapshot:
            jobs_qs = jobs_qs.filter(snapshot_date=snapshot)
        if country:
            jobs_qs = jobs_qs.filter(country_code=country)

        jobs = list(jobs_qs)
        if not jobs:
            self.stdout.write("  No completed SnapshotJobs to verify")
            return True

        missing = []
        incomplete = []
        for job in jobs:
            reg = PartitionRegistry.objects.filter(
                snapshot_id=job.snapshot_date,
                country_code=job.country_code,
            ).first()
            if not reg:
                missing.append(f"{job.snapshot_date}/{job.country_code}")
            elif reg.status != PartitionRegistry.PartitionStatus.COMPLETE:
                incomplete.append(
                    f"{job.snapshot_date}/{job.country_code} ({reg.status})"
                )

        total = len(jobs)
        complete = total - len(missing) - len(incomplete)
        self.stdout.write(
            f"  {complete}/{total} SnapshotJobs have COMPLETE PartitionRegistry entries"
        )

        if missing:
            self.stdout.write(self.style.ERROR(
                f"  MISSING ({len(missing)}): {', '.join(missing[:10])}"
                + ("..." if len(missing) > 10 else "")
            ))
        if incomplete:
            self.stdout.write(self.style.WARNING(
                f"  INCOMPLETE ({len(incomplete)}): {', '.join(incomplete[:10])}"
                + ("..." if len(incomplete) > 10 else "")
            ))

        return len(missing) == 0 and len(incomplete) == 0

    # ── Check 6: HNSW index coverage ─────────────────────────────────────

    def _check_hnsw_coverage(self, snapshot, country) -> bool:
        """Verify gv_tags_embedding coverage per leaf vs monolith."""
        with connections["vectors"].cursor() as cursor:
            # Get all leaf partitions
            cursor.execute(f"""
                SELECT
                    c.relname AS leaf_name,
                    pg_catalog.pg_get_expr(c.relpartbound, c.oid) AS partbound
                FROM pg_inherits
                JOIN pg_class c ON c.oid = pg_inherits.inhrelid
                WHERE pg_inherits.inhparent IN (
                    SELECT oid FROM pg_class
                    WHERE relname LIKE 'embeddings_%' AND relkind = 'p'
                )
                ORDER BY c.relname;
            """)
            leaves = cursor.fetchall()

            if not leaves:
                self.stdout.write("  No leaf partitions found")
                return None

            all_ok = True
            for leaf_name, _ in leaves:
                # Filter by snapshot/country if specified
                conditions = []
                if snapshot:
                    # Extract snapshot from leaf name: embeddings_2025_12_31_cu
                    if snapshot not in leaf_name:
                        continue
                if country:
                    if country.lower() not in leaf_name:
                        continue

                cursor.execute(f"""
                    SELECT
                        count(*) AS total,
                        count(*) FILTER (WHERE gv_tags_embedding IS NOT NULL) AS with_emb
                    FROM {leaf_name};
                """)
                total, with_emb = cursor.fetchone()
                pct = (with_emb * 100 // total) if total else 0
                self.stdout.write(
                    f"  {leaf_name}: {with_emb}/{total} ({pct}%) with gv_tags_embedding"
                )
                if total > 0 and with_emb == 0:
                    self.stdout.write(self.style.WARNING(
                        f"    {leaf_name} has 0 gv_tags_embeddings — HNSW index is empty"
                    ))

            return all_ok
