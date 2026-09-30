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
                f"Per-leaf mode: creating all indexes on {leaf_name} only..."
            ))
            created = self._create_leaf_indexes(leaf_name)
            self.stdout.write(self.style.SUCCESS(
                f"Created {created} index(es) on leaf {leaf_name}."
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
    def _create_leaf_indexes(self, leaf_name: str) -> int:
        """Rebuild ALL indexes dropped by drop_osmentity_vector_indexes per-leaf mode.

        Called after a bulk upsert completes.  Rebuilds:
          1. HNSW vector indexes (gv_tags_embedding, gv_nle_embedding)
          2. GIST geometry index (geom)
          3. Auxiliary B-tree indexes (osm_type+osm_id, wikidata_uri, wkg_class)

        Uses CONCURRENTLY for HNSW (can't hold ShareLock on large data);
        plain CREATE INDEX for the B-tree/GIST indexes (faster, no transaction
        issues).
        """
        conn = connections['vectors']
        created = 0

        # ---- 1. HNSW vector indexes (CONCURRENTLY required) ----
        old_autocommit = conn.get_autocommit()
        conn.set_autocommit(True)
        try:
            with conn.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")
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
                missing_hnsw = cursor.fetchall()

            for (col_name,) in missing_hnsw:
                idx_name = f"idx_{leaf_name}_{col_name}_hnsw"
                self.stdout.write(f"  Creating HNSW index {idx_name}...")
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

        # ---- 2. GIST geometry index + auxiliary B-tree indexes ----
        # These are plain CREATE INDEX (no CONCURRENTLY needed — no live
        # readers during Step 1 rebuild window).
        aux_indexes = [
            (
                f"idx_{leaf_name}_geom",
                f"CREATE INDEX IF NOT EXISTS idx_{leaf_name}_geom "
                f"ON {leaf_name} USING gist (geom);",
            ),
            (
                f"idx_{leaf_name}_osm",
                f"CREATE INDEX IF NOT EXISTS idx_{leaf_name}_osm "
                f"ON {leaf_name} USING btree (osm_type, osm_id);",
            ),
            (
                f"idx_{leaf_name}_wikidata",
                f"CREATE INDEX IF NOT EXISTS idx_{leaf_name}_wikidata "
                f"ON {leaf_name} USING btree (wikidata_uri);",
            ),
            (
                f"idx_{leaf_name}_wkg_class",
                f"CREATE INDEX IF NOT EXISTS idx_{leaf_name}_wkg_class "
                f"ON {leaf_name} USING btree (wkg_class);",
            ),
        ]
        with conn.cursor() as cursor:
            for idx_name, idx_sql in aux_indexes:
                # Only create if missing.
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = %s);",
                    [idx_name],
                )
                if not cursor.fetchone()[0]:
                    self.stdout.write(f"  Creating index {idx_name}...")
                    cursor.execute(idx_sql)
                    self.stdout.write(f"    Created {idx_name}")
                    created += 1

        return created

    # kept for backward compatibility (global mode calls this)
    def _create_leaf_hnsw(self, leaf_name: str) -> int:
        """Alias: create only HNSW indexes (used by global mode)."""
        return self._create_leaf_indexes(leaf_name)

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
