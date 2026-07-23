from django.core.management.base import BaseCommand
from django.db import connections


class Command(BaseCommand):
    help = "Drop pgvector IVFFlat indexes on semantic_search_osmentity in the vectors DB."

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING("Dropping pgvector IVFFlat indexes on semantic_search_osmentity (vectors DB)..."))

        with connections['vectors'].cursor() as cursor:
            # Drop GV-Tags IVFFlat index if it exists
            cursor.execute("DROP INDEX IF EXISTS osmentity_gv_tags_ivfflat_idx;")
            # Drop GV-NLE IVFFlat index if it exists
            cursor.execute("DROP INDEX IF EXISTS osmentity_gv_nle_ivfflat_idx;")

        self.stdout.write(self.style.SUCCESS("Dropped osmentity_gv_tags_ivfflat_idx and osmentity_gv_nle_ivfflat_idx (if they existed)."))
