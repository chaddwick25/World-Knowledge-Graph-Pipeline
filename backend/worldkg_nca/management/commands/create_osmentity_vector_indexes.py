from django.core.management.base import BaseCommand
from django.db import connections


class Command(BaseCommand):
    help = "Create pgvector indexes (HNSW on leaf partitions + legacy IVFFlat) on semantic_search_osmentity."

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING("Creating pgvector indexes on semantic_search_osmentity (vectors DB)..."))

        conn = connections['vectors']
        # Phase 6: HNSW index creation.  CONCURRENTLY requires autocommit
        # (no transaction block).  We set autocommit on the connection,
        # run the index creation, then restore the original setting.
        old_autocommit = conn.get_autocommit()
        conn.set_autocommit(True)
        try:
            with conn.cursor() as cursor:
                # Ensure pgvector extension is available in this database
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")

                # Phase 6: Create HNSW indexes on all leaf partitions that have
                # vector columns but no HNSW index yet.  Leaf partitions are
                # named embeddings_{snapshot_id}_{country_code}.
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
                        WITH (m = 16, ef_construction = 128);
                    """)
                self.stdout.write(f"    Created {idx_name}")
                created += 1

            # Legacy IVFFlat indexes on the parent (for backward compat with
            # the monolith if it's still in use).  These are no-ops on the
            # partitioned table in PG15 but harmless.
            with conn.cursor() as cursor:
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS osmentity_gv_tags_ivfflat_idx
                    ON semantic_search_osmentity
                    USING ivfflat (gv_tags_embedding vector_cosine_ops)
                    WITH (lists = 100);
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS osmentity_gv_nle_ivfflat_idx
                    ON semantic_search_osmentity
                    USING ivfflat (gv_nle_embedding vector_cosine_ops)
                    WITH (lists = 100);
                """)
        finally:
            conn.set_autocommit(old_autocommit)

        self.stdout.write(self.style.SUCCESS(
            f"Created {created} HNSW index(es) on leaf partitions + legacy IVFFlat indexes."
        ))
