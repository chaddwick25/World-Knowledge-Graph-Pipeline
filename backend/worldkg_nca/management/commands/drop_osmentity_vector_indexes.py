from django.core.management.base import BaseCommand
from django.db import connections


class Command(BaseCommand):
    help = "Drop pgvector indexes (IVFFlat + HNSW) on semantic_search_osmentity and its leaf partitions."

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING("Dropping pgvector indexes on semantic_search_osmentity (vectors DB)..."))

        with connections['vectors'].cursor() as cursor:
            # Drop legacy IVFFlat indexes on the parent (monolith era)
            cursor.execute("DROP INDEX IF EXISTS osmentity_gv_tags_ivfflat_idx;")
            cursor.execute("DROP INDEX IF EXISTS osmentity_gv_nle_ivfflat_idx;")

            # Phase 6: Drop HNSW indexes on all leaf partitions.
            # Leaf partitions are named embeddings_{snapshot_id}_{country_code}.
            # HNSW maintenance during bulk upsert is O(m * ef_construction)
            # per row — dropping + rebuilding once at the end is 3-5x faster.
            cursor.execute("""
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE indexdef LIKE '%hnsw%'
                  AND (
                    tablename LIKE 'embeddings\\_%' ESCAPE '\\'
                    OR tablename = 'semantic_search_osmentity'
                  )
                ORDER BY indexname;
            """)
            hnsw_indexes = cursor.fetchall()
            for idx_name, idx_def in hnsw_indexes:
                cursor.execute(f"DROP INDEX IF EXISTS {idx_name};")
                self.stdout.write(f"  Dropped HNSW index: {idx_name}")

        self.stdout.write(self.style.SUCCESS(
            f"Dropped {len(hnsw_indexes)} HNSW index(es) + legacy IVFFlat indexes."
        ))
