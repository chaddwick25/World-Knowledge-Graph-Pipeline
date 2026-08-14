from django.core.management.base import BaseCommand
from django.db import connections


class Command(BaseCommand):
    help = (
        "Drop HNSW indexes on semantic_search_osmentity and its partitions.\n\n"
        "Default (no args): global mode — drops root → snapshot → every leaf.\n"
        "With --country + --snapshot: per-leaf mode — drops only the HNSW "
        "indexes on embeddings_{snapshot}_{country} (no root/snapshot/other "
        "leaves touched).  Per-leaf mode is what Step 1 uses so other "
        "countries' search stays online during a single country's upsert."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--country", type=str, default=None,
            help="Country code (e.g. 'ni'). If set with --snapshot, drop only "
                 "the specified leaf's HNSW indexes.",
        )
        parser.add_argument(
            "--snapshot", type=str, default=None,
            help="Snapshot date 'YYYY_MM_DD'. If set with --country, drop only "
                 "the specified leaf's HNSW indexes.",
        )

    def handle(self, *args, **options):
        country = options.get("country")
        snapshot = options.get("snapshot")

        if country and snapshot:
            leaf_name = f"embeddings_{snapshot}_{country.lower()}"
            self.stdout.write(self.style.WARNING(
                f"Per-leaf mode: dropping HNSW indexes on {leaf_name} only..."
            ))
            dropped = self._drop_leaf_hnsw(leaf_name)
            self.stdout.write(self.style.SUCCESS(
                f"Dropped {dropped} HNSW index(es) on leaf {leaf_name}."
            ))
            return

        if country or snapshot:
            self.stderr.write(self.style.ERROR(
                "Both --country and --snapshot must be provided together "
                "(or neither for global mode)."
            ))
            return

        # Global mode: existing behavior (root → snapshot → all leaves)
        self.stdout.write(self.style.WARNING(
            "Global mode: dropping HNSW indexes on semantic_search_osmentity "
            "(vectors DB)..."
        ))
        total = self._drop_all_hnsw()
        self.stdout.write(self.style.SUCCESS(
            f"Dropped {total} index(es) (global)."
        ))

    # ------------------------------------------------------------------ #
    # Per-leaf mode
    # ------------------------------------------------------------------ #
    def _drop_leaf_hnsw(self, leaf_name: str) -> int:
        """Drop HNSW indexes on a single leaf partition.

        Only the leaf's own HNSW indexes are dropped — root/snapshot
        partitioned indexes are NOT touched (they are `ON ONLY` and don't
        affect per-row upsert performance).  Other countries' leaves are
        untouched so their search stays online.
        """
        with connections['vectors'].cursor() as cursor:
            cursor.execute(
                """
                SELECT i.indexname
                FROM pg_indexes i
                LEFT JOIN pg_constraint c
                  ON c.conname = i.indexname
                 AND c.contype IN ('p', 'u')
                WHERE i.tablename = %s
                  AND c.contype IS NULL
                  AND (
                    i.indexdef LIKE '%%hnsw%%'
                    OR i.indexname LIKE '%%hnsw%%'
                  );
                """,
                [leaf_name],
            )
            indexes = cursor.fetchall()

            for (idx_name,) in indexes:
                cursor.execute(f"DROP INDEX IF EXISTS {idx_name};")
                self.stdout.write(
                    f"  Dropped leaf HNSW index: {idx_name} (on {leaf_name})"
                )
        return len(indexes)

    # ------------------------------------------------------------------ #
    # Global mode (existing behavior, refactored into a method)
    # ------------------------------------------------------------------ #
    def _drop_all_hnsw(self) -> int:
        with connections['vectors'].cursor() as cursor:
            # Drop HNSW indexes on the partitioned table hierarchy.
            #
            # For partitioned indexes, PostgreSQL requires dropping the PARENT
            # first.  Dropping a parent partitioned index automatically drops
            # ALL child indexes on every sub-partition and leaf — no CASCADE
            # needed.  You CANNOT drop a child index while the parent exists
            # (the parent depends on it).
            #
            # Order:
            #   1. Drop root partitioned HNSW indexes (ON semantic_search_osmentity)
            #      → automatically drops all children on snapshot sub-partitions + leaves
            #   2. Drop any remaining snapshot-level partitioned HNSW indexes
            #      → automatically drops their leaf children
            #   3. Drop any remaining leaf HNSW indexes (orphaned, no parent)
            #
            # Exclude any index that backs a constraint (pkey, unique) —
            # dropping those would corrupt the schema.

            # Step 1: Drop root partitioned HNSW indexes on semantic_search_osmentity.
            # These are ON ONLY indexes.  Match by 'hnsw' in indexdef OR name.
            # Exclude constraint-backed indexes.
            cursor.execute("""
                SELECT i.indexname
                FROM pg_indexes i
                LEFT JOIN pg_constraint c
                  ON c.conname = i.indexname
                 AND c.contype IN ('p', 'u')
                WHERE i.tablename = 'semantic_search_osmentity'
                  AND c.contype IS NULL
                  AND (
                    i.indexdef LIKE '%hnsw%'
                    OR i.indexname LIKE '%hnsw%'
                  );
            """)
            root_indexes = cursor.fetchall()
            for (idx_name,) in root_indexes:
                cursor.execute(f"DROP INDEX IF EXISTS {idx_name};")
                self.stdout.write(f"  Dropped root HNSW index: {idx_name}")

            # Step 2: Drop snapshot-level partitioned HNSW indexes.
            # These are ON ONLY indexes on tables like embeddings_2025_12_31
            # (the snapshot sub-partition, which is itself partitioned by
            # country_code).  The root drop above may have already removed
            # them, but IF NOT EXISTS handles that.
            cursor.execute("""
                SELECT i.indexname, i.tablename
                FROM pg_indexes i
                LEFT JOIN pg_constraint c
                  ON c.conname = i.indexname
                 AND c.contype IN ('p', 'u')
                WHERE i.indexdef LIKE '%ON ONLY%'
                  AND i.indexdef LIKE '%hnsw%'
                  AND i.tablename LIKE 'embeddings\\_%' ESCAPE '\\'
                  AND c.contype IS NULL
                ORDER BY i.tablename, i.indexname;
            """)
            snapshot_indexes = cursor.fetchall()
            for idx_name, tbl_name in snapshot_indexes:
                cursor.execute(f"DROP INDEX IF EXISTS {idx_name};")
                self.stdout.write(f"  Dropped snapshot HNSW index: {idx_name} (on {tbl_name})")

            # Step 3: Drop any remaining leaf HNSW indexes.
            # These are on leaf partitions (embeddings_{snapshot}_{cc}) and
            # may survive if they were created independently (not via parent).
            cursor.execute("""
                SELECT indexname, indexdef, tablename
                FROM pg_indexes
                WHERE indexdef LIKE '%hnsw%'
                  AND tablename LIKE 'embeddings\\_%' ESCAPE '\\'
                  AND indexdef NOT LIKE '%ON ONLY%'
                ORDER BY indexname;
            """)
            leaf_indexes = cursor.fetchall()
            for idx_name, idx_def, tbl_name in leaf_indexes:
                cursor.execute(f"DROP INDEX IF EXISTS {idx_name};")
                self.stdout.write(f"  Dropped leaf HNSW index: {idx_name} (on {tbl_name})")

        total = len(root_indexes) + len(snapshot_indexes) + len(leaf_indexes)
        self.stdout.write(
            f"  ({len(root_indexes)} root + {len(snapshot_indexes)} snapshot "
            f"+ {len(leaf_indexes)} leaf HNSW) + legacy indexes."
        )
        return total
