from django.core.management.base import BaseCommand
from django.db import connections


class Command(BaseCommand):
    help = "Create pgvector IVFFlat indexes on semantic_search_osmentity in the vectors DB."

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING("Creating pgvector IVFFlat indexes on semantic_search_osmentity (vectors DB)..."))

        with connections['vectors'].cursor() as cursor:
            # Ensure pgvector extension is available in this database
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")

            # GV-Tags IVFFlat index (semantic embedding, cosine distance)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS osmentity_gv_tags_ivfflat_idx
                ON semantic_search_osmentity
                USING ivfflat (gv_tags_embedding vector_cosine_ops)
                WITH (lists = 100);
            """)

            # GV-NLE IVFFlat index (spatial embedding, cosine distance)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS osmentity_gv_nle_ivfflat_idx
                ON semantic_search_osmentity
                USING ivfflat (gv_nle_embedding vector_cosine_ops)
                WITH (lists = 100);
            """)

        self.stdout.write(self.style.SUCCESS("Ensured osmentity_gv_tags_ivfflat_idx and osmentity_gv_nle_ivfflat_idx exist with lists=100."))
