from django.core.management.base import BaseCommand
from django.db import connections


class Command(BaseCommand):
    help = (
        "Create HNSW indexes on semantic_search_osmentity leaf partitions.\n\n"
        "Default (no args): global mode — creates HNSW on every leaf that is "
        "missing one.\n"
        "With --country + --snapshot: per-leaf mode — creates HNSW only on "
        "embeddings_{snapshot}_{country}.  Per-leaf mode is what Step 1 uses "
        "so the rebuild cost scales with the country being upserted, not the "
        "total rows across all countries."
    )

    HNSW_M = 16
    HNSW_EF_CONSTRUCTION = 128

    def add_arguments(self, parser):
        parser.add_argument(
            "--country", type=str, default=None,
            help="Country code. If set with --snapshot, create HNSW only on "
                 "the specified leaf.",
        )
        parser.add_argument(
            "--snapshot", type=str, default=None,
            help="Snapshot date. If set with --country, create HNSW only on "
                 "the specified leaf.",
        )

    def handle(self, *args, **options):
        country = options.get("country")
        snapshot = options.get("snapshot")

        if country and snapshot:
            leaf_name = f"embeddings_{snapshot}_{country.lower()}"
            self.stdout.write(self.style.WARNING(
                f"Per-leaf mode: creating HNSW indexes on {leaf_name} only..."
            ))
            created = self._create_leaf_hnsw(leaf_name)
            self.stdout.write(self.style.SUCCESS(
                f"Created {created} HNSW index(es) on leaf {leaf_name}."
            ))
            return

        if country or snapshot:
            self.stderr.write(self.style.ERROR(
                "Both --country and --snapshot must be provided together "
                "(or neither for global mode)."
            ))
            return

        # Global mode: existing behavior (all leaves missing HNSW)
        self.stdout.write(self.style.WARNING(
            "Global mode: creating HNSW indexes on semantic_search_osmentity "
            "(vectors DB)..."
        ))
        created = self._create_all_missing_hnsw()
        self.stdout.write(self.style.SUCCESS(
            f"Created {created} HNSW index(es) on leaf partitions."
        ))

    # ------------------------------------------------------------------ #
    # Per-leaf mode
    # ------------------------------------------------------------------ #
    def _create_leaf_hnsw(self, leaf_name: str) -> int:
        """Create HNSW indexes on a single leaf partition.

        Creates HNSW on `gv_tags_embedding` and `gv_nle_embedding` (whichever
        vector columns exist on the leaf) using `CREATE INDEX CONCURRENTLY IF
        NOT EXISTS`.  Only this leaf is touched.
        """
        conn = connections['vectors']
        # CONCURRENTLY requires autocommit (no transaction block).
        old_autocommit = conn.get_autocommit()
        conn.set_autocommit(True)
        try:
            with conn.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")

                # Find vector columns on this leaf that don't yet have an
                # HNSW index.
                cursor.execute(
                    """
                    SELECT a.attname AS column_name
                    FROM pg_class t
                    JOIN pg_namespace n ON t.relnamespace = n.oid
                    JOIN pg_attribute a ON a.attrelid = t.oid
                    JOIN pg_type ty ON a.atttypid = ty.oid
                    WHERE t.relkind = 'r'
                      AND n.nspname = 'public'
                      AND t.relname = %s
                      AND a.attname IN ('gv_tags_embedding', 'gv_nle_embedding')
                      AND ty.typname = 'vector'
                      AND NOT EXISTS (
                        SELECT 1 FROM pg_indexes i
                        WHERE i.tablename = t.relname
                          AND i.indexdef LIKE '%%hnsw%%'
                          AND i.indexdef LIKE '%%' || a.attname || '%%'
                      )
                    ORDER BY a.attname;
                    """,
                    [leaf_name],
                )
                missing = cursor.fetchall()

            created = 0
            for (col_name,) in missing:
                idx_name = f"idx_{leaf_name}_{col_name}_hnsw"
                self.stdout.write(
                    f"  Creating HNSW index {idx_name} on {leaf_name}.{col_name}..."
                )
                with conn.cursor() as cursor:
                    cursor.execute(f"""
                        CREATE INDEX CONCURRENTLY IF NOT EXISTS {idx_name}
                        ON {leaf_name}
                        USING hnsw ({col_name} vector_cosine_ops)
                        WITH (m = {self.HNSW_M},
                              ef_construction = {self.HNSW_EF_CONSTRUCTION});
                    """)
                self.stdout.write(f"    Created {idx_name}")
                created += 1
        finally:
            conn.set_autocommit(old_autocommit)
        return created

    # ------------------------------------------------------------------ #
    # Global mode (existing behavior, refactored into a method)
    # ------------------------------------------------------------------ #
    def _create_all_missing_hnsw(self) -> int:
        conn = connections['vectors']
        # CONCURRENTLY requires autocommit (no transaction block).
        old_autocommit = conn.get_autocommit()
        conn.set_autocommit(True)
        try:
            with conn.cursor() as cursor:
                # Ensure pgvector extension is available in this database
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")

                # Create HNSW indexes on all leaf partitions that have
                # vector columns but no HNSW index yet.
                cursor.execute("""
                    SELECT t.relname AS table_name,
                           a.attname AS column_name
                    FROM pg_class t
                    JOIN pg_namespace n ON t.relnamespace = n.oid
                    JOIN pg_attribute a ON a.attrelid = t.oid
                    JOIN pg_type ty ON a.atttypid = ty.oid
                    WHERE t.relkind = 'r'
                      AND n.nspname = 'public'
                      AND t.relname LIKE 'embeddings\\_%' ESCAPE '\\'
                      AND a.attname IN ('gv_tags_embedding', 'gv_nle_embedding')
                      AND ty.typname = 'vector'
                      AND NOT EXISTS (
                        SELECT 1 FROM pg_indexes i
                        WHERE i.tablename = t.relname
                          AND i.indexdef LIKE '%hnsw%'
                          AND i.indexdef LIKE '%' || a.attname || '%'
                      )
                    ORDER BY t.relname, a.attname;
                """)
                missing = cursor.fetchall()

            # Create each HNSW index in its own autocommit statement
            created = 0
            for table_name, col_name in missing:
                idx_name = f"idx_{table_name}_{col_name}_hnsw"
                self.stdout.write(f"  Creating HNSW index {idx_name} on {table_name}.{col_name}...")
                with conn.cursor() as cursor:
                    cursor.execute(f"""
                        CREATE INDEX CONCURRENTLY IF NOT EXISTS {idx_name}
                        ON {table_name}
                        USING hnsw ({col_name} vector_cosine_ops)
                        WITH (m = {self.HNSW_M},
                              ef_construction = {self.HNSW_EF_CONSTRUCTION});
                    """)
                self.stdout.write(f"    Created {idx_name}")
                created += 1
        finally:
            conn.set_autocommit(old_autocommit)
        return created
