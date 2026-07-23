from django.core.management.base import BaseCommand
from django.db import connections


class Command(BaseCommand):
    help = "Create HNSW index on static_embedding for ANN search in the vectors DB."

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING("Creating HNSW index on static_embedding (vectors DB)..."))

        with connections['vectors'].cursor() as cursor:
            # Ensure pgvector extension is available
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")

            # HNSW index on static_embedding (cosine distance)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS osmentity_static_embedding_hnsw_idx
                ON semantic_search_osmentity
                USING hnsw (static_embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 64);
            """)

        self.stdout.write(self.style.SUCCESS("Created osmentity_static_embedding_hnsw_idx with m=16, ef_construction=64."))
